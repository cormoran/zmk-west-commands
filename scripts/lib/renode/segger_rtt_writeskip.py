# Zephyr-aware SEGGER RTT capture helper for Renode (adapted from Renode's own
# scripts/single-node/segger-rtt.py).
#
# Renode's stock helper hooks `SEGGER_RTT_WriteNoLock`, but Zephyr's RTT log
# backend (subsys/logging/backends/log_backend_rtt.c) never calls that -- it
# always calls `SEGGER_RTT_WriteSkipNoLock` (in both BLOCK and non-BLOCK
# modes). So against a real Zephyr/ZMK image the stock hook silently never
# fires (symbol not found -> WarningLog, no crash) and captures nothing. The
# register-argument shape is identical (r0=BufferIndex, r1=pBuffer,
# r2=NumBytes per AAPCS), so the same hook body works -- only the symbol name
# differs.
#
# This file is `include`d over the Renode monitor. Renode strips the `mc_`
# prefix from monitor commands defined in included .py files, so the function
# below is invoked from the monitor / resc as `setup_segger_rtt_wskip`.
#
# Working capture chain (see renode_harness.boot_single_real(rtt=True)):
#   sysbus LoadELF <bin>                 # FIRST, so the symbol resolves
#   include @<this file>
#   machine CreateVirtualConsole "segger_rtt"
#   setup_segger_rtt_wskip sysbus.segger_rtt
#   emulation CreateServerSocketTerminal <port> "rtt_term" false
#   connector Connect sysbus.segger_rtt rtt_term
# then read the <port> socket exactly like a UART console socket.
#
# If the symbol is absent (e.g. a non-RTT build), setup logs a warning and
# installs no hook -- it never fails, so it is safe to call unconditionally.


def mc_setup_segger_rtt_wskip(console):
    bus = monitor.Machine.SystemBus

    def write(cpu, _):
        pointer = cpu.GetRegister(1).RawValue
        length = cpu.GetRegister(2).RawValue
        for i in range(length):
            console.DisplayChar(bus.ReadByte(pointer + i))
        cpu.SetRegisterUlong(0, length)
        cpu.PC = cpu.LR

    for cpu in bus.GetCPUs():
        found, addresses = bus.TryGetAllSymbolAddresses(
            "SEGGER_RTT_WriteSkipNoLock", context=cpu
        )
        if not found:
            cpu.WarningLog(
                "Symbol 'SEGGER_RTT_WriteSkipNoLock' not found. Make sure the "
                "binary is loaded before calling setup_segger_rtt_wskip (and "
                "that it is an RTT-logging build)."
            )
        for address in addresses:
            cpu.AddHook(address, write)
