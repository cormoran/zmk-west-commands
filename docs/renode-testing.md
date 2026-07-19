# Renode testing in depth

This page covers `west zmk-renode-test` beyond the [README](../README.md)
quickstart: the advanced flags, the `ZMK_RENODE_*` module-test env contract,
observing a real image over SEGGER RTT, ble-mode performance, troubleshooting,
and limitations. For how a real hardware image boots under emulation at all
(platform stubs, fake CCM, on-air constraints) see
[renode-internals.md](renode-internals.md).

`west zmk-renode-test` has **two modes** (`--mode`, default `uart`); `--elf` is
the DUT in both. The generic smoke (boot + Studio) is what
`.github/actions/zmk-renode-test/action.yml` always runs before any
module-specific test. A module's own `tests/renode/*_test.py` run afterwards and
`import renode_harness` directly for anything more specific.

## Command reference

```
usage: west zmk-renode-test [-h] --elf ELF [--mode {uart,ble}] [--host-elf HOST_ELF]
                            [--no-rpc] [--boot-timeout BOOT_TIMEOUT] [--skip-smoke]
                            [--rtt] [--min-virtual MIN_VIRTUAL]
                            [--virtual-budget VIRTUAL_BUDGET] [--steady-quantum Q]
                            [--storage-addr ADDR] [--storage-size SIZE]
                            [--renode-version VER]
                            [tests_dir]
```

Common:

| Flag | Applies to | Meaning |
|---|---|---|
| `--elf` (required) | both | the DUT firmware ELF (built by the caller). |
| `--mode {uart,ble}` | — | `uart` (default) or `ble`. |
| `--host-elf` | ble | the `renode-ble-host` app ELF. Given → full S4/S5 smoke; omitted → boot-liveness only. |
| `--no-rpc` | uart | check only the boot banner (modules without Studio RPC). |
| `--boot-timeout` | uart | seconds to wait for the ZMK boot banner (default 20). |
| `--skip-smoke` | both | skip the smoke test; run only `tests_dir`. |

Advanced (rarely needed):

| Flag | Applies to | Meaning |
|---|---|---|
| `--rtt` | ble (liveness) | capture Zephyr SEGGER RTT log output during the run and fail on RTT fatal lines. See [Observing a real image over SEGGER RTT](#observing-a-real-image-over-segger-rtt). |
| `--min-virtual` | ble (liveness) | virtual seconds to run before PC sampling (default 20). |
| `--virtual-budget` | ble (with host) | virtual seconds to reach the encrypted read before failing (default 20; ~3.3 s is typical). |
| `--steady-quantum` | ble (with host) | after S4, raise the global time-sync quantum (e.g. `0.001`) for the steady-state phase. See [ble-mode performance](#ble-mode-performance). |
| `--storage-addr` / `--storage-size` | ble | NVS `storage_partition` address/size preloaded as erased `0xFF` (default `0xec000`/`0x8000`, xiao_ble). |
| `--renode-version` | both | Renode portable release to install/use (default `1.16.1`; must match the checked-in `.repl`). |

## uart mode: building a Renode-testable ELF

uart mode boots an ELF built with this repo's `renode-studio-uart` snippet (this
repo is also a Zephyr module — the snippet and a Renode-only Studio RPC UART
transport are both inert unless the snippet is used; see
`renode-test-module/Kconfig`). See the [README](../README.md#uart-mode-default)
for the `build.yaml` artifact and the `CONFIG_ZMK_STUDIO` physical-layout
requirement.

> **Newer-ZMK board note.** The HWv2 `xiao_ble//zmk` board (and the node labels
> the `renode-studio-uart` overlay disables) only exist on newer ZMK; this
> repo's own CI pins ZMK `main` for the Renode job
> (`scripts/west-test-renode.yml`) while the rest of the tests stay on
> `v0.3-branch`.

## ble mode: what it proves

ble mode boots **two real ARM images** on one emulated Renode BLE medium — the
unmodified ZMK DUT (advertiser) and the [`renode-ble-host`](../renode-ble-host/)
app (the simulated computer) — and asserts the host reaches
`STAGE:S4-SECURITY-CHANGED OK` (encrypted link up) and `STAGE:S5-GATT-READ OK`
(encrypted read of the ZMK Studio RPC characteristic). **What it proves:** LE SC
Just Works pairing and an encrypted GATT read run end-to-end on the real
firmware — the same code paths as a hardware Studio-over-BLE session — with
**zero firmware-side deviation** on the DUT. It is a **functional** check, not a
cryptographic one (see the fake-CCM disclaimer in
[renode-internals.md](renode-internals.md#fake-ccm--not-cryptographically-real)).

The smoke passes as soon as S4+S5 appear; `--virtual-budget` (default 20 virtual
seconds) caps how long it waits, and a wall-clock safety net stops a wedged run.
A run reaches the encrypted read (S5) at ~3.3 s virtual and ~**35–50 s wall**
(≈0.10× realtime on a lightly-loaded host).

**Without `--host-elf`**, ble mode degrades to a **boot-liveness check**: it
boots just the real DUT image (no host, no BLE peer) and proves it is still
running — not parked in a Zephyr fatal — after `--min-virtual` virtual seconds
(default 20). It runs the emulation to that threshold, then samples the CPU `PC`
a few times and resolves each symbol. It **fails** if any sample lands in a
fatal frame (`arch_system_halt` / `z_fatal_error` / `k_sys_fatal_error_handler`
— a Zephyr fatal parks the CPU spinning in `arch_system_halt`) and **passes**
otherwise; if the image happens to have a console (observation builds), its
output is captured and also checked for `FATAL ERROR` / `Halting system`, but
console output is not required.

## Module-test env contract

After the smoke test, every `*_test.py` directly under `tests_dir` runs as
`python3 <file> -v` with the harness on `PYTHONPATH` and this environment set:

| Variable | When | Meaning |
|---|---|---|
| `ZMK_RENODE_MODE` | always | `uart` or `ble` — which harness the test should build. |
| `ZMK_RENODE_ELF` | always | absolute path to the DUT ELF. |
| `ZMK_RENODE_STORAGE_ADDR` / `ZMK_RENODE_STORAGE_SIZE` | ble | NVS `storage_partition` overrides (hex), for `boot_single_real` / `boot_ble_pair`. |
| `ZMK_RENODE_HOST_ELF` | ble, with `--host-elf` | absolute path to the `renode-ble-host` app ELF, for `boot_ble_pair`. |

A `uart`-mode test builds a single UART-RPC machine via
`renode_harness.boot_single(...)`. A `ble`-mode test builds a real machine via
`renode_harness.boot_single_real(...)` (liveness) or a two-machine pair via
`renode_harness.boot_ble_pair(dut_elf, host_elf)`.

## Observing a real image over SEGGER RTT

PC-symbol sampling proves the image is *alive*, but a silent real image gives no
log output. The **recommended observation path** for ble-mode liveness is to
build with Zephyr's SEGGER RTT log backend and pass `--rtt`:

```bash
$ west zmk-renode-test --mode ble --rtt --elf build/zephyr/zmk.elf
```

An RTT-logging build is still **real-hardware-flashable** — it is Kconfig-only,
no firmware source changes:

```
CONFIG_LOG=y
CONFIG_USE_SEGGER_RTT=y
CONFIG_LOG_BACKEND_RTT=y
```

`--rtt` hooks `SEGGER_RTT_WriteSkipNoLock` (the function Zephyr's
`log_backend_rtt` actually calls — Renode's stock `segger-rtt.py` hooks
`SEGGER_RTT_WriteNoLock`, which Zephyr never calls, so it captures nothing; see
`scripts/lib/renode/segger_rtt_writeskip.py`). The captured log is printed and
also scanned for the same `FATAL ERROR` / `Halting system` markers, so a real
image's boot banner (`Welcome to ZMK!`) and BT identity line become visible. On
a non-RTT build the hook install is a graceful no-op (the capture is just empty).

## ble-mode performance

ble mode's wall cost is dominated by the **10 µs two-machine time-sync quantum**,
not by the Python peripheral stubs (fake CCM / FICR / NVMC / QSPI / USBD). The
proof: stripping the fake-CCM per-transform debug-string build changes nothing
(0.099× vs 0.100×), yet *coarsening the quantum after pairing* — which does not
reduce the number of CCM transforms per virtual second — recovers up to ~7×.
Measurements (Renode 1.16.1, one lightly-loaded x86-64 host; median of ≥2 runs;
all still **PASS** S4+S5):

| Configuration | Wall to S5 | virtual/wall ratio | PASS |
|---|---|---|---|
| Default UART smoke (single machine, reference) | ~14 s | — | ✅ |
| ble-mode liveness (single machine, reference) | ~85 s / 21 s vt | ~0.25× | ✅ |
| **ble baseline** (10 µs quantum, parallel CPUs) | ~33–48 s | **0.10×** | ✅ |
| ble + fake-CCM debug string removed | ~34 s | 0.099× | ✅ (no change) |
| ble + `SetGlobalSerialExecution true` | ~43 s | 0.055× | ✅ (slower) |
| ble + `PerformanceInMips 100` | ~33 s | 0.098× | ✅ (no change; ≈ default) |
| ble + `PerformanceInMips 1` | — | — | ❌ (too slow to pair) |
| Quantum `0.00003` / `0.0001` from boot | — | — | ❌ (never advertises) |
| **Steady phase** after S4, quantum `0.0001` (10×) | — | **≥0.35×** | ✅ (link survives) |
| **Steady phase** after S4, quantum `0.001` (100×) | — | **≥0.70×** | ✅ (link survives, ~7×) |

**Root cause.** Two nRF52840 CPUs re-synchronising every 10 µs of virtual time
run at ~0.10× realtime; a single machine (no BLE medium, default quantum) runs
at ~0.25×. The fine quantum is *load-bearing through connection + pairing* (the
soft link-layer's radio-event prepare runs late and asserts otherwise), but once
the encrypted link is up (host `STAGE:S4`) the link-layer tolerates a
100×-coarser quantum: the connection stays up with no disconnect / LL assert and
an encrypted GATT read (S5) still completes.

**`--steady-quantum` (fine-then-coarse).** For a module's own *long* BLE test
(many virtual seconds of steady RPC traffic), pass e.g. `--steady-quantum 0.001`:
the harness raises the global quantum the moment the encrypted link comes up,
running the steady-state phase ~7× faster. Equivalently, a module test using
`renode_harness.boot_ble_pair(...)` directly calls
`renode_harness.raise_global_quantum(session, "0.001")` after it observes
`STAGE:S4`. The `--host-elf` smoke itself exits at S5 (a tiny virtual gap after
S4), so it gains almost nothing from the flag — it mostly *validates* the
schedule; the win is for post-pairing workloads.

**What did not help** (all measured, all still correct): removing the fake-CCM
debug logging (Python stubs are cold, <0.1× effect); `SetGlobalSerialExecution`
(parallel CPU execution is the faster default); raising `PerformanceInMips`
(already effectively saturated). Lowering MIPS or coarsening the *boot* quantum
breaks pairing outright.

**Relationship to `west zmk-ble-test`.** That command runs BabbleSim
(`nrf52_bsim`) BLE tests — protocol-accurate POSIX binaries, fast, driving a
full Studio request/response script via [`ble-studio-host`](../ble-studio-host/).
ble mode here instead runs the **real ARM binary** under Renode and only proves
the encrypted-link Studio read reaches the DUT. They are complementary.

## Troubleshooting

| Symptom (on stderr / in the smoke output) | Likely cause / fix |
|---|---|
| uart: `never saw ZMK boot banner on console UART` | ELF not built with the `renode-studio-uart` snippet, wrong board (needs an nRF52840 with the checked-in `.repl`), or the `CONFIG_ZMK_STUDIO` physical-layout build-assert changed the image. |
| uart: `no Studio RPC response frame received (timeout)` | module doesn't enable `CONFIG_ZMK_STUDIO` — pass `--no-rpc` — or the protos/`protoc` aren't installed (see the README Requirements). |
| ble liveness: `CPU parked in a fatal frame -- image faulted` | Zephyr fatal (often FICR/NVS). For a non-xiao_ble board, set `--storage-addr`/`--storage-size` to that board's `storage_partition`. Use `--rtt` to see the real fatal reason. |
| ble liveness: `only reached Ns virtual ... emulation stalled?` | image spin-hung (e.g. NVMC poll) or the host is very slow — raise the implicit wall budget by lowering `--min-virtual`, or investigate with `--rtt`. |
| ble host: `virtual-time budget exhausted ... before the encrypted read` | pairing never completed — check the printed DUT/host console tails; usually a DLE / quantum regression (see [renode-internals.md](renode-internals.md#the-two-on-air-constraints-both-load-bearing)). |
| ble host: nonzero `radio 'trimming' warnings` | an on-air PDU exceeded `27+4` bytes — the host-side `CONFIG_BT_CTLR_DATA_LENGTH_MAX=27` cap or a CCM offset regressed (internals). |
| ble host: `security_changed err=9`, 30 s SMP timeout | fake-CCM RX transform regressed to lazy (internals). |

## Limitations

- **uart mode** cannot exercise USB-CDC or BLE transports — Studio RPC is
  re-bound to a UART, so it is a functional check of the RPC subsystem and boot,
  not of the real hardware transport.
- **ble mode is functional, not cryptographic** — the shared fake CCM is an
  identity transform (see [renode-internals.md](renode-internals.md)).
- **Studio-over-USB is not reachable** — the USB transport is stubbed to idle
  (unplugged cable), so there is no USB Studio round trip in ble mode.
- **Only LE SC Just Works** is exercised; identity addresses come from the FICR
  model (static-random, `device_addr_for_machine(n)`); the radio's 31-byte
  payload cap forces the host-side DLE cap. Steady-state test-time is addressed
  by `--steady-quantum`; the pairing phase's 10 µs quantum is a hard floor for
  the current soft link-layer.
