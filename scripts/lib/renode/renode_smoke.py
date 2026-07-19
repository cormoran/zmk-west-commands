#!/usr/bin/env python3
"""Generic Renode smoke test for any ZMK module's built ELF.

Given a firmware ELF built with the Renode Studio-RPC-over-UART overlay +
transport (see build_fw.py's generic mode / references/renode-notes.md),
boot it under Renode using platforms/single.resc and assert:

  1. The real ZMK boot banner appears on the console UART ("proves" the
     platform description, ELF load, and CPU execution all work).
  2. A core Studio RPC GetDeviceInfo request round-trips a well-formed
     Response with a non-empty device name.

This is what `.github/actions/zmk-renode-test/action.yml` always runs,
regardless of which module it's testing -- it's the "does this thing even
boot and speak Studio RPC" gate before any module-specific test runs. A
module's own tests (e.g. this template's tests/renode/test_renode.py)
import renode_harness directly for anything more specific (their own custom
RPC subsystem, etc.).

Usage:
    python renode_smoke.py --elf /path/to/zmk.elf \\
        --studio-proto-dir /path/to/zmk-studio-messages/proto/zmk

    # or let it auto-discover the proto dir under a west topdir:
    python renode_smoke.py --elf /path/to/zmk.elf --west-topdir /path/to/module

Exits non-zero (with a message on stderr) on any failure.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(SCRIPTS_DIR))
import renode_harness  # noqa: E402

# A Zephyr fatal error parks the CPU spinning in arch_system_halt (observed
# for the vt~10s BT_ASSERT oops when FICR/NVS is wrong); z_fatal_error /
# k_sys_fatal_error_handler are the frames just before it. If a PC sample lands
# on any of these, the image has faulted -- fail liveness.
FATAL_SYMBOLS = ("arch_system_halt", "z_fatal_error", "k_sys_fatal_error_handler")
# Console markers (observation builds only) that also mean a fatal.
FATAL_CONSOLE_MARKERS = ("FATAL ERROR", "Halting system")


def _parse_virtual_seconds(text: str) -> float | None:
    """Pull an 'Elapsed Virtual Time: HH:MM:SS.ffffff' value (in seconds) out
    of `machine GetTimeSourceInfo` output; returns None if absent."""
    m = re.search(r"Elapsed Virtual Time:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _clean_symbol(find_symbol_output: str) -> str:
    """Reduce a `sysbus FindSymbolAt <addr>` monitor reply to just the symbol
    name, stripping the echoed command, ANSI colour codes and the prompt. Returns
    '' when the address has no symbol (a bare/tag address resolves to nothing)."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", find_symbol_output)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("sysbus FindSymbolAt") or line.startswith("("):
            continue
        return line
    return ""


def run_smoke(
    elf: Path,
    renode_path: str,
    studio_proto_dir: Path | None = None,
    check_rpc: bool = True,
    expect_name_nonempty: bool = True,
    boot_timeout: float = 15.0,
    rpc_timeout: float = 10.0,
) -> None:
    """Boot `elf` under Renode and assert the ZMK boot banner appears; unless
    `check_rpc` is False, also assert a core Studio RPC GetDeviceInfo round
    trip (which requires `studio_proto_dir`)."""
    if check_rpc:
        if studio_proto_dir is None:
            raise ValueError("studio_proto_dir is required unless check_rpc is False")
        studio_pb2 = renode_harness.load_studio_pb2(studio_proto_dir)

    session, console, rpc = renode_harness.boot_single(renode_path, elf)
    try:
        print("waiting for ZMK boot banner...", file=sys.stderr)
        banner = renode_harness.wait_for_text(
            console._sock, "Welcome to ZMK", timeout=boot_timeout
        )
        if "Welcome to ZMK" not in banner:
            raise AssertionError(f"never saw ZMK boot banner on console UART; got:\n{banner}")
        print("boot banner OK", file=sys.stderr)

        if not check_rpc:
            print("skipping Studio RPC check (--no-rpc)", file=sys.stderr)
            return

        req = studio_pb2.Request()
        req.request_id = 1
        req.core.get_device_info = True
        rpc.send(req.SerializeToString())
        resp_bytes = rpc.read_frame(timeout=rpc_timeout)
        if resp_bytes is None:
            raise AssertionError("no Studio RPC response frame received (timeout)")

        resp = studio_pb2.Response()
        resp.ParseFromString(resp_bytes)
        if resp.WhichOneof("type") != "request_response":
            raise AssertionError(f"expected a request_response, got {resp.WhichOneof('type')!r}")
        if resp.request_response.WhichOneof("subsystem") != "core":
            raise AssertionError(
                "expected core subsystem in response, got "
                f"{resp.request_response.WhichOneof('subsystem')!r}"
            )
        name = resp.request_response.core.get_device_info.name
        if expect_name_nonempty and not name:
            raise AssertionError("GetDeviceInfoResponse.name was empty")
        print(f"core Studio RPC GetDeviceInfo OK (name={name!r})", file=sys.stderr)
    finally:
        rpc.close()
        console.close()
        session.stop()


def run_liveness_smoke(
    elf: Path,
    renode_path: str,
    min_virtual: float = 20.0,
    sample_count: int = 5,
    storage_addr: int = renode_harness.STORAGE_ADDR_DEFAULT,
    storage_size: int = renode_harness.STORAGE_SIZE_DEFAULT,
    boot_wait: float = 3.0,
    wall_budget: float | None = None,
    rtt: bool = False,
    device_addr: int | None = None,
) -> None:
    """Real-binary liveness smoke: boot a real flashable image (no UART Studio
    transport) and prove it is still running -- not parked in a Zephyr fatal --
    after `min_virtual` virtual seconds.

    Runs the emulation until virtual time reaches `min_virtual`, then samples
    `sysbus.cpu PC` `sample_count` times over a few more virtual seconds and
    resolves each via the monitor. FAILS if any sample lands in a fatal frame
    (see FATAL_SYMBOLS) or if console/RTT output shows a fatal marker; PASSES
    otherwise. Sampled symbols and any console/RTT output are printed for
    diagnosis. Does not require console output.

    `rtt=True` additionally captures Zephyr's SEGGER RTT log output during the
    run (the recommended observation path for real-binary mode -- an RTT build
    is real-hardware-flashable, Kconfig-only: CONFIG_LOG + CONFIG_USE_SEGGER_RTT
    + CONFIG_LOG_BACKEND_RTT). RTT output is printed and also scanned for the
    same fatal markers. `device_addr` overrides the FICR BLE identity.
    """
    if wall_budget is None:
        # nRF52840 under Renode runs a few x faster than real time; give ample
        # margin over min_virtual so a slow host still reaches the threshold.
        wall_budget = max(60.0, min_virtual * 4 + 30.0)

    session, console, rpc = renode_harness.boot_single_real(
        renode_path,
        elf,
        storage_addr=storage_addr,
        storage_size=storage_size,
        boot_wait=boot_wait,
        rtt=rtt,
        device_addr=device_addr,
    )
    assert session.mon is not None
    mon = session.mon
    rtt_sock = getattr(session, "rtt_socket", None)
    console_buf = ""
    rtt_buf = ""
    try:
        # 1. Let it run until it has clocked `min_virtual` virtual seconds.
        print(f"running to >= {min_virtual:.0f}s virtual time...", file=sys.stderr)
        deadline = time.monotonic() + wall_budget
        vt = 0.0
        while time.monotonic() < deadline:
            console_buf += renode_harness.drain_text(console._sock, timeout=1.0)
            if rtt_sock is not None:
                rtt_buf += renode_harness.drain_text(rtt_sock._sock, timeout=0.2)
            vt = _parse_virtual_seconds(mon.execute("machine GetTimeSourceInfo", settle=0.3)) or vt
            if vt >= min_virtual:
                break
        print(f"virtual time reached {vt:.2f}s", file=sys.stderr)
        if vt < min_virtual:
            raise AssertionError(
                f"only reached {vt:.2f}s virtual in {wall_budget:.0f}s wall time "
                f"(wanted >= {min_virtual:.0f}s) -- emulation stalled?"
            )

        # 2. Sample the PC a few times over a couple more virtual seconds.
        samples: list[tuple[str, str]] = []
        for _ in range(sample_count):
            pc_reply = mon.execute("sysbus.cpu PC", settle=0.3)
            m = re.search(r"0x[0-9A-Fa-f]+", pc_reply)
            pc = m.group(0) if m else pc_reply.strip()
            sym = _clean_symbol(mon.execute(f"sysbus FindSymbolAt {pc}", settle=0.3))
            samples.append((pc, sym))
            console_buf += renode_harness.drain_text(console._sock, timeout=0.5)
            if rtt_sock is not None:
                rtt_buf += renode_harness.drain_text(rtt_sock._sock, timeout=0.2)

        for pc, sym in samples:
            print(f"  PC {pc} -> {sym or '<no symbol>'}", file=sys.stderr)
        if console_buf.strip():
            print("--- console output ---", file=sys.stderr)
            print(console_buf, file=sys.stderr)
        if rtt_buf.strip():
            print("--- RTT output ---", file=sys.stderr)
            print(rtt_buf, file=sys.stderr)

        # 3. Verdict.
        halted = [(pc, sym) for pc, sym in samples if sym in FATAL_SYMBOLS]
        if halted:
            raise AssertionError(
                "CPU parked in a fatal frame -- image faulted: "
                + ", ".join(f"{pc}={sym}" for pc, sym in halted)
            )
        marker = next((m for m in FATAL_CONSOLE_MARKERS if m in console_buf or m in rtt_buf), None)
        if marker:
            raise AssertionError(f"console/RTT reported a fatal error ({marker!r})")
        print("liveness OK (CPU running, no fatal frame)", file=sys.stderr)
    finally:
        if rtt_sock is not None:
            rtt_sock.close()
        rpc.close()
        console.close()
        session.stop()


# Host-console STAGE markers emitted by the renode-ble-host app.
BLE_SECURITY_OK = "STAGE:S4-SECURITY-CHANGED OK"
BLE_GATT_READ_OK = "STAGE:S5-GATT-READ OK"
BLE_FAIL_MARKERS = (
    "STAGE:S5-GATT-READ FAIL",
    "STAGE:S3-SECURITY-CHANGED FAIL",
)


def run_ble_smoke(
    dut_elf: Path,
    host_elf: Path,
    renode_path: str,
    virtual_budget: float = 20.0,
    wall_budget: float = 780.0,
    storage_addr: int = renode_harness.STORAGE_ADDR_DEFAULT,
    storage_size: int = renode_harness.STORAGE_SIZE_DEFAULT,
    steady_quantum: str | None = None,
) -> None:
    """Studio-over-BLE smoke: boot a real ZMK DUT and the renode-ble-host app on
    one Renode BLE medium (fake CCM in both machines), then assert the host
    reaches an encrypted GATT read of the ZMK Studio RPC characteristic.

    PASSES when the host console shows both `STAGE:S4-SECURITY-CHANGED OK`
    (encrypted link up) and `STAGE:S5-GATT-READ OK` (encrypted read) before the
    DUT clocks `virtual_budget` virtual seconds (default 20s -- generous vs the
    ~3.3s observed). FAILS on any host FAIL marker, on the virtual-time budget,
    or on the `wall_budget` wall-clock safety net. On failure the tails of both
    consoles are printed. NOT a cryptographic assertion -- the CCM is a shared
    identity transform (see README.md's Studio-over-BLE section).

    `steady_quantum` (e.g. "0.001") enables the validated "fine-then-coarse"
    schedule: as soon as the encrypted link is up (host STAGE:S4) the global
    time-sync quantum is raised from the load-bearing 10us boot value to this
    coarser value. Pairing needs 10us, but the steady encrypted link tolerates a
    100x-coarser quantum (S5 still passes; no disconnect / LL assert), which runs
    the post-pairing phase ~7x faster -- the main lever for a module's own
    long-running BLE test (the smoke itself exits at S5, so it mostly *validates*
    the schedule rather than getting faster). None (default) keeps 10us throughout.
    See renode_harness.raise_global_quantum and README's BLE performance section.
    """
    import tempfile

    log_fd, log_path = tempfile.mkstemp(prefix="zmk-ble-renode-", suffix=".log")
    os.close(log_fd)

    print(
        f"booting DUT + renode-ble-host on one BLE medium "
        f"(virtual budget {virtual_budget:.0f}s, wall safety {wall_budget:.0f}s)...",
        file=sys.stderr,
    )
    session, dut_console, dut_rpc, host_console = renode_harness.boot_ble_pair(
        renode_path,
        dut_elf=dut_elf,
        host_elf=host_elf,
        storage_addr=storage_addr,
        storage_size=storage_size,
        renode_log=Path(log_path),
    )
    assert session.mon is not None
    mon = session.mon
    dut_buf = ""
    host_buf = ""
    reason = None
    steady_raised = False
    try:
        deadline = time.monotonic() + wall_budget
        vt = 0.0
        while time.monotonic() < deadline:
            host_buf += renode_harness.drain_text(host_console._sock, timeout=0.5)
            dut_buf += renode_harness.drain_text(dut_console._sock, timeout=0.5)

            # Fine-then-coarse: once the encrypted link is up (S4), raise the
            # global quantum so the steady-state phase runs coarser/faster. The
            # 10us boot quantum is only needed through connection + pairing.
            if steady_quantum and not steady_raised and BLE_SECURITY_OK in host_buf:
                renode_harness.raise_global_quantum(session, steady_quantum)
                steady_raised = True
                print(
                    f"raised global quantum to {steady_quantum} after encrypted link up",
                    file=sys.stderr,
                )

            if BLE_GATT_READ_OK in host_buf and BLE_SECURITY_OK in host_buf:
                break
            bad = next((m for m in BLE_FAIL_MARKERS if m in host_buf), None)
            if bad:
                reason = f"host reported a failure marker ({bad!r})"
                break

            mon.execute('mach set "dut"', settle=0.2)
            vt = _parse_virtual_seconds(mon.execute("machine GetTimeSourceInfo", settle=0.3)) or vt
            if vt >= virtual_budget:
                reason = (
                    f"virtual-time budget exhausted ({vt:.1f}s >= {virtual_budget:.0f}s) "
                    "before the encrypted read"
                )
                break
        else:
            reason = f"wall-clock safety budget exhausted ({wall_budget:.0f}s)"

        # Final drain so late markers/log lines are captured.
        host_buf += renode_harness.drain_text(host_console._sock, timeout=1.0)
        dut_buf += renode_harness.drain_text(dut_console._sock, timeout=0.5)

        renode_log = ""
        try:
            renode_log = Path(log_path).read_text(errors="replace")
        except OSError:
            pass
        trimming = renode_log.count("trimming")

        stages = [ln.strip() for ln in host_buf.splitlines() if "STAGE:" in ln]
        if reason is None and BLE_GATT_READ_OK in host_buf and BLE_SECURITY_OK in host_buf:
            for ln in stages:
                print(f"  host| {ln}", file=sys.stderr)
            print(
                f"BLE smoke OK (encrypted Studio RPC read reached at vt~{vt:.1f}s; "
                f"radio 'trimming' warnings: {trimming})",
                file=sys.stderr,
            )
            if trimming:
                print(
                    f"WARNING: {trimming} radio 'trimming' line(s) in the Renode log "
                    "(expected 0 for a clean fake-CCM run)",
                    file=sys.stderr,
                )
            return

        # Failure: dump both consoles' tails for diagnosis.
        print("--- host console STAGE markers ---", file=sys.stderr)
        for ln in stages:
            print(f"  host| {ln}", file=sys.stderr)
        print("--- host console tail ---", file=sys.stderr)
        print("\n".join(host_buf.splitlines()[-25:]), file=sys.stderr)
        print("--- DUT console tail ---", file=sys.stderr)
        print("\n".join(dut_buf.splitlines()[-25:]), file=sys.stderr)
        if trimming:
            print(
                f"(also: {trimming} radio 'trimming' line(s) in the Renode log)",
                file=sys.stderr,
            )
        raise AssertionError(reason or "encrypted Studio RPC read not reached")
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass
        host_console.close()
        dut_rpc.close()
        dut_console.close()
        session.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--elf", required=True, type=Path)
    ap.add_argument(
        "--studio-proto-dir",
        type=Path,
        help="path to zmk-studio-messages' proto/zmk dir (auto-discovered from --west-topdir if omitted)",
    )
    ap.add_argument("--west-topdir", type=Path, help="used to auto-discover --studio-proto-dir")
    ap.add_argument("--renode-version", default=renode_harness.RENODE_VERSION_DEFAULT)
    ap.add_argument("--boot-timeout", type=float, default=15.0)
    ap.add_argument("--rpc-timeout", type=float, default=10.0)
    ap.add_argument(
        "--no-rpc",
        action="store_true",
        help="check only the boot banner (for modules that do not enable Studio RPC)",
    )
    ap.add_argument(
        "--real-binary",
        action="store_true",
        help="real-binary liveness smoke: boot a real flashable USB/BLE/QSPI image "
        "(no UART RPC) and check it is not parked in a Zephyr fatal after --min-virtual "
        "seconds, via PC-symbol sampling. Ignores --no-rpc/--studio-proto-dir.",
    )
    ap.add_argument(
        "--min-virtual",
        type=float,
        default=20.0,
        help="real-binary mode: virtual seconds to run before sampling (default: 20).",
    )
    ap.add_argument(
        "--rtt",
        action="store_true",
        help="real-binary mode: capture Zephyr SEGGER RTT log output during the "
        "liveness run (RTT-logging builds); printed and scanned for fatal markers.",
    )
    ap.add_argument(
        "--ble",
        action="store_true",
        help="Studio-over-BLE smoke: boot the real DUT (--elf) and the "
        "renode-ble-host app (--host-elf) on one BLE medium and assert an "
        "encrypted Studio RPC read (S4+S5). Runs ~6-7 min wall. See README.",
    )
    ap.add_argument(
        "--host-elf",
        type=Path,
        help="BLE mode: the renode-ble-host app ELF (built with `west build -b "
        "nrf52840dk/nrf52840 -s <this repo>/renode-ble-host`).",
    )
    ap.add_argument(
        "--ble-virtual-budget",
        type=float,
        default=20.0,
        help="BLE mode: virtual seconds to reach the encrypted read before "
        "failing (default: 20; ~3.3s is typical).",
    )
    ap.add_argument(
        "--ble-steady-quantum",
        default=None,
        help="BLE mode: after the encrypted link is up (S4), raise the global "
        "time-sync quantum to this value (e.g. 0.001) for the steady-state phase. "
        "Pairing needs the 10us boot quantum, but the encrypted link tolerates a "
        "100x-coarser quantum and runs ~7x faster -- use for long BLE tests. "
        "Default: keep 10us throughout. See README's BLE performance section.",
    )
    ap.add_argument(
        "--storage-addr",
        type=lambda s: int(s, 0),
        default=renode_harness.STORAGE_ADDR_DEFAULT,
        help="real-binary/BLE mode: NVS storage_partition address to preload as erased "
        "0xFF (default: 0xec000, xiao_ble).",
    )
    ap.add_argument(
        "--storage-size",
        type=lambda s: int(s, 0),
        default=renode_harness.STORAGE_SIZE_DEFAULT,
        help="real-binary/BLE mode: NVS storage_partition size (default: 0x8000, xiao_ble).",
    )
    args = ap.parse_args(argv)

    if not args.elf.is_file():
        print(f"ELF not found: {args.elf}", file=sys.stderr)
        return 2

    renode_path = renode_harness.find_or_install_renode(version=args.renode_version)
    if renode_path is None:
        print("Renode is not installed and could not be auto-installed", file=sys.stderr)
        return 2

    if args.ble:
        if args.host_elf is None or not args.host_elf.is_file():
            print("BLE mode requires --host-elf <renode-ble-host ELF>", file=sys.stderr)
            return 2
        try:
            run_ble_smoke(
                dut_elf=args.elf,
                host_elf=args.host_elf,
                renode_path=renode_path,
                virtual_budget=args.ble_virtual_budget,
                storage_addr=args.storage_addr,
                storage_size=args.storage_size,
                steady_quantum=args.ble_steady_quantum,
            )
        except AssertionError as err:
            print(f"SMOKE TEST FAILED: {err}", file=sys.stderr)
            return 1
        print("SMOKE TEST OK", file=sys.stderr)
        return 0

    if args.real_binary:
        try:
            run_liveness_smoke(
                elf=args.elf,
                renode_path=renode_path,
                min_virtual=args.min_virtual,
                storage_addr=args.storage_addr,
                storage_size=args.storage_size,
                rtt=args.rtt,
            )
        except AssertionError as err:
            print(f"SMOKE TEST FAILED: {err}", file=sys.stderr)
            return 1
        print("SMOKE TEST OK", file=sys.stderr)
        return 0

    proto_dir = None
    if not args.no_rpc:
        proto_dir = args.studio_proto_dir
        if proto_dir is None:
            if not args.west_topdir:
                print("either --studio-proto-dir or --west-topdir is required", file=sys.stderr)
                return 2
            proto_dir = renode_harness.find_studio_proto_dir(args.west_topdir)

    try:
        run_smoke(
            elf=args.elf,
            renode_path=renode_path,
            studio_proto_dir=proto_dir,
            check_rpc=not args.no_rpc,
            boot_timeout=args.boot_timeout,
            rpc_timeout=args.rpc_timeout,
        )
    except AssertionError as err:
        print(f"SMOKE TEST FAILED: {err}", file=sys.stderr)
        return 1

    print("SMOKE TEST OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
