import concurrent.futures
import itertools
import shutil
from west import log
from west.commands import WestCommand
from west.util import west_topdir
from west.manifest import Manifest
import yaml

from pathlib import Path
import os
import subprocess
import tempfile


class ZMKBuild(WestCommand):
    # CMake args for debug build with Segger J-Link and RTT console
    DEBUG_CMAKE_ARGS = [
        "-DCMAKE_BUILD_TYPE=Debug",
        # Enable RTT
        "-DCONFIG_USE_SEGGER_RTT=y" "-DCONFIG_RTT_CONSOLE=y",
        "-DCONFIG_UART_CONSOLE=n",
        "-DCONFIG_LOG=y",
        "-DCONFIG_LOG_BACKEND_RTT=y",
        "-DCONFIG_LOG_BACKEND_UART=n",
        # Enable shell
        "-DCONFIG_SHELL=y",
        "-DCONFIG_KERNEL_SHELL=y",
        "-DCONFIG_SHELL_BACKEND_RTT=y",
        # Enable debug info
        "-DCONFIG_DEBUG_INFO=y",
        "-DCONFIG_THREAD_NAME=y",
        "-DCONFIG_DEBUG_THREAD_INFO=y",
    ]

    def __init__(self):
        super().__init__(
            name="zmk-build",
            help="Build ZMK firmware with given zmk-config directory",
            description="""
            Build ZMK firmware with specified zmk-config directory using west build.
            The command parses build.yaml to set up the build target automatically.
            """,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        parser.add_argument(
            "config_path",
            nargs="?",
            default=Path.cwd() / "config",
            help="""
            path to your zmk-config/config directory or zmk-config directory.
            """,
        )
        parser.add_argument(
            "west_args",
            nargs="*",
            default=[],
            help="""
            Additional arguments to pass to the `west build` command.
            Should be prepended with -- like `-- -p -n`
            """,
        )
        parser.add_argument(
            "-d",
            "--build-dir",
            help="""
            Path to the directory to output test artifacts.
            Artifact name is appended and the output results in `<build dir>/<artifact name>/`.
            `<west workspace root>/build` by default.
            """,
        )
        parser.add_argument(
            "-m",
            "--extra-modules",
            nargs="*",
            default=[],
            help="""
            Additional ZMK modules to include.
            When building your zmk-config, root of the zmk-config should be specified.
            """,
        )
        parser.add_argument(
            "--extra-module-auto-discovery",
            nargs="*",
            choices=["zmk-config", "current", "walk-up", "none"],
            default=["zmk-config", "current", "walk-up"],
            help="""
            Strategies to find extra modules automatically.
            'zmk-config': add parent of config directory as extra module if zephyr/module.yml exists there
            'current': add current working directory as extra module if zephyr/module.yml exists there
            'walk-up': walk up from parent of current directory to find zephyr/module.yml and add the first matched directory as extra module
            'none': to disable auto discovery
            """,
        )
        parser.add_argument(
            "--build-yaml",
            help="""
            Path to build.yaml file.
            By default, searched in order:
             <config_path>/../build.y[a]ml (zmk-config's official way)
            -> <config_path>/build.y[a]ml (this command's extension).
            In addition to ZMK's offical definition, 'snippets' field is recognized to specify multiple snippets
            """,
        )
        parser.add_argument(
            "-b",
            "--board",
            nargs="+",
            help="""
            Specify the target boards to build for. Prioritized over build.yaml setting (=works as filter if build.yaml found).
            """,
        )
        parser.add_argument(
            "-s",
            "--shield",
            nargs="+",
            help="""
            Specify the shields to build for. Prioritized over build.yaml setting (=works as filter if build.yaml found).
            """,
        )
        parser.add_argument(
            "-S",
            "--snippet",
            nargs="+",
            default=[],
            help="""
            Specify snippets to build for. Merged with build.yaml setting.
            """,
        )
        parser.add_argument(
            "-a",
            "--artifact",
            help="""
            Used for build directory naming. Prioritized over build.yaml setting. Works as filter for build.yaml records with artifact name.
            Artifact .uf2 file will be placed at <build dir>/<artifact name>/zephyr/zmk.uf2
            zmk-config directory name by default if build target is only one.
            If multiple build targets are specified, board name and shield name are appended to artifact name.
            If --reset is specified, '_reset' is appended to artifact name.
            If --debug-jlink is specified, '_debug' is appended to artifact name.
            """,
        )
        parser.add_argument(
            "-as",
            "--artifact-suffix",
            help="""
            Suffix to append to artifact name for build directory naming.
            """,
        )
        parser.add_argument(
            "--cmake-args",
            help="""
            Additional arguments to pass like `west build  -- <cmake-args>`. Merged with build.yaml.
            Need to be passed as string with white space like --cmake-args ' -D foo -D bar'
            """,
        )
        parser.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            help="Reduce output verbosity if build succeeds.",
        )
        parser.add_argument(
            "-n",
            "--no-run",
            action="store_true",
            help="Skip build and just outputs list of detected build targets.",
        )
        parser.add_argument(
            "-i",
            "--interactive",
            action="store_true",
            help="Interactively select build target from all detected candidates. Requires `pip install -r <path to zmk-west-commands>/requirements.txt` to work.",
        )
        parser.add_argument(
            "-P",
            "--parallelism",
            type=int,
            default=os.cpu_count(),
            help="""
            Number of parallel build jobs. Defaults to number of CPU cores.
            """,
        )
        parser.add_argument(
            "-p",
            "--pristine",
            choices=["auto", "always", "never"],
            default="always",
            help="pristine build folder setting (the same to west build argument)",
        )
        parser.add_argument(
            "--debug-jlink",
            action="store_true",
            help="""
            Build for debug with Segger J-Link and RTT console.
            """,
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="""'
            Build with reset settings on startup mode by specifying -DCONFIG_ZMK_SETTINGS_RESET_ON_START
            """,
        )
        parser.add_argument(
            "--flash",
            nargs="?",
            default=None,
            const=True,
            help="""
            Flash the built firmware after successful build.
            Optional argument to specify the runner to flash to. (The same to west flash --runner)
            """,
        )
        return parser

    def do_run(self, args, unknown_args):
        manifest = Manifest.from_topdir()
        log.inf(f"[*] west workspace: {manifest.topdir}")
        log.inf(f"[*] west manifest: {manifest.abspath}")
        try:
            zmk = next(filter(lambda p: p.name == "zmk", manifest.projects))
        except StopIteration:
            log.die("[*] ZMK project not found in manifest.")

        # Rewrite args
        if (Path(args.config_path) / "config").is_dir():
            log.inf(f"[*] zmk-config/config directory: {Path(args.config_path)}")
            args.config_path = str(Path(args.config_path) / "config")

        zmk_config = args.config_path
        build_yaml = (
            self._load_yaml(args.build_yaml)
            if args.build_yaml
            else self._find_build_yaml(Path(zmk_config))
        )
        self._run_for_all(zmk, manifest, args, build_yaml)

    def _find_build_yaml(self, config_path: Path) -> dict:
        parent_dir = config_path.parent
        for search_dir in [parent_dir, config_path]:
            for ext in ["yaml", "yml"]:
                build_yml = search_dir / f"build.{ext}"
                if build_yml.exists():
                    log.inf(f"[*] Found build.yaml at: {build_yml}")
                    return self._load_yaml(build_yml)
        return {}

    def _load_yaml(self, path: Path | str) -> dict:
        with open(path, "r") as f:
            yamls = list(filter(lambda y: y is not None, yaml.safe_load_all(f)))
            res = {
                "boards": list(
                    itertools.chain.from_iterable(map(lambda y: y.get("board", []), yamls))
                ),
                "shields": list(
                    itertools.chain.from_iterable(map(lambda y: y.get("shield", []), yamls))
                ),
                "include": list(
                    itertools.chain.from_iterable(map(lambda y: y.get("include", []), yamls))
                ),
            }
            log.dbg(f"[*] Built setups from build.yml: {res}")
            return res

    def discover_extra_modules(self, id: int, strategy: str, config_path: Path) -> list[str]:
        candidates = []
        if strategy == "zmk-config":
            candidates.append(config_path.parent.absolute())
        elif strategy == "current":
            candidates.append(Path.cwd().absolute())
        elif strategy == "walk-up":
            current_dir = Path.cwd().absolute()
            for parent in current_dir.parents:
                if (parent / "zephyr" / "module.yml").exists():
                    candidates.append(parent.absolute())
                    break
        result = list(
            map(str, filter(lambda p: (p / "zephyr" / "module.yml").exists(), candidates))
        )
        if len(result) > 0:
            log.inf(f"[{id}] Auto discovered extra modules ({strategy}): {result}")
        return result

    def _run_for_all(self, zmk, manifest, args, build_yaml: dict):
        # build all build matrix
        boards = set(args.board if args.board else build_yaml.get("boards", []))
        shields = set(args.shield if args.shield else build_yaml.get("shields", []))
        includes = build_yaml.get("include", [])
        matrix = [] + includes
        for board in boards:
            for shield in shields:
                matrix.append({"board": board, "shield": shield})

        # filter by argument
        arg_boards = set(args.board) if args.board else set()
        arg_shields = set(args.shield) if args.shield else set()

        def filter_out_matrix_by_args(inc):
            if len(arg_boards) > 0 and inc["board"] not in arg_boards:
                return False
            if len(arg_shields) > 0 and inc.get("shield") not in arg_shields:
                return False
            if args.artifact and "artifact" in inc and args.artifact != inc.get("artifact", None):
                return False
            return True

        matrix = list(filter(filter_out_matrix_by_args, matrix))
        if len(matrix) == 0:
            log.die("No build targets found. Specify boards/shields or check build.yaml.")

        if len(matrix) > 1 and args.flash:
            log.die("Cannot flash when multiple build targets are specified.")

        # set artifact name if not exists
        for i, inc in enumerate(matrix):
            if "artifact" not in inc:
                if len(matrix) > 0:
                    prefix = (args.artifact + "__") if args.artifact else ""
                    inc["artifact"] = f'{prefix}{inc["board"]}__{inc["shield"]}'
                else:
                    inc["artifact"] = (
                        args.artifact if args.artifact else Path(args.config_path).parent.name
                    )
        if len(matrix) != len(set(map(lambda inc: inc["artifact"], matrix))):
            log.die("Duplicated artifact names found.")

        if args.interactive:
            import questionary

            matrix = questionary.checkbox(
                "Select build targets to build",
                choices=[questionary.Choice(record["artifact"], record) for record in matrix],
            ).ask()
            if not matrix:
                log.die("No build targets selected.")
        else:
            log.inf(f"[*] {len(matrix)} build targets found")
            for i, inc in enumerate(matrix):
                log.inf(
                    f'[*] - [{i}] {inc["artifact"]} board={inc["board"]}, shield={inc["shield"]}'
                )
                log.dbg(f"[*]  - {inc}")

        if args.no_run:
            exit(0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallelism) as executor:
            futures = [
                executor.submit(self._run_single_build, id, zmk, manifest, args, inc)
                for id, inc in enumerate(matrix)
            ]
            code = 0
            for future in concurrent.futures.as_completed(futures):
                try:
                    code2 = future.result()
                    if code2 != 0:
                        code = code2
                except Exception as e:
                    log.err(f"Build failed: {e}")
            exit(code)

    def _run_single_build(self, id, zmk, manifest, args, build_setup):
        artifact_name = build_setup["artifact"]
        if args.reset:
            artifact_name += "_reset"
        if args.debug_jlink:
            artifact_name += "_debug"
        if args.artifact_suffix:
            artifact_name += f"_{args.artifact_suffix}"

        log.inf(f"[{id}] Building for {artifact_name}")
        zmk_src_dir = Path(zmk.abspath) / "app"
        build_dir = (
            Path(args.build_dir) / artifact_name
            if args.build_dir
            else Path(west_topdir()) / "build" / artifact_name
        )
        west_args = args.west_args
        build_cmake_args = build_setup.get("cmake-args", build_setup.get("cmake_args", ""))
        cmake_args = (
            (args.cmake_args.split() if args.cmake_args else [])
            + build_cmake_args.split()
            + (self.DEBUG_CMAKE_ARGS if args.debug_jlink else [])
            + (["-DCONFIG_ZMK_SETTINGS_RESET_ON_START=y"] if args.reset else [])
        )
        config_path = Path(args.config_path).absolute()

        extra_modules = list(
            set(
                [str(Path(extra_module).absolute()) for extra_module in args.extra_modules]
                + list(
                    itertools.chain.from_iterable(
                        [
                            self.discover_extra_modules(id, strategy, config_path)
                            for strategy in args.extra_module_auto_discovery
                        ]
                    )
                )
            )
        )
        # NOTE: 'snippets' is this commands' extension. Not supported by ZMK official build
        snippets = list(map(lambda s: f"-S {s}", args.snippet + build_setup.get("snippets", [])))
        if "snippet" in build_setup:
            snippets.append(f"-S {build_setup['snippet']}")
        os.makedirs(build_dir, exist_ok=True)

        with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False) as out_log:
            command = (
                [
                    "west",
                    "build",
                    "-s",
                    str(zmk_src_dir),
                    "-d",
                    str(build_dir),
                    "-b",
                    build_setup["board"],
                    "-p",
                    args.pristine,
                ]
                + west_args
                + snippets
                + [
                    "--",
                    f'-DSHIELD={build_setup["shield"]}',
                    f'-DZMK_EXTRA_MODULES={";".join(extra_modules)}',
                    f"-DZMK_CONFIG={config_path}",
                ]
                + cmake_args
            )
            log.dbg(f"[{id}] command: " + " ".join(command))
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=10,
            )
            for line in proc.stdout:
                out_log.write(line)
                if not args.quiet:
                    log.inf(f"[{id}] " + line.rstrip())

        proc.wait()
        log_file_path = build_dir / "stdout_and_stderr.log"
        shutil.move(out_log.name, log_file_path)

        if proc.returncode != 0:
            log.err(f"[{id}] Build failed in {build_dir}. See log in {log_file_path}")
            if args.quiet:
                with open(log_file_path, "r") as f:
                    for line in f:
                        log.err(f"[{id}] " + line.rstrip())
        elif not Path(build_dir / "zephyr" / "zmk.uf2").exists():
            log.err(
                f'[{id}] Build succeeded but firmware artifact not found in {build_dir / "zephyr" / "zmk.uf2"}'
            )
        else:
            log.inf(
                f'[{id}] Build succeeded. Firmware artifact at: {build_dir / "zephyr" / "zmk.uf2"}'
            )

        if args.flash and proc.returncode == 0:
            return self._flash(id, args, build_dir)

        return proc.returncode

    def _flash(self, id, args, build_dir: Path):
        command = [
            "west",
            "flash",
            "-d",
            str(build_dir),
        ]
        if isinstance(args.flash, str):
            command += ["--runner", args.flash]
        log.inf(f"[{id}] Flashing with command: " + " ".join(command))
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False) as flash_log:
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=10
            )
            for line in proc.stdout:
                flash_log.write(line)
                if not args.quiet:
                    log.inf(f"[{id}] " + line.rstrip())
        log.inf(f"[{id}] Waiting for flashing to complete...")
        proc.wait()
        log_file_path = build_dir / "flash.log"
        shutil.move(flash_log.name, log_file_path)

        if proc.returncode != 0:
            log.err(
                f"[{id}] Flashing failed for build dir: {build_dir} See log in {log_file_path}"
            )
            if args.quiet:
                with open(log_file_path, "r") as f:
                    for line in f:
                        log.err(f"[{id}] " + line.rstrip())
        else:
            log.inf(f"[{id}] Flashing succeeded for build dir: {build_dir}")
        return proc.returncode
