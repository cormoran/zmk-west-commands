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
            help="""
            path to your zmk-config/config directory
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
        # parser.add_argument(
        #     '-v', '--verbose',
        #     action='store_true',
        #     help='Enable verbose output (the same to west build argument)'
        # )
        parser.add_argument(
            "-p",
            "--pristine",
            choices=["auto", "always", "never"],
            default="auto",
            help="pristine build folder setting (the same to west build argument)",
        )
        return parser

    def do_run(self, args, unknown_args):
        manifest = Manifest.from_topdir()
        log.inf(f"west workspace: {manifest.topdir}")
        log.inf(f"west manifest: {manifest.abspath}")
        try:
            zmk = next(filter(lambda p: p.name == "zmk", manifest.projects))
        except StopIteration:
            log.die("ZMK project not found in manifest.")

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
                    log.inf(f"Found build.yaml at: {build_yml}")
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
            log.dbg(f"* Built setups from build.yml: {res}")
            return res

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

        # set artifact name if not exists
        for i, inc in enumerate(matrix):
            if "artifact" not in inc:
                if len(matrix) > 0:
                    prefix = (args.artifact + "__") if args.artifact else ""
                    inc["artifact"] = f'{prefix}{inc["board"]}__{inc["shield"]}'
                else:
                    inc["artifact"] = (
                        args.artifact if args.artifact else Path(args.config_path).name
                    )
        if len(matrix) != len(set(map(lambda inc: inc["artifact"], matrix))):
            log.die("Duplicated artifact names found.")

        if args.interactive:
            import questionary

            matrix = questionary.checkbox(
                "Select build targets to build",
                choices=[questionary.Choice(record["artifact"], record) for record in matrix],
            ).ask()
        else:
            log.inf(f"{len(matrix)} build targets found")
            for i, inc in enumerate(matrix):
                log.inf(f'- [{i}] {inc["artifact"]} board={inc["board"]}, shield={inc["shield"]}')
                log.dbg(f"  - {inc}")

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
            args.cmake_args.split() if args.cmake_args else []
        ) + build_cmake_args.split()
        config_path = Path(args.config_path).absolute()
        extra_modules = [str(Path(extra_module).absolute()) for extra_module in args.extra_modules]
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
        elif not Path(build_dir / "zephyr" / "zmk.uf2").exists():
            log.err(
                f'[{id}] Build succeeded but firmware artifact not found in {build_dir / "zephyr" / "zmk.uf2"}'
            )
        else:
            log.inf(
                f'[{id}] Build succeeded. Firmware artifact at: {build_dir / "zephyr" / "zmk.uf2"}'
            )

        return proc.returncode
