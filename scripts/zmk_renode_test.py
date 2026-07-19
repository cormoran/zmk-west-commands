"""`west zmk-renode-test` -- boot a built ZMK ELF in the Renode emulator, run
the generic boot + core Studio RPC smoke test, then a module's own
`tests/renode/*_test.py` files.

This never builds firmware: the ELF is always built by the caller (typically
a `build.yaml` artifact using the `renode-studio-uart` snippet this repo
provides as a Zephyr module). See README.md and the harness under
`scripts/lib/renode/` (renode_harness.py, references in cormoran/zmk-workspace's
skills/test-zmk-renode for the underlying Renode gotchas).
"""

import os
import subprocess
import sys
from pathlib import Path

from west import log
from west.commands import WestCommand
from west.manifest import Manifest
from west.util import west_topdir

# scripts/lib/renode holds the harness (renode_harness.py, renode_smoke.py,
# rpc_client.py), install_renode.sh and platforms/. It is put on PYTHONPATH
# for the module's own test files too -- they do `import renode_harness`.
LIB_RENODE_DIR = Path(__file__).resolve().parent / "lib" / "renode"


class ZMKRenodeTest(WestCommand):
    """Run ZMK Renode emulator tests against a pre-built firmware ELF."""

    def __init__(self):
        super().__init__(
            name="zmk-renode-test",
            help="run ZMK Renode emulator tests against a pre-built ELF",
            description=(
                "Boot a caller-built ZMK firmware ELF in the Renode emulator, run a "
                "generic boot + core Studio RPC smoke test, then the module's own "
                "tests/renode/*_test.py files. Does not build firmware."
            ),
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        parser.add_argument(
            "tests_dir",
            nargs="?",
            help=(
                "Directory with the module's own *_test.py files, run non-recursively "
                "via `python3 <file> -v` with PYTHONPATH and ZMK_RENODE_ELF set. "
                "Omit to run only the generic smoke test."
            ),
        )
        parser.add_argument(
            "--elf",
            required=True,
            help="Path to the firmware ELF to test (built by the caller).",
        )
        parser.add_argument(
            "--renode-version",
            default="1.16.1",
            help="Renode portable release version to install/use (default: 1.16.1).",
        )
        parser.add_argument(
            "--boot-timeout",
            type=float,
            default=20.0,
            help="Seconds to wait for the ZMK boot banner (default: 20).",
        )
        parser.add_argument(
            "--skip-smoke",
            action="store_true",
            help="Skip the generic boot + Studio RPC smoke test.",
        )
        parser.add_argument(
            "--no-rpc",
            action="store_true",
            help="Smoke test checks the boot banner only (for modules without Studio RPC).",
        )
        parser.add_argument(
            "--real-binary",
            action="store_true",
            help=(
                "Boot a real flashable image (ZMK's studio-rpc-usb-uart snippet: USB CDC + "
                "QSPI + BLE) on the real-binary platform. The generic smoke becomes a "
                "liveness check (run >= --min-virtual virtual seconds, then PC-symbol "
                "sampling -- no UART Studio transport exists in a real image). See README."
            ),
        )
        parser.add_argument(
            "--min-virtual",
            type=float,
            default=20.0,
            help="Real-binary mode: virtual seconds to run before PC sampling (default: 20).",
        )
        parser.add_argument(
            "--rtt",
            action="store_true",
            help=(
                "Real-binary mode: capture Zephyr SEGGER RTT log output during the "
                "liveness run and fail on RTT fatal lines (for RTT-logging builds: "
                "CONFIG_LOG + CONFIG_USE_SEGGER_RTT + CONFIG_LOG_BACKEND_RTT). See README."
            ),
        )
        parser.add_argument(
            "--ble",
            action="store_true",
            help=(
                "Studio-over-BLE mode: boot the real DUT (--elf) and the renode-ble-host "
                "app (--host-elf) on one BLE medium and assert an encrypted Studio RPC read "
                "(fake CCM; ~6-7 min wall). Implies a real DUT image. See README."
            ),
        )
        parser.add_argument(
            "--host-elf",
            help=(
                "BLE mode: the renode-ble-host app ELF (build it with `west build -b "
                "nrf52840dk/nrf52840 -s <this repo>/renode-ble-host`). Required with --ble."
            ),
        )
        parser.add_argument(
            "--ble-virtual-budget",
            type=float,
            default=20.0,
            help="BLE mode: virtual seconds to reach the encrypted read before failing "
            "(default: 20; ~3.3s is typical).",
        )
        parser.add_argument(
            "--ble-steady-quantum",
            default=None,
            help="BLE mode: after the encrypted link is up (S4), raise the global "
            "time-sync quantum to this value (e.g. 0.001) for the steady-state phase "
            "(~7x faster; pairing still needs the 10us boot quantum). For long BLE "
            "tests; the smoke itself exits at S5 so it gains little. See README.",
        )
        parser.add_argument(
            "--storage-addr",
            type=lambda s: int(s, 0),
            default=None,
            help=(
                "Real-binary/BLE mode: NVS storage_partition address preloaded as erased 0xFF "
                "(default: 0xec000, xiao_ble)."
            ),
        )
        parser.add_argument(
            "--storage-size",
            type=lambda s: int(s, 0),
            default=None,
            help="Real-binary mode: NVS storage_partition size (default: 0x8000, xiao_ble).",
        )
        return parser

    def do_run(self, args, unknown_args):
        elf = Path(args.elf).absolute()
        if not elf.is_file():
            log.die(
                f"ELF not found: {elf} (this command does not build firmware -- build it "
                "first, e.g. `west zmk-build <zmk-config> -af <artifact>`)"
            )

        # Make the harness (and the module's own tests) importable.
        sys.path.insert(0, str(LIB_RENODE_DIR))
        import renode_harness  # noqa: E402

        renode_path = renode_harness.find_or_install_renode(version=args.renode_version)
        if renode_path is None:
            log.die("Renode is not installed and could not be auto-installed.")
        log.inf(f"[*] Renode: {renode_path}")

        host_elf = None
        if args.ble:
            if not args.host_elf:
                log.die("--ble requires --host-elf <renode-ble-host ELF> (see README).")
            host_elf = Path(args.host_elf).absolute()
            if not host_elf.is_file():
                log.die(f"host ELF not found: {host_elf}")

        if not args.skip_smoke:
            if args.ble:
                self._run_ble_smoke(args, elf, host_elf, renode_path)
            elif args.real_binary:
                self._run_liveness_smoke(args, elf, renode_path)
            else:
                self._run_smoke(args, elf, renode_path)
        else:
            log.inf("[*] Skipping generic smoke test (--skip-smoke)")

        if args.tests_dir:
            self._run_module_tests(args, elf)

    def _run_ble_smoke(self, args, elf: Path, host_elf: Path, renode_path: str) -> None:
        import renode_smoke  # noqa: E402

        kwargs = {}
        if args.storage_addr is not None:
            kwargs["storage_addr"] = args.storage_addr
        if args.storage_size is not None:
            kwargs["storage_size"] = args.storage_size
        if getattr(args, "ble_steady_quantum", None):
            kwargs["steady_quantum"] = args.ble_steady_quantum

        log.inf("[*] Running Studio-over-BLE smoke test (real DUT + renode-ble-host)")
        try:
            renode_smoke.run_ble_smoke(
                dut_elf=elf,
                host_elf=host_elf,
                renode_path=renode_path,
                virtual_budget=args.ble_virtual_budget,
                **kwargs,
            )
        except AssertionError as err:
            log.die(f"BLE smoke test FAILED: {err}")
        log.inf("[*] BLE smoke test OK")

    def _run_liveness_smoke(self, args, elf: Path, renode_path: str) -> None:
        import renode_smoke  # noqa: E402

        kwargs = {}
        if args.storage_addr is not None:
            kwargs["storage_addr"] = args.storage_addr
        if args.storage_size is not None:
            kwargs["storage_size"] = args.storage_size

        log.inf("[*] Running real-binary liveness smoke test")
        try:
            renode_smoke.run_liveness_smoke(
                elf=elf,
                renode_path=renode_path,
                min_virtual=args.min_virtual,
                rtt=args.rtt,
                **kwargs,
            )
        except AssertionError as err:
            log.die(f"liveness smoke test FAILED: {err}")
        log.inf("[*] Liveness smoke test OK")

    def _run_smoke(self, args, elf: Path, renode_path: str) -> None:
        import renode_harness  # noqa: E402
        import renode_smoke  # noqa: E402

        proto_dir = None
        if not args.no_rpc:
            # protobuf is a runtime dep only for the RPC round trip.
            try:
                import google.protobuf  # noqa: F401
            except ImportError:
                log.die(
                    "the `protobuf` Python package is required for the Studio RPC smoke "
                    "test -- install it (see requirements-test.txt) or pass --no-rpc / "
                    "--skip-smoke."
                )
            proto_dir = self._find_studio_proto_dir(renode_harness)

        log.inf("[*] Running generic Renode smoke test")
        try:
            renode_smoke.run_smoke(
                elf=elf,
                renode_path=renode_path,
                studio_proto_dir=proto_dir,
                check_rpc=not args.no_rpc,
                boot_timeout=args.boot_timeout,
            )
        except AssertionError as err:
            log.die(f"smoke test FAILED: {err}")
        log.inf("[*] Smoke test OK")

    def _find_studio_proto_dir(self, renode_harness) -> Path:
        """Resolve zmk-studio-messages' proto/zmk dir. Prefer the west
        manifest (same pattern zmk_test.py uses for `zmk`); fall back to the
        harness's recursive glob under the west topdir."""
        topdir = Path(west_topdir())
        try:
            manifest = Manifest.from_topdir()
            project = next(filter(lambda p: p.name == "zmk-studio-messages", manifest.projects))
            candidate = Path(project.abspath) / "proto" / "zmk"
            if candidate.is_dir():
                log.inf(f"[*] Studio protos: {candidate} (from manifest)")
                return candidate
            log.wrn(
                f"zmk-studio-messages resolved to {project.abspath} but "
                f"{candidate} does not exist; falling back to a recursive search."
            )
        except StopIteration:
            log.wrn(
                "zmk-studio-messages not found in the west manifest; "
                "falling back to a recursive search under the workspace."
            )

        proto_dir = renode_harness.find_studio_proto_dir(topdir)
        log.inf(f"[*] Studio protos: {proto_dir} (from search)")
        return proto_dir

    def _run_module_tests(self, args, elf: Path) -> None:
        tests_dir = Path(args.tests_dir).absolute()
        if not tests_dir.is_dir():
            log.die(f"tests_dir is not a directory: {tests_dir}")

        test_files = sorted(tests_dir.glob("*_test.py"))
        if not test_files:
            log.wrn(f"no *_test.py files found directly under {tests_dir}")
            return

        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(LIB_RENODE_DIR) + (os.pathsep + existing if existing else "")
        env["ZMK_RENODE_ELF"] = str(elf)
        if args.real_binary or args.ble:
            # Tell the module's own tests to build real-binary machines
            # (renode_harness.boot_single_real / boot_ble_pair) rather than the
            # UART-RPC one, and honor the same storage-partition overrides.
            import renode_harness  # noqa: E402

            env["ZMK_RENODE_REAL"] = "1"
            addr = (
                args.storage_addr
                if args.storage_addr is not None
                else renode_harness.STORAGE_ADDR_DEFAULT
            )
            size = (
                args.storage_size
                if args.storage_size is not None
                else renode_harness.STORAGE_SIZE_DEFAULT
            )
            env["ZMK_RENODE_STORAGE_ADDR"] = hex(addr)
            env["ZMK_RENODE_STORAGE_SIZE"] = hex(size)
        if args.ble:
            # BLE mode contract: a module's own tests build a two-machine
            # renode_harness.boot_ble_pair(dut_elf=ZMK_RENODE_ELF,
            # host_elf=ZMK_RENODE_HOST_ELF).
            env["ZMK_RENODE_BLE"] = "1"
            env["ZMK_RENODE_HOST_ELF"] = str(Path(args.host_elf).absolute())

        failures = []
        for test_file in test_files:
            log.inf(f"[*] Running {test_file}")
            proc = subprocess.run(
                [sys.executable, str(test_file), "-v"],
                env=env,
            )
            if proc.returncode != 0:
                failures.append(test_file)
                log.err(f"[*] FAILED: {test_file} (exit {proc.returncode})")

        if failures:
            log.die("module Renode tests failed: " + ", ".join(str(f) for f in failures))
        log.inf("[*] All module Renode tests passed")
