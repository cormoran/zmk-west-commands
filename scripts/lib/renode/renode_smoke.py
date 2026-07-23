#!/usr/bin/env python3
"""Generic Renode smoke test for any ZMK module's built ELF.

Given a real, hardware-flashable ZMK firmware ELF, boot it under Renode and
assert:

  1. The real ZMK boot banner appears (proves the platform description, ELF
     load, and CPU execution all work).
  2. A core Studio RPC GetDeviceInfo request round-trips a well-formed
     Response with a non-empty device name.

This is what `.github/actions/zmk-renode-test/action.yml` always runs,
regardless of which module it's testing -- it's the "does this thing even
boot and speak Studio RPC" gate before any module-specific test runs. A
module's own tests (e.g. this template's tests/renode/test_renode.py)
import renode_harness directly for anything more specific (their own custom
RPC subsystem, etc.).

Four modes (`--mode`, default `ble`); `--elf` is the DUT (the central half in
wired-split / ble-split mode). ble mode boots a real hardware image and (with
`--host-elf`) drives an encrypted Studio-over-BLE read, or without a host a
boot-liveness check. usb mode boots the SAME real hardware image on the
NRF_USBD_Full usb platform and drives a Studio GetDeviceInfo round trip over the
emulated USB CDC (plus the boot banner when the image has a console CDC).
wired-split mode boots a wired-split central (`--elf`) + peripheral
(`--peripheral-elf`) on a Renode UART hub and asserts BOTH a Studio GetDeviceInfo
round trip over the central's USB CDC AND a peripheral keypress relayed over the
wired link to the central. ble-split boots three images on one BLE medium (split
central + peripheral + host). See docs/renode-testing.md and
docs/design/renode-internals.md.

Usage:
    # ble mode (default -- real image + host app):
    python renode_smoke.py --elf /path/to/zmk.elf \\
        --host-elf /path/to/renode-ble-host/zephyr.elf

    # usb mode (same real image as ble mode; Studio RPC over emulated USB CDC):
    python renode_smoke.py --mode usb --elf /path/to/zmk.elf \\
        --west-topdir /path/to/module

    # wired-split mode (wired split; central Studio over USB CDC):
    python renode_smoke.py --mode wired-split --elf /path/to/central.elf \\
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
import rpc_client  # noqa: E402

# A Zephyr fatal error parks the CPU spinning in arch_system_halt (observed
# for the vt~10s BT_ASSERT oops when FICR/NVS is wrong); z_fatal_error /
# k_sys_fatal_error_handler are the frames just before it. If a PC sample lands
# on any of these, the image has faulted -- fail liveness.
FATAL_SYMBOLS = ("arch_system_halt", "z_fatal_error", "k_sys_fatal_error_handler")
# Console markers (observation builds only) that also mean a fatal.
FATAL_CONSOLE_MARKERS = ("FATAL ERROR", "Halting system")
# A crashed DUT (kernel oops / Zephyr BLE-controller LL assert) printed to a
# console/RTT stream. Under Renode's coarse quantum the 3-machine ble-split's
# soft link layer intermittently asserts (lll.c / lll_peripheral.c) and oopses;
# once that happens the attempt is dead, so the BLE smokes bail on it IMMEDIATELY
# and let the whole-emulation retry re-roll -- rather than waiting out the full
# virtual-time budget on a corpse (a crash at vt~7s otherwise burnt ~20 min of
# wall time running to the 120s budget).
CRASH_MARKERS = ("ZEPHYR FATAL ERROR", "Kernel oops", "ASSERTION FAIL", "Halting system")

# ---------------------------------------------------------------------------
# Transport orthogonalization: the test's two independent axes are the
# host-link (how the central answers Studio RPC) and the split-link (how the
# central reaches the peripheral). `--mode` is retained as a preset that
# expands to a (host-link, split-link) pair. See docs/design/renode-transport-orthogonal.md.
# ---------------------------------------------------------------------------
HOST_LINKS = ("usb", "ble", "none")
SPLIT_LINKS = ("none", "wired", "ble")

# The four backward-compatible presets, each a (host-link, split-link) pair.
# wired-split is a WIRED split whose central STILL answers Studio RPC -- over
# the emulated USB CDC, which is free because the wired split link only consumes
# the two nRF52840 UARTEs (console uart0 + split uart1), leaving USB for Studio.
MODE_PRESETS: dict[str, tuple[str, str]] = {
    "ble": ("ble", "none"),
    "usb": ("usb", "none"),
    "wired-split": ("usb", "wired"),
    "ble-split": ("ble", "ble"),
}

# (host-link, split-link) cells the harness knows how to run. The four presets
# plus the Studio-less wired split reachable only via the axis flags
# (--host-link none --split-link wired). Cells not listed here are rejected with
# an explanatory error (see resolve_links).
SUPPORTED_LINKS: set[tuple[str, str]] = {
    ("ble", "none"),
    ("usb", "none"),
    ("usb", "wired"),  # the wired-split preset: wired split + Studio over USB
    ("ble", "ble"),
    ("none", "wired"),  # Studio-less wired split (axis-flags only, no preset)
}


def canonical_mode(host_link: str, split_link: str) -> str:
    """The preset name for a (host, split) pair, or the canonical
    "<host>+<split>" string when the pair is not one of the four presets. Used
    for the backward-compatible ZMK_RENODE_MODE env var / logging."""
    for name, pair in MODE_PRESETS.items():
        if pair == (host_link, split_link):
            return name
    return f"{host_link}+{split_link}"


def resolve_links(
    mode: str | None, host_link: str | None, split_link: str | None
) -> tuple[str, str]:
    """Resolve the (host-link, split-link) pair from either a --mode preset or
    the explicit --host-link/--split-link axis flags, enforcing mutual
    exclusion. Raises ValueError with a user-facing message on any conflict,
    unknown value, or unsupported combination.

    * no flags at all -> the default preset ("ble" == host ble, split none);
    * --mode X         -> MODE_PRESETS[X];
    * axis flags       -> (host_link or "none", split_link or "none").
    --mode may not be combined with either axis flag."""
    axes_given = host_link is not None or split_link is not None
    if mode is not None and axes_given:
        raise ValueError(
            "--mode and --host-link/--split-link are mutually exclusive; use one vocabulary"
        )
    if mode is not None:
        if mode not in MODE_PRESETS:
            raise ValueError(f"unknown --mode {mode!r} (choices: {', '.join(MODE_PRESETS)})")
        return MODE_PRESETS[mode]
    if not axes_given:
        return MODE_PRESETS["ble"]

    host = host_link or "none"
    split = split_link or "none"
    if host not in HOST_LINKS:
        raise ValueError(f"unknown --host-link {host!r} (choices: {', '.join(HOST_LINKS)})")
    if split not in SPLIT_LINKS:
        raise ValueError(f"unknown --split-link {split!r} (choices: {', '.join(SPLIT_LINKS)})")
    if (host, split) not in SUPPORTED_LINKS:
        supported = ", ".join(f"{h}x{s}" for h, s in sorted(SUPPORTED_LINKS))
        raise ValueError(
            f"unsupported combination host-link={host} x split-link={split}. "
            f"Supported: {supported}. See docs/design/renode-transport-orthogonal.md."
        )
    return host, split


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


def _assert_get_device_info(
    studio_pb2,
    rpc,
    rpc_timeout: float,
    expect_name_nonempty: bool,
    rounds: int = 1,
) -> str:
    """Send a core Studio RPC GetDeviceInfo over `rpc` (a framed RpcSocket) and
    assert a well-formed response; returns the device name.

    `rounds` > 1 repeats the request/response exchange on the same session. This
    matters for the USB transport: a request/response is one host->device OUT
    transfer followed by one device->host IN transfer, and a bug that only
    delivers the *first* transfer of either direction per session (e.g. an
    endpoint that is never re-armed) passes a single-shot check but fails the
    second round -- so the USB smoke sends at least two."""
    name = ""
    for round_index in range(rounds):
        req = studio_pb2.Request()
        req.request_id = round_index + 1
        req.core.get_device_info = True
        rpc.send(req.SerializeToString())
        resp_bytes = rpc.read_frame(timeout=rpc_timeout)
        if resp_bytes is None:
            raise AssertionError(
                f"no Studio RPC response frame received (timeout) on round {round_index + 1}"
            )

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
        suffix = f" (round {round_index + 1}/{rounds})" if rounds > 1 else ""
        print(f"core Studio RPC GetDeviceInfo OK (name={name!r}){suffix}", file=sys.stderr)
    return name


# ---------------------------------------------------------------------------
# Standardized smoke checks. Every mode runs the same three, in order:
#   CHECK 1/3 connection   -- the transport/link the mode exercises is up.
#   CHECK 2/3 key input    -- a keypress (position 0), injected on the DUT (on the
#                             PERIPHERAL for a split), is processed by the DUT /
#                             central, observed as its "position: 0" keymap log.
#   CHECK 3/3 Studio RPC   -- a real framed GetDeviceInfo round trips a well-formed
#                             response with a non-empty device name.
# Per-mode run_*_smoke functions below wire the mode-specific transports into
# these three. See docs/renode-testing.md.
# ---------------------------------------------------------------------------

# renode-ble-host S6 markers: it dumps each GetDeviceInfo response indication as
# `STAGE:S6-RPC-CHUNK <hex>` and prints S6_DONE once the framed response closes.
S6_CHUNK_MARKER = "STAGE:S6-RPC-CHUNK "
S6_DONE_MARKER = "STAGE:S6-RPC-DONE"


def _assert_key_processed(
    session,
    log_sock,
    machine: str | None,
    source: str,
    timeout: float,
    hold: float = 1.0,
    reinject_every: float = 3.0,
):
    """CHECK 2/3: inject a keypress at position 0 (on `machine`; the peripheral
    for a split) and wait for the DUT/central to log processing it
    ("position: 0") on `log_sock` (a console or RTT RpcSocket).

    The press is RE-INJECTED every `reinject_every` s across `timeout`, and each
    press is held `hold` s. Both matter for the split relay: a split link may
    still be coming up when the first press fires (a lost press then never
    relays), and on a heavily loaded / coarse-quantum run a single split-relay
    notification can be dropped or delayed -- so a later press lands. Holding the
    key ~`hold` s of wall time also guarantees it spans several kscan poll periods
    (10 ms virtual each) even when Renode is running well below real time (e.g. the
    3-machine ble-split at a 10 us quantum). A single quick tap was reliable for a
    single DUT but flaky for the peripheral->central relay on some ZMK branches.
    Kept gentle here (a few short presses): the emulated BLE controller is easily
    destabilized by sustained activity (an LL assert / kernel oops), so the
    ble-split leans on its whole-emulation retry to absorb a bad roll rather than
    hammering within one attempt."""
    marker = renode_harness.KEYPRESS_POSITION_MARKER
    renode_harness.drain_text(log_sock._sock, timeout=0.2)  # discard buffered
    print(
        f"CHECK 2/3 key input: injecting keypress on {source} (retry up to {timeout:.0f}s)...",
        file=sys.stderr,
    )
    deadline = time.monotonic() + timeout
    buf = ""
    next_inject = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_inject:
            renode_harness.inject_keypress(session, machine=machine, hold=hold)
            next_inject = time.monotonic() + reinject_every
        buf += renode_harness.drain_text(log_sock._sock, timeout=0.5)
        if marker in buf:
            print(f"CHECK 2/3 key input OK ({source} keypress -> {marker!r})", file=sys.stderr)
            return
    print("--- key-log tail ---", file=sys.stderr)
    print("\n".join(buf.splitlines()[-25:]), file=sys.stderr)
    raise AssertionError(
        f"keypress injected on {source} was never processed (expected {marker!r} "
        "-- is the kscan/relay path up and DBG logging enabled?)"
    )


def _parse_ble_device_info(studio_pb2, host_buf: str, expect_name_nonempty: bool = True) -> str:
    """CHECK 3/3 for the BLE host-link: reassemble the framed GetDeviceInfo
    response the renode-ble-host dumped as S6-RPC-CHUNK hex lines, parse it and
    return the device name. Raises AssertionError if the chunks are absent /
    incomplete or the response is not a well-formed GetDeviceInfoResponse."""
    chunks: list[bytes] = []
    for line in host_buf.splitlines():
        idx = line.find(S6_CHUNK_MARKER)
        if idx == -1:
            continue
        try:
            chunks.append(bytes.fromhex(line[idx + len(S6_CHUNK_MARKER) :].strip()))
        except ValueError:
            pass
    if not chunks:
        raise AssertionError(
            "no S6-RPC-CHUNK response chunks from the host "
            "(the framed GetDeviceInfo indication never arrived)"
        )
    payload = rpc_client.deframe(b"".join(chunks))
    if payload is None:
        raise AssertionError(
            f"the S6 GetDeviceInfo response never closed a frame ({len(chunks)} chunk(s), no EOF)"
        )
    resp = studio_pb2.Response()
    resp.ParseFromString(payload)
    if resp.WhichOneof("type") != "request_response":
        raise AssertionError(
            f"S6 GetDeviceInfo: expected a request_response, got {resp.WhichOneof('type')!r}"
        )
    if resp.request_response.WhichOneof("subsystem") != "core":
        raise AssertionError(
            "S6 GetDeviceInfo: expected core subsystem, got "
            f"{resp.request_response.WhichOneof('subsystem')!r}"
        )
    name = resp.request_response.core.get_device_info.name
    if expect_name_nonempty and not name:
        raise AssertionError("S6 GetDeviceInfoResponse.name was empty")
    print(f"CHECK 3/3 Studio RPC GetDeviceInfo OK over BLE (name={name!r})", file=sys.stderr)
    return name


# devtool custom-subsystem SetStudioLockState payloads (the bytes carried in a
# Studio custom CallRequest): cormoran.devtool.Request{set_studio_lock_state:
# {state: STUDIO_LOCK_STATE_LOCKED / _UNLOCKED}}. Hand-encoded so the smoke does
# not need to compile the devtool proto.
_DEVTOOL_SET_LOCK_LOCKED = bytes.fromhex("0a020801")
_DEVTOOL_SET_LOCK_UNLOCKED = bytes.fromhex("0a020802")


def _drain_frames(rpc, quiet_time: float = 0.4) -> None:
    """Read and discard Studio frames until none arrive for `quiet_time`."""
    while rpc.read_frame(timeout=quiet_time) is not None:
        pass


def _assert_unlock_burst(studio_pb2, rpc, rpc_timeout: float) -> None:
    """Exercise a genuine device->host BURST -- two device->host transfers
    back-to-back with no host->device packet in between -- and assert both are
    delivered. A devtool SetStudioLockState(UNLOCKED) that actually changes the
    lock state makes the firmware emit a core lock_state_changed *notification*
    AND the custom CallResponse, one right after the other; that is exactly the
    case the DualCdcAcmBridge device->host read one-shot must re-arm across (the
    2-round GetDeviceInfo check above never produces a dev->host burst, only
    strictly alternating request/response). If the bridge dropped the second of
    the two, the response would arrive but the notification would not (or vice
    versa) -- so both are required here.

    Gracefully skipped when the studio proto has no `custom` subsystem at all
    (an upstream zmk-studio-messages build -- the burst needs the custom-RPC
    subsystem, a fork feature), or when the image has that subsystem but ships no
    `cormoran__devtool` (run_usb_smoke is generic; not every studio-rpc-usb-uart
    image ships either)."""
    # The burst rides the custom-RPC subsystem, which only the fork's
    # zmk-studio-messages defines. On an upstream proto studio_pb2.Request has no
    # `custom` field, so building the request below would AttributeError -- skip
    # cleanly instead (the 2-round GetDeviceInfo above still guards the re-arm).
    if "custom" not in studio_pb2.Request.DESCRIPTOR.fields_by_name:
        print(
            "studio proto has no custom subsystem (upstream image); "
            "skipping device->host burst assertion",
            file=sys.stderr,
        )
        return
    # Discover the devtool subsystem index (list_custom_subsystems is unsecured).
    req = studio_pb2.Request()
    req.request_id = 90
    req.custom.list_custom_subsystems.SetInParent()
    rpc.send(req.SerializeToString())
    resp_bytes = rpc.read_frame(timeout=rpc_timeout)
    if resp_bytes is None:
        raise AssertionError("no list_custom_subsystems response (timeout)")
    resp = studio_pb2.Response()
    resp.ParseFromString(resp_bytes)
    if (
        resp.WhichOneof("type") != "request_response"
        or resp.request_response.WhichOneof("subsystem") != "custom"
    ):
        raise AssertionError("unexpected list_custom_subsystems response shape")
    devtool_index = next(
        (
            s.index
            for s in resp.request_response.custom.list_custom_subsystems.subsystems
            if "devtool" in s.identifier
        ),
        None,
    )
    if devtool_index is None:
        print(
            "no cormoran__devtool subsystem; skipping device->host burst assertion",
            file=sys.stderr,
        )
        return

    def _set_lock(request_id: int, payload: bytes) -> None:
        r = studio_pb2.Request()
        r.request_id = request_id
        r.custom.call.subsystem_index = devtool_index
        r.custom.call.payload = payload
        rpc.send(r.SerializeToString())

    # Force a known starting state (LOCKED) so the following UNLOCK is guaranteed
    # to be a real transition -- only a state *change* raises the notification.
    _set_lock(91, _DEVTOOL_SET_LOCK_LOCKED)
    _drain_frames(rpc)

    # The unlock: expect BOTH a core lock_state_changed notification and the
    # custom CallResponse for this request_id, in either order.
    unlock_id = 92
    _set_lock(unlock_id, _DEVTOOL_SET_LOCK_UNLOCKED)
    saw_notification = False
    saw_response = False
    deadline = time.monotonic() + rpc_timeout
    while not (saw_notification and saw_response) and time.monotonic() < deadline:
        frame = rpc.read_frame(timeout=deadline - time.monotonic())
        if frame is None:
            break
        r = studio_pb2.Response()
        try:
            r.ParseFromString(frame)
        except Exception:
            continue
        kind = r.WhichOneof("type")
        if kind == "notification":
            if (
                r.notification.WhichOneof("subsystem") == "core"
                and r.notification.core.WhichOneof("notification_type") == "lock_state_changed"
            ):
                saw_notification = True
        elif kind == "request_response" and r.request_response.request_id == unlock_id:
            saw_response = True

    if not saw_response and not saw_notification:
        raise AssertionError("unlock produced no device->host frames at all (timeout)")
    if not saw_response:
        raise AssertionError(
            "unlock notification arrived but its CallResponse did not -- a "
            "device->host burst transfer was dropped"
        )
    if not saw_notification:
        raise AssertionError(
            "unlock CallResponse arrived but the lock_state_changed notification "
            "did not -- a device->host burst transfer was dropped"
        )
    print(
        "device->host burst OK (lock_state_changed notification + CallResponse delivered)",
        file=sys.stderr,
    )


# usb-mode: the DualCdcAcmBridge external name; its two IUART channels are
# machine-registered as sysbus.<name>_cdc0 / _cdc1 (see
# renode_harness.attach_dual_cdc_bridge and platforms/models/DualCdcAcmBridge.cs).
USB_BRIDGE_NAME = "bridge"
USB_REPL_TEMPLATE = "xiao_nrf52840_usb.repl"


def _mon_flag(mon, command: str) -> bool | None:
    """Run a monitor command whose reply is a bare boolean property value
    (e.g. `sysbus.bridge_cdc0 IsWired`); returns None when no True/False line
    could be parsed from the (ANSI-colored, echo-prefixed) reply."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", mon.execute(command, settle=0.3))
    for line in text.splitlines():
        line = line.strip()
        if line in ("True", "False"):
            return line == "True"
    return None


def run_usb_smoke(
    elf: Path,
    renode_path: str,
    studio_proto_dir: Path,
    boot_settle: float = 8.0,
    wiring_timeout: float = 30.0,
    boot_timeout: float = 20.0,
    rpc_timeout: float = 10.0,
    expect_name_nonempty: bool = True,
    storage_addr: int = renode_harness.STORAGE_ADDR_DEFAULT,
    storage_size: int = renode_harness.STORAGE_SIZE_DEFAULT,
    event_timeout: float = 10.0,
    max_attempts: int = 2,
) -> None:
    """usb-mode smoke with a bounded whole-emulation retry.

    Boots the SAME real flashable `studio-rpc-usb-uart` image ble mode runs --
    but on the NRF_USBD_Full usb platform (boot_single_real(...,
    repl_template="xiao_nrf52840_usb.repl")), attaches the DualCdcAcmBridge USB
    host external after the guest's USB init settles, and asserts a core Studio
    RPC GetDeviceInfo round trip over the emulated USB CDC.

    The bridge parses the image's real configuration descriptors, so the smoke
    adapts to the composite it finds (auto-detected via the bridge channels'
    IsWired properties -- no flag needed):

      * a standard `studio-rpc-usb-uart` image has ONE CDC function (Studio) +
        HID -- cdc0 carries Studio RPC and there is no console CDC to assert;
      * an image that also enables `CONFIG_ZMK_USB_LOGGING` has TWO CDC
        functions -- the board console CDC enumerates FIRST (cdc0) and the
        Studio snippet CDC second (cdc1), so the smoke ALSO asserts the ZMK
        boot banner on the console channel.

    WHY the retry (one whole-emulation re-boot, the ble-split pattern): a usb
    run is fast and deterministic on an idle host, but the wall-clock-paced
    attach/settle steps can lose the race when another heavyweight emulation is
    hogging the machine (observed only under that contention in Phase 2). A
    genuine break fails BOTH attempts."""
    studio_pb2 = renode_harness.load_studio_pb2(studio_proto_dir)

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            print(
                f"--- usb smoke attempt {attempt}/{max_attempts} "
                "(fresh emulation; previous attempt failed) ---",
                file=sys.stderr,
            )
        try:
            _run_usb_attempt(
                studio_pb2,
                elf=elf,
                renode_path=renode_path,
                boot_settle=boot_settle,
                wiring_timeout=wiring_timeout,
                boot_timeout=boot_timeout,
                rpc_timeout=rpc_timeout,
                expect_name_nonempty=expect_name_nonempty,
                storage_addr=storage_addr,
                storage_size=storage_size,
                event_timeout=event_timeout,
            )
            if attempt > 1:
                print(f"usb smoke OK on attempt {attempt}", file=sys.stderr)
            return
        except (AssertionError, TimeoutError, OSError) as err:
            # TimeoutError/OSError: a boot/monitor/socket failure under host
            # contention (e.g. the Renode process dying mid-attach with a
            # BrokenPipeError) -- retried like an assertion failure.
            last_err = err
            print(f"usb smoke attempt {attempt}/{max_attempts} FAILED: {err!r}", file=sys.stderr)
    assert last_err is not None
    if isinstance(last_err, AssertionError):
        raise last_err
    raise AssertionError(repr(last_err))


def _run_usb_attempt(
    studio_pb2,
    elf: Path,
    renode_path: str,
    boot_settle: float,
    wiring_timeout: float,
    boot_timeout: float,
    rpc_timeout: float,
    expect_name_nonempty: bool,
    storage_addr: int,
    storage_size: int,
    event_timeout: float = 10.0,
) -> None:
    """One usb-mode attempt: boot, attach the bridge, assert (see run_usb_smoke)."""
    import random

    port_base = random.randint(26000, 40000)
    print(
        "booting real image on the NRF_USBD_Full usb platform...",
        file=sys.stderr,
    )
    session, console, rpc = renode_harness.boot_single_real(
        renode_path,
        elf,
        storage_addr=storage_addr,
        storage_size=storage_size,
        port_base=port_base,
        repl_template=USB_REPL_TEMPLATE,
        rtt=True,  # capture the DUT's SEGGER RTT log for the key-input check
    )
    assert session.mon is not None
    mon = session.mon
    cdc: list = []
    try:
        # Let the guest finish its USB driver bring-up (ENABLE + USBPULLUP)
        # before the host attaches -- a SETUP fired before the guest's INTEN is
        # set would be silently lost (see docs/design/renode-usb-design.md).
        t0 = time.monotonic()
        while time.monotonic() - t0 < boot_settle:
            renode_harness.drain_text(console._sock, timeout=0.5)

        # Two-step attach (create bridge + wire both channel terminals while
        # paused, THEN attach = start enumeration) so no device output is lost.
        cdc = list(renode_harness.attach_dual_cdc_bridge(session, port_base + 4, port_base + 5))

        # Wait for enumeration + descriptor parsing to wire the first CDC
        # channel, then auto-detect whether a second CDC function exists.
        deadline = time.monotonic() + wiring_timeout
        while time.monotonic() < deadline:
            if _mon_flag(mon, f"sysbus.{USB_BRIDGE_NAME}_cdc0 IsWired"):
                break
        else:
            raise AssertionError(
                "USB enumeration never wired the first CDC channel "
                f"(no sysbus.{USB_BRIDGE_NAME}_cdc0 IsWired within {wiring_timeout:.0f}s) "
                "-- is the ELF a studio-rpc-usb-uart (USB-CDC) image?"
            )
        dual_cdc = bool(_mon_flag(mon, f"sysbus.{USB_BRIDGE_NAME}_cdc1 IsWired"))
        # Give the bridge a moment to finish its post-wiring control sequence
        # (SET_LINE_CODING / DTR) and arm the device->host pumps.
        time.sleep(2.0)

        if dual_cdc:
            # Both CDC functions present (CONFIG_ZMK_USB_LOGGING image): the
            # board console CDC enumerates first, the Studio CDC second. The
            # console CDC reliably carries the Zephyr boot banner ("*** Booting
            # Zephyr OS build ... ***", printk -- buffered in the CDC ring from
            # boot and flushed once the host configures the device); the "Welcome
            # to ZMK" line is a *log* message that may be routed to another
            # backend (e.g. RTT), so it is not asserted here.
            print(
                "two CDC functions found (console + Studio); waiting for the Zephyr "
                "boot banner on the console CDC...",
                file=sys.stderr,
            )
            banner = renode_harness.wait_for_text(
                cdc[0]._sock, "Booting Zephyr", timeout=boot_timeout
            )
            if "Booting Zephyr" not in banner:
                raise AssertionError(
                    f"never saw the Zephyr boot banner on the console CDC; got:\n{banner}"
                )
            print("console-CDC boot banner OK", file=sys.stderr)
            studio = cdc[1]
        else:
            print(
                "single CDC function found (Studio only; no console CDC to assert)",
                file=sys.stderr,
            )
            studio = cdc[0]
        print("CHECK 1/3 connection OK (USB enumerated; Studio CDC wired)", file=sys.stderr)

        # CHECK 2/3 key input: inject a keypress on the DUT and confirm it is
        # processed. The single-CDC Studio image has no console CDC, so the
        # "position: 0" keymap log rides the DUT's SEGGER RTT (renode_tester.conf
        # RTT logging + boot_single_real rtt=True).
        if session.rtt_socket is None:
            raise AssertionError(
                "usb mode: no DUT RTT socket for the key-input check "
                "(boot_single_real rtt=True + renode_tester RTT logging required)"
            )
        _assert_key_processed(
            session, session.rtt_socket, machine="single", source="the DUT", timeout=event_timeout
        )

        # CHECK 3/3 Studio RPC. Two rounds: the USB path is where request #2 used
        # to be lost (only the first device->host IN transfer per session was
        # delivered before the bridge's read one-shot re-arm was fixed), so guard
        # against regressing it.
        print("CHECK 3/3 Studio RPC GetDeviceInfo over USB CDC...", file=sys.stderr)
        _assert_get_device_info(studio_pb2, studio, rpc_timeout, expect_name_nonempty, rounds=2)
        # And a genuine device->host burst (two dev->host transfers back-to-back:
        # a lock_state_changed notification + the CallResponse), which the
        # alternating-request/response check above never produces -- this is the
        # case the bridge's device->host read-callback re-arm has to drain.
        _assert_unlock_burst(studio_pb2, studio, rpc_timeout)
    finally:
        for sock in cdc:
            sock.close()
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


def run_usb_wired_smoke(
    central_elf: Path,
    peripheral_elf: Path,
    renode_path: str,
    studio_proto_dir: Path,
    boot_settle: float = 8.0,
    wiring_timeout: float = 30.0,
    boot_timeout: float = 20.0,
    rpc_timeout: float = 10.0,
    expect_name_nonempty: bool = True,
    storage_addr: int = renode_harness.STORAGE_ADDR_DEFAULT,
    storage_size: int = renode_harness.STORAGE_SIZE_DEFAULT,
    settle: float = 3.0,
    event_timeout: float = 30.0,  # split relay: re-inject over a wide window (see _assert_key_processed)
    max_attempts: int = 2,
) -> None:
    """usb+wired-split smoke (the orthogonal usb host-link x wired split-link
    combination -- see docs/design/renode-transport-orthogonal.md), with the same
    bounded whole-emulation retry as run_usb_smoke.

    Boots a WIRED split pair (renode_harness.boot_usb_wired_split) whose CENTRAL
    is a real studio-rpc-usb-uart image on the NRF_USBD_Full usb platform, then
    asserts BOTH halves of what makes this combination valuable:

      1. the central answers a core Studio RPC GetDeviceInfo over the emulated
         USB CDC (attach_dual_cdc_bridge, as usb mode) -- Studio RPC on a wired
         split central, which no UART-bound transport can do (both UARTEs are
         taken by console + split link); AND
      2. the wired split link is up -- a keypress injected on the peripheral is
         relayed over the split UART and processed by the central (its keymap
         logs "position: 0" on the central's uart0 console).

    Passing both proves the central simultaneously services Studio over USB and
    the wired split over the UARTEs. The central keeps its console on uart0 (USB
    carries Studio, not console), so the boot banner and the relayed-key log are
    read from the UART console; the USB composite is a single Studio CDC (+ HID).

    WHY the retry (one whole-emulation re-boot): as in run_usb_smoke, the
    wall-clock-paced USB attach/settle can lose a race under heavy host
    contention; a genuine break fails BOTH attempts."""
    studio_pb2 = renode_harness.load_studio_pb2(studio_proto_dir)

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            print(
                f"--- usb+wired smoke attempt {attempt}/{max_attempts} "
                "(fresh emulation; previous attempt failed) ---",
                file=sys.stderr,
            )
        try:
            _run_usb_wired_attempt(
                studio_pb2,
                central_elf=central_elf,
                peripheral_elf=peripheral_elf,
                renode_path=renode_path,
                boot_settle=boot_settle,
                wiring_timeout=wiring_timeout,
                boot_timeout=boot_timeout,
                rpc_timeout=rpc_timeout,
                expect_name_nonempty=expect_name_nonempty,
                storage_addr=storage_addr,
                storage_size=storage_size,
                settle=settle,
                event_timeout=event_timeout,
            )
            if attempt > 1:
                print(f"usb+wired smoke OK on attempt {attempt}", file=sys.stderr)
            return
        except (AssertionError, TimeoutError, OSError) as err:
            last_err = err
            print(
                f"usb+wired smoke attempt {attempt}/{max_attempts} FAILED: {err!r}",
                file=sys.stderr,
            )
    assert last_err is not None
    if isinstance(last_err, AssertionError):
        raise last_err
    raise AssertionError(repr(last_err))


def _run_usb_wired_attempt(
    studio_pb2,
    central_elf: Path,
    peripheral_elf: Path,
    renode_path: str,
    boot_settle: float,
    wiring_timeout: float,
    boot_timeout: float,
    rpc_timeout: float,
    expect_name_nonempty: bool,
    storage_addr: int,
    storage_size: int,
    settle: float,
    event_timeout: float,
) -> None:
    """One usb+wired attempt: boot the pair, assert the central boot banner,
    attach the USB CDC bridge + GetDeviceInfo, then the peripheral->central wired
    relay (see run_usb_wired_smoke)."""
    import random

    port_base = random.randint(26000, 40000)
    print(
        "booting usb+wired split (central on the NRF_USBD_Full usb platform)...",
        file=sys.stderr,
    )
    session, central_console, peripheral_console = renode_harness.boot_usb_wired_split(
        renode_path,
        central_elf=central_elf,
        peripheral_elf=peripheral_elf,
        storage_addr=storage_addr,
        storage_size=storage_size,
        port_base=port_base,
    )
    assert session.mon is not None
    mon = session.mon
    cdc: list = []
    try:
        # CHECK 1/3 connection: the central boots (banner on uart0) AND its USB is
        # enumerated by the host bridge. Attaching the bridge here -- as part of
        # the connection check, before the key injection below -- is LOAD-BEARING:
        # the central is a real USB image that busy-waits in USB init until the
        # host enumerates it, so until the DualCdcAcmBridge attaches the central
        # cannot run its main loop and would never process a relayed key event.
        # (Observed on the main+dya ZMK branch, where that stall is pronounced.)
        print("waiting for central ZMK boot banner (uart0 console)...", file=sys.stderr)
        banner = renode_harness.wait_for_text(
            central_console._sock, "Welcome to ZMK", timeout=boot_timeout
        )
        if "Welcome to ZMK" not in banner:
            raise AssertionError(f"central never saw the ZMK boot banner on uart0; got:\n{banner}")

        # Let the guest finish USB bring-up before the host attaches (a SETUP
        # fired before the guest's INTEN is set is silently lost), then attach the
        # USB CDC bridge and wait for enumeration to wire the Studio CDC.
        t0 = time.monotonic()
        while time.monotonic() - t0 < boot_settle:
            renode_harness.drain_text(central_console._sock, timeout=0.5)
        cdc = list(renode_harness.attach_dual_cdc_bridge(session, port_base + 4, port_base + 5))
        deadline = time.monotonic() + wiring_timeout
        while time.monotonic() < deadline:
            if _mon_flag(mon, f"sysbus.{USB_BRIDGE_NAME}_cdc0 IsWired"):
                break
        else:
            raise AssertionError(
                "USB enumeration never wired the first CDC channel "
                f"(no sysbus.{USB_BRIDGE_NAME}_cdc0 IsWired within {wiring_timeout:.0f}s) "
                "-- is the central a studio-rpc-usb-uart (USB-CDC) image?"
            )
        # console is on uart0 here, so the USB composite is normally a single
        # Studio CDC; auto-detect anyway (an image that also put console on USB
        # would enumerate console first, Studio second).
        dual_cdc = bool(_mon_flag(mon, f"sysbus.{USB_BRIDGE_NAME}_cdc1 IsWired"))
        time.sleep(2.0)
        studio = cdc[1] if dual_cdc else cdc[0]
        print(
            "CHECK 1/3 connection OK (central booted; USB enumerated; wired split link ready)",
            file=sys.stderr,
        )

        # CHECK 2/3 key input: a keypress injected on the PERIPHERAL is relayed
        # over the wired split link and processed by the now-unblocked central
        # ("position: 0" on uart0). Settle first (the split boot-order race -- see
        # boot_split_wired).
        time.sleep(settle)
        _assert_key_processed(
            session,
            central_console,
            machine="peripheral",
            source="the peripheral (relayed over the wired split link)",
            timeout=event_timeout,
        )

        # CHECK 3/3 Studio RPC GetDeviceInfo over the central's USB CDC. Two
        # rounds, as the single-CDC usb smoke does: the device->host pump must
        # deliver more than the first reply per session (regression guard for the
        # DualCdcAcmBridge re-arm fix).
        print("CHECK 3/3 Studio RPC GetDeviceInfo over USB CDC...", file=sys.stderr)
        _assert_get_device_info(studio_pb2, studio, rpc_timeout, expect_name_nonempty, rounds=2)
        print(
            "usb+wired OK (all 3 checks: central boot + USB enum + wired relay "
            f"{renode_harness.KEYPRESS_POSITION_MARKER!r} + Studio RPC over USB CDC)",
            file=sys.stderr,
        )
    finally:
        for sock in cdc:
            sock.close()
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
    studio_proto_dir: Path,
    virtual_budget: float = 20.0,
    wall_budget: float = 300.0,  # keep the single (ble+usb) job under the 15 min CI cap
    storage_addr: int = renode_harness.STORAGE_ADDR_DEFAULT,
    storage_size: int = renode_harness.STORAGE_SIZE_DEFAULT,
    steady_quantum: str | None = None,
    event_timeout: float = 10.0,
) -> None:
    """ble-mode Studio smoke (with host): boot a real ZMK DUT and the
    renode-ble-host app on one Renode BLE medium (fake CCM in both machines), then
    run the three standardized checks over the encrypted BLE link:

      CHECK 1/3 connection -- the host reaches an encrypted link (STAGE:S4).
      CHECK 2/3 key input  -- a keypress injected on the DUT is processed
                              ("position: 0" on the DUT's SEGGER RTT log).
      CHECK 3/3 Studio RPC -- the host completes a real framed GetDeviceInfo
                              round trip over the RPC characteristic (STAGE:S6:
                              write request -> reassemble the indicated response);
                              this Python side parses it and asserts a non-empty
                              device name.

    The host app drives S1-S6 autonomously (scan/connect/pair/read/RPC); this
    function waits for S4 + the S6 round trip to complete, then actively injects
    the keypress. FAILS on any host FAIL marker, the virtual-time budget, or the
    `wall_budget` wall-clock safety net. On failure the tails of both consoles are
    printed. NOT a cryptographic assertion -- the CCM is a shared identity
    transform (see README.md's Studio-over-BLE section).

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
    dut_rtt_buf = ""
    host_buf = ""
    reason = None
    steady_raised = False
    try:
        deadline = time.monotonic() + wall_budget
        vt = 0.0
        while time.monotonic() < deadline:
            host_buf += renode_harness.drain_text(host_console._sock, timeout=0.5)
            dut_buf += renode_harness.drain_text(dut_console._sock, timeout=0.5)
            if session.dut_rtt is not None:
                dut_rtt_buf += renode_harness.drain_text(session.dut_rtt._sock, timeout=0.1)

            # Fail fast if the DUT oopsed / hit an LL assert -- no point waiting out
            # the budget on a corpse (see CRASH_MARKERS).
            crash = next((m for m in CRASH_MARKERS if m in dut_rtt_buf or m in dut_buf), None)
            if crash:
                reason = f"the DUT crashed ({crash!r} -- Renode BLE-controller instability)"
                break

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

            # The host app chains S1..S6 autonomously; the S6 round trip completing
            # (S6_DONE) implies the S4 encrypted link. Require both explicitly.
            if S6_DONE_MARKER in host_buf and BLE_SECURITY_OK in host_buf:
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
        if reason is None and S6_DONE_MARKER in host_buf and BLE_SECURITY_OK in host_buf:
            for ln in stages:
                print(f"  host| {ln}", file=sys.stderr)

            # The three standardized checks. Connection (S4) is already proven by
            # reaching here; then actively inject the keypress, then parse the RPC.
            print(
                f"CHECK 1/3 connection OK (encrypted BLE link up, host S4, vt~{vt:.1f}s)",
                file=sys.stderr,
            )
            if session.dut_rtt is None:
                raise AssertionError(
                    "ble mode: no DUT RTT socket for the key-input check "
                    "(renode_tester RTT logging required)"
                )
            _assert_key_processed(
                session, session.dut_rtt, machine="dut", source="the DUT", timeout=event_timeout
            )
            studio_pb2 = renode_harness.load_studio_pb2(studio_proto_dir)
            _parse_ble_device_info(studio_pb2, host_buf)

            print(
                f"BLE smoke OK (all 3 checks over the encrypted link; "
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
        if session.dut_rtt is not None:
            session.dut_rtt.close()
        host_console.close()
        dut_rpc.close()
        dut_console.close()
        session.stop()


def run_ble_split_smoke(
    central_elf: Path,
    peripheral_elf: Path,
    host_elf: Path,
    renode_path: str,
    studio_proto_dir: Path,
    virtual_budget: float = 45.0,
    wall_budget: float = 240.0,
    storage_addr: int = renode_harness.STORAGE_ADDR_DEFAULT,
    storage_size: int = renode_harness.STORAGE_SIZE_DEFAULT,
    steady_quantum: str | None = None,
    event_timeout: float = 15.0,  # split relay: a few gentle re-injects (see _assert_key_processed)
    max_attempts: int = 8,
    total_wall_budget: float = 360.0,  # HARD cap on the whole smoke (all retries) -- the CI job also
    # spends ~7 min building the 3 images, so keep the smoke ~6 min to fit the 15 min job cap
) -> None:
    """ble-split-mode smoke with a time-bounded whole-emulation retry.

    Runs `_run_ble_split_attempt` (one fresh 3-machine boot) repeatedly until one
    reaches the full chain, up to `max_attempts` or until `total_wall_budget` s
    have elapsed -- whichever comes first -- so the whole smoke (all retries) fits
    the ~15 min CI job cap.

    WHY the retry: the split (peripheral<->central) and host (host<->central)
    links each do an LE Secure Connections pairing, and on Renode's shared BLE
    medium two pairings running close together can cross their SMP DHKey-Check
    PDUs ("Unexpected SMP code 0x0d" -> "in-progress pairing has been deleted" ->
    err 9), and the 3-machine soft link layer at a 10us quantum intermittently
    hits a controller LL assert (lll.c / lll_peripheral.c -> kernel oops). Both
    are transient properties of the emulated radio, NOT firmware regressions (a
    real radio has no cross-talk and meets the LL timing). A fresh emulation
    re-rolls the timing. `_run_ble_split_attempt` BAILS an attempt the instant a
    half crashes (see CRASH_MARKERS), so a bad roll costs ~1-2 min, not the full
    virtual-time budget -- which is what lets several retries fit the cap.
    Sequencing the two pairings by delaying the host does NOT help -- once one
    link is an active connection its events collide with the other's pairing.

    See `_run_ble_split_attempt` for the per-attempt assertions and parameters.
    """
    last_err: AssertionError | None = None
    deadline = time.monotonic() + total_wall_budget
    attempt = 0
    # A fresh attempt needs enough runway to actually reach the chain (~a winning
    # attempt is ~3-4 min wall); don't start one that can't finish in the time left.
    min_runway = 150.0
    while attempt < max_attempts and time.monotonic() < deadline - min_runway:
        attempt += 1
        # Cap this attempt to the remaining total budget, so all retries together
        # never exceed total_wall_budget (the outer deadline is only checked
        # BETWEEN attempts; without this a late attempt could overrun it).
        attempt_wall = min(wall_budget, deadline - time.monotonic())
        if attempt > 1:
            print(
                f"--- ble-split attempt {attempt} "
                "(fresh emulation; previous attempt hit the transient SMP race / LL assert) ---",
                file=sys.stderr,
            )
        try:
            _run_ble_split_attempt(
                central_elf=central_elf,
                peripheral_elf=peripheral_elf,
                host_elf=host_elf,
                renode_path=renode_path,
                studio_proto_dir=studio_proto_dir,
                virtual_budget=virtual_budget,
                wall_budget=attempt_wall,
                storage_addr=storage_addr,
                storage_size=storage_size,
                steady_quantum=steady_quantum,
                event_timeout=event_timeout,
            )
            if attempt > 1:
                print(f"ble-split smoke OK on attempt {attempt}", file=sys.stderr)
            return
        except AssertionError as err:
            last_err = err
            print(f"ble-split attempt {attempt} FAILED: {err}", file=sys.stderr)
    if last_err is None:
        raise AssertionError("ble-split smoke made no attempt (total wall budget too small?)")
    print(
        f"ble-split smoke exhausted {attempt} attempt(s) in "
        f"{total_wall_budget:.0f}s without a clean run (Renode BLE instability)",
        file=sys.stderr,
    )
    raise last_err


def _run_ble_split_attempt(
    central_elf: Path,
    peripheral_elf: Path,
    host_elf: Path,
    renode_path: str,
    studio_proto_dir: Path,
    virtual_budget: float = 45.0,
    wall_budget: float = 300.0,
    storage_addr: int = renode_harness.STORAGE_ADDR_DEFAULT,
    storage_size: int = renode_harness.STORAGE_SIZE_DEFAULT,
    steady_quantum: str | None = None,
    event_timeout: float = 15.0,  # split relay: a few gentle re-injects (see _assert_key_processed)
) -> None:
    """One ble-split attempt: boot a WIRELESS split keyboard (central + peripheral
    halves) and the renode-ble-host on ONE Renode BLE medium (fake CCM in all
    three machines), then run the three standardized checks over the full
    peripheral -> central -> host chain:

      CHECK 1/3 connection -- BOTH encrypted links are up: the split link (the
                              peripheral's RTT shows "Security changed ... level
                              2") AND the host<->central link (host STAGE:S4).
      CHECK 2/3 key input  -- a keypress injected on the PERIPHERAL is relayed
                              over the encrypted split link and processed by the
                              central ("position: 0" on the central's RTT).
      CHECK 3/3 Studio RPC -- the host completes a real framed GetDeviceInfo round
                              trip against the CENTRAL (STAGE:S6); parsed here.

    Passing all three proves the whole chain end to end: the same central holds
    the encrypted split link to the peripheral (whose key it relays) AND serves
    Studio RPC to the host. Also asserts 0 radio "trimming" warnings on either
    link (the DLE-27 cap on both halves + host keeps every on-air PDU within
    Renode's 31-byte radio cap).

    FAILS on the virtual-time budget or the `wall_budget` wall-clock safety net,
    or if the host completes the RPC but the split link never secured (the central
    served Studio without the peripheral -- not the full chain). Transient pairing
    "Security failed" markers are NOT fatal (the links rescan and retry). On
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
    crtt_buf = ""
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
            if session.central_rtt is not None:
                crtt_buf += renode_harness.drain_text(session.central_rtt._sock, timeout=0.1)

            # Fail fast: if either half oopsed / hit an LL assert, this attempt is
            # a corpse -- bail now and let the whole-emulation retry re-roll,
            # rather than running to the virtual-time budget (~20 min wall).
            crash = next(
                (m for m in CRASH_MARKERS if m in rtt_buf or m in crtt_buf or m in central_buf),
                None,
            )
            if crash:
                reason = (
                    f"a split half crashed ({crash!r} -- transient Renode BLE-controller "
                    "instability); bailing early to retry"
                )
                break

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

            # The host chains S1..S6; S6_DONE (the framed GetDeviceInfo round trip)
            # implies its S4 encrypted link. Require it plus the secured split link.
            if (
                S6_DONE_MARKER in host_buf
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

        host_reached = S6_DONE_MARKER in host_buf and BLE_SECURITY_OK in host_buf
        split_ok = split_l2_at is not None
        stages = [ln.strip() for ln in host_buf.splitlines() if "STAGE:" in ln]

        retry_note = ""
        if split_fail_seen or host_fail_seen:
            which = ", ".join(
                w for w, seen in (("split", split_fail_seen), ("host", host_fail_seen)) if seen
            )
            retry_note = f" (recovered after transient pairing retry on: {which})"

        # Success requires BOTH: the split link secured AND the host RPC round trip.
        if reason is None and host_reached and split_ok:
            for ln in stages:
                print(f"  host| {ln}", file=sys.stderr)

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

            # The three standardized checks. Connection (both encrypted links) is
            # already proven; then actively inject the peripheral keypress and
            # parse the RPC response.
            print(
                f"CHECK 1/3 connection OK (split link L2 vt~{split_l2_at:.1f}s + host S4"
                f"{retry_note})",
                file=sys.stderr,
            )
            if session.central_rtt is None:
                raise AssertionError(
                    "ble-split mode: no central RTT socket for the key-input check "
                    "(renode_split_left.conf RTT logging required)"
                )
            _assert_key_processed(
                session,
                session.central_rtt,
                machine="peripheral",
                source="the peripheral (relayed over the encrypted split link)",
                timeout=event_timeout,
            )
            studio_pb2 = renode_harness.load_studio_pb2(studio_proto_dir)
            _parse_ble_device_info(studio_pb2, host_buf)

            print(
                f"BLE-split smoke OK (all 3 checks: peripheral->central->host chain; "
                f"radio 'trimming' warnings: {trimming})",
                file=sys.stderr,
            )
            return

        if reason is None and host_reached and not split_ok:
            reason = (
                "host completed the Studio RPC round trip but the split link never "
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
        if getattr(session, "central_rtt", None) is not None:
            session.central_rtt.close()
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
        help="DUT firmware ELF (all modes; the CENTRAL half in wired-split mode).",
    )
    ap.add_argument(
        "--mode",
        choices=tuple(MODE_PRESETS),
        default=None,
        help="Backward-compatible preset expanding to a (host-link, split-link) pair "
        "(default, no flags: ble). ble: real hardware image; with --host-elf a full "
        "encrypted Studio-over-BLE read (S4/S5), without it a boot-liveness check. usb: "
        "the SAME real image, Studio GetDeviceInfo over the emulated USB CDC. ble-split: "
        "a wireless split -- --elf is the split CENTRAL, --peripheral-elf the split "
        "PERIPHERAL, --host-elf the host. wired-split: wired-split central (--elf) + "
        "--peripheral-elf on a Renode UART hub; a Studio GetDeviceInfo round trip over "
        "the central's USB CDC + a peripheral keypress relayed over the wired link to the "
        "central. Mutually exclusive with --host-link/--split-link.",
    )
    ap.add_argument(
        "--host-link",
        choices=HOST_LINKS,
        default=None,
        help="How the central answers Studio RPC: usb (emulated USB CDC), ble (emulated "
        "BLE GATT), none (boot-liveness only). Mutually exclusive with --mode. See "
        "docs/design/renode-transport-orthogonal.md.",
    )
    ap.add_argument(
        "--split-link",
        choices=SPLIT_LINKS,
        default=None,
        help="How the central reaches the peripheral: none (not a split), wired (UART "
        "hub), ble (radio + fake CCM). Mutually exclusive with --mode.",
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
        help="wired-split / ble-split mode: the split PERIPHERAL half's firmware ELF (--elf "
        "is the CENTRAL half).",
    )
    ap.add_argument(
        "--studio-proto-dir",
        type=Path,
        help="usb / usb+wired mode: path to zmk-studio-messages' proto/zmk dir "
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
        help="ble / usb mode: NVS storage_partition address to preload as erased 0xFF "
        "(default: 0xec000, xiao_ble).",
    )
    adv.add_argument(
        "--storage-size",
        type=lambda s: int(s, 0),
        default=renode_harness.STORAGE_SIZE_DEFAULT,
        help="ble / usb mode: NVS storage_partition size (default: 0x8000, xiao_ble).",
    )
    args = ap.parse_args(argv)

    if not args.elf.is_file():
        print(f"ELF not found: {args.elf}", file=sys.stderr)
        return 2

    try:
        host, split = resolve_links(args.mode, args.host_link, args.split_link)
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return 2
    print(
        f"host-link={host} x split-link={split} ({canonical_mode(host, split)})", file=sys.stderr
    )

    if args.host_elf is not None and host != "ble":
        print("--host-elf is only valid with a ble host-link", file=sys.stderr)
        return 2
    if args.peripheral_elf is not None and split == "none":
        print("--peripheral-elf is only valid with a wired/ble split-link", file=sys.stderr)
        return 2
    if split != "none":
        if args.peripheral_elf is None:
            print(f"split-link={split} requires --peripheral-elf", file=sys.stderr)
            return 2
        if not args.peripheral_elf.is_file():
            print(f"peripheral ELF not found: {args.peripheral_elf}", file=sys.stderr)
            return 2

    renode_path = renode_harness.find_or_install_renode(version=args.renode_version)
    if renode_path is None:
        print("Renode is not installed and could not be auto-installed", file=sys.stderr)
        return 2

    # The usb host-link (usb and usb+wired) needs the Studio protos.
    def _proto_dir():
        proto_dir = args.studio_proto_dir
        if proto_dir is None:
            if not args.west_topdir:
                print("either --studio-proto-dir or --west-topdir is required", file=sys.stderr)
                return None
            proto_dir = renode_harness.find_studio_proto_dir(args.west_topdir)
        return proto_dir

    try:
        if (host, split) == ("none", "wired"):
            run_split_smoke(
                central_elf=args.elf,
                peripheral_elf=args.peripheral_elf,
                renode_path=renode_path,
                boot_timeout=args.boot_timeout,
            )
        elif (host, split) == ("ble", "ble"):
            if args.host_elf is None or not args.host_elf.is_file():
                print(
                    f"--host-elf is required for ble x ble; not found: {args.host_elf}",
                    file=sys.stderr,
                )
                return 2
            proto_dir = _proto_dir()  # needed to parse the S6 GetDeviceInfo response
            if proto_dir is None:
                return 2
            run_ble_split_smoke(
                central_elf=args.elf,
                peripheral_elf=args.peripheral_elf,
                host_elf=args.host_elf,
                renode_path=renode_path,
                studio_proto_dir=proto_dir,
                virtual_budget=args.virtual_budget,
                storage_addr=args.storage_addr,
                storage_size=args.storage_size,
                steady_quantum=args.steady_quantum,
            )
        elif (host, split) == ("usb", "wired"):
            proto_dir = _proto_dir()
            if proto_dir is None:
                return 2
            run_usb_wired_smoke(
                central_elf=args.elf,
                peripheral_elf=args.peripheral_elf,
                renode_path=renode_path,
                studio_proto_dir=proto_dir,
                boot_timeout=args.boot_timeout,
                rpc_timeout=args.rpc_timeout,
                storage_addr=args.storage_addr,
                storage_size=args.storage_size,
            )
        elif (host, split) == ("ble", "none"):
            if args.host_elf is not None:
                if not args.host_elf.is_file():
                    print(f"host ELF not found: {args.host_elf}", file=sys.stderr)
                    return 2
                proto_dir = _proto_dir()  # needed to parse the S6 GetDeviceInfo response
                if proto_dir is None:
                    return 2
                run_ble_studio_smoke(
                    dut_elf=args.elf,
                    host_elf=args.host_elf,
                    renode_path=renode_path,
                    studio_proto_dir=proto_dir,
                    virtual_budget=args.virtual_budget,
                    storage_addr=args.storage_addr,
                    storage_size=args.storage_size,
                    steady_quantum=args.steady_quantum,
                )
            else:
                print(
                    "ble host-link without --host-elf: checking DUT boot liveness only "
                    "(no encrypted Studio read).",
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
        elif (host, split) == ("usb", "none"):
            proto_dir = _proto_dir()
            if proto_dir is None:
                return 2
            run_usb_smoke(
                elf=args.elf,
                renode_path=renode_path,
                studio_proto_dir=proto_dir,
                boot_timeout=args.boot_timeout,
                rpc_timeout=args.rpc_timeout,
                storage_addr=args.storage_addr,
                storage_size=args.storage_size,
            )
        else:  # unreachable: resolve_links already gated SUPPORTED_LINKS
            print(
                f"unsupported combination host-link={host} x split-link={split}", file=sys.stderr
            )
            return 2
    except AssertionError as err:
        print(f"SMOKE TEST FAILED: {err}", file=sys.stderr)
        return 1

    print("SMOKE TEST OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
