"""`west zmk-renode-test` -- boot a built ZMK ELF in the Renode emulator, run a
boot + Studio smoke test, then a module's own `tests/renode/*_test.py` files.

The test has two independent axes (see docs/design/renode-transport-orthogonal.md):
`--host-link {usb,ble,none}` (how the central answers Studio RPC) and
`--split-link {none,wired,ble}` (how the central reaches the peripheral).
`--mode` is retained as a backward-compatible preset expanding to a
(host-link, split-link) pair; the two vocabularies are mutually exclusive. The
one supported combination without a preset is `--host-link none --split-link
wired` (a Studio-less wired split). The four presets:

  * **ble** (default) -- the DUT is the exact `studio-rpc-usb-uart` *hardware*
    image, with no extra module config; platform stubs make it boot. With
    `--host-elf`, the `renode-ble-host` app pairs over an emulated BLE medium and
    does an encrypted Studio GATT read (S4/S5). Without `--host-elf`, it degrades
    to a boot-liveness check.
  * **usb** -- the SAME real hardware image as ble mode, but Studio RPC rides
    the emulated USB: the NRF_USBD_Full model + DualCdcAcmBridge USB host
    enumerate the image's real USB composite. Smoke = a core Studio
    GetDeviceInfo round trip over the USB CDC (plus the boot banner when the
    image also enables the board console CDC via CONFIG_ZMK_USB_LOGGING).
  * **wired-split** -- a WIRED split pair whose central STILL speaks Studio:
    `--elf` is the central half and `--peripheral-elf` the peripheral half,
    booted as two machines whose split links (uart1) are cross-connected via a
    Renode UART hub. Studio RPC rides the emulated USB CDC (free because the
    wired split only consumes the two nRF52840 UARTEs: console uart0 + split
    uart1). Smoke = a Studio GetDeviceInfo round trip over the central's USB CDC
    AND a keypress injected on the peripheral relayed over the wired link and
    processed by the central.
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
                "extra config, Studio over emulated BLE), --mode usb (the same real image, "
                "Studio over the emulated USB CDC), --mode wired-split (wired-split central "
                "answering Studio over USB + --peripheral-elf on a Renode UART hub), and "
                "--mode ble-split (wireless split central + --peripheral-elf + --host-elf on "
                "one BLE medium). Does not build firmware."
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
            choices=("usb", "ble", "wired-split", "ble-split"),
            default=None,
            help=(
                "Backward-compatible preset expanding to a (host-link, split-link) pair "
                "(default, no flags: ble). ble: real hardware image; with --host-elf the "
                "renode-ble-host app pairs and does an encrypted Studio GATT read (S4/S5), "
                "without it a boot-liveness check. usb: the SAME real image, Studio RPC "
                "over the emulated USB CDC. ble-split: a WIRELESS split -- --elf is the "
                "split CENTRAL, --peripheral-elf the split PERIPHERAL, --host-elf the "
                "host. wired-split: wired-split central (--elf) answering Studio over USB + "
                "--peripheral-elf on a Renode UART hub. Mutually exclusive with "
                "--host-link/--split-link. See "
                "docs/design/renode-transport-orthogonal.md and docs/renode-testing.md."
            ),
        )
        parser.add_argument(
            "--host-link",
            choices=("usb", "ble", "none"),
            default=None,
            help=(
                "How the central answers Studio RPC: usb (emulated USB CDC), ble "
                "(emulated BLE GATT), none (boot-liveness only). Mutually exclusive with "
                "--mode; pair with --split-link. --host-link usb --split-link wired (a "
                "wired split whose central still speaks Studio) is also the wired-split "
                "preset. See docs/design/renode-transport-orthogonal.md."
            ),
        )
        parser.add_argument(
            "--split-link",
            choices=("none", "wired", "ble"),
            default=None,
            help=(
                "How the central reaches the peripheral: none (not a split), wired (UART "
                "hub), ble (radio + fake CCM). Mutually exclusive with --mode."
            ),
        )
        parser.add_argument(
            "--peripheral-elf",
            help=(
                "wired-split / ble-split mode: the split PERIPHERAL half's firmware ELF "
                "(--elf is the CENTRAL half). Required for both split modes."
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
            "--boot-timeout",
            type=float,
            default=20.0,
            help="wired-split/usb mode: seconds to wait for the ZMK boot banner (default: 20).",
        )
        parser.add_argument(
            "--skip-smoke",
            action="store_true",
            help="Skip the smoke test; run only the module's own tests_dir.",
        )

        # Advanced knobs -- rarely needed; see docs/renode-testing.md and
        # docs/design/renode-internals.md. Kept out of the common story on purpose.
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
            help="ble/usb mode: NVS storage_partition address preloaded as erased 0xFF "
            "(default: 0xec000, xiao_ble).",
        )
        adv.add_argument(
            "--storage-size",
            type=lambda s: int(s, 0),
            default=None,
            help="ble/usb mode: NVS storage_partition size (default: 0x8000, xiao_ble).",
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

        # Make the harness (and the module's own tests) importable.
        sys.path.insert(0, str(LIB_RENODE_DIR))
        import renode_harness  # noqa: E402
        import renode_smoke  # noqa: E402

        # Resolve the two orthogonal axes from either a --mode preset or the
        # explicit --host-link/--split-link flags (mutually exclusive).
        try:
            host, split = renode_smoke.resolve_links(args.mode, args.host_link, args.split_link)
        except ValueError as err:
            log.die(str(err))
        # Stash the resolved pair for _run_module_tests' env contract.
        args._host_link, args._split_link = host, split
        log.inf(
            f"[*] host-link={host} x split-link={split} "
            f"({renode_smoke.canonical_mode(host, split)})"
        )

        if args.host_elf and host != "ble":
            log.die("--host-elf is only valid with a ble host-link.")
        if args.peripheral_elf and split == "none":
            log.die("--peripheral-elf is only valid with a wired/ble split-link.")

        renode_path = renode_harness.find_or_install_renode(version=args.renode_version)
        if renode_path is None:
            log.die("Renode is not installed and could not be auto-installed.")
        log.inf(f"[*] Renode: {renode_path}")

        host_elf = None
        if host == "ble" and args.host_elf:
            host_elf = Path(args.host_elf).absolute()
            if not host_elf.is_file():
                log.die(f"host ELF not found: {host_elf}")

        peripheral_elf = None
        if split != "none":
            if not args.peripheral_elf:
                log.die(
                    f"split-link={split} requires --peripheral-elf (the peripheral half's ELF)."
                )
            peripheral_elf = Path(args.peripheral_elf).absolute()
            if not peripheral_elf.is_file():
                log.die(
                    f"peripheral ELF not found: {peripheral_elf} (this command does not "
                    "build firmware -- build it first)"
                )
            if (host, split) == ("ble", "ble") and host_elf is None:
                log.die("--host-elf is required for a ble host-link x ble split-link (ble-split).")

        if not args.skip_smoke:
            if (host, split) == ("ble", "ble"):
                self._run_ble_split_smoke(args, elf, peripheral_elf, host_elf, renode_path)
            elif (host, split) == ("ble", "none"):
                if host_elf is not None:
                    self._run_ble_studio_smoke(args, elf, host_elf, renode_path)
                else:
                    log.inf(
                        "[*] ble host-link without --host-elf: checking DUT boot liveness "
                        "only (no encrypted Studio read)."
                    )
                    self._run_ble_liveness_smoke(args, elf, renode_path)
            elif (host, split) == ("none", "wired"):
                self._run_split_smoke(args, elf, peripheral_elf, renode_path)
            elif (host, split) == ("usb", "wired"):
                self._run_usb_wired_smoke(args, elf, peripheral_elf, renode_path)
            elif (host, split) == ("usb", "none"):
                self._run_usb_smoke(args, elf, renode_path)
            else:  # unreachable: resolve_links already gated the supported set
                log.die(f"unsupported combination host-link={host} x split-link={split}")
        else:
            log.inf("[*] Skipping smoke test (--skip-smoke)")

        if args.tests_dir:
            self._run_module_tests(args, elf)

    def _run_ble_studio_smoke(self, args, elf: Path, host_elf: Path, renode_path: str) -> None:
        import renode_harness  # noqa: E402
        import renode_smoke  # noqa: E402

        # protobuf is a hard runtime dep here: the ble smoke now asserts a real
        # framed GetDeviceInfo round trip (CHECK 3), parsed from the host's S6 dump.
        try:
            import google.protobuf  # noqa: F401
        except ImportError:
            log.die(
                "the `protobuf` Python package is required for the ble-mode Studio RPC "
                "smoke test -- install it (see requirements-test.txt) or pass --skip-smoke."
            )
        proto_dir = self._find_studio_proto_dir(renode_harness)

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
                studio_proto_dir=proto_dir,
                virtual_budget=args.virtual_budget,
                **kwargs,
            )
        except AssertionError as err:
            log.die(f"BLE smoke test FAILED: {err}")
        log.inf("[*] BLE smoke test OK")

    def _run_ble_split_smoke(
        self, args, central_elf: Path, peripheral_elf: Path, host_elf: Path, renode_path: str
    ) -> None:
        import renode_harness  # noqa: E402
        import renode_smoke  # noqa: E402

        try:
            import google.protobuf  # noqa: F401
        except ImportError:
            log.die(
                "the `protobuf` Python package is required for the ble-split Studio RPC "
                "smoke test -- install it (see requirements-test.txt) or pass --skip-smoke."
            )
        proto_dir = self._find_studio_proto_dir(renode_harness)

        kwargs = {}
        if args.storage_addr is not None:
            kwargs["storage_addr"] = args.storage_addr
        if args.storage_size is not None:
            kwargs["storage_size"] = args.storage_size
        if getattr(args, "steady_quantum", None):
            kwargs["steady_quantum"] = args.steady_quantum

        log.inf(
            "[*] Running BLE-split smoke test (split central + peripheral + renode-ble-host; "
            "3 checks: split L2 + host S4 connection, peripheral key relayed to central, "
            "host Studio GetDeviceInfo round trip)"
        )
        try:
            renode_smoke.run_ble_split_smoke(
                central_elf=central_elf,
                peripheral_elf=peripheral_elf,
                host_elf=host_elf,
                renode_path=renode_path,
                studio_proto_dir=proto_dir,
                virtual_budget=max(args.virtual_budget, 120.0),
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

    def _run_usb_smoke(self, args, elf: Path, renode_path: str) -> None:
        import renode_harness  # noqa: E402
        import renode_smoke  # noqa: E402

        # protobuf is a hard runtime dep here: usb mode always asserts the
        # Studio RPC round trip (that is the mode's whole point).
        try:
            import google.protobuf  # noqa: F401
        except ImportError:
            log.die(
                "the `protobuf` Python package is required for the usb-mode Studio RPC "
                "smoke test -- install it (see requirements-test.txt) or pass --skip-smoke."
            )
        proto_dir = self._find_studio_proto_dir(renode_harness)

        kwargs = {}
        if args.storage_addr is not None:
            kwargs["storage_addr"] = args.storage_addr
        if args.storage_size is not None:
            kwargs["storage_size"] = args.storage_size

        log.inf(
            "[*] Running usb-mode Renode smoke test (real image; Studio RPC over the "
            "emulated USB CDC)"
        )
        try:
            renode_smoke.run_usb_smoke(
                elf=elf,
                renode_path=renode_path,
                studio_proto_dir=proto_dir,
                boot_timeout=args.boot_timeout,
                **kwargs,
            )
        except AssertionError as err:
            log.die(f"usb smoke test FAILED: {err}")
        log.inf("[*] USB smoke test OK")

    def _run_usb_wired_smoke(
        self, args, central_elf: Path, peripheral_elf: Path, renode_path: str
    ) -> None:
        import renode_harness  # noqa: E402
        import renode_smoke  # noqa: E402

        # protobuf is a hard runtime dep here: usb+wired always asserts the
        # Studio RPC round trip over USB (that is half the point of the combo).
        try:
            import google.protobuf  # noqa: F401
        except ImportError:
            log.die(
                "the `protobuf` Python package is required for the usb+wired Studio RPC "
                "smoke test -- install it (see requirements-test.txt) or pass --skip-smoke."
            )
        proto_dir = self._find_studio_proto_dir(renode_harness)

        kwargs = {}
        if args.storage_addr is not None:
            kwargs["storage_addr"] = args.storage_addr
        if args.storage_size is not None:
            kwargs["storage_size"] = args.storage_size

        log.inf(
            "[*] Running usb+wired-split Renode smoke test (wired split; central Studio "
            "RPC over the emulated USB CDC + peripheral keypress relayed to the central)"
        )
        try:
            renode_smoke.run_usb_wired_smoke(
                central_elf=central_elf,
                peripheral_elf=peripheral_elf,
                renode_path=renode_path,
                studio_proto_dir=proto_dir,
                boot_timeout=args.boot_timeout,
                **kwargs,
            )
        except AssertionError as err:
            log.die(f"usb+wired smoke test FAILED: {err}")
        log.inf("[*] usb+wired smoke test OK")

    def _run_split_smoke(
        self, args, central_elf: Path, peripheral_elf: Path, renode_path: str
    ) -> None:
        import renode_smoke  # noqa: E402

        log.inf("[*] Running Studio-less wired-split Renode smoke test (central + peripheral)")
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

        import renode_smoke  # noqa: E402

        host = getattr(args, "_host_link", None)
        split = getattr(args, "_split_link", None)
        if host is None or split is None:
            host, split = renode_smoke.resolve_links(args.mode, args.host_link, args.split_link)

        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(LIB_RENODE_DIR) + (os.pathsep + existing if existing else "")
        # Module-test env contract (see docs/design/renode-transport-orthogonal.md):
        #   ZMK_RENODE_HOST_LINK  = usb | ble | none
        #   ZMK_RENODE_SPLIT_LINK = none | wired | ble
        #   ZMK_RENODE_MODE       = the preset name when the pair is a preset, else
        #                           the canonical "<host>+<split>" string (kept for
        #                           backward compatibility with older consumers)
        #   ZMK_RENODE_ELF        = the DUT / central ELF
        # A non-none split-link also exports ZMK_RENODE_PERIPHERAL_ELF; the
        # real-image links (ble / usb host, ble split) export the storage-partition
        # overrides; a ble host-link with a host exports ZMK_RENODE_HOST_ELF.
        env["ZMK_RENODE_HOST_LINK"] = host
        env["ZMK_RENODE_SPLIT_LINK"] = split
        env["ZMK_RENODE_MODE"] = renode_smoke.canonical_mode(host, split)
        env["ZMK_RENODE_ELF"] = str(elf)
        if split != "none":
            # split tests build the peripheral half too (--elf is the central;
            # ZMK_RENODE_PERIPHERAL_ELF is the peripheral -- see the boot_* harness fns).
            env["ZMK_RENODE_PERIPHERAL_ELF"] = str(Path(args.peripheral_elf).absolute())
        # A real flashable image is booted whenever the host rides USB/BLE or the
        # split rides BLE (all of which use the USBD/QSPI/FICR/NVMC-stub platform
        # and honour these storage overrides); a plain wired-only pair does not.
        if host in ("ble", "usb") or split == "ble":
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
        if host == "ble" and args.host_elf:
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
