#!/usr/bin/env python3
"""Reusable Renode + ZMK Studio RPC test harness.

Extracted from renode_test.py (the test-zmk-renode skill's own suite) so any
ZMK module repo's test code -- not just this skill's -- can import
RenodeSession / wait_for_text / studio-proto loading without vendoring
renode_test.py itself. This is what `.github/actions/zmk-renode-test/`
exports on PYTHONPATH for a module repo's own `tests/renode/` to import.

Nothing in here is specific to the "studio-rpc-perf" workspace or to any
particular module -- every path is a parameter. See SKILL.md and
references/renode-notes.md for the *why* behind these mechanics (silent
boot hangs, one-client-only UART sockets, etc.); this module only carries
the *how*.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# rpc_client.py lives next to this file regardless of caller's sys.path
# setup (the `zmk-renode-test` command puts this dir on PYTHONPATH, but we
# don't want to *require* that for rpc_client specifically).
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from rpc_client import RpcSocket, frame  # noqa: E402  (re-exported for callers)

# This module lives at scripts/lib/renode/renode_harness.py inside
# zmk-west-commands, with platforms/ (single.resc, split_wired.resc, the
# .repl) and install_renode.sh as its direct siblings under that same dir.
# Keeping them grouped in one directory means the path constants below don't
# depend on where this file is imported from -- true whether it's imported
# from inside this repo or, via PYTHONPATH, from a consuming module repo's
# own test suite (the `west zmk-renode-test` command puts this dir on
# PYTHONPATH). SKILL_DIR is retained as the public name for "the directory
# Renode is launched from" (its cwd; platforms/ must be under it so the
# `.resc` `@platforms/...` paths resolve -- see references/renode-notes.md).
SKILL_DIR = SCRIPTS_DIR
PLATFORMS_DIR = SKILL_DIR / "platforms"
INSTALL_RENODE_SCRIPT = SCRIPTS_DIR / "install_renode.sh"

RENODE_VERSION_DEFAULT = "1.16.1"

__all__ = [
    "RENODE_VERSION_DEFAULT",
    "SKILL_DIR",
    "PLATFORMS_DIR",
    "INSTALL_RENODE_SCRIPT",
    "RpcSocket",
    "frame",
    "renode_root",
    "find_or_install_renode",
    "MonitorConnection",
    "RenodeSession",
    "drain_text",
    "wait_for_text",
    "compile_protos",
    "load_studio_pb2",
    "find_studio_proto_dir",
    "boot_single",
    "boot_single_real",
    "boot_ble_pair",
    "boot_split_wired",
    "STORAGE_ADDR_DEFAULT",
    "STORAGE_SIZE_DEFAULT",
    "DEFAULT_DEVICE_ADDR",
    "device_addr_for_machine",
    "raise_global_quantum",
    "SEGGER_RTT_HELPER",
]

# xiao_ble storage_partition (from the board's zephyr.dts): the internal-flash
# region NVS/settings uses. Renode's MappedMemory zero-fills, but NVS needs
# erased sectors to read 0xFF, so real-binary mode preloads this range with
# 0xFF before `start` (see boot_single_real). Defaults match xiao_ble; override
# for other boards via the CLI flags.
STORAGE_ADDR_DEFAULT = 0xEC000
STORAGE_SIZE_DEFAULT = 0x8000

# Default BLE identity served by the FICR model (see platforms/models/ficr.py):
# the static-random address C0:E7:E7:E7:E7:E7 (48-bit int, MSB 0xC0 first).
# Machine 0 uses this; multi-machine tests derive a distinct address per machine
# via device_addr_for_machine() so two machines never share a BLE address.
DEFAULT_DEVICE_ADDR = 0xC0E7E7E7E7E7

# The Zephyr-aware SEGGER RTT capture helper, `include`d over the monitor when
# boot_single_real(rtt=True) is used (see that function and the file header).
SEGGER_RTT_HELPER = SCRIPTS_DIR / "segger_rtt_writeskip.py"


def raise_global_quantum(session: "RenodeSession", quantum: str) -> None:
    """Raise (or lower) the emulation global time-sync quantum on a live session.

    BLE mode boots at a 10us global quantum (SetGlobalQuantum "0.00001"), which is
    load-bearing through connection + pairing but is also the dominant wall-clock
    cost of two-machine BLE runs (see README's "BLE-mode performance" section): the
    two CPUs re-synchronise every 10us of virtual time, so the emulation runs at
    ~0.10x realtime. Once the encrypted link is up (host STAGE:S4), the soft
    link-layer tolerates a much coarser quantum: raising it to "0.0001" (10x) or
    "0.001" (100x) keeps the connection alive with no disconnect / LL assert and an
    encrypted GATT read (S5) still succeeds -- hardware-in-the-loop-free measured
    ~7x steady-state speedup at "0.001". Use this from a module's own long-running
    BLE test AFTER it has observed the encrypted link, to run the steady-state
    workload ("fine-then-coarse"). Do NOT call it before pairing -- a coarse
    quantum from boot breaks advertising/pairing entirely (verified: even 0.00003
    never connects)."""
    assert session.mon is not None
    session.mon.execute(f'emulation SetGlobalQuantum "{quantum}"')


def device_addr_for_machine(index: int) -> int:
    """Return a deterministic 48-bit BLE static-random address for machine
    `index`, keeping the MSB (top byte) fixed at 0xC0 so it stays a valid
    static-random address (top two bits 0b11). Machine 0 returns the default
    C0:E7:E7:E7:E7:E7; each subsequent machine bumps the low 40 bits by
    `index`, so a two-machine BLE test can call this per machine and get
    distinct identities."""
    msb = DEFAULT_DEVICE_ADDR & 0xFF0000000000
    low = ((DEFAULT_DEVICE_ADDR & 0xFFFFFFFFFF) + index) & 0xFFFFFFFFFF
    return msb | low


# --------------------------------------------------------------------------
# Renode install discovery / bootstrap
# --------------------------------------------------------------------------


def renode_root() -> Path:
    return Path(os.environ.get("RENODE_ROOT", Path.home() / ".renode"))


def find_or_install_renode(
    install_script: Path | None = None, version: str = RENODE_VERSION_DEFAULT
) -> str | None:
    """Return the path to the Renode launcher, installing it via
    `install_script` (install_renode.sh, defaults to this skill's own copy)
    if it's not already present under `renode_root()/<version>/renode`.
    Returns None if neither is possible
    (caller should skip/fail accordingly)."""
    launcher = renode_root() / version / "renode"
    if launcher.is_file() and os.access(launcher, os.X_OK):
        return str(launcher)

    if install_script is None:
        install_script = INSTALL_RENODE_SCRIPT
    if not install_script.is_file():
        return None

    try:
        result = subprocess.run(
            ["bash", str(install_script), version],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    last_line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if last_line and Path(last_line).is_file():
        return last_line
    return str(launcher) if launcher.is_file() else None


# --------------------------------------------------------------------------
# Minimal Renode session: monitor (-P) + one or more UART sockets.
# --------------------------------------------------------------------------


class MonitorConnection:
    def __init__(self, port: int, timeout: float = 20.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.settimeout(2.0)
        self._drain()

    def _drain(self) -> bytes:
        data = b""
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        return data

    def execute(self, command: str, settle: float = 0.3) -> str:
        self._drain()
        self.sock.sendall((command + "\n").encode())
        time.sleep(settle)
        return self._drain().decode(errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class RenodeSession:
    """Launches one Renode process, exposes a monitor connection, and lets
    the caller connect to whatever UART sockets the given .resc script sets
    up. IMPORTANT: connect to each UART socket exactly once and keep it open
    for the whole session -- Renode's CreateServerSocketTerminal only
    reliably serves the first client connection for the life of the process
    (see references/renode-notes.md).

    `cwd` is the directory Renode is launched from; `resc_path` must be
    inside it (or a subdirectory) since `.resc` `i @relative/path`
    directives resolve against Renode's own cwd, not the script's location.
    """

    def __init__(
        self,
        renode_path: str,
        resc_path: Path,
        monitor_port: int,
        variables: dict,
        cwd: Path,
    ):
        self.renode_path = renode_path
        self.resc_path = Path(resc_path)
        self.monitor_port = monitor_port
        self.variables = variables
        self.cwd = Path(cwd)
        self.proc: subprocess.Popen | None = None
        self.mon: MonitorConnection | None = None

    def start(self, boot_wait: float = 3.0) -> None:
        resc_rel = self.resc_path.resolve().relative_to(self.cwd.resolve())
        var_str = "; ".join(f"${k}={v}" for k, v in self.variables.items())
        exec_cmd = f"{var_str}; i @{resc_rel}" if var_str else f"i @{resc_rel}"
        cmd = [
            self.renode_path,
            "--disable-xwt",
            "--hide-log",
            "-P",
            str(self.monitor_port),
            "-e",
            exec_cmd,
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + boot_wait + 10
        last_err = None
        while time.monotonic() < deadline:
            try:
                self.mon = MonitorConnection(self.monitor_port, timeout=2.0)
                return
            except OSError as err:
                last_err = err
                time.sleep(0.3)
        raise TimeoutError(f"Renode monitor never came up on port {self.monitor_port}: {last_err}")

    def go(self) -> None:
        """Issue `start` to begin emulation. Call only after connecting to
        every UART socket you need, per the class docstring."""
        assert self.mon is not None
        self.mon.execute("start")

    def connect_uart(self, port: int, connect_timeout: float = 20.0) -> RpcSocket:
        return RpcSocket(host="127.0.0.1", port=port, connect_timeout=connect_timeout)

    def stop(self) -> None:
        if self.mon is not None:
            self.mon.close()
        if self.proc is not None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def drain_text(sock, timeout: float = 1.0) -> str:
    """Read whatever is currently available on a raw console UART socket."""
    sock.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    return data.decode(errors="replace")


def wait_for_text(sock, needle: str, timeout: float) -> str:
    """Poll a console socket until `needle` appears in the accumulated text,
    or the timeout elapses. Returns everything read (for debugging)."""
    deadline = time.monotonic() + timeout
    buf = ""
    while time.monotonic() < deadline:
        buf += drain_text(sock, timeout=0.5)
        if needle in buf:
            return buf
    return buf


# --------------------------------------------------------------------------
# Protobuf message helpers (compile protos on the fly with protoc)
# --------------------------------------------------------------------------


def compile_protos(proto_files, include_dirs, out_dir: Path | None = None) -> Path:
    """Compile the given .proto files with protoc's Python plugin into
    `out_dir` (a fresh temp dir if not given), add that dir to sys.path, and
    return it. Caller then does e.g. `import studio_pb2`.

    Raises RuntimeError on protoc failure (missing protoc, bad .proto,
    etc.) -- callers that want unittest-style skip-on-missing-toolchain
    behavior should catch this and re-raise as unittest.SkipTest.
    """
    if out_dir is None:
        out_dir = Path(tempfile.mkdtemp(prefix="zmk-proto-"))
    else:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

    cmd = (
        ["protoc"]
        + [f"-I{d}" for d in include_dirs]
        + [f"--python_out={out_dir}"]
        + [str(p) for p in proto_files]
    )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"protoc failed: {result.stderr}")

    # Old protoc (<3.19) generates descriptor code that only works with the
    # protobuf runtime's pure-Python implementation.
    os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
    sys.path.insert(0, str(out_dir))
    return out_dir


def find_studio_proto_dir(west_topdir: Path) -> Path:
    """Auto-discover zmk-studio-messages' `proto/zmk` dir under a west
    topdir. Works for both this skill's own workspace and any module repo
    using the standard `dependencies/modules/msgs/zmk-studio-messages`
    west-manifest layout (falls back to a recursive search if the layout
    differs)."""
    west_topdir = Path(west_topdir)
    direct = (
        west_topdir / "dependencies" / "modules" / "msgs" / "zmk-studio-messages" / "proto" / "zmk"
    )
    if direct.is_dir():
        return direct

    matches = sorted(west_topdir.glob("**/zmk-studio-messages/proto/zmk"))
    if matches:
        return matches[0]

    raise FileNotFoundError(
        f"could not find zmk-studio-messages proto dir under {west_topdir} "
        "(expected dependencies/modules/msgs/zmk-studio-messages/proto/zmk)"
    )


def load_studio_pb2(proto_dir: Path):
    """Compile all of zmk-studio-messages' proto/zmk/*.proto (core.proto,
    custom.proto, studio.proto, ...) in one protoc invocation and import
    the top-level studio_pb2 module (which imports the others as needed).
    `proto_dir` is the `proto/zmk` dir itself (see find_studio_proto_dir)."""
    proto_dir = Path(proto_dir)
    if not proto_dir.is_dir():
        raise FileNotFoundError(f"zmk-studio-messages proto dir not found: {proto_dir}")

    proto_files = sorted(str(p) for p in proto_dir.glob("*.proto"))
    compile_protos(proto_files, include_dirs=[proto_dir])
    import studio_pb2  # type: ignore

    return studio_pb2


# --------------------------------------------------------------------------
# Convenience: boot a single-board ELF using platforms/single.resc.
# --------------------------------------------------------------------------


def boot_single(
    renode_path: str,
    elf: Path,
    boot_wait: float = 3.0,
    port_base: int | None = None,
) -> tuple["RenodeSession", "RpcSocket", "RpcSocket"]:
    """Boot `elf` under Renode using this skill's platforms/single.resc
    (console on uart0, Studio RPC on uart1 -- see overlays/studio-rpc-uart.overlay).
    Returns (session, console_socket, rpc_socket); caller is responsible for
    calling session.stop() (and closing the sockets) when done, e.g. via
    unittest's addCleanup or a try/finally. Does NOT wait for the boot
    banner or start the emulation running -- call session.go() is already
    done here, but asserting on the banner/RPC round-trip is left to the
    caller since expectations differ per test.
    """
    if port_base is None:
        import random

        port_base = random.randint(26000, 40000)

    session = RenodeSession(
        renode_path,
        PLATFORMS_DIR / "single.resc",
        monitor_port=port_base,
        variables={
            "bin": f"@{elf}",
            "console_port": port_base + 1,
            "rpc_port": port_base + 2,
        },
        cwd=SKILL_DIR,
    )
    session.start(boot_wait=boot_wait)
    console = session.connect_uart(port_base + 1)
    rpc = session.connect_uart(port_base + 2)
    session.go()
    return session, console, rpc


# --------------------------------------------------------------------------
# Convenience: boot a REAL flashable image (USB CDC + QSPI + BLE) using
# platforms/single_real.resc + xiao_nrf52840_real.repl (see
# docs/renode-internals.md for what the platform stubs do and why).
# --------------------------------------------------------------------------


def _materialize_ficr(device_addr: int) -> str:
    """Write a temp copy of platforms/models/ficr.py with its DEVICEADDR0/
    DEVICEADDR1 constants rewritten to `device_addr` (a 48-bit BLE address, MSB
    first). Used so each machine in a multi-machine emulation can serve a
    distinct BLE identity -- two machines sharing FICR DEVICEADDR would advertise
    the same address and break BLE tests. Returns the temp file path (caller
    deletes it once the platform has loaded)."""
    addr0 = device_addr & 0xFFFFFFFF  # DEVICEADDR[0] = low 32 bits
    addr1 = (device_addr >> 32) & 0xFFFF  # DEVICEADDR[1] = high 16 bits
    src = (PLATFORMS_DIR / "models" / "ficr.py").read_text()
    src = re.sub(r"^DEVICEADDR0 = .*$", f"DEVICEADDR0 = {hex(addr0)}", src, count=1, flags=re.M)
    src = re.sub(r"^DEVICEADDR1 = .*$", f"DEVICEADDR1 = {hex(addr1)}", src, count=1, flags=re.M)
    fd, path = tempfile.mkstemp(prefix="zmk-ficr-", suffix=".py")
    with os.fdopen(fd, "w") as fh:
        fh.write(src)
    return path


def _materialize_real_repl(ficr_path: str | None = None) -> str:
    """Write a temp copy of platforms/xiao_nrf52840_real.repl with the model
    `filename:` paths rewritten to absolute. Renode resolves PythonPeripheral
    filenames against neither the .repl dir nor its cwd, so the checked-in repl
    keeps them repo-relative (readable) and we make them absolute here. Returns
    the temp file path (caller deletes it once the platform has loaded).

    If `ficr_path` is given (an already-materialized per-machine ficr .py, see
    _materialize_ficr), the FICR model's filename is pointed at it instead of the
    checked-in models/ficr.py -- this is how a per-machine BLE address is injected
    without touching the other model stubs."""
    template = (PLATFORMS_DIR / "xiao_nrf52840_real.repl").read_text()
    abs_models = str((PLATFORMS_DIR / "models").resolve())
    repl = template.replace('filename: "platforms/models/', f'filename: "{abs_models}/')
    if ficr_path is not None:
        repl = repl.replace(f'filename: "{abs_models}/ficr.py"', f'filename: "{ficr_path}"')
    fd, path = tempfile.mkstemp(prefix="xiao_nrf52840_real-", suffix=".repl")
    with os.fdopen(fd, "w") as fh:
        fh.write(repl)
    return path


def _materialize_ccm_repl() -> str:
    """Write a temp copy of platforms/ccm.repl with the models/ccm.py
    `filename:` rewritten to absolute (same reason as _materialize_real_repl --
    Renode does not resolve a PythonPeripheral filename against the .repl dir or
    its cwd). Returns the temp file path (caller deletes it once loaded)."""
    template = (PLATFORMS_DIR / "ccm.repl").read_text()
    abs_ccm = str((PLATFORMS_DIR / "models" / "ccm.py").resolve())
    repl = template.replace('filename: "platforms/models/ccm.py"', f'filename: "{abs_ccm}"')
    fd, path = tempfile.mkstemp(prefix="zmk-ccm-", suffix=".repl")
    with os.fdopen(fd, "w") as fh:
        fh.write(repl)
    return path


def _write_ff_binary(size: int) -> str:
    """Write a temp `size`-byte all-0xFF file to preload as erased NVS sectors
    (Renode zero-fills flash; NVS needs 0xFF to see erased sectors)."""
    fd, path = tempfile.mkstemp(prefix="zmk-nvs-ff-", suffix=".bin")
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"\xff" * size)
    return path


def boot_single_real(
    renode_path: str,
    elf: Path,
    storage_addr: int = STORAGE_ADDR_DEFAULT,
    storage_size: int = STORAGE_SIZE_DEFAULT,
    boot_wait: float = 3.0,
    port_base: int | None = None,
    device_addr: int | None = None,
    rtt: bool = False,
) -> tuple["RenodeSession", "RpcSocket", "RpcSocket"]:
    """Boot a real flashable `elf` under Renode using platforms/single_real.resc
    (the USBD/QSPI/FICR/NVMC-stub platform) with the storage partition preloaded
    as erased 0xFF sectors. Returns (session, console_socket, rpc_socket); as with
    boot_single the caller owns cleanup (session.stop() + closing sockets).

    A real image has no UART Studio transport, so `rpc_socket` here is just the
    (idle) uart1 terminal -- kept for symmetry and for a module's own tests.
    uart0 (console_socket) carries a console only for observation builds; a pure
    real image is silent, so liveness is judged by PC-symbol sampling, not UART
    output -- see renode_smoke.run_liveness_smoke.

    `device_addr` (a 48-bit BLE static-random address, MSB first) overrides the
    FICR DEVICEADDR the image advertises; the default (None) keeps the checked-in
    ficr.py value (C0:E7:E7:E7:E7:E7). A future two-machine harness passes a
    distinct address per machine -- see device_addr_for_machine().

    `rtt=True` sets up Zephyr-aware SEGGER RTT capture (segger_rtt_writeskip.py):
    an RTT VirtualConsole is created, hooked, and exposed on port_base+3, and the
    connected socket is stashed on `session.rtt_socket` for the caller to read
    (RTT-logging builds only -- CONFIG_LOG + CONFIG_USE_SEGGER_RTT +
    CONFIG_LOG_BACKEND_RTT; on a non-RTT build the hook install is a graceful
    no-op and the socket stays silent). session.rtt_socket is None when rtt=False.
    """
    if port_base is None:
        import random

        port_base = random.randint(26000, 40000)

    ficr_path = _materialize_ficr(device_addr) if device_addr is not None else None
    repl_path = _materialize_real_repl(ficr_path)
    ff_path = _write_ff_binary(storage_size)
    session = RenodeSession(
        renode_path,
        PLATFORMS_DIR / "single_real.resc",
        monitor_port=port_base,
        variables={
            "bin": f"@{elf}",
            "console_port": port_base + 1,
            "rpc_port": port_base + 2,
            "platform": f"@{repl_path}",
        },
        cwd=SKILL_DIR,
    )
    session.rtt_socket = None
    try:
        session.start(boot_wait=boot_wait)
        # connect_uart blocks until the resc's CreateServerSocketTerminal lines
        # run (which come after LoadPlatformDescription), so by here the temp
        # repl has been consumed and the platform is loaded.
        console = session.connect_uart(port_base + 1)
        rpc = session.connect_uart(port_base + 2)
        assert session.mon is not None
        if rtt:
            # The resc has already LoadELF'd (needed so the RTT symbol resolves),
            # so we can include the helper, create+hook the RTT console and expose
            # it as a socket terminal -- all before `start`, so no early RTT bytes
            # are lost. setup_segger_rtt_wskip is a no-op if the symbol is absent.
            rtt_port = port_base + 3
            session.mon.execute(f"include @{SEGGER_RTT_HELPER}")
            session.mon.execute('machine CreateVirtualConsole "segger_rtt"')
            session.mon.execute("setup_segger_rtt_wskip sysbus.segger_rtt")
            session.mon.execute(
                f'emulation CreateServerSocketTerminal {rtt_port} "rtt_term" false'
            )
            session.mon.execute("connector Connect sysbus.segger_rtt rtt_term")
            session.rtt_socket = session.connect_uart(rtt_port)
        # Preload erased NVS sectors before the CPU runs (LoadBinary reads the
        # file synchronously here, so it is safe to delete afterwards).
        session.mon.execute(f"sysbus LoadBinary @{ff_path} {hex(storage_addr)}")
        session.go()
    finally:
        for tmp in (repl_path, ff_path, ficr_path):
            if tmp is None:
                continue
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return session, console, rpc


# --------------------------------------------------------------------------
# Convenience: boot TWO real images on one BLE medium for Studio-over-BLE
# tests (platforms/two_machine_ble.resc + the fake CCM). DUT = unmodified real
# ZMK BLE image (advertiser); host = the renode-ble-host app (scan/connect/
# pair/encrypted read). See README.md's Studio-over-BLE section for what this
# proves and the (non-cryptographic) fake-CCM disclaimer.
# --------------------------------------------------------------------------


def boot_ble_pair(
    renode_path: str,
    dut_elf: Path,
    host_elf: Path,
    storage_addr: int = STORAGE_ADDR_DEFAULT,
    storage_size: int = STORAGE_SIZE_DEFAULT,
    boot_wait: float = 4.0,
    port_base: int | None = None,
    renode_log: Path | None = None,
) -> tuple["RenodeSession", "RpcSocket", "RpcSocket", "RpcSocket"]:
    """Boot two real flashable images on one BLE medium under Renode using
    platforms/two_machine_ble.resc, so the host image can pair with and do an
    encrypted Studio-RPC GATT read against the DUT.

    `dut_elf` is an unmodified real ZMK BLE image (peripheral/advertiser);
    `host_elf` is the renode-ble-host app (the simulated computer). Both boot on
    the USBD/QSPI/FICR/NVMC-stub real platform, each with a distinct FICR BLE
    identity (device_addr_for_machine(0)/(1) -- two machines must not share a
    BLE address), plus the fake AES-CCM peripheral injected into BOTH machines
    so the encrypted link can come up (Renode has no CCM model -- see
    platforms/models/ccm.py; this is an identity transform, NOT real crypto).

    Returns (session, dut_console, dut_rpc, host_console); the caller owns
    cleanup (session.stop() + closing the sockets). The DUT NVS storage
    partition is preloaded with erased 0xFF sectors before `start` (as in
    boot_single_real). If `renode_log` is given, Renode's log is written there
    (so a caller can scan it for radio "trimming" warnings).
    """
    if port_base is None:
        import random

        port_base = random.randint(26000, 40000)

    dut_ficr = _materialize_ficr(device_addr_for_machine(0))
    host_ficr = _materialize_ficr(device_addr_for_machine(1))
    dut_repl = _materialize_real_repl(dut_ficr)
    host_repl = _materialize_real_repl(host_ficr)
    ccm_repl = _materialize_ccm_repl()
    ff_path = _write_ff_binary(storage_size)
    tmps = [dut_ficr, host_ficr, dut_repl, host_repl, ccm_repl, ff_path]

    session = RenodeSession(
        renode_path,
        PLATFORMS_DIR / "two_machine_ble.resc",
        monitor_port=port_base,
        variables={
            "dut_bin": f"@{dut_elf}",
            "host_bin": f"@{host_elf}",
            "dut_platform": f"@{dut_repl}",
            "host_platform": f"@{host_repl}",
            "ccm": f"@{ccm_repl}",
            "d_console": port_base + 1,
            "d_rpc": port_base + 2,
            "h_console": port_base + 3,
        },
        cwd=SKILL_DIR,
    )
    try:
        session.start(boot_wait=boot_wait)
        assert session.mon is not None
        if renode_log is not None:
            session.mon.execute(f"logFile @{renode_log}")
        # connect_uart blocks until the resc's CreateServerSocketTerminal lines
        # have run, so the temp platforms have been consumed by here.
        dut_console = session.connect_uart(port_base + 1)
        dut_rpc = session.connect_uart(port_base + 2)
        host_console = session.connect_uart(port_base + 3)
        # Preload the DUT's erased NVS sectors before the CPUs run. LoadBinary is
        # machine-scoped, so select the DUT first (the resc leaves "host"
        # selected as the last-created machine). The host app keeps keys in RAM
        # (no NVS backend), so it needs no preload.
        session.mon.execute('mach set "dut"')
        session.mon.execute(f"sysbus LoadBinary @{ff_path} {hex(storage_addr)}")
        session.go()
    finally:
        for tmp in tmps:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return session, dut_console, dut_rpc, host_console


# --------------------------------------------------------------------------
# Convenience: boot TWO plain (snippet/overlay-built) images as a WIRED split
# pair (platforms/split_wired.resc). The two halves' split-link UARTs (uart1)
# are cross-connected through a Renode UART hub so the emulated central and
# peripheral talk over a virtual wire -- ZMK's `zmk,wired-split` transport, no
# BLE. Each half's console (uart0) is exposed on its own TCP socket. See
# docs/renode-testing.md's split-mode section and docs/renode-internals.md.
# --------------------------------------------------------------------------


def boot_split_wired(
    renode_path: str,
    central_elf: Path,
    peripheral_elf: Path,
    boot_wait: float = 3.0,
    port_base: int | None = None,
) -> tuple["RenodeSession", "RpcSocket", "RpcSocket"]:
    """Boot a wired-split pair under Renode using platforms/split_wired.resc: two
    machines ("central" + "peripheral"), each with its console on uart0 (own TCP
    socket) and its split link on uart1, both uart1s cross-connected through a
    single Renode UART hub ("split_link") so the two emulated boards form a
    point-to-point wired link.

    Returns (session, central_console, peripheral_console); as with boot_single
    the caller owns cleanup (session.stop() + closing the sockets). Both consoles
    are connected before `start` so no early boot-banner bytes are lost.

    The plain xiao_nrf52840.repl is used (no USB/QSPI/FICR stubs): a wired-split
    image built with a split overlay/snippet disables USB + QSPI and does not
    enable BLE, so it needs none of the real-image platform help.

    IMPORTANT boot-order gotcha (see references/renode-notes.md): there is no
    cross-machine execution-order guarantee at t=0, so a peripheral event fired
    in the first few ms can race the central's UART RX-enable and be dropped. A
    caller that wants to observe a relayed event should wait for BOTH boot
    banners and then settle ~2-3 s before generating a cross-machine event."""
    if port_base is None:
        import random

        port_base = random.randint(26000, 40000)

    session = RenodeSession(
        renode_path,
        PLATFORMS_DIR / "split_wired.resc",
        monitor_port=port_base,
        variables={
            "central_bin": f"@{central_elf}",
            "peripheral_bin": f"@{peripheral_elf}",
            "central_console_port": port_base + 1,
            "peripheral_console_port": port_base + 2,
        },
        cwd=SKILL_DIR,
    )
    session.start(boot_wait=boot_wait)
    central_console = session.connect_uart(port_base + 1)
    peripheral_console = session.connect_uart(port_base + 2)
    session.go()
    return session, central_console, peripheral_console
