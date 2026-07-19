"""`west zmk-renode-test` -- boot a built ZMK ELF in the Renode emulator, run a
boot + Studio smoke test, then a module's own `tests/renode/*_test.py` files.

Four modes (`--mode`, default `ble`):

  * **ble** (default) -- the DUT is the exact `studio-rpc-usb-uart` *hardware*
    image, with no extra module config; platform stubs make it boot. With
    `--host-elf`, the `renode-ble-host` app pairs over an emulated BLE medium and
    does an encrypted Studio GATT read (S4/S5). Without `--host-elf`, it degrades
    to a boot-liveness check.
  * **uart** -- the DUT is built with this repo's `renode-studio-uart` snippet;
    console + Studio RPC ride emulated UARTs. Smoke = boot banner + a core
    Studio GetDeviceInfo round trip.
  * **split** -- a WIRED split pair: `--elf` is the central half and
    `--peripheral-elf` the peripheral half, booted as two machines whose split
    links (uart1) are cross-connected via a Renode UART hub. Smoke = BOTH halves
    reach the boot banner AND a keypress injected on the peripheral is relayed
    over the wired link and processed by the central. No Studio RPC (the two
    nRF52840 UARTEs are consumed by console + split link).
  * **ble-split** -- a WIRELESS split: three real images on one BLE medium.
    `--elf` is the split CENTRAL half (Studio), `--peripheral-elf` the split
    PERIPHERAL half, `--host-elf` the host. Smoke = the encrypted split link
    comes up (peripheral -> central L2) THEN the host reads Studio through the
    central (peripheral -> central -> host).

This never builds firmware: the ELF is always built by the caller. See
README.md, docs/renode-testing.md, and the harness under `scripts/lib/renode/`
(renode_harness.py).
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
                "boot + Studio smoke test, then the module's own tests/renode/*_test.py "
                "files. Four modes: --mode ble (default, the real hardware image with no "
                "extra config, Studio over emulated BLE), --mode uart (snippet-built DUT "
                "over emulated UARTs), --mode split (wired-split central + --peripheral-elf "
                "on a Renode UART hub), and --mode ble-split (wireless split central + "
                "--peripheral-elf + --host-elf on one BLE medium). Does not build firmware."
            ),
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        parser.add_argument(
            "tests_dir",
            nargs="?",
            help=(
                "Directory with the module's own *_test.py files, run non-recursively "
                "via `python3 <file> -v` with PYTHONPATH and the ZMK_RENODE_* env "
                "contract set. Omit to run only the smoke test."
            ),
        )
        parser.add_argument(
            "--elf",
            required=True,
            help=(
                "Path to the DUT firmware ELF to test (built by the caller). All modes; "
                "the CENTRAL half in split mode."
            ),
        )
        parser.add_argument(
            "--mode",
            choices=("uart", "ble", "split", "ble-split"),
            default="ble",
            help=(
                "ble (default): the real hardware image with no extra config; with "
                "--host-elf the renode-ble-host app pairs and does an encrypted Studio "
                "GATT read (S4/S5), without it a boot-liveness check. "
                "ble-split: a WIRELESS split -- --elf is the split CENTRAL half, "
                "--peripheral-elf the split PERIPHERAL half, --host-elf the host; the "
                "smoke asserts the encrypted split link comes up THEN the host reads "
                "Studio through the central (peripheral -> central -> host). "
                "uart: snippet-built DUT, console + Studio RPC over emulated UARTs; smoke = "
                "boot banner + GetDeviceInfo. "
                "split: wired-split central (--elf) + --peripheral-elf on a Renode UART "
                "hub; smoke = both boot banners + a peripheral keypress relayed to the "
                "central. See docs/renode-testing.md."
            ),
        )
        parser.add_argument(
            "--peripheral-elf",
            help=(
                "split / ble-split mode: the split PERIPHERAL half's firmware ELF (--elf is "
                "the CENTRAL half). Required for both split modes."
            ),
        )
        parser.add_argument(
            "--host-elf",
            help=(
                "ble / ble-split mode: the renode-ble-host app ELF (build it with `west "
                "build -b nrf52840dk/nrf52840 -s <this repo>/renode-ble-host`). In ble "
                "mode: given -> full S4/S5 Studio-over-BLE smoke, omitted -> boot-liveness "
                "only. Required for ble-split mode."
            ),
        )
        parser.add_argument(
            "--no-rpc",
            action="store_true",
            help="uart mode: smoke checks the boot banner only (for modules without Studio RPC).",
        )
        parser.add_argument(
            "--boot-timeout",
            type=float,
            default=20.0,
            help="uart mode: seconds to wait for the ZMK boot banner (default: 20).",
        )
        parser.add_argument(
            "--skip-smoke",
            action="store_true",
            help="Skip the smoke test; run only the module's own tests_dir.",
        )

        # Advanced knobs -- rarely needed; see docs/renode-testing.md and
        # docs/renode-internals.md. Kept out of the common story on purpose.
        adv = parser.add_argument_group(
            "advanced", "rarely-needed knobs (see docs/renode-testing.md)"
        )
        adv.add_argument(
            "--rtt",
            action="store_true",
            help=(
                "ble mode (liveness, no --host-elf): capture Zephyr SEGGER RTT log output "
                "and fail on RTT fatal lines (RTT-logging builds: CONFIG_LOG + "
                "CONFIG_USE_SEGGER_RTT + CONFIG_LOG_BACKEND_RTT)."
            ),
        )
        adv.add_argument(
            "--min-virtual",
            type=float,
            default=20.0,
            help="ble mode (liveness): virtual seconds to run before PC sampling (default: 20).",
        )
        adv.add_argument(
            "--virtual-budget",
            type=float,
            default=20.0,
            help="ble mode (with --host-elf): virtual seconds to reach the encrypted read "
            "before failing (default: 20; ~3.3s is typical).",
        )
        adv.add_argument(
            "--steady-quantum",
            default=None,
            help="ble mode (with --host-elf): after the encrypted link is up (S4), raise "
            "the global time-sync quantum to this value (e.g. 0.001) for the steady-state "
            "phase (~7x faster; pairing still needs the 10us boot quantum). Mainly for a "
            "module's own long BLE tests. See docs/renode-testing.md.",
        )
        adv.add_argument(
            "--storage-addr",
            type=lambda s: int(s, 0),
            default=None,
            help="ble mode: NVS storage_partition address preloaded as erased 0xFF "
            "(default: 0xec000, xiao_ble).",
        )
        adv.add_argument(
            "--storage-size",
            type=lambda s: int(s, 0),
            default=None,
            help="ble mode: NVS storage_partition size (default: 0x8000, xiao_ble).",
        )
        adv.add_argument(
            "--renode-version",
            default="1.16.1",
            help="Renode portable release version to install/use (default: 1.16.1).",
        )
        return parser

    def do_run(self, args, unknown_args):
        elf = Path(args.elf).absolute()
        if not elf.is_file():
            log.die(
                f"ELF not found: {elf} (this command does not build firmware -- build it "
                "first, e.g. `west zmk-build <zmk-config> -af <artifact>`)"
            )

        if args.host_elf and args.mode not in ("ble", "ble-split"):
            log.die("--host-elf is only valid with --mode ble / ble-split.")
        if args.peripheral_elf and args.mode not in ("split", "ble-split"):
            log.die("--peripheral-elf is only valid with --mode split / ble-split.")

        # Make the harness (and the module's own tests) importable.
        sys.path.insert(0, str(LIB_RENODE_DIR))
        import renode_harness  # noqa: E402

        renode_path = renode_harness.find_or_install_renode(version=args.renode_version)
        if renode_path is None:
            log.die("Renode is not installed and could not be auto-installed.")
        log.inf(f"[*] Renode: {renode_path}")

        host_elf = None
        if args.mode in ("ble", "ble-split") and args.host_elf:
            host_elf = Path(args.host_elf).absolute()
            if not host_elf.is_file():
                log.die(f"host ELF not found: {host_elf}")

        peripheral_elf = None
        if args.mode in ("split", "ble-split"):
            if not args.peripheral_elf:
                log.die(
                    f"--mode {args.mode} requires --peripheral-elf (the peripheral half's ELF)."
                )
            peripheral_elf = Path(args.peripheral_elf).absolute()
            if not peripheral_elf.is_file():
                log.die(
                    f"peripheral ELF not found: {peripheral_elf} (this command does not "
                    "build firmware -- build it first)"
                )
            if args.mode == "ble-split" and host_elf is None:
                log.die("--host-elf is required for --mode ble-split.")

        if not args.skip_smoke:
            if args.mode == "ble-split":
                self._run_ble_split_smoke(args, elf, peripheral_elf, host_elf, renode_path)
            elif args.mode == "ble":
                if host_elf is not None:
                    self._run_ble_studio_smoke(args, elf, host_elf, renode_path)
                else:
                    log.inf(
                        "[*] ble mode without --host-elf: no host given, checking DUT "
                        "boot liveness only (no encrypted Studio read)."
                    )
                    self._run_ble_liveness_smoke(args, elf, renode_path)
            elif args.mode == "split":
                self._run_split_smoke(args, elf, peripheral_elf, renode_path)
            else:
                self._run_uart_smoke(args, elf, renode_path)
        else:
            log.inf("[*] Skipping smoke test (--skip-smoke)")

        if args.tests_dir:
            self._run_module_tests(args, elf)

    def _run_ble_studio_smoke(self, args, elf: Path, host_elf: Path, renode_path: str) -> None:
        import renode_smoke  # noqa: E402

        kwargs = {}
        if args.storage_addr is not None:
            kwargs["storage_addr"] = args.storage_addr
        if args.storage_size is not None:
            kwargs["storage_size"] = args.storage_size
        if getattr(args, "steady_quantum", None):
            kwargs["steady_quantum"] = args.steady_quantum

        log.inf("[*] Running Studio-over-BLE smoke test (real DUT + renode-ble-host)")
        try:
            renode_smoke.run_ble_studio_smoke(
                dut_elf=elf,
                host_elf=host_elf,
                renode_path=renode_path,
                virtual_budget=args.virtual_budget,
                **kwargs,
            )
        except AssertionError as err:
            log.die(f"BLE smoke test FAILED: {err}")
        log.inf("[*] BLE smoke test OK")

    def _run_ble_split_smoke(
        self, args, central_elf: Path, peripheral_elf: Path, host_elf: Path, renode_path: str
    ) -> None:
        import renode_smoke  # noqa: E402

        kwargs = {}
        if args.storage_addr is not None:
            kwargs["storage_addr"] = args.storage_addr
        if args.storage_size is not None:
            kwargs["storage_size"] = args.storage_size
        if getattr(args, "steady_quantum", None):
            kwargs["steady_quantum"] = args.steady_quantum

        log.inf(
            "[*] Running BLE-split smoke test (split central + peripheral + renode-ble-host; "
            "asserts split link L2 then host Studio read)"
        )
        try:
            renode_smoke.run_ble_split_smoke(
                central_elf=central_elf,
                peripheral_elf=peripheral_elf,
                host_elf=host_elf,
                renode_path=renode_path,
                virtual_budget=max(args.virtual_budget, 40.0),
                **kwargs,
            )
        except AssertionError as err:
            log.die(f"BLE-split smoke test FAILED: {err}")
        log.inf("[*] BLE-split smoke test OK")

    def _run_ble_liveness_smoke(self, args, elf: Path, renode_path: str) -> None:
        import renode_smoke  # noqa: E402

        kwargs = {}
        if args.storage_addr is not None:
            kwargs["storage_addr"] = args.storage_addr
        if args.storage_size is not None:
            kwargs["storage_size"] = args.storage_size

        log.inf("[*] Running ble-mode boot-liveness smoke test")
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

    def _run_uart_smoke(self, args, elf: Path, renode_path: str) -> None:
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

        log.inf("[*] Running uart-mode Renode smoke test")
        try:
            renode_smoke.run_uart_smoke(
                elf=elf,
                renode_path=renode_path,
                studio_proto_dir=proto_dir,
                check_rpc=not args.no_rpc,
                boot_timeout=args.boot_timeout,
            )
        except AssertionError as err:
            log.die(f"smoke test FAILED: {err}")
        log.inf("[*] Smoke test OK")

    def _run_split_smoke(
        self, args, central_elf: Path, peripheral_elf: Path, renode_path: str
    ) -> None:
        import renode_smoke  # noqa: E402

        log.inf("[*] Running wired-split Renode smoke test (central + peripheral)")
        try:
            renode_smoke.run_split_smoke(
                central_elf=central_elf,
                peripheral_elf=peripheral_elf,
                renode_path=renode_path,
                boot_timeout=args.boot_timeout,
            )
        except AssertionError as err:
            log.die(f"split smoke test FAILED: {err}")
        log.inf("[*] Split smoke test OK")

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
        # Module-test env contract (see docs/renode-testing.md):
        #   ZMK_RENODE_MODE  = uart | ble  (which harness a test should build)
        #   ZMK_RENODE_ELF   = the DUT ELF
        # ble mode also exports the storage-partition overrides and, when a host
        # was given, ZMK_RENODE_HOST_ELF for renode_harness.boot_ble_pair().
        env["ZMK_RENODE_MODE"] = args.mode
        env["ZMK_RENODE_ELF"] = str(elf)
        if args.mode == "split":
            # split-mode tests build a wired pair via renode_harness.boot_split_wired;
            # --elf is the central, ZMK_RENODE_PERIPHERAL_ELF the peripheral half.
            env["ZMK_RENODE_PERIPHERAL_ELF"] = str(Path(args.peripheral_elf).absolute())
        if args.mode in ("ble", "ble-split"):
            # ble-mode tests build a real image via renode_harness.boot_single_real
            # (liveness), boot_ble_pair (two-machine) or boot_ble_split (three-
            # machine), honoring these overrides.
            import renode_harness  # noqa: E402

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
            if args.host_elf:
                env["ZMK_RENODE_HOST_ELF"] = str(Path(args.host_elf).absolute())
            # ble-split: --elf is the split CENTRAL; ZMK_RENODE_PERIPHERAL_ELF is
            # the split PERIPHERAL half (see renode_harness.boot_ble_split).
            if args.mode == "ble-split" and getattr(args, "peripheral_elf", None):
                env["ZMK_RENODE_PERIPHERAL_ELF"] = str(Path(args.peripheral_elf).absolute())

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
