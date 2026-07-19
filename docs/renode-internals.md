# Renode internals: booting a real ZMK image under emulation

This page explains **how a real, flashable ZMK hardware image boots under
Renode at all** — the platform stubs, the NVS preload, the fake CCM, and the
two on-air constraints that make `--mode ble` work. You do not need any of this
to *use* `west zmk-renode-test`; it is here for the curious and for anyone
extending the harness. For usage, flags, and troubleshooting see
[renode-testing.md](renode-testing.md); for the two-mode overview see the
repo [README](../README.md).

The default **ble mode** boots the *exact* `studio-rpc-usb-uart` hardware
artifact (USB CDC + QSPI NOR + BLE all enabled), with **zero firmware-side
deviation** — that is its whole appeal (no extra module build config), and it
means the emulator has to stand in for the peripherals that image expects (this
page). The alternative **uart mode** sidesteps all of this: it boots an ELF
built with the `renode-studio-uart` snippet, which re-binds Studio RPC + the
console to real UART peripherals Renode drives directly.

## Why a real image needs platform help

Renode's stock nRF52840 has no USBD/QSPI/FICR/NVMC models, so a real image would
hang or oops on it. The `scripts/lib/renode/platforms/xiao_nrf52840_real.repl`
platform adds five things (see that file and
`scripts/lib/renode/platforms/models/`):

1. **QSPI stub** (`0x40029000`) — completes the `nrfx_qspi` busy-wait on
   `EVENTS_READY`; the JEDEC probe then mismatches so `nordic_qspi_nor` fails
   gracefully (`-ENODEV`) instead of hanging. The external NOR is not the
   settings backend, so this is harmless.
2. **USBD stub** (`0x40027000`) — returns `EVENTCAUSE.READY` so
   `nrf_usbd_common` enable completes, then reads 0 (no VBUS) so the driver
   idles like an unplugged cable.
3. **FICR model** (`0x10000000`) — serves real `CODEPAGESIZE`/`CODESIZE` (so
   `settings_nvs` sizes its partition instead of failing `-EDOM`) and a BLE
   identity address. Without it, settings never load, BT host init stalls, and
   the HCI Read-BD_ADDR times out into a `BT_ASSERT` oops around 10 s.
4. **NVMC model** (`0x4001E000`) — the flash controller. With no model the
   region reads 0, so a BLE-enabled image can spin-poll `NVMC.READY` forever the
   first time it touches flash (an observed *silent* hang in two-machine runs).
   The model serves `READY`/`READYNEXT`=1 and implements real page erase
   (`ERASEPAGE`/`ERASEPCR0` fill the 4 KiB page with `0xFF`), so NVS garbage
   collection works once a settings sector fills.
5. **NVS preload** — Renode zero-fills flash, but NVS needs erased sectors to
   read `0xFF`, so the storage partition is preloaded with `0xFF` (else
   `nvs_mount` fails `-EDEADLK`). Defaults to the **xiao_ble**
   `storage_partition` (`0xec000`, size `0x8000`); override with
   `--storage-addr`/`--storage-size` for other boards.

## Per-machine BLE identity (multi-machine)

The FICR model's `DEVICEADDR` is parameterized so two machines in one emulation
can advertise **distinct** BLE addresses (sharing one breaks BLE tests).
`renode_harness.boot_single_real(..., device_addr=<48-bit int>)` injects a
per-machine copy of the FICR model; `renode_harness.device_addr_for_machine(n)`
returns a deterministic static-random address per machine (machine 0 =
`C0:E7:E7:E7:E7:E7`, machine 1 = `…:E8`, …). `boot_ble_pair` uses this so the
DUT and host advertise different addresses; `boot_ble_split` extends it to
**three** machines (central = `…:E7`, peripheral = `…:E8`, host = `…:E9`).

## Fake CCM — NOT cryptographically real

Renode has no AES-CCM engine, so every machine in ble / ble-split mode shares a
*fake* CCM peripheral
([`platforms/models/ccm.py`](../scripts/lib/renode/platforms/models/ccm.py))
that is an **identity transform**: it just appends/strips 4 dummy MIC bytes and
reports MIC-OK. It only has to be self-consistent because both endpoints run the
same fake. This is perfect for a **functional** test — the encrypted code paths
on both sides execute for real — but it validates **nothing** about
cryptography. Do not use it to check crypto correctness.

> **ble-split: three machines, two encrypted links, one CCM.**
> `boot_ble_split` puts the fake CCM in **all three** machines
> (`three_machine_ble.resc`), because *both* on-air links are encrypted: the
> split peripheral↔central link (ZMK split does an encrypted GATT link at BT
> security L2) **and** the central↔host Studio link. The central holds both at
> once (GAP central to the peripheral, GAP peripheral to the host).

Two hard-won `ccm.py` details are preserved with comments in the file (each was
a real failure mode):

| Symptom | Root cause / fix |
|---|---|
| Renode `Payload length (34) … trimming` + peer disconnect `0x3d` right after the encryption start | lazy TX transform sent stale OUTPTR bytes — the transform must be **eager** (radio builds the frame before firmware reads `EVENTS_ENDCRYPT`) |
| 30 s SMP timeout, `security_changed err=9`, DUT never TX-encrypts | lazy RX transform — Zephyr's `isr_rx_pdu` reads the OUT buffer before `EVENTS_ENDCRYPT`, so RX must be **eager** too |
| Fast `0x3d` disconnect right after pairing | CCM payload copied at offset **+2** instead of **+3** (the nRF52 CCM data structure is Header/Length/RFU/Payload; radio `S1INCL=1`) |

## The two on-air constraints (both load-bearing)

- **Host-side data-length cap.** `renode-ble-host`'s `prj.conf` sets
  `CONFIG_BT_CTLR_DATA_LENGTH_MAX=27`. Every encrypted on-air PDU is
  `payload + 4-byte MIC`; `27+4 = 31` is exactly Renode's `NRF52840_Radio`
  packet cap. LE Data Length's effective value is `min(local, remote)`, so
  capping the **host** caps both directions — which is why the DUT needs no
  change. Without it the DUT negotiates larger PDUs and anything over `27+4`
  gets "trimmed" by the radio and the link breaks.
  In **ble-split** the host's cap only covers the host↔central link; the
  peripheral↔central split link is between two ZMK images that neither default
  to 27, so the `renode_split` shield caps **both** split halves' `.conf` with
  `CONFIG_BT_CTLR_DATA_LENGTH_MAX=27` (the central's single controller cap
  covers both of its links). All three machines therefore cap to 27, and the
  smoke asserts **0** `trimming` warnings as a regression guard. (ZMK exposes
  this Zephyr-controller Kconfig directly, so no firmware patch is needed.)
- **Global quantum `0.00001` (10 µs).** The two-machine `.resc` sets a 10 µs
  sync quantum; coarser values (even `0.00003` and `0.0001`) break the soft
  link-layer so the host never receives an advertisement. This 10 µs sync is the
  dominant wall-clock cost of ble mode (the two CPUs re-synchronise 100 000×
  per virtual second). The fine quantum is *load-bearing through connection +
  pairing* (the soft link-layer's radio-event prepare runs late and asserts
  otherwise), but once the encrypted link is up (host `STAGE:S4`) the link-layer
  tolerates a 100×-coarser quantum — the basis of the `--steady-quantum`
  fine-then-coarse lever (see [renode-testing.md](renode-testing.md#ble-mode-performance)).
  In **ble-split** the same 10 µs quantum is load-bearing through **both**
  pairings (`three_machine_ble.resc`), and with three CPUs re-syncing it is the
  heaviest run here (~0.1× realtime; both pairings settle by ~18 s virtual).

## Wired split: the UART hub (no platform stubs needed)

`--mode split` needs none of the above. A wired-split image built with the
`renode_wired_split` shield disables USB + QSPI and does not enable BLE, so both
halves boot on the **plain** `xiao_nrf52840.repl` — no USBD/QSPI/FICR/NVMC stubs,
no NVS preload, no fake CCM.

The one platform mechanic is the **UART hub**. `platforms/split_wired.resc`
creates two machines (`central`, `peripheral`); each puts its console on `uart0`
(its own TCP socket) and its split link on `uart1`. Both `uart1`s are connected
to a single `emulation CreateUARTHub "split_link"`, which makes a point-to-point
byte pipe between the two emulated boards — a UART hub, not a
`CreateServerSocketTerminal`, precisely because the two ends are *both* emulated
UARTs (a server-socket terminal is for a host-side TCP client). ZMK's
`zmk,wired-split` transport (uart1 on both halves) then runs over it unchanged.

The one gotcha is timing, not wiring: there is **no cross-machine
execution-order guarantee at `t=0`**, so a peripheral split event emitted in the
first few ms can arrive before the central's `uart_irq_rx_enable()` runs and be
dropped. `renode_harness.boot_split_wired` connects both consoles before `start`;
`run_split_smoke` then waits for both boot banners and settles ~3 s before
injecting the keypress it asserts on.
