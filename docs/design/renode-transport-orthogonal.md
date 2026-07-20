# Design: orthogonal transports for `west zmk-renode-test`

Status: **proposed** (2026-07-19). Splits the single `--mode` axis into two
independent ones -- how the central reaches the *host* (Studio RPC), and how the
central reaches the *peripheral* (split link) -- so combinations the fixed presets
never offered become reachable. The headline new combination is **`usb` host-link
+ `wired` split-link**: a wired split whose central *can* speak Studio RPC (over
emulated USB CDC) for the first time, because USB consumes no UARTE and needs no
BLE fine-quantum.

`--mode` stays, unchanged, as a set of backward-compatible presets.

## Motivation

`--mode {ble,usb,wired-split,ble-split}` couples two decisions that are actually
independent:

* **host-link** -- the path the central uses to answer Studio RPC to a computer:
  emulated **USB** CDC, emulated **BLE** GATT, or **none** (boot-liveness only,
  no RPC).
* **split-link** -- the path between the two halves of a split keyboard:
  **none** (not a split), **wired** (UART hub), or **BLE** (radio + fake CCM).

The four presets only ever expose four of the nine `host-link x split-link`
cells. The cell the presets structurally *cannot* express is the valuable one:

> **`usb` x `wired`.** A wired split has historically had **no** Studio RPC on the
> central, because the nRF52840's two UARTEs are both consumed (console = uart0,
> split link = uart1), leaving none for a UART Studio transport -- so `--mode
> split` only proves the relay, never a Studio round trip. Route Studio over
> **USB** instead and both UARTEs stay free: the central answers Studio RPC over
> the emulated USB CDC *and* holds a wired split link to the peripheral. It is
> also faster than any BLE split (no 10 us global quantum).

A second useful new cell is **`usb` x `ble`** (a BLE split whose central speaks
Studio over USB rather than BLE) -- cheaper to reason about than `ble-split`
because only one of the two links is a radio pairing.

## CLI contract

Two new options, on `west zmk-renode-test` and `renode_smoke.py` alike:

```
--host-link  {usb,ble,none}        how the central answers Studio RPC
--split-link {none,wired,ble}      how the central reaches the peripheral
```

`--mode` is retained and expands to a `(host-link, split-link)` pair:

| `--mode`    | host-link | split-link |
|-------------|-----------|------------|
| `ble`       | `ble`     | `none`     |
| `usb`       | `usb`     | `none`     |
| `split`     | `none`    | `wired`    |
| `ble-split` | `ble`     | `ble`      |

Rules:

* `--mode` and (`--host-link` | `--split-link`) are **mutually exclusive**.
  Passing a preset *and* an axis flag is an error -- pick one vocabulary.
* The default is unchanged: no flags at all == `--mode ble` == host-link `ble`,
  split-link `none` (the real image, Studio over BLE with `--host-elf`, else
  boot-liveness).
* Existing invocations keep working verbatim: every current `--mode X` maps to
  exactly the behaviour it has today (the presets are the old code paths).

`--peripheral-elf` is required whenever `split-link != none`; `--host-elf` is
meaningful whenever `host-link == ble` (the `renode-ble-host` app) and, for a
BLE split-link, is what pairs with the central. `--host-elf` with `host-link in
{usb,none}` is an error, as today.

### The `(host-link, split-link)` support matrix

Not every cell is meaningful or buildable; this is what the harness accepts
TODAY. "preset" = reachable via `--mode`; "axis-only" = supported but reachable
only via the axis flags (no preset); "reserved" = a valid name the resolver
rejects until a smoke is added; "--" = rejected as impossible.

| host-link \ split-link | `none`            | `wired`                    | `ble`                  |
|------------------------|-------------------|----------------------------|------------------------|
| `none`                 | -- (nothing to do)| `none`x`wired` (axis-only) | reserved               |
| `usb`                  | `usb` (preset)    | `wired-split` (preset)     | reserved               |
| `ble`                  | `ble` (preset)    | reserved                   | `ble-split` (preset)   |

The set the resolver accepts is exactly the four presets plus the Studio-less
`none`x`wired` cell (`SUPPORTED_LINKS` in `renode_smoke.py`). A "reserved" cell
parses but errors with "unsupported combination ..." — the name is claimed and
the dispatch is ready, but no smoke/artifacts exist yet; add one by extending
`SUPPORTED_LINKS`, the dispatch, and a `run_*_smoke`. Every accepted cell is
wired into CI except the Studio-less `none`x`wired` (its coverage is a strict
subset of the `wired-split` preset, which asserts the same wired relay plus
Studio over USB).

`usb`x`wired` — the wired split whose central still speaks Studio over USB — is
the headline of this work; it is the `wired-split` preset.

## Which firmware artifact each cell needs

The harness never builds firmware; the caller does. Each cell dictates how the
`--elf` (central) and `--peripheral-elf` are built:

| cell              | central `--elf`                                              | `--peripheral-elf`                     | host `--elf`         |
|-------------------|-------------------------------------------------------------|----------------------------------------|----------------------|
| `usb` (preset)    | real `studio-rpc-usb-uart` image (`build-ble.yaml`)         | --                                     | --                   |
| `ble` (preset)    | same real image (`build-ble.yaml`)                          | --                                     | `renode-ble-host`    |
| `none`x`wired` (axis-only) | wired central (`build-split.yaml`, USB/BLE off)   | wired peripheral (`build-split.yaml`)  | --                   |
| `ble-split`       | split-central + studio-rpc-usb-uart (`build-ble-split.yaml`)| split-peripheral (`build-ble-split.yaml`)| `renode-ble-host`  |
| **`wired-split`** (`usb`x`wired`) | `renode_usb_wired_split` central (USB Studio + wired split, `build-usb-split.yaml`) | `renode_wired_split` peripheral (`build-usb-split.yaml`) | -- |
| `usb`x`ble` (reserved) | split-central + studio-rpc-usb-uart | ble peripheral | -- |

The new artifact for the `wired-split` preset is a **wired-split central that
keeps USB on**: the Studio-less `renode_wired_split` shield disables USB
(`CONFIG_ZMK_USB=n`) to
dodge the USBD boot hang, which is exactly what makes it lack Studio. The new
central shield (`renode_usb_wired_split_left`, working name) instead:

* keeps USB **on** and adds the `studio-rpc-usb-uart` snippet (Studio + console
  ride USB CDC) -- so it boots on the **real-image** platform
  (`xiao_nrf52840_usb.repl`, the NRF_USBD_Full model), exactly like `usb` mode;
* enables `CONFIG_ZMK_SPLIT` + `zmk,wired-split` on **uart0** (only one UARTE is
  needed now that console left the UART), cross-connected to the peripheral;
* keeps QSPI/NVS handled by the real platform's stubs (FICR/NVMC), as `usb` mode
  already does.

The peripheral half is the **existing** `renode_wired_split` peripheral artifact
from `build-split.yaml` unchanged -- it already speaks `zmk,wired-split` on
uart1 and needs no USB. The two halves' split UARTEs are wired to one Renode UART
hub, as in `split` mode.

> Wire-up caveat to validate during implementation: the peripheral drives its
> split link on **uart1** (its console is uart0); the USB central has its console
> on USB, so it is free to drive the split link on **uart0**. The UART hub is
> symmetric, so hub-connecting central.uart0 <-> peripheral.uart1 is fine as long
> as both ends agree on baud/mode. If ZMK's `zmk,wired-split` needs matching node
> labels, the central shield can put the wired-split on uart1 too and expose
> console purely over USB -- decided at implementation time against a real build.

## Platform / harness composition

Everything composes from pieces that already exist; no new Renode model is
needed.

* **New resc `usb_wired_split.resc`** = `split_wired.resc` (UART hub between two
  machines) with the *central* machine loaded from the materialized
  `xiao_nrf52840_usb.repl` (NRF_USBD_Full + the FICR/NVMC/QSPI stubs) instead of
  the plain repl, and its NVS storage preloaded 0xFF (as `boot_single_real`
  does). The peripheral machine stays on the plain `xiao_nrf52840.repl`. The
  central's split UARTE and the peripheral's split UARTE both `connector Connect`
  to the hub.
* **New harness fn `boot_usb_wired_split(...)`** = the intersection of
  `boot_split_wired` (hub, two consoles) and `boot_single_real` (per-machine
  materialized real repl + 0xFF NVS preload for the central). Returns
  `(session, central_console_or_none, peripheral_console)`; the central console
  is the USB one, attached separately.
* **Studio RPC** reuses `attach_dual_cdc_bridge(session, ...)` verbatim against
  the central machine after `go()` + USB-init settle -- identical to
  `run_usb_smoke`.
* **The wired relay assertion** reuses `run_split_smoke`'s injection: pulse the
  peripheral's first kscan GPIO and wait for the central to log `position: 0`.

So `run_usb_wired_smoke` = `run_usb_smoke` (attach bridge, GetDeviceInfo over the
Studio CDC, boot banner on the console CDC when present) **and**
`run_split_smoke`'s relay half (peripheral keypress -> central `position: 0`),
against one two-machine session. Both halves must pass. That is a strictly
stronger proof than either preset alone: the central simultaneously answers
Studio *and* services the wired split.

## Module-test env contract

Module `tests/renode/*_test.py` files today read `ZMK_RENODE_MODE` +
`ZMK_RENODE_ELF` (+ `_PERIPHERAL_ELF` / `_HOST_ELF` / `_STORAGE_*`). Extend, do
not break:

* Always export the two axes:
  * `ZMK_RENODE_HOST_LINK` = `usb` | `ble` | `none`
  * `ZMK_RENODE_SPLIT_LINK` = `none` | `wired` | `ble`
* Keep exporting `ZMK_RENODE_MODE` for backward compatibility: set it to the
  preset name when the `(host,split)` pair is a preset (so `usb`x`wired` reports
  `wired-split`), else to the canonical `"<host>+<split>"` string (e.g.
  `none+wired`). Existing consumers that only understand the four preset values
  keep working for preset combos; new consumers read the two axis vars.
* `ZMK_RENODE_ELF` / `_PERIPHERAL_ELF` / `_HOST_ELF` / `_STORAGE_ADDR` /
  `_STORAGE_SIZE` are unchanged; `_PERIPHERAL_ELF` is exported whenever
  split-link != none, `_HOST_ELF` whenever a host was given.

The action (`action.yml`) gains `host-link` / `split-link` inputs mirroring the
CLI, `mode` retained; the same mutual-exclusion rule applies in the shim.

## CI

Keep the matrix small, not the nine-cell cross product:

1. `ble` + `usb` (one job boots the single real image and runs both the ble and
   usb Studio smokes against it)
2. `wired-split` (`--mode wired-split` == `usb`x`wired`: build the USB-Studio
   wired-split central + the wired peripheral, assert Studio over USB **and** the
   `position: 0` relay). This subsumes the old Studio-less `split` job -- its
   relay coverage is a strict subset -- so there is no separate `none`x`wired`
   job.
3. `ble-split` (below).

`ble-split` stays as its own (heaviest, flakiest) job as today. The remaining
NEW cells (`usb`x`ble`, `ble`+`wired`, `none`+`ble`) are documented as reachable
but are not added to CI, to hold the line on runtime; a follow-up can promote any
of them behind a manual/`workflow_dispatch` trigger.

## Relationship to the ZMK wired-relay feature

The `custom-studio-protocol` fork's **split relay event** (`ZMK_SPLIT_RELAY_EVENT`)
is today implemented only over BLE: peripheral->central rides the generic
`zmk_split_transport_peripheral_event` path (transport-agnostic, already works
over wired), but central->peripheral (`zmk_split_central_send_relay_event`) is
defined only in `app/src/split/bluetooth/central.c` with a dedicated chunked GATT
characteristic. A separate, parallel change ports the central->peripheral relay
to the **wired** transport so the fork's relay works on a wired split too. That
firmware change is orthogonal to this test-infra change but is what makes a
`usb`x`wired` central a fully-featured wired split (Studio + bidirectional
relay); tracked and PR'd against `cormoran/zmk` `main+custom-studio-protocol`.

## Phasing

0. This doc + the CLI/env naming (present to cormoran).
1. Pure-Python orthogonalization: parse `--host-link/--split-link`, preset
   expansion, mutual exclusion, dispatch table over `(host,split)`, env export.
   All existing modes keep their exact code paths (regression: the 5 presets
   still dispatch identically). No new Renode behaviour yet.
2. `usb`x`wired`: new shield + `build-usb-split.yaml`, `usb_wired_split.resc`,
   `boot_usb_wired_split`, `run_usb_wired_smoke`. Gate: local Renode green
   (Studio GetDeviceInfo over USB CDC + peripheral `position: 0` relay).
3. CI job #5 + action `host-link`/`split-link` inputs + README/testing/internals
   doc updates.
4. (Parallel, separate PR) ZMK wired relay C->P.
