# Design: Studio-over-USB on the unchanged firmware image (Renode "usb mode")

Status: **implemented through Phase 3**, 2026-07-19. Phases 0-2 closed gaps
(a)-(d); Phase 2 gate green 2026-07-19 -- a real `studio-rpc-usb-uart`
flashable image answers a Studio RPC `GetDeviceInfo` round trip over the
emulated USB CDC via `DualCdcAcmBridge` + two TCP socket terminals
(`renode_harness.attach_dual_cdc_bridge`). Phase 3 (same day) productized it:
`west zmk-renode-test --mode usb` dispatches to `renode_smoke.run_usb_smoke`
(RPC assert always; console-banner assert auto-detected when a second CDC is
wired; bounded one-retry, the ble-split pattern), the `ZMK_RENODE_MODE=usb`
module-test env contract, the `mode: usb` action input, a CI smoke step
reusing the ble job's real-image artifact, and user/internals docs
(README, renode-testing.md, renode-internals.md). Not done (future
consolidation): ble mode intentionally keeps the python `usbd_stub` -- the
no-host C# fork boots identically (parity-checked), but switching ble mode
onto it buys nothing today and would couple the modes; revisit when the fork
next changes. Phase 4 (wired-split central Studio-over-USB, HID report
capture, upstreaming the NRF_USBD fixes) remains open. Phase-2 findings that
amend the text below:

- **Bridge channels are machine peripherals, not externals.** `connector
  Connect <uart> <terminal>` runs `BackendTerminal.AttachTo`, which resolves
  the IUART's machine via `GetMachine()` -- fatal crash on 1.16.1 for a
  machine-less external IUART (so `CDCToUARTConverter`-style `IUART +
  IExternal` cannot be wired to a socket terminal at all). The channels are
  registered on sysbus with `NullRegistrationPoint` (the VirtualConsole
  pattern) instead.
- **CDC interface order** (verified empirically from the real descriptors):
  when both CDCs are enabled, the board console CDC is the composite's first
  CDC function and the snippet Studio CDC the second. The standard
  `studio-rpc-usb-uart` artifacts (`exp-hw-obs`/`exp-hw-real`) have the board
  CDC *disabled* (it requires `CONFIG_ZMK_USB_LOGGING`), so their composite
  is 1xCDC (Studio, interfaces 0/1) + HID and there is no console-CDC channel
  to assert; the dual-channel path (console banner on cdc0 + Studio RPC on
  cdc1) was proven on the split-central artifact, which enables both.
- **Two-step attach.** Terminal wiring must complete before enumeration
  starts or the first post-configuration device output races the hookup;
  `CreateDualCdcAcmBridge` / `AttachDualCdcAcmBridge` split exists for this,
  and `pause`/`start` completions must be polled (`machine IsPaused`) -- both
  return before the state change lands on a busy machine.

## Goal

Replace uart mode's firmware-side deviation (the `renode-studio-uart` snippet +
`renode-test-module`'s `ZMK_TRANSPORT_NONE` transport clone) with a mode that
boots the **exact `studio-rpc-usb-uart` flashable image** — the same ELF ble
mode already runs — and drives **Studio RPC over the emulated USB CDC-ACM**,
bidirectionally, from the existing TCP harness. Zero firmware change, zero
root/kernel-module requirements, CI-identical environment.

Expected payoff beyond parity with uart mode:

- Delete the snippet + transport-clone module (the whole reason uart mode
  needs a special build).
- Much faster than ble mode (single machine, register/DMA-driven, no 10 µs
  BLE quantum): uart-mode-like wall clock on the *real* image. Candidate to
  become the default mode for module RPC tests.
- Wired-split tests gain Studio RPC on the central (both UARTEs stay free for
  split link + console — previously impossible: only 2 UARTEs exist).
- Opens the door to asserting real USB HID keystroke reports later.

## Where the 2026-07-19 spike left off (and what this study adds)

The spike (`/home/ubuntu/usb-spike/`, recorded in the project memory) proved:

- Renode 1.16.1 **ships a `USB.NRF_USBD` C# model** (not wired into the stock
  nrf52840.repl). The real image boots against it with **no usbd stub**:
  ENABLE/USBPULLUP proceed. VBUS is a non-issue — `NRF_CLOCK` (which covers
  POWER at 0x40000000) hardcodes `USBREGSTATUS.VBUSDETECT|OUTPUTRDY = 1` and
  the driver reads it synchronously at attach; no POWER IRQ needed.
- Two model-level blockers stop Studio-over-USB:
  1. `NRF_USBD.HandleSetupPacket` answers SET_ADDRESS / SET_CONFIGURATION
     in-model ("simplification" per the source comment), so the guest never
     reaches `USB_DC_CONFIGURED`.
  2. `CDCToUARTConverter` is one-way (device→host; `WriteChar` is an echo;
     IN endpoint hardcoded to 2). No host→device data path exists at all.

The spike concluded "multi-day, upstream-risk". This study resolves the two
unknowns that drove that verdict:

1. **No Renode fork/rebuild is needed.** Runtime C# compilation via
   `include @File.cs` is verified working in the installed 1.16.1 portable
   build (probe: `/home/ubuntu/usb-research-probe/AdhocProbe.cs`). NRF_USBD is
   a self-contained 638-line MIT file; every member is private/non-virtual so
   it must be **forked, not subclassed** — but the fork lives in this repo and
   loads at runtime, exactly like our python stubs do today.
2. **The precise gap list is bounded** (~350 changed/new LOC in the fork +
   a ~250 LOC new host-bridge external; details below), because most of the
   machinery already exists: guest→host IN transfers work, `USBHost` performs
   real enumeration automatically on `Register`, and `USBEndpoint` provides
   both push (`SetDataReadCallbackOneShot`) and `DataWritten` hooks.

Source of truth for the analysis: renode-infrastructure at commit
`add012af003a0f620d3da52828262676f374d121` (the exact submodule commit of the
`renode/renode` v1.16.1 tag). Local copies of the analyzed files:
`/tmp/renode-usb/`. Relevant files, all MIT-licensed:

- `src/Emulator/Peripherals/Peripherals/USB/NRF_USBD.cs` (638 lines)
- `src/Emulator/Peripherals/Peripherals/USB/USBHost.cs` (152 lines)
- `src/Emulator/Peripherals/Peripherals/USB/CDCToUARTConverter.cs` (103 lines)
- `src/Emulator/Peripherals/Peripherals/Miscellaneous/NRF_CLOCK.cs` (263 lines)

## Firmware-side contract (verified in Zephyr 4.1 / ZMK sources)

The whole readiness chain hangs on **one host action: SET_CONFIGURATION**.

- `usb_dc_nrfx.c` + `nrf_usbd_common.c` (legacy stack): attach needs
  `POWER.USBREGSTATUS` VBUS bits (already always-on in Renode's NRF_CLOCK,
  read synchronously at `usb_dc_attach()`), then `EVENTCAUSE.READY` (bit 11)
  after `ENABLE=1`, then the driver raises `USBPULLUP=1`.
- SETUP: guest reads `BMREQUESTTYPE..WLENGTHH` (0x480–0x49C) on
  `EVENTS_EP0SETUP`; control-IN answers via `EPIN[0].PTR/MAXCNT` +
  `TASKS_STARTEPIN[0]`; host→device data stage via `TASKS_EP0RCVOUT` +
  `SIZE.EPOUT[0]` + `TASKS_STARTEPOUT[0]` DMA; status stage via
  `TASKS_EP0STATUS`.
- **SET_ADDRESS must NOT be surfaced to the guest** — real nRF hardware
  handles it autonomously; the driver only asserts `addr == USBD->USBADDR`
  (`usb_dc_nrfx.c` `usb_dc_set_address`). The model must self-ack it *and
  latch USBADDR*.
- Bulk OUT (host→guest): `EVENTS_EPDATA` + `EPDATASTATUS` (IN bits 0–8, OUT
  bits 16–24, W1C) + `SIZE.EPOUT[n]` + `EPOUT[n].PTR/MAXCNT/AMOUNT` +
  `TASKS_STARTEPOUT[n]` → `EVENTS_ENDEPOUT[n]`.
- ZMK readiness: `USB_DC_CONFIGURED` → `zmk_usb_is_hid_ready()` →
  endpoint selects `ZMK_TRANSPORT_USB` → Studio's UART transport (bound to
  the snippet's CDC-ACM node) gets `rx_start()`. **No DTR dependency** —
  Zephyr's legacy `cdc_acm.c` gates TX purely on `configured`
  (`tx_work_handler`), and SET_LINE_CODING/SET_CONTROL_LINE_STATE only store
  state. We'll still send DTR=1 for fidelity, but it is not load-bearing.
- After SET_CONFIGURATION, `cdc_acm` queues a ZLP on its IN endpoint before
  real TX. Harmless here: the model ACKs IN transfers immediately
  (no host-side polling machinery exists or is needed).
- Errata accesses to tolerate silently: writes to `0x40027C1C`,
  `0x40027800/0x804`, and the unmapped `0x4006EC00–0x4006ED14` block
  (sysbus warn+ignore is fine — already the observed behavior).

## Renode-side gap list (the actual work)

Everything ships in this repo as two runtime-included C# files plus ~10 lines
of repl/resc changes. `USBHost`, `USBDeviceCore`, `USBEndpoint`, `NRF_CLOCK`
need no changes.

### (a) Fork `NRF_USBD.cs` → `NRF_USBD_Full.cs`: fix EP0 (~80–120 LOC)

- `HandleSetupPacket`: for Standard SET_ADDRESS, do **not** raise Ep0Setup;
  latch `USBADDR` (currently never assigned) and self-ack (matches real HW).
  For everything else (incl. SET_CONFIGURATION and class requests): latch the
  packet **and its host→device data payload** (the `arg2` parameter, currently
  dropped) and raise Ep0Setup only.
- `TASKS_EP0STATUS` is currently a no-op — make it invoke the pending
  `setupPacketResultCallback` (this one-liner is what unblocks forwarding
  SET_CONFIGURATION without hanging USBHost's enumeration).
- Implement the EP0 OUT data stage: `TASKS_EP0RCVOUT` arms delivery; DMA the
  latched payload to `EPOUT[0].PTR` on `TASKS_STARTEPOUT[0]`; define
  `SIZE.EPOUT[0]`; raise `EVENTS_ENDEPOUT[0]`/`EP0DATADONE`.
- Register fixes: `BMREQUESTTYPE.TYPE` is hardcoded 0 (class requests would
  read as Standard!) and `RECIPIENT` is a tag — read both from the latched
  packet; `WINDEXH`/`WLENGTHH` are enum-only (never defined) — define them;
  set `EVENTCAUSE.READY` on ENABLE=1 with W1C semantics (today it "works"
  only because the driver's write-1-to-clear *sets* the plain R/W flag);
  add `INTENSET`/`INTENCLR` (0x304/0x308) handling (~10 LOC).

### (b) Same fork: guest-facing bulk OUT (~150–220 LOC)

- Keep (instead of discarding) the HostToDevice `USBEndpoint` refs created in
  `InitiateUSBCore`; subscribe their `DataWritten`.
- Per-OUT-ep byte queues; on arrival set the `EPDATASTATUS` OUT bit (implement
  bits 16–24 as real W1C flags — and fix the existing IN-bit W1C bug, where
  write-back currently *sets* the flag) + raise `EVENTS_EPDATA`.
- Define `SIZE.EPOUT[n]` (pending chunk length), `EPOUT[n].PTR/MAXCNT/AMOUNT`
  (only 3 of 8 are even in the enum today), `TASKS_STARTEPOUT[n]` →
  `sysbus.WriteBytes` capped at MAXCNT → set AMOUNT → raise
  `EVENTS_ENDEPOUT[n]`.

### (c) Same fork: generalize IN routing (~20–40 LOC)

Today all non-EP0 IN data funnels to a single hardcoded framework endpoint
(id 2 — why `CDCToUARTConverter` defaults to 2). Pass explicit `id:` values in
`InitiateUSBCore`'s `WithEndpoint` calls so framework endpoint ids match nRF
ep numbers, keep the refs in an array, and route `GetData(ep)` accordingly.

### (d) New external: `DualCdcAcmBridge : USBHost, IExternal` (~200–280 LOC)

- Exposes **two IUART channels** (console CDC + Studio-RPC CDC) so the
  harness's existing `CreateServerSocketTerminal` + `connect_uart()` plumbing
  works unchanged. (Pattern: `CDCToUARTConverter` already is
  `USBHost + IUART`; either two parameterized instances or one class with two
  child IUART objects.)
- Device→host pump per channel: `GetEndpoint(inEp).SetDataReadCallbackOneShot`
  with re-arm (copy of CDCToUARTConverter's loop). Host→device: `WriteChar` →
  `GetEndpoint(outEp, HostToDevice).WriteData(...)` → delivered by (b).
- `DeviceEnumerated` override: send CDC `SET_CONTROL_LINE_STATE (DTR=1)` (and
  optionally `SET_LINE_CODING` — its 7-byte payload rides the
  `additionalData` arg that (a) makes reach the guest).
- **Endpoint discovery**: don't hardcode ep numbers. In `DeviceEnumerated`,
  issue a forwarded `GET_DESCRIPTOR(Configuration)` control read — the guest
  answers with its *real* descriptors via the (already working) EP0 IN path —
  and parse the two CDC data interface ep pairs (~60 LOC). Ctor override as
  escape hatch.
- **Enumeration timing**: stock `USBHost` enumerates on `Register` after a
  fixed 1000 ms virtual delay, decoupled from the guest. To avoid racing the
  guest's USB init (SETUP fired before INTEN is set = silent hang), trigger
  `Register` off the fork's `USBPULLUP` 0→1 edge (models real hardware
  semantics; small event/callback from the fork to the bridge), or as a
  fallback keep a configurable delay + one retry.

### Integration (~10 lines + harness glue)

- New platform variant (or additions to `xiao_nrf52840_real.repl` route):
  `usbd: USB.NRF_USBD_Full @ sysbus 0x40027000` replacing the python
  `usbd_stub.py`, `-> nvic@39`; `.resc` gains `include @.../NRF_USBD_Full.cs`,
  `include @.../DualCdcAcmBridge.cs`, `emulation CreateDualCdcAcmBridge`-style
  setup + two `CreateServerSocketTerminal`s.
- `zmk_renode_test.py`: `--mode usb` — same DUT artifact as ble mode, smoke =
  console-over-CDC assertion + `rpc_client.py` round trip (both reused from
  uart mode).

## Mode interactions (important)

- **ble mode must NOT enumerate USB.** ZMK prefers the USB transport when
  `zmk_usb_is_hid_ready()`; if the bridge enumerated the DUT in ble mode,
  Studio would flip to USB and the BLE Studio smoke would break. usb mode =
  bridge registered; ble mode = keep today's unplugged-idle behavior (the
  forked model without a registered host behaves exactly like the stub:
  enable+pullup, then silence).
- uart mode stays until usb mode is proven in CI across consumer repos; then
  deprecate the snippet + `renode-test-module` (they exist only to dodge USB).

## Risks

| Risk | Assessment / mitigation |
|---|---|
| W1C / IRQ-recompute fidelity vs `nrf_usbd_common`'s read-then-write-back loops | The known-sharpest edge; covered by Phase-1 gate (observe `USB_DC_CONFIGURED` via RTT before building the bridge) |
| Enumeration races guest USB init | Pullup-edge-triggered enumeration (see (d)) |
| Composite descriptor parsing surprises (2×CDC+HID ordering across boards/configs) | Parse real descriptors instead of hardcoding; ctor escape hatch |
| Renode version bump drifts `USBDeviceCore` API under the fork | Fork is pinned+self-contained; `renode-version` is already pinned in the action; re-diff against upstream on bumps (upstream NRF_USBD is near-dormant) |
| SOF-dependent guest paths (`CONFIG_USB_DEVICE_SOF`) | Not enabled in ZMK images today; note only |
| Errata hidden-register accesses | Warn+ignore on unmapped sysbus already observed benign |

Explicitly *not* risks: VBUS/POWER modeling (already always-on), DTR gating
(not load-bearing in Zephyr legacy cdc_acm), per-run wall-clock (no BLE
quantum involved).

## Rejected alternatives

| Option | Why rejected |
|---|---|
| USBIP (`emulation CreateUSBIPServer`) | Needs `vhci-hcd` + root on the runner (absent in the dev container, uncertain in CI); still requires the same NRF_USBD EP0/OUT fixes anyway |
| Emulated Linux host machine (`USBConnector` + `MPFS_USB`, Fomu-test style) | Boots a second OS per test; heavyweight; still needs model fixes |
| Python-peripheral USBD host | PythonPeripheral cannot raise IRQs, has no timers; a USB stack won't fit its request-reactive model |
| Symbol hooks on cdc_acm/uart functions | Fragile against inlining/symbol changes; abandons the "real register-level path" value of the test |
| Wait for upstream Renode | The "simplification" comment is unchanged on master; no open PR/issue found |

## Phased plan (each phase lands separately with a verification gate)

- **Phase 0 (~0.5d)** — Scaffold: vendor the fork (pristine copy from
  `add012af`, renamed), runtime-include it, swap the repl entry, keep no
  bridge. Gate: boot parity with today's real-binary boot (liveness smoke),
  RTT log shows enable+pullup.
- **Phase 1 (~1d)** — EP0 fixes (a) + IN routing (c), enumeration via stock
  `USBHost` auto-enum (bare `USBHost` subclass, no data bridge). Gate: RTT
  shows `USB_DC_CONFIGURED`; ZMK log shows endpoint transport = USB and
  Studio `rx_start`.
- **Phase 2 (~1–1.5d)** — Bulk OUT (b) + `DualCdcAcmBridge` (d) + descriptor
  parsing + DTR. Gate: boot log readable on console-CDC TCP socket;
  `rpc_client.py` `GetDeviceInfo` round trip green on the Studio-CDC socket.
- **Phase 3 (~0.5–1d)** — Productize: `--mode usb`, env contract
  (`ZMK_RENODE_MODE=usb`), action input, docs, CI job (same DUT artifact as
  ble). Gate: repo CI green incl. new job; uart mode untouched.
- **Phase 4 (optional)** — Wired-split + Studio-over-USB on the central;
  HID IN report capture for keystroke assertions; upstream the NRF_USBD fixes
  to renode-infrastructure (good-citizen PR; the fork stays regardless).

Total: **~4–5 agent-days**, no firmware changes, no Renode rebuild, no new
host privileges. The spike's "multi-day" sizing was right; its "upstream-risk"
concern is resolved by the runtime-include fork.
