//
// Copyright (c) 2010-2024 Antmicro
//
// This file is licensed under the MIT License.
// Full license text is available in 'licenses/MIT.txt'.
//
// A bidirectional dual-channel CDC-ACM USB host bridge for the NRF_USBD_Full
// fork (see that file and docs/renode-usb-design.md, gap (d)). Where the stock
// CDCToUARTConverter is one-way (device->host only, one hardcoded IN endpoint,
// attachable once per emulation), this external:
//
//  - registers the USB device ONCE (one USBHost enumeration) but exposes TWO
//    IUART channels -- a real ZMK studio-rpc-usb-uart image is a composite
//    device with two CDC-ACM functions (console + Studio RPC), and each
//    channel needs its own CreateServerSocketTerminal;
//  - discovers the CDC data-interface endpoint pairs by forwarding a real
//    GET_DESCRIPTOR(Configuration) to the guest and parsing the answer, so no
//    endpoint numbers are hardcoded (ctor arguments exist as an escape hatch);
//  - drives host->device data through the fork's bulk OUT path
//    (USBEndpoint.WriteData -> DataWritten -> EPDATASTATUS/EPDATA), and
//    device->host data through the SetDataReadCallbackOneShot re-arm loop;
//  - sends SET_LINE_CODING and SET_CONTROL_LINE_STATE (DTR=1) per CDC control
//    interface for host fidelity (Zephyr's legacy cdc_acm gates TX only on
//    `configured`, so DTR is not load-bearing -- see the design doc).
//
// Loaded at runtime via the ad-hoc C# compiler (`include @....cs` from the
// monitor or a .resc), like NRF_USBD_Full itself. Two-step setup (so the
// channel terminals can be wired before any data flows):
//
//   sysbus.usbd CreateDualCdcAcmBridge "bridge"
//   emulation CreateServerSocketTerminal 3457 "cdc0_term" false
//   connector Connect sysbus.bridge_cdc0 cdc0_term        # and cdc1 likewise
//   sysbus.usbd AttachDualCdcAcmBridge "bridge"           # starts enumeration
//
// "bridge" is an external; "bridge_cdc0"/"bridge_cdc1" are machine-registered
// IUART peripherals (channels in configuration-descriptor interface order).
//

using System;
using System.Collections.Generic;
using System.Threading;

using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Core.USB;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.UART;

using USBDirection = Antmicro.Renode.Core.USB.Direction;

namespace Antmicro.Renode.Peripherals.USB
{
    public static class DualCdcAcmBridgeExtensions
    {
        // First step of the two-step setup: create the bridge external plus its
        // two IUART channels. The channels are registered IN the device's
        // machine (sysbus + NullRegistrationPoint, the VirtualConsole pattern)
        // rather than as externals: BackendTerminal.AttachTo -- what `connector
        // Connect <channel> <terminal>` ends up calling -- resolves the
        // channel's machine via GetMachine(), which is fatal for a machine-less
        // external IUART (verified crash on Renode 1.16.1).
        public static DualCdcAcmBridge CreateDualCdcAcmBridge(this IUSBDevice attachTo, string name,
            int channel0InEndpoint = 0, int channel0OutEndpoint = 0,
            int channel1InEndpoint = 0, int channel1OutEndpoint = 0)
        {
            var emulation = EmulationManager.Instance.CurrentEmulation;
            var machine = attachTo.GetMachine();
            var bridge = new DualCdcAcmBridge(channel0InEndpoint, channel0OutEndpoint,
                channel1InEndpoint, channel1OutEndpoint);
            emulation.ExternalsManager.AddExternal(bridge, name);
            for(var i = 0; i < DualCdcAcmBridge.ChannelCount; i++)
            {
                var channel = bridge.GetChannel(i);
                machine.RegisterAsAChildOf(machine.SystemBus, channel, NullRegistrationPoint.Instance);
                machine.SetLocalName(channel, $"{name}_cdc{i}");
            }
            bridgesByName[name] = bridge;
            return bridge;
        }

        // Second step of the two-step setup: attach an already-created bridge to
        // the device, starting enumeration. Splitting create from attach lets
        // the caller wire each channel external to its server socket terminal
        // (and connect the TCP clients) BEFORE any device data can flow, so no
        // early console output is lost.
        public static void AttachDualCdcAcmBridge(this IUSBDevice attachTo, string name)
        {
            DualCdcAcmBridge bridge;
            if(!bridgesByName.TryGetValue(name, out bridge))
            {
                throw new ArgumentException($"No DualCdcAcmBridge named '{name}'; call CreateDualCdcAcmBridge first");
            }
            var emulation = EmulationManager.Instance.CurrentEmulation;
            var usbConnector = new USBConnector();
            emulation.ExternalsManager.AddExternal(usbConnector, $"usb_connector_{name}");
            emulation.Connector.Connect(attachTo, usbConnector);
            usbConnector.RegisterInController(bridge);
        }

        // One-step convenience mirroring CreateAndAttachCDCToUARTConverter, but
        // with the USBConnector external's name derived from `name` (the stock
        // method uses a fixed name, so it can only ever attach once per
        // emulation). Note the device->host race documented on
        // AttachDualCdcAcmBridge: with this variant, terminals wired after the
        // call may miss the first bytes the guest sends post-enumeration.
        public static void CreateAndAttachDualCdcAcmBridge(this IUSBDevice attachTo, string name,
            int channel0InEndpoint = 0, int channel0OutEndpoint = 0,
            int channel1InEndpoint = 0, int channel1OutEndpoint = 0)
        {
            CreateDualCdcAcmBridge(attachTo, name,
                channel0InEndpoint, channel0OutEndpoint, channel1InEndpoint, channel1OutEndpoint);
            AttachDualCdcAcmBridge(attachTo, name);
        }

        // The ad-hoc-compiled assembly lives for the whole Renode process; one
        // emulation per process (the harness's usage), so a plain static
        // registry keyed by external name is sufficient.
        private static readonly Dictionary<string, DualCdcAcmBridge> bridgesByName = new Dictionary<string, DualCdcAcmBridge>();
    }

    public class DualCdcAcmBridge : USBHost, IExternal
    {
        public DualCdcAcmBridge(int channel0InEndpoint = 0, int channel0OutEndpoint = 0,
            int channel1InEndpoint = 0, int channel1OutEndpoint = 0)
        {
            channels = new[]
            {
                new CdcAcmBridgeChannel(0, channel0InEndpoint, channel0OutEndpoint),
                new CdcAcmBridgeChannel(1, channel1InEndpoint, channel1OutEndpoint),
            };
        }

        public CdcAcmBridgeChannel GetChannel(int index)
        {
            return channels[index];
        }

        public const int ChannelCount = 2;

        protected override void DeviceEnumerated(IUSBDevice device)
        {
            // Runs in its own synced block, well after the forwarded
            // SET_CONFIGURATION completed -- so unlike the requests inside
            // USBHost.EnumerateDevice (which overwrite each other in the fork's
            // single latched-SETUP slot within one synced block), each request
            // chained below is its own transaction: the next one is only sent
            // from the previous one's completion callback (invoked when the
            // guest fires TASKS_EP0STATUS).
            this.Log(LogLevel.Debug, "Device enumerated; reading configuration descriptor");
            device.USBCore.HandleSetupPacket(GetConfigurationDescriptorPacket(DescriptorHeaderLength), header =>
            {
                if(header.Length < DescriptorHeaderLength)
                {
                    this.Log(LogLevel.Error, "Short configuration descriptor header ({0} bytes); cannot wire CDC channels", header.Length);
                    return;
                }
                var totalLength = (ushort)(header[2] | (header[3] << 8));
                this.Log(LogLevel.Debug, "Configuration descriptor header: {0} (wTotalLength={1})",
                    BitConverter.ToString(header), totalLength);
                device.USBCore.HandleSetupPacket(GetConfigurationDescriptorPacket(totalLength), full =>
                {
                    WireChannels(device, full);
                });
            });
        }

        private void WireChannels(IUSBDevice device, byte[] configurationDescriptor)
        {
            var pairs = ParseCdcDataPairs(configurationDescriptor);
            this.Log(LogLevel.Debug, "Configuration descriptor ({0} bytes, {1} CDC function(s)): {2}",
                configurationDescriptor.Length, pairs.Count, BitConverter.ToString(configurationDescriptor));
            if(pairs.Count < channels.Length)
            {
                this.Log(LogLevel.Warning, "Expected {0} CDC functions but found {1}; wiring what is available",
                    channels.Length, pairs.Count);
            }

            var controlRequests = new Queue<KeyValuePair<SetupPacket, byte[]>>();
            for(var i = 0; i < channels.Length && i < pairs.Count; i++)
            {
                var pair = pairs[i];
                channels[i].Wire(device, pair.InEndpoint, pair.OutEndpoint);
                this.Log(LogLevel.Info, "CDC channel {0}: control interface {1}, data IN ep {2}, OUT ep {3}{4}",
                    i, pair.ControlInterface, channels[i].InEndpoint, channels[i].OutEndpoint,
                    channels[i].InEndpoint != pair.InEndpoint || channels[i].OutEndpoint != pair.OutEndpoint
                        ? " (constructor override)" : "");
                // SET_LINE_CODING (115200 8N1; the guest only stores it) then
                // SET_CONTROL_LINE_STATE with DTR|RTS, per control interface.
                controlRequests.Enqueue(new KeyValuePair<SetupPacket, byte[]>(
                    ClassRequestPacket(SetLineCoding, 0, pair.ControlInterface, 7),
                    new byte[] { 0x00, 0xC2, 0x01, 0x00, 0x00, 0x00, 0x08 }));
                controlRequests.Enqueue(new KeyValuePair<SetupPacket, byte[]>(
                    ClassRequestPacket(SetControlLineState, DtrAndRts, pair.ControlInterface, 0),
                    null));
            }

            SendControlSequence(device, controlRequests, () =>
            {
                foreach(var channel in channels)
                {
                    if(channel.IsWired)
                    {
                        StartDeviceToHostPump(device, channel);
                    }
                }
                this.Log(LogLevel.Info, "CDC channels wired; DTR asserted");
            });
        }

        private void SendControlSequence(IUSBDevice device, Queue<KeyValuePair<SetupPacket, byte[]>> requests, Action done)
        {
            if(requests.Count == 0)
            {
                done();
                return;
            }
            var request = requests.Dequeue();
            device.USBCore.HandleSetupPacket(request.Key, _ => SendControlSequence(device, requests, done), request.Value);
        }

        // Device->host pump: the CDCToUARTConverter pattern -- a one-shot read
        // callback per IN endpoint, re-armed after every delivery. Empty chunks
        // (end-of-packet markers / ZLPs) forward nothing but still re-arm.
        //
        // The re-arm is deferred (ReArmDeviceToHostPump) rather than issued
        // synchronously from inside the callback: USBEndpoint.HandlePacket hands a
        // queued device->host chunk to the armed one-shot `dataCallback` and then
        // sets `dataCallback = null` *after* the callback returns. A re-arm done
        // synchronously within the callback is therefore immediately clobbered by
        // that trailing null, so only the very first delivery (armed from wiring,
        // outside any HandlePacket) would ever fire and every device->host
        // transfer after the first is left sitting undelivered in the endpoint
        // buffer (observed as "only the 1st Studio reply per session reaches the
        // host"). Deferring the re-arm so its SetDataReadCallbackOneShot runs
        // after HandlePacket has unwound avoids the clobber; it serializes on the
        // endpoint buffer lock and picks up any chunk queued in the meantime.
        private void StartDeviceToHostPump(IUSBDevice device, CdcAcmBridgeChannel channel)
        {
            var endpoint = device.USBCore.GetEndpoint(channel.InEndpoint, USBDirection.DeviceToHost);
            if(endpoint == null)
            {
                this.Log(LogLevel.Error, "No DeviceToHost endpoint {0} for CDC channel {1}", channel.InEndpoint, channel.Index);
                return;
            }
            endpoint.SetDataReadCallbackOneShot((_, data) =>
            {
                foreach(var value in data)
                {
                    channel.HandleDeviceData(value);
                }
                ReArmDeviceToHostPump(device, channel);
            });
        }

        // Re-arm the device->host read one-shot from outside the current
        // HandlePacket call stack (see StartDeviceToHostPump). A ThreadPool hop is
        // enough: SetDataReadCallbackOneShot serializes on the endpoint's buffer
        // lock, so the re-arm runs only after HandlePacket releases it (and its
        // clobbering `dataCallback = null` has executed). The guest's IN transfer
        // already completed synchronously in the model, so this host-side delay is
        // invisible to firmware. Swallow teardown races (the device/endpoint may
        // be gone once the emulation is stopping) to keep them off the ThreadPool.
        private void ReArmDeviceToHostPump(IUSBDevice device, CdcAcmBridgeChannel channel)
        {
            ThreadPool.QueueUserWorkItem(_ =>
            {
                try
                {
                    StartDeviceToHostPump(device, channel);
                }
                catch(Exception e)
                {
                    this.Log(LogLevel.Debug, "Device->host pump re-arm aborted on CDC channel {0}: {1}", channel.Index, e.Message);
                }
            });
        }

        // Walk the real configuration descriptor and collect, in interface
        // order, each CDC function's control-interface number and its data
        // interface's bulk IN/OUT endpoint numbers. A CDC-ACM function is a
        // Communications interface (class 0x02) followed by a CDC Data
        // interface (class 0x0A) carrying the two bulk endpoints; other
        // interfaces (e.g. HID) are skipped.
        private List<CdcDataPair> ParseCdcDataPairs(byte[] descriptor)
        {
            var pairs = new List<CdcDataPair>();
            var pendingControlInterface = -1;
            var currentClass = -1;
            var inEndpoint = 0;
            var outEndpoint = 0;

            Action flush = () =>
            {
                if(currentClass == CdcDataClass && inEndpoint != 0 && outEndpoint != 0)
                {
                    pairs.Add(new CdcDataPair
                    {
                        ControlInterface = pendingControlInterface,
                        InEndpoint = inEndpoint,
                        OutEndpoint = outEndpoint,
                    });
                    pendingControlInterface = -1;
                }
                inEndpoint = 0;
                outEndpoint = 0;
            };

            for(var offset = 0; offset + 1 < descriptor.Length && descriptor[offset] > 0; offset += descriptor[offset])
            {
                var type = descriptor[offset + 1];
                if(type == InterfaceDescriptorType && offset + 6 < descriptor.Length)
                {
                    flush();
                    currentClass = descriptor[offset + 5];
                    if(currentClass == CdcCommunicationsClass)
                    {
                        pendingControlInterface = descriptor[offset + 2];
                    }
                }
                else if(type == EndpointDescriptorType && offset + 4 < descriptor.Length && currentClass == CdcDataClass)
                {
                    var address = descriptor[offset + 2];
                    var isBulk = (descriptor[offset + 3] & 0x3) == 0x2;
                    if(isBulk)
                    {
                        if((address & 0x80) != 0)
                        {
                            inEndpoint = address & 0x0F;
                        }
                        else
                        {
                            outEndpoint = address & 0x0F;
                        }
                    }
                }
            }
            flush();
            return pairs;
        }

        private static SetupPacket GetConfigurationDescriptorPacket(ushort count)
        {
            return new SetupPacket
            {
                Recipient = PacketRecipient.Device,
                Type = PacketType.Standard,
                Direction = USBDirection.DeviceToHost,
                Request = (byte)StandardRequest.GetDescriptor,
                Value = ConfigurationDescriptorValue,
                Index = 0,
                Count = count,
            };
        }

        private static SetupPacket ClassRequestPacket(byte request, ushort value, int interfaceNumber, ushort count)
        {
            return new SetupPacket
            {
                Recipient = PacketRecipient.Interface,
                Type = PacketType.Class,
                Direction = USBDirection.HostToDevice,
                Request = request,
                Value = value,
                Index = (ushort)interfaceNumber,
                Count = count,
            };
        }

        private readonly CdcAcmBridgeChannel[] channels;

        private const int DescriptorHeaderLength = 9;
        private const ushort ConfigurationDescriptorValue = 0x0200;
        private const byte InterfaceDescriptorType = 0x04;
        private const byte EndpointDescriptorType = 0x05;
        private const int CdcCommunicationsClass = 0x02;
        private const int CdcDataClass = 0x0A;
        private const byte SetLineCoding = 0x20;
        private const byte SetControlLineState = 0x22;
        private const ushort DtrAndRts = 0x0003;

        private struct CdcDataPair
        {
            public int ControlInterface;
            public int InEndpoint;
            public int OutEndpoint;
        }
    }

    // One CDC channel of the bridge, exposed as its own IUART (registered in
    // the device's machine like a VirtualConsole -- see
    // CreateDualCdcAcmBridge) so it can be wired to its own
    // CreateServerSocketTerminal. Bytes written before the channel is wired
    // (endpoints not yet discovered) are queued and flushed on wiring, so an
    // early TCP client cannot lose data.
    public class CdcAcmBridgeChannel : IUART
    {
        public CdcAcmBridgeChannel(int index, int inEndpointOverride = 0, int outEndpointOverride = 0)
        {
            Index = index;
            this.inEndpointOverride = inEndpointOverride;
            this.outEndpointOverride = outEndpointOverride;
            pendingHostData = new Queue<byte>();
            innerLock = new object();
        }

        public void Reset()
        {
            // Intentionally left empty: channel state tracks the (host-side)
            // bridge wiring, which outlives machine resets.
        }

        // Host-side input (e.g. the TCP client of a server socket terminal):
        // forward to the device's bulk OUT endpoint.
        public void WriteChar(byte value)
        {
            USBEndpoint endpoint = null;
            lock(innerLock)
            {
                if(device == null)
                {
                    pendingHostData.Enqueue(value);
                    return;
                }
                endpoint = device.USBCore.GetEndpoint(OutEndpoint, USBDirection.HostToDevice);
            }
            endpoint?.WriteData(new[] { value });
        }

        // Device-side output (called by the bridge's IN-endpoint pump).
        public void HandleDeviceData(byte value)
        {
            CharReceived?.Invoke(value);
        }

        public void Wire(IUSBDevice usbDevice, int discoveredInEndpoint, int discoveredOutEndpoint)
        {
            byte[] flush = null;
            USBEndpoint endpoint = null;
            lock(innerLock)
            {
                InEndpoint = inEndpointOverride != 0 ? inEndpointOverride : discoveredInEndpoint;
                OutEndpoint = outEndpointOverride != 0 ? outEndpointOverride : discoveredOutEndpoint;
                device = usbDevice;
                if(pendingHostData.Count > 0)
                {
                    flush = pendingHostData.ToArray();
                    pendingHostData.Clear();
                    endpoint = device.USBCore.GetEndpoint(OutEndpoint, USBDirection.HostToDevice);
                }
            }
            if(flush != null)
            {
                endpoint?.WriteData(flush);
            }
        }

        public bool IsWired => device != null;

        public int Index { get; }

        public int InEndpoint { get; private set; }

        public int OutEndpoint { get; private set; }

        public uint BaudRate { get; set; }

        public Bits StopBits { get; set; }

        public Parity ParityBit { get; set; }

        public byte DataBits { get; set; }

        public event Action<byte> CharReceived;

        private volatile IUSBDevice device;
        private readonly int inEndpointOverride;
        private readonly int outEndpointOverride;
        private readonly Queue<byte> pendingHostData;
        private readonly object innerLock;
    }
}
