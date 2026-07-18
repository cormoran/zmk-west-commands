"""BabbleSim (bsim) BLE test orchestration core for `west zmk-ble-test`.

This is a Python port of the template repo's `tests/ble/run-ble-test.sh`
(cormoran/zmk-module-template-with-custom-studio-rpc). The orchestration --
case discovery, firmware/host-app builds, phy + handbrake launch, output
tee'ing -- lives here; the west command (`scripts/zmk_ble_test.py`) resolves
the workspace paths and drives it.

The **pass/fail pipeline is kept byte-compatible** with the bash script by
shelling out to the exact upstream pipeline
(`sort -s -t: -k1,1 | sed -E -n -f events.patterns` then `diff -auZ`), so
existing `events.patterns` / `events.snapshot` files produce identical
results and stay diffable against upstream ZMK conventions.

Per-case file conventions (a directory is a case iff it has
`nrf52_bsim.keymap`):

| File                  | Meaning                                              |
|-----------------------|------------------------------------------------------|
| `nrf52_bsim.keymap`   | marks the case; DUT keymap                           |
| `nrf52_bsim.conf`     | shared DUT + peripheral Kconfig (via `ZMK_CONFIG`)   |
| `central.conf`        | role-specific extra conf for the DUT (central)       |
| `peripheral.conf`     | role-specific extra conf for peripheral builds       |
| `peripheral*.overlay` | one split-peripheral build each; presence => split   |
| `siblings.txt`        | one command line per extra simulated device          |
| `events.patterns`     | sed -E -n filter for the combined output log         |
| `events.snapshot`     | expected filtered output                             |
| `pending`             | mismatch reported as PENDING instead of FAILED       |
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


class BleTestError(Exception):
    """Fatal, actionable error (missing bsim, build failure, ...)."""


# Status constants for a single case.
PASS = "PASS"
FAILED = "FAILED"
PENDING = "PENDING"


def sanitize_prefix(name: str) -> str:
    """Turn an arbitrary module directory name into a bsim-id-safe prefix
    (keep [A-Za-z0-9], collapse everything else to `_`)."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return cleaned or "ble"


def discover_cases(tests_path: Path) -> list[Path]:
    """A directory is a test case iff it contains `nrf52_bsim.keymap`.
    Recurse from `tests_path` (which itself may be a single case)."""
    tests_path = Path(tests_path)
    return sorted({p.parent for p in tests_path.rglob("nrf52_bsim.keymap")})


@dataclass
class CaseResult:
    rel: str
    status: str


class BleRunner:
    def __init__(
        self,
        *,
        zmk_app: Path,
        module_dir: Path,
        topdir: Path,
        bsim_out_path: Path,
        bsim_components_path: Path | None,
        prefix: str,
        auto_accept: bool,
        verbose: bool,
        log,
    ):
        self.zmk_app = Path(zmk_app)
        self.module_dir = Path(module_dir)
        self.topdir = Path(topdir)
        self.bsim_out_path = Path(bsim_out_path)
        self.bin_dir = self.bsim_out_path / "bin"
        self.prefix = prefix
        self.auto_accept = auto_accept
        self.verbose = verbose
        self.quiet = not verbose
        self.log = log

        self.build_root = self.topdir / "build" / "ble"
        # Base directory case-relative paths (and hence sim ids) are computed
        # against -- kept identical to the bash script's
        # `${testcase#"$MODULE_DIR/tests/ble/"}` so existing snapshots and
        # literal `siblings.txt` names stay byte-compatible.
        self.tests_ble_root = self.module_dir / "tests" / "ble"

        self.env = os.environ.copy()
        self.env["BSIM_OUT_PATH"] = str(self.bsim_out_path)
        if bsim_components_path is not None:
            self.env["BSIM_COMPONENTS_PATH"] = str(bsim_components_path)
        # Serialize concurrent appends to a case's output.log (one lock per
        # case is created in _run_simulation; this is only a default).

    # ------------------------------------------------------------------
    # Build helpers
    # ------------------------------------------------------------------

    def _west_build(
        self, build_dir: Path, board: str, source: Path, extra_args: list[str], log_path: Path
    ) -> None:
        """Run `west build`, capturing output to `log_path`. Raises
        BleTestError on failure (message points at the log)."""
        cmd = ["west", "build", "-d", str(build_dir), "-b", board, str(source), "--", *extra_args]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w") as lf:
            proc = subprocess.run(
                cmd,
                cwd=str(self.topdir),
                stdout=lf,
                stderr=subprocess.STDOUT,
                env=self.env,
            )
        if proc.returncode != 0:
            raise BleTestError(f"build failed: {source} (see {log_path})")

    def _stage(self, built_exe: Path, name: str) -> None:
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built_exe, self.bin_dir / name)

    def build_host_apps(self) -> None:
        """Build the host ("computer") binaries once per run and stage them
        into `$BSIM_OUT_PATH/bin`:

        - ZMK's generic BLE central (`<zmk>/app/tests/ble/central`) ->
          `ble_test_central.exe` (always, unprefixed).
        - every module app matching `tests/ble/*_central/CMakeLists.txt`
          (plain board `nrf52_bsim`) -> `<prefix>_<appname>.exe`, plus a
          plain `<appname>.exe` alias so existing literal `siblings.txt`
          names keep working.
        """
        self.log.inf("[*] Building host apps")
        central_src = self.zmk_app / "tests" / "ble" / "central"
        if central_src.is_dir():
            build_dir = self.build_root / "central"
            self._west_build(
                build_dir, "nrf52_bsim", central_src, [], build_dir.with_suffix(".build.log")
            )
            self._stage(build_dir / "zephyr" / "zephyr.exe", "ble_test_central.exe")
            self.log.inf("[*]   staged ble_test_central.exe")
        else:
            self.log.wrn(f"ZMK generic central app not found at {central_src}")

        for cmake in sorted((self.module_dir / "tests" / "ble").glob("*_central/CMakeLists.txt")):
            app_dir = cmake.parent
            appname = app_dir.name
            build_dir = self.build_root / appname
            self._west_build(
                build_dir, "nrf52_bsim", app_dir, [], build_dir.with_suffix(".build.log")
            )
            built = build_dir / "zephyr" / "zephyr.exe"
            self._stage(built, f"{self.prefix}_{appname}.exe")
            # Back-compat alias for case data that references the plain name.
            self._stage(built, f"{appname}.exe")
            self.log.inf(f"[*]   staged {self.prefix}_{appname}.exe (+ {appname}.exe alias)")

    # ------------------------------------------------------------------
    # Per-case orchestration
    # ------------------------------------------------------------------

    def _case_rel(self, case_dir: Path) -> str:
        try:
            return case_dir.relative_to(self.tests_ble_root).as_posix()
        except ValueError:
            return case_dir.name

    def run_case(self, case_dir: Path) -> CaseResult:
        case_dir = Path(case_dir).resolve()
        rel = self._case_rel(case_dir)
        sim_id = f"{self.prefix}_{rel.replace('/', '_')}"
        case_build = self.build_root / rel
        case_build.mkdir(parents=True, exist_ok=True)
        self.log.inf(f"Running {rel}:")

        peripheral_overlays = sorted(case_dir.glob("peripheral*.overlay"))

        # --- Build peripherals (one per overlay) ---
        extra_peripheral_args: list[str] = []
        peripheral_conf = case_dir / "peripheral.conf"
        if peripheral_conf.is_file():
            extra_peripheral_args.append(f"-DEXTRA_CONF_FILE={peripheral_conf}")

        for overlay in peripheral_overlays:
            pn = overlay.name[: -len(".overlay")]
            build_dir = case_build / pn
            self._west_build(
                build_dir,
                "nrf52_bsim//zmk_test_mock",
                self.zmk_app,
                [
                    f"-DZMK_CONFIG={case_dir}",
                    f"-DZMK_EXTRA_MODULES={self.module_dir}",
                    f"-DEXTRA_DTC_OVERLAY_FILE={overlay}",
                    *extra_peripheral_args,
                ],
                case_build / f"{pn}.build.log",
            )

        # --- Build the DUT (central) ---
        extra_central_args: list[str] = []
        central_conf = case_dir / "central.conf"
        if central_conf.is_file():
            extra_central_args.append(f"-DEXTRA_CONF_FILE={central_conf}")
        if peripheral_overlays:
            self.log.inf("Found peripheral overlays, building the test as a split central")
            extra_central_args.append("-DCONFIG_ZMK_SPLIT_ROLE_CENTRAL=y")

        dut_build = case_build / "dut"
        self._west_build(
            dut_build,
            "nrf52_bsim//zmk_test_mock",
            self.zmk_app,
            [
                f"-DZMK_CONFIG={case_dir}",
                f"-DZMK_EXTRA_MODULES={self.module_dir}",
                *extra_central_args,
            ],
            case_build / "dut.build.log",
        )

        # --- Stage DUT + peripheral executables ---
        self._stage(dut_build / "zephyr" / "zmk.exe", sim_id)
        for overlay in peripheral_overlays:
            pn = overlay.name[: -len(".overlay")]
            self._stage(case_build / pn / "zephyr" / "zmk.exe", f"{sim_id}_{pn}.exe")

        # --- Run the simulation ---
        siblings = self._read_siblings(case_dir / "siblings.txt")
        output_log = case_build / "output.log"
        self._run_simulation(sim_id, siblings, output_log)

        # --- Evaluate the snapshot ---
        return CaseResult(rel, self._evaluate(case_dir, case_build, output_log, rel))

    def _read_siblings(self, path: Path) -> list[str]:
        if not path.is_file():
            return []
        lines = []
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line:
                lines.append(line)
        return lines

    def _run_simulation(self, sim_id: str, siblings: list[str], output_log: Path) -> None:
        output_log.parent.mkdir(parents=True, exist_ok=True)
        if output_log.exists():
            output_log.unlink()

        procs: list[subprocess.Popen] = []
        threads: list[threading.Thread] = []
        lock = threading.Lock()

        with open(output_log, "a") as log_handle:

            def spawn(argv: list[str], tee: bool) -> None:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(self.bin_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    env=self.env,
                )
                procs.append(proc)

                def reader(p=proc):
                    assert p.stdout is not None
                    for line in p.stdout:
                        if tee:
                            with lock:
                                log_handle.write(line)
                                log_handle.flush()
                        if not self.quiet:
                            sys.stdout.write(line)
                    p.stdout.close()

                t = threading.Thread(target=reader, daemon=True)
                t.start()
                threads.append(t)

            # d=0 DUT, d=1 handbrake, d=2.. siblings; only DUT + siblings are
            # tee'd into output.log (matching the bash script).
            spawn([f"./{sim_id}", "-d=0", f"-s={sim_id}"], tee=True)
            spawn(["./bs_device_handbrake", f"-s={sim_id}", "-d=1", "-r=10"], tee=False)
            for line in siblings:
                expanded = line.replace("{prefix}", self.prefix)
                argv = shlex.split(expanded) + [f"-s={sim_id}"]
                spawn(argv, tee=True)

            # Phy runs in the foreground and ends the simulation.
            phy = [
                "./bs_2G4_phy_v1",
                f"-s={sim_id}",
                f"-D={2 + len(siblings)}",
                "-sim_length=50e6",
            ]
            try:
                subprocess.run(
                    phy,
                    cwd=str(self.bin_dir),
                    stdout=subprocess.DEVNULL if self.quiet else None,
                    stderr=subprocess.DEVNULL if self.quiet else subprocess.STDOUT,
                    env=self.env,
                    timeout=120,
                )
            except subprocess.TimeoutExpired:
                self.log.wrn(f"[*] phy timed out for {sim_id}; killing devices")

            # Devices exit once the phy disconnects; give them a moment, then
            # reap so their tee threads flush before we read output.log.
            for proc in procs:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            for t in threads:
                t.join(timeout=5)

    def _evaluate(self, case_dir: Path, case_build: Path, output_log: Path, rel: str) -> str:
        patterns = case_dir / "events.patterns"
        snapshot = case_dir / "events.snapshot"
        filtered = case_build / "filtered_output.log"

        # Byte-compatible with run-ble-test.sh:
        #   sort -s -t ':' -k 1,1 output.log | sed -E -n -f events.patterns
        with open(filtered, "w") as out:
            sort = subprocess.Popen(
                ["sort", "-s", "-t", ":", "-k", "1,1", str(output_log)],
                stdout=subprocess.PIPE,
            )
            sed = subprocess.run(
                ["sed", "-E", "-n", "-f", str(patterns)],
                stdin=sort.stdout,
                stdout=out,
            )
            assert sort.stdout is not None
            sort.stdout.close()
            sort.wait()
        if sed.returncode != 0:
            self.log.wrn(f"[*] sed filter returned {sed.returncode} for {rel}")

        diff = subprocess.run(
            ["diff", "-auZ", str(snapshot), str(filtered)],
            capture_output=True,
            text=True,
        )
        if diff.returncode == 0:
            self.log.inf(f"PASS: {rel}")
            return PASS

        if (case_dir / "pending").is_file():
            self.log.inf(f"PENDING: {rel}")
            return PENDING

        if self.auto_accept:
            self.log.inf(f"Auto-accepting failure for {rel}")
            shutil.copy2(filtered, snapshot)
            self.log.inf(f"PASS: {rel}")
            return PASS

        self.log.err(f"FAILED: {rel}")
        if diff.stdout:
            for line in diff.stdout.rstrip().splitlines():
                self.log.inf(line)
        return FAILED

    # ------------------------------------------------------------------
    # Driver
    # ------------------------------------------------------------------

    def run(self, cases: list[Path], parallel: int = 1) -> list[CaseResult]:
        results: list[CaseResult] = []
        if parallel <= 1:
            for case in cases:
                results.append(self.run_case(case))
        else:
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                results = list(pool.map(self.run_case, cases))

        # Aggregate summary (parity with the bash pass-fail.log).
        tests_dir = self.build_root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        with open(tests_dir / "pass-fail.log", "w") as f:
            for r in sorted(results, key=lambda r: r.rel):
                f.write(f"{r.status}: {r.rel}\n")

        self.log.inf("[*] Summary:")
        for r in sorted(results, key=lambda r: r.rel):
            self.log.inf(f"    {r.status}: {r.rel}")
        return results
