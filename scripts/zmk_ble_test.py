"""`west zmk-ble-test` -- run a ZMK module's BabbleSim (bsim) BLE tests.

A Python port of the template repo's `tests/ble/run-ble-test.sh`: discover
test cases (a directory with `nrf52_bsim.keymap`), build the DUT from the
workspace ZMK app with the module added via `ZMK_EXTRA_MODULES`, build any
split peripherals and host ("computer") apps, launch them under the bsim
2G4 phy, and diff the filtered device output against a checked-in snapshot.

The orchestration lives in `scripts/lib/ble/runner.py`; this command only
resolves the workspace paths (zmk app, west topdir, module, bsim tree) and
drives it. See README.md's `west zmk-ble-test` section and
`examples/ble-studio-central/` for the Studio-over-BLE host app.
"""

import os
import sys
from pathlib import Path

from west import log
from west.commands import WestCommand
from west.manifest import Manifest
from west.util import west_topdir

# scripts/lib/ble holds the orchestration core (runner.py).
LIB_BLE_DIR = Path(__file__).resolve().parent / "lib" / "ble"

BSIM_MISSING_HELP = (
    "BabbleSim not found or not compiled. Fetch and build it in your west "
    "workspace:\n"
    "  west config manifest.group-filter -- +babblesim\n"
    "  west update --narrow\n"
    "  make -C <topdir>/dependencies/tools/bsim everything -j$(nproc)\n"
    "then set BSIM_OUT_PATH (and BSIM_COMPONENTS_PATH), or pass --bsim <path>."
)


class ZMKBleTest(WestCommand):
    """Run a ZMK module's BabbleSim BLE tests."""

    def __init__(self):
        super().__init__(
            name="zmk-ble-test",
            help="run ZMK module BabbleSim (bsim) BLE tests",
            description=(
                "Discover BLE test cases (dirs with nrf52_bsim.keymap), build the DUT "
                "from the workspace ZMK app + this module, build split peripherals and "
                "host apps, run them under the bsim phy, and diff the filtered output "
                "against events.snapshot. Ported from the template's run-ble-test.sh."
            ),
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        parser.add_argument(
            "tests_path",
            nargs="?",
            help=(
                "Test case, or a parent directory to search recursively. A directory "
                "is a case iff it contains nrf52_bsim.keymap. Default: current dir."
            ),
        )
        parser.add_argument(
            "-m",
            "--module",
            help=(
                "Module repo root added via ZMK_EXTRA_MODULES (default: the enclosing "
                "git repo of the current directory, or the current directory)."
            ),
        )
        parser.add_argument(
            "--auto-accept",
            action="store_true",
            help="Regenerate events.snapshot on mismatch (also honors ZMK_TESTS_AUTO_ACCEPT).",
        )
        parser.add_argument(
            "--sim-prefix",
            help=(
                "bsim simulation-id / exe-name prefix. Default: sanitized module dir "
                "name. Also expands a literal {prefix} placeholder in siblings.txt."
            ),
        )
        parser.add_argument(
            "--bsim",
            help="BSIM_OUT_PATH override (path to a compiled BabbleSim tree).",
        )
        parser.add_argument(
            "-j",
            "--parallel",
            type=int,
            default=1,
            help="Run cases concurrently (default 1). Per-case sim ids isolate the phys.",
        )
        parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            help="Stream device output to the console (logs are always written to disk).",
        )
        return parser

    def do_run(self, args, unknown_args):
        # Resolve the zmk project the same way zmk_test.py does.
        manifest = Manifest.from_topdir()
        try:
            zmk = next(filter(lambda p: p.name == "zmk", manifest.projects))
        except StopIteration:
            log.die("ZMK project not found in the west manifest.")
        zmk_app = Path(zmk.abspath) / "app"
        if not zmk_app.is_dir():
            log.die(f"ZMK app dir not found: {zmk_app}")

        topdir = Path(west_topdir())
        module_dir = self._resolve_module(args.module)
        bsim_out_path, bsim_components_path = self._resolve_bsim(args.bsim)

        tests_path = Path(args.tests_path).absolute() if args.tests_path else Path.cwd()
        if not tests_path.exists():
            log.die(f"tests_path does not exist: {tests_path}")

        sys.path.insert(0, str(LIB_BLE_DIR))
        from runner import BleRunner, discover_cases, sanitize_prefix  # noqa: E402

        prefix = args.sim_prefix or sanitize_prefix(module_dir.name)
        auto_accept = args.auto_accept or bool(os.environ.get("ZMK_TESTS_AUTO_ACCEPT"))

        cases = discover_cases(tests_path)
        if not cases:
            log.die(f"no test cases (nrf52_bsim.keymap) found under {tests_path}")
        log.inf(f"[*] {len(cases)} case(s) under {tests_path}")
        for c in cases:
            log.inf(f"[*]   {c}")

        runner = BleRunner(
            zmk_app=zmk_app,
            module_dir=module_dir,
            topdir=topdir,
            bsim_out_path=bsim_out_path,
            bsim_components_path=bsim_components_path,
            prefix=prefix,
            auto_accept=auto_accept,
            verbose=args.verbose,
            log=log,
        )

        try:
            runner.build_host_apps()
            results = runner.run(cases, parallel=max(1, args.parallel))
        except Exception as err:  # BleTestError and friends
            log.die(str(err))

        failed = [r for r in results if r.status == "FAILED"]
        if failed:
            log.die(f"{len(failed)} BLE case(s) FAILED: " + ", ".join(r.rel for r in failed))
        log.inf("[*] All BLE cases passed")

    def _resolve_module(self, module_arg: str | None) -> Path:
        if module_arg:
            module = Path(module_arg).absolute()
            if not module.is_dir():
                log.die(f"--module is not a directory: {module}")
            return module
        # Default: enclosing git repo of cwd (like zmk-test's -m usage).
        import subprocess

        try:
            top = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                cwd=str(Path.cwd()),
            )
            if top.returncode == 0 and top.stdout.strip():
                return Path(top.stdout.strip())
        except OSError:
            pass
        return Path.cwd()

    def _resolve_bsim(self, bsim_arg: str | None):
        bsim = bsim_arg or os.environ.get("BSIM_OUT_PATH")
        if not bsim:
            # Try the conventional location under the west topdir.
            candidate = Path(west_topdir()) / "dependencies" / "tools" / "bsim"
            if (candidate / "bin" / "bs_2G4_phy_v1").is_file():
                bsim = str(candidate)
        if not bsim:
            log.die(BSIM_MISSING_HELP)

        bsim_path = Path(bsim).absolute()
        if not (bsim_path / "bin" / "bs_2G4_phy_v1").is_file():
            log.die(
                f"BSIM_OUT_PATH={bsim_path} has no bin/bs_2G4_phy_v1 (uncompiled?).\n"
                + BSIM_MISSING_HELP
            )

        components = os.environ.get("BSIM_COMPONENTS_PATH")
        components_path = Path(components).absolute() if components else bsim_path / "components"
        return bsim_path, components_path
