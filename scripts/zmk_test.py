import shutil
import tempfile
from west import log
from west.commands import WestCommand
from west.util import west_topdir
from west.manifest import Manifest

import os
import subprocess
from pathlib import Path


class ZMKTest(WestCommand):
    """Run ZMK tests."""

    def __init__(self):
        super().__init__(
            name="zmk-test",
            help="run ZMK tests",
            description="Run the ZMK test suite with zmk's run-test.sh script.",
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        parser.add_argument(
            "test_path",
            nargs="?",
            help="""
            Specify (parent) test directory to run.
            The command finds tests recursively by searching `native_posix_64.keymap`.
            Current directory by default.
            """,
        )
        parser.add_argument(
            "-d",
            "--build-dir",
            help="""
            Path to the ZMK build directory to output test artifacts.
            <west workspace root>/build by default.
            """,
        )
        parser.add_argument(
            "-m",
            "--extra-modules",
            nargs="*",
            default=[],
            help="""
            Additional ZMK modules to include during testing.
            Useful when running test under your zmk-module to include your module itself by specifying zmk-module repository root.
            """,
        )
        parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            help="Enable verbose output for west itself and tests.",
        )
        return parser

    def do_run(self, args, unknown_args):
        manifest = Manifest.from_topdir()
        try:
            zmk = next(filter(lambda p: p.name == "zmk", manifest.projects))
        except StopIteration:
            log.die("ZMK project not found in manifest.")

        zmk_src_dir = os.path.join(zmk.abspath, "app")
        test_path = (
            Path(args.test_path).absolute()
            if args.test_path and args.test_path != "all"
            else Path.cwd()
        )
        build_dir = (
            Path(args.build_dir).absolute() if args.build_dir else Path(west_topdir()) / "build"
        )
        extra_modules = list(map(lambda m: str(Path(m).absolute()), args.extra_modules))
        log.inf(f"Running ZMK tests under {test_path} with build dir {build_dir}")

        env = os.environ.copy()
        env["ZMK_SRC_DIR"] = zmk_src_dir
        env["ZMK_BUILD_DIR"] = str(build_dir)
        env["ZMK_EXTRA_MODULES"] = ";".join(extra_modules)
        env["ZMK_TESTS_VERBOSE"] = "1"
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False) as out_log:
            proc = subprocess.Popen(
                [f"{zmk_src_dir}/run-test.sh", "."],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=10,
                env=env,
                cwd=test_path,
            )

            for line in proc.stdout:
                out_log.write(line)
                if (
                    line.startswith("PASS:")
                    or line.startswith("FAILED:")
                    or line.startswith("PENDING:")
                    or line.startswith("Running:")
                ):
                    log.inf(line.rstrip())
                elif args.verbose:
                    log.dbg(line.rstrip())
        proc.wait()
        log_file_path = build_dir / "stdout_and_stderr.log"
        shutil.move(out_log.name, log_file_path)

        if proc.returncode != 0 and not args.verbose:
            log.err(f"Tests failed. See {log_file_path} for details.")
            with open(log_file_path, "r") as log_file:
                for line in log_file:
                    log.inf(line.rstrip())
        return proc.returncode
