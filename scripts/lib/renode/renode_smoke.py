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

Four modes (`--mode`, default `ble`); `--elf` is the DUT (the central half in
split / ble-split mode). ble mode boots a real hardware image and (with `--host-elf`) drives
an encrypted Studio-over-BLE read, or without a host a boot-liveness check. uart
mode boots a snippet-built DUT and checks the boot banner + a core Studio
GetDeviceInfo. split mode boots a wired-split central (`--elf`) + peripheral
(`--peripheral-elf`) on a Renode UART hub and checks both boot banners + a
peripheral keypress relayed to the central. See docs/renode-testing.md and
docs/renode-internals.md.

Usage:
    # ble mode (default -- real image + host app):
    python renode_smoke.py --elf /path/to/zmk.elf \\
        --host-elf /path/to/renode-ble-host/zephyr.elf

    # uart mode:
    python renode_smoke.py --mode uart --elf /path/to/zmk.elf \\
        --studio-proto-dir /path/to/zmk-studio-messages/proto/zmk
    # or let it auto-discover the proto dir under a west topdir:
    python renode_smoke.py --mode uart --elf /path/to/zmk.elf --west-topdir /path/to/module

    # split mode (wired split -- central + peripheral):
    python renode_smoke.py --mode split --elf /path/to/central.elf \\
        --peripheral-elf /path/to/peripheral.elf

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


def run_uart_smoke(
    elf: Path,
    renode_path: str,
    studio_proto_dir: Path | None = None,
    check_rpc: bool = True,
    expect_name_nonempty: bool = True,
    boot_timeout: float = 15.0,
    rpc_timeout: float = 10.0,
) -> None:
    """uart-mode smoke: boot `elf` (built with the renode-studio-uart snippet)
    under Renode and assert the ZMK boot banner appears; unless `check_rpc` is
    False, also assert a core Studio RPC GetDeviceInfo round trip (which
    requires `studio_proto_dir`)."""
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


# Wired-split smoke defaults. The peripheral's kscan-gpio-direct first input
# (renode_split shield: xiao_d 0 == gpio0 pin 2) maps to keymap position 0, so
# toggling gpio0 pin 2 low presses that key; the central logs the relayed press
# as "position: 0" (keymap.c LOG_DBG, on at ZMK's default DBG log level). This
# is the same injection the historical test-zmk-renode T2 case used.
SPLIT_KEYPRESS_GPIO_PORT = "gpio0"
SPLIT_KEYPRESS_GPIO_PIN = 2
SPLIT_RELAYED_EVENT_MARKER = "position: 0"


def run_split_smoke(
    central_elf: Path,
    peripheral_elf: Path,
    renode_path: str,
    boot_timeout: float = 20.0,
    settle: float = 3.0,
    event_timeout: float = 10.0,
) -> None:
    """Wired-split smoke: boot a central + peripheral pair on a Renode UART hub
    (renode_harness.boot_split_wired) and assert (1) BOTH halves reach the ZMK
    boot banner on their console UART, and (2) the wired split link is up -- a
    synthetic keypress injected on the peripheral is relayed over the split UART
    and processed by the central (its keymap logs the relayed key position).

    This is the correct, valuable smoke for a WIRED split: there is no third UART
    left for a Studio RPC transport (console = uart0, split link = uart1 exhaust
    the nRF52840's two UARTEs), so it proves the split pairing/relay rather than a
    Studio round trip.

    Injection: after both banners appear, settle `settle` seconds (the split
    boot-order race -- see boot_split_wired), then pulse the peripheral's first
    kscan GPIO low over the monitor and wait up to `event_timeout` for the
    central console to show the relayed event marker."""
    session, central_console, peripheral_console = renode_harness.boot_split_wired(
        renode_path,
        central_elf=central_elf,
        peripheral_elf=peripheral_elf,
    )
    assert session.mon is not None
    mon = session.mon
    try:
        print("waiting for both ZMK boot banners...", file=sys.stderr)
        central_banner = renode_harness.wait_for_text(
            central_console._sock, "Welcome to ZMK", timeout=boot_timeout
        )
        if "Welcome to ZMK" not in central_banner:
            raise AssertionError(f"central never saw the ZMK boot banner; got:\n{central_banner}")
        peripheral_banner = renode_harness.wait_for_text(
            peripheral_console._sock, "Welcome to ZMK", timeout=boot_timeout
        )
        if "Welcome to ZMK" not in peripheral_banner:
            raise AssertionError(
                f"peripheral never saw the ZMK boot banner; got:\n{peripheral_banner}"
            )
        print("both boot banners OK", file=sys.stderr)

        # Let both sides fully settle (UART RX-enable on both ends, kscan init)
        # before injecting an event -- an event fired too early races the
        # central's RX-enable and gets silently dropped (boot-order race).
        time.sleep(settle)
        renode_harness.drain_text(central_console._sock, timeout=0.2)  # discard buffered

        print(
            f"injecting keypress on peripheral ({SPLIT_KEYPRESS_GPIO_PORT} pin "
            f"{SPLIT_KEYPRESS_GPIO_PIN})...",
            file=sys.stderr,
        )
        mon.execute('mach set "peripheral"')
        mon.execute(f"sysbus.{SPLIT_KEYPRESS_GPIO_PORT} OnGPIO {SPLIT_KEYPRESS_GPIO_PIN} true")
        time.sleep(0.3)
        mon.execute(f"sysbus.{SPLIT_KEYPRESS_GPIO_PORT} OnGPIO {SPLIT_KEYPRESS_GPIO_PIN} false")

        central_log = renode_harness.wait_for_text(
            central_console._sock, SPLIT_RELAYED_EVENT_MARKER, timeout=event_timeout
        )
        if SPLIT_RELAYED_EVENT_MARKER not in central_log:
            print("--- central console tail ---", file=sys.stderr)
            print("\n".join(central_log.splitlines()[-25:]), file=sys.stderr)
            raise AssertionError(
                "central never processed a key event relayed from the peripheral "
                f"(expected {SPLIT_RELAYED_EVENT_MARKER!r}) -- wired split link not up"
            )
        print(
            f"wired split relay OK (central saw {SPLIT_RELAYED_EVENT_MARKER!r})",
            file=sys.stderr,
        )
    finally:
        peripheral_console.close()
        central_console.close()
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
    """ble-mode boot-liveness smoke (no host): boot a real flashable image (no
    UART Studio transport) and prove it is still running -- not parked in a
    Zephyr fatal -- after `min_virtual` virtual seconds. This is what ble mode
    degrades to when no --host-elf is given.

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

# ZMK split-PERIPHERAL RTT log markers (peripheral.c security_changed):
#   LOG_DBG("Security changed: %s level %u", addr, level)   -- on success
#   LOG_ERR("Security failed: %s level %u err %d", ...)      -- on failure
# Seeing "Security changed" + "level 2" proves the encrypted split link
# (peripheral <-> central) reached BT_SECURITY_L2. (These need
# CONFIG_ZMK_LOG_LEVEL_DBG + RTT, set by the renode_split right.conf.)
SPLIT_L2_NEEDLE = "Security changed"
SPLIT_L2_LEVEL = "level 2"
SPLIT_FAIL_NEEDLE = "Security failed"


def _split_l2_seen(rtt_buf: str) -> bool:
    """True once the peripheral RTT shows an encrypted (L2) split link."""
    return SPLIT_L2_NEEDLE in rtt_buf and SPLIT_L2_LEVEL in rtt_buf


def run_ble_studio_smoke(
    dut_elf: Path,
    host_elf: Path,
    renode_path: str,
    virtual_budget: float = 20.0,
    wall_budget: float = 780.0,
    storage_addr: int = renode_harness.STORAGE_ADDR_DEFAULT,
    storage_size: int = renode_harness.STORAGE_SIZE_DEFAULT,
    steady_quantum: str | None = None,
) -> None:
    """ble-mode Studio smoke (with host): boot a real ZMK DUT and the renode-ble-host app on
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


def run_ble_split_smoke(
    central_elf: Path,
    peripheral_elf: Path,
    host_elf: Path,
    renode_path: str,
    virtual_budget: float = 40.0,
    wall_budget: float = 1500.0,
    storage_addr: int = renode_harness.STORAGE_ADDR_DEFAULT,
    storage_size: int = renode_harness.STORAGE_SIZE_DEFAULT,
    steady_quantum: str | None = None,
) -> None:
    """ble-split-mode smoke: boot a WIRELESS split keyboard (central + peripheral
    halves) and the renode-ble-host on ONE Renode BLE medium (fake CCM in all
    three machines), then assert the full peripheral -> central -> host encrypted
    chain, IN ORDER:

      1. the split link comes up encrypted -- the peripheral's RTT log shows
         "Security changed: ... level 2" (BT_SECURITY_L2 between the peripheral
         and central halves); THEN
      2. the host reaches an encrypted Studio GATT read of the CENTRAL half --
         both STAGE:S4-SECURITY-CHANGED OK (encrypted link up) and
         STAGE:S5-GATT-READ OK (encrypted read).

    Reaching S5 through the split central proves the whole chain: the host's
    encrypted Studio read is served by the same central that holds the encrypted
    split link to the peripheral. Also asserts 0 radio "trimming" warnings on
    either link (the DLE-27 cap on both halves + host should keep every on-air
    PDU within Renode's 31-byte radio cap).

    FAILS on the peripheral's "Security failed" marker, any host FAIL marker, the
    virtual-time budget, or the `wall_budget` wall-clock safety net. Also fails if
    the host reaches S5 but the split link never secured (that would mean the
    central served Studio without the peripheral -- not the full chain). On
    failure the peripheral RTT + host console tails are printed. NOT a
    cryptographic assertion -- the CCM is a shared identity transform.

    `steady_quantum` (e.g. "0.001") raises the global time-sync quantum once BOTH
    links are secured (split L2 + host S4), for a faster steady-state phase; None
    (default) keeps the load-bearing 10us quantum throughout. Three machines at
    10us is HEAVY -- budget accordingly (see docs/renode-testing.md)."""
    import tempfile

    log_fd, log_path = tempfile.mkstemp(prefix="zmk-ble-split-renode-", suffix=".log")
    os.close(log_fd)

    print(
        "booting split central + peripheral + renode-ble-host on one BLE medium "
        f"(virtual budget {virtual_budget:.0f}s, wall safety {wall_budget:.0f}s)...",
        file=sys.stderr,
    )
    session, central_console, peripheral_rtt, host_console = renode_harness.boot_ble_split(
        renode_path,
        central_elf=central_elf,
        peripheral_elf=peripheral_elf,
        host_elf=host_elf,
        storage_addr=storage_addr,
        storage_size=storage_size,
        renode_log=Path(log_path),
    )
    assert session.mon is not None
    mon = session.mon
    rtt_buf = ""
    host_buf = ""
    central_buf = ""
    reason = None
    split_l2_at = None
    steady_raised = False
    split_fail_seen = False
    host_fail_seen = False
    try:
        deadline = time.monotonic() + wall_budget
        vt = 0.0
        while time.monotonic() < deadline:
            if peripheral_rtt is not None:
                rtt_buf += renode_harness.drain_text(peripheral_rtt._sock, timeout=0.3)
            host_buf += renode_harness.drain_text(host_console._sock, timeout=0.3)
            central_buf += renode_harness.drain_text(central_console._sock, timeout=0.2)

            if split_l2_at is None and _split_l2_seen(rtt_buf):
                split_l2_at = vt
                print(
                    f"split link encrypted (peripheral reached L2) at vt~{vt:.1f}s",
                    file=sys.stderr,
                )

            # Fine-then-coarse: once BOTH links are up, optionally coarsen.
            if (
                steady_quantum
                and not steady_raised
                and split_l2_at is not None
                and BLE_SECURITY_OK in host_buf
            ):
                renode_harness.raise_global_quantum(session, steady_quantum)
                steady_raised = True
                print(
                    f"raised global quantum to {steady_quantum} after both links up",
                    file=sys.stderr,
                )

            if (
                BLE_GATT_READ_OK in host_buf
                and BLE_SECURITY_OK in host_buf
                and split_l2_at is not None
            ):
                break

            # NOTE: transient failure markers are NOT fatal here. Under the
            # 3-machine load a first pairing attempt on either link can lose an
            # SMP packet ("Security failed" / "Unexpected SMP code") and ZMK (and
            # the host app) simply disconnect, rescan and RETRY -- a later attempt
            # succeeds (hardware-observed under Renode). So we only ever succeed
            # on the positive markers and only ever fail on the time budgets; the
            # failure markers are just counted for the diagnostic report.
            if SPLIT_FAIL_NEEDLE in rtt_buf:
                split_fail_seen = True
            if any(m in host_buf for m in BLE_FAIL_MARKERS):
                host_fail_seen = True

            mon.execute('mach set "central"', settle=0.2)
            vt = _parse_virtual_seconds(mon.execute("machine GetTimeSourceInfo", settle=0.3)) or vt
            if vt >= virtual_budget:
                reason = (
                    f"virtual-time budget exhausted ({vt:.1f}s >= {virtual_budget:.0f}s) "
                    "before the full chain completed"
                )
                break
        else:
            reason = f"wall-clock safety budget exhausted ({wall_budget:.0f}s)"

        # Final drain so late markers/log lines are captured.
        if peripheral_rtt is not None:
            rtt_buf += renode_harness.drain_text(peripheral_rtt._sock, timeout=1.0)
        host_buf += renode_harness.drain_text(host_console._sock, timeout=1.0)
        central_buf += renode_harness.drain_text(central_console._sock, timeout=0.3)

        renode_log = ""
        try:
            renode_log = Path(log_path).read_text(errors="replace")
        except OSError:
            pass
        trimming = renode_log.count("trimming")

        host_reached = BLE_GATT_READ_OK in host_buf and BLE_SECURITY_OK in host_buf
        split_ok = split_l2_at is not None
        stages = [ln.strip() for ln in host_buf.splitlines() if "STAGE:" in ln]

        retry_note = ""
        if split_fail_seen or host_fail_seen:
            which = ", ".join(
                w for w, seen in (("split", split_fail_seen), ("host", host_fail_seen)) if seen
            )
            retry_note = f" (recovered after transient pairing retry on: {which})"

        # Success requires BOTH: the split link secured AND the host read.
        if reason is None and host_reached and split_ok:
            for ln in stages:
                print(f"  host| {ln}", file=sys.stderr)
            print(
                f"BLE-split smoke OK: split link L2 (vt~{split_l2_at:.1f}s) then host "
                f"encrypted Studio read; radio 'trimming' warnings: {trimming}{retry_note}",
                file=sys.stderr,
            )
            if trimming:
                print(
                    f"WARNING: {trimming} radio 'trimming' line(s) in the Renode log "
                    "(expected 0 for a clean fake-CCM run -- check the DLE-27 cap)",
                    file=sys.stderr,
                )
                raise AssertionError(
                    f"{trimming} radio 'trimming' warning(s) -- an on-air PDU exceeded "
                    "Renode's 31-byte cap (DLE not capped to 27 on some link)"
                )
            return

        if reason is None and host_reached and not split_ok:
            reason = (
                "host reached the encrypted Studio read but the split link never "
                "secured (no peripheral 'Security changed ... level 2') -- the central "
                "served Studio without the peripheral, so the full chain is unproven"
            )

        # Failure: dump the peripheral RTT + host console tails for diagnosis.
        print(f"--- split L2 secured: {split_ok} (at vt~{split_l2_at}) ---", file=sys.stderr)
        print("--- host console STAGE markers ---", file=sys.stderr)
        for ln in stages:
            print(f"  host| {ln}", file=sys.stderr)
        print("--- peripheral RTT tail ---", file=sys.stderr)
        print("\n".join(rtt_buf.splitlines()[-30:]), file=sys.stderr)
        print("--- central console tail (usually silent: USB-CDC) ---", file=sys.stderr)
        print("\n".join(central_buf.splitlines()[-15:]), file=sys.stderr)
        if trimming:
            print(
                f"(also: {trimming} radio 'trimming' line(s) in the Renode log)", file=sys.stderr
            )
        raise AssertionError(reason or "peripheral->central->host encrypted chain not reached")
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass
        if peripheral_rtt is not None:
            peripheral_rtt.close()
        host_console.close()
        central_console.close()
        for sock in getattr(session, "_idle_sockets", []):
            try:
                sock.close()
            except OSError:
                pass
        session.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--elf",
        required=True,
        type=Path,
        help="DUT firmware ELF (all modes; the CENTRAL half in split mode).",
    )
    ap.add_argument(
        "--mode",
        choices=("uart", "ble", "split", "ble-split"),
        default="ble",
        help="ble (default): real hardware image; with --host-elf a full encrypted "
        "Studio-over-BLE read (S4/S5), without it a boot-liveness check. ble-split: a "
        "wireless split -- --elf is the split CENTRAL, --peripheral-elf the split "
        "PERIPHERAL, --host-elf the host; asserts the split link secures then the host "
        "reads Studio through the central. uart: snippet-built DUT, boot banner + Studio "
        "GetDeviceInfo over emulated UARTs. split: wired-split central (--elf) + "
        "--peripheral-elf on a Renode UART hub; smoke = both boot banners + a peripheral "
        "keypress relayed to the central.",
    )
    ap.add_argument(
        "--host-elf",
        type=Path,
        help="ble mode only: the renode-ble-host app ELF (built with `west build -b "
        "nrf52840dk/nrf52840 -s <this repo>/renode-ble-host`). Given -> full S4/S5 "
        "smoke; omitted -> boot-liveness only. Required for --mode ble-split.",
    )
    ap.add_argument(
        "--peripheral-elf",
        type=Path,
        help="split / ble-split mode: the split PERIPHERAL half's firmware ELF (--elf is "
        "the CENTRAL half).",
    )
    ap.add_argument(
        "--no-rpc",
        action="store_true",
        help="uart mode: check only the boot banner (for modules without Studio RPC).",
    )
    ap.add_argument(
        "--studio-proto-dir",
        type=Path,
        help="uart mode: path to zmk-studio-messages' proto/zmk dir "
        "(auto-discovered from --west-topdir if omitted).",
    )
    ap.add_argument("--west-topdir", type=Path, help="used to auto-discover --studio-proto-dir")
    ap.add_argument("--renode-version", default=renode_harness.RENODE_VERSION_DEFAULT)
    ap.add_argument("--boot-timeout", type=float, default=15.0)
    ap.add_argument("--rpc-timeout", type=float, default=10.0)

    # Advanced ble-mode knobs (see docs/renode-testing.md).
    adv = ap.add_argument_group("advanced ble-mode knobs")
    adv.add_argument(
        "--min-virtual",
        type=float,
        default=20.0,
        help="ble mode (liveness): virtual seconds to run before PC sampling (default: 20).",
    )
    adv.add_argument(
        "--rtt",
        action="store_true",
        help="ble mode (liveness): capture Zephyr SEGGER RTT log output during the "
        "run (RTT-logging builds); printed and scanned for fatal markers.",
    )
    adv.add_argument(
        "--virtual-budget",
        type=float,
        default=20.0,
        help="ble mode (with host): virtual seconds to reach the encrypted read before "
        "failing (default: 20; ~3.3s is typical).",
    )
    adv.add_argument(
        "--steady-quantum",
        default=None,
        help="ble mode (with host): after the encrypted link is up (S4), raise the global "
        "time-sync quantum to this value (e.g. 0.001) for the steady-state phase. Pairing "
        "needs the 10us boot quantum, but the encrypted link tolerates a 100x-coarser "
        "quantum and runs ~7x faster -- use for long BLE tests. Default: keep 10us "
        "throughout. See docs/renode-testing.md.",
    )
    adv.add_argument(
        "--storage-addr",
        type=lambda s: int(s, 0),
        default=renode_harness.STORAGE_ADDR_DEFAULT,
        help="ble mode: NVS storage_partition address to preload as erased 0xFF "
        "(default: 0xec000, xiao_ble).",
    )
    adv.add_argument(
        "--storage-size",
        type=lambda s: int(s, 0),
        default=renode_harness.STORAGE_SIZE_DEFAULT,
        help="ble mode: NVS storage_partition size (default: 0x8000, xiao_ble).",
    )
    args = ap.parse_args(argv)

    if not args.elf.is_file():
        print(f"ELF not found: {args.elf}", file=sys.stderr)
        return 2
    if args.host_elf is not None and args.mode not in ("ble", "ble-split"):
        print("--host-elf is only valid with --mode ble / ble-split", file=sys.stderr)
        return 2
    if args.peripheral_elf is not None and args.mode not in ("split", "ble-split"):
        print("--peripheral-elf is only valid with --mode split / ble-split", file=sys.stderr)
        return 2
    if args.mode in ("split", "ble-split"):
        if args.peripheral_elf is None:
            print(f"--mode {args.mode} requires --peripheral-elf", file=sys.stderr)
            return 2
        if not args.peripheral_elf.is_file():
            print(f"peripheral ELF not found: {args.peripheral_elf}", file=sys.stderr)
            return 2

    renode_path = renode_harness.find_or_install_renode(version=args.renode_version)
    if renode_path is None:
        print("Renode is not installed and could not be auto-installed", file=sys.stderr)
        return 2

    if args.mode == "split":
        try:
            run_split_smoke(
                central_elf=args.elf,
                peripheral_elf=args.peripheral_elf,
                renode_path=renode_path,
                boot_timeout=args.boot_timeout,
            )
        except AssertionError as err:
            print(f"SMOKE TEST FAILED: {err}", file=sys.stderr)
            return 1
        print("SMOKE TEST OK", file=sys.stderr)
        return 0

    if args.mode == "ble-split":
        if args.peripheral_elf is None or not args.peripheral_elf.is_file():
            print(f"--peripheral-elf not found: {args.peripheral_elf}", file=sys.stderr)
            return 2
        if args.host_elf is None or not args.host_elf.is_file():
            print(
                f"--host-elf is required for ble-split; not found: {args.host_elf}",
                file=sys.stderr,
            )
            return 2
        try:
            run_ble_split_smoke(
                central_elf=args.elf,
                peripheral_elf=args.peripheral_elf,
                host_elf=args.host_elf,
                renode_path=renode_path,
                virtual_budget=args.virtual_budget,
                storage_addr=args.storage_addr,
                storage_size=args.storage_size,
                steady_quantum=args.steady_quantum,
            )
        except AssertionError as err:
            print(f"SMOKE TEST FAILED: {err}", file=sys.stderr)
            return 1
        print("SMOKE TEST OK", file=sys.stderr)
        return 0

    if args.mode == "ble":
        try:
            if args.host_elf is not None:
                if not args.host_elf.is_file():
                    print(f"host ELF not found: {args.host_elf}", file=sys.stderr)
                    return 2
                run_ble_studio_smoke(
                    dut_elf=args.elf,
                    host_elf=args.host_elf,
                    renode_path=renode_path,
                    virtual_budget=args.virtual_budget,
                    storage_addr=args.storage_addr,
                    storage_size=args.storage_size,
                    steady_quantum=args.steady_quantum,
                )
            else:
                print(
                    "ble mode without --host-elf: no host given, checking DUT boot liveness "
                    "only (no encrypted Studio read).",
                    file=sys.stderr,
                )
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

    # uart mode.
    proto_dir = None
    if not args.no_rpc:
        proto_dir = args.studio_proto_dir
        if proto_dir is None:
            if not args.west_topdir:
                print("either --studio-proto-dir or --west-topdir is required", file=sys.stderr)
                return 2
            proto_dir = renode_harness.find_studio_proto_dir(args.west_topdir)

    try:
        run_uart_smoke(
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
