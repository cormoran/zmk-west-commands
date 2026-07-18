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
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(SCRIPTS_DIR))
import renode_harness  # noqa: E402


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
    args = ap.parse_args(argv)

    if not args.elf.is_file():
        print(f"ELF not found: {args.elf}", file=sys.stderr)
        return 2

    proto_dir = None
    if not args.no_rpc:
        proto_dir = args.studio_proto_dir
        if proto_dir is None:
            if not args.west_topdir:
                print("either --studio-proto-dir or --west-topdir is required", file=sys.stderr)
                return 2
            proto_dir = renode_harness.find_studio_proto_dir(args.west_topdir)

    renode_path = renode_harness.find_or_install_renode(version=args.renode_version)
    if renode_path is None:
        print("Renode is not installed and could not be auto-installed", file=sys.stderr)
        return 2

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
