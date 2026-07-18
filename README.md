# zmk-west-commands

A west module that provides useful commands for zmk-config build and zmk-module development.

## How to use

Add the following items to your west manifest.

```yaml:west.yml
manifest:
  remotes:
    - name: cormoran
      url-base: https://github.com/cormoran
  projects:
    - name: zmk-west-commands
      remote: cormoran
      revision: main # or latest commit hash
      import: true
```

Then, you can use `west zmk-build` and `west zmk-test` commands.

```bash
$ west update
$ west -h
...
extension commands from project manifest (path: zmk-west-commands):
  zmk-test:             Run ZMK unit tests
  zmk-build:            Build ZMK firmware for given zmk-config
...
# Optionally required to use interactive option
$ pip install -r <path to zmk-west-commands>/requirements.txt
```

### west zmk-build

A small `west build` wrapper command for zmk modules.

This command reads zmk's `build.yaml` and automatically configures options for `west build`.

```bash
$ cd <path to your zmk-config>
$ west zmk-build
```

You can filter build targets if multiple targets exist in `build.yaml`.

```bash
# Select build targets interactively by -i (requires additional dependency)
$ west zmk-build -i

# Specify specific artifact
$ west zmk-build -a mykbd
# Filter build targets by artifact name regex pattern
$ west zmk-build -af 'mykbd-*'
```

You can also flash directly after the build. It internally executes `west flash -d <build dir> --skip-build`.

```bash
# Using the default runner of the target board (e.g. UF2 for XIAO nrf52840)
$ west zmk-build --flash
# Specify runner arguments with `+` prefix
$ west zmk-build --flash +r jlink
# Skip build
$ west zmk-build --flash -sb
```

There are some useful shortcuts to specify cmake arguments:

```bash
# Erase persistent settings (e.g. BLE pairing setting) on restart
# It's the same as --cmake-args ' -DCONFIG_ZMK_SETTINGS_RESET_ON_START'
$ west zmk-build --reset
# Enable USB logging by building with -S zmk-usb-logging
$ west zmk-build --debug-print
# Build with debug mode and enable RTT console for segger jlink
$ west zmk-build --debug-jlink
```

#### VSCode Integration

You can generate VSCode settings for IntelliSense and debugging:

```bash
# Generate .vscode/c_cpp_properties.json and .vscode/launch.json
$ west zmk-build --vscode
```

This creates:

- `.vscode/c_cpp_properties.json` - IntelliSense configuration using compile_commands.json
- `.vscode/launch.json` - Debugging configuration for Cortex-Debug extension with J-Link

Note: Currently only supports nRF52840 boards.

##### Extended behavior

As an extended behavior, this command recognizes the `snippets` field in `build.yaml` to allow specifying multiple snippets.

```yaml:build.yaml
include:
  - artifact: foo
    # snippet: zmk-usb-logging # ZMK's official definition
    snippets:
      - zmk-usb-logging
      - studio-rpc-usb-uart
```

### west zmk-test

This command wraps zmk's `run-test.sh` to set up required environment variables automatically.

```bash
# Run all tests under specified directory
$ west zmk-test <path to zmk test directory> -m <path to your zmk module or zmk-config>
```

```bash
# west zmk-test -h
usage: west zmk-test [-h] [-d BUILD_DIR] [-m [EXTRA_MODULES ...]] [-v] [test_path]

Run the ZMK test suite with zmk's run-test.sh script.

positional arguments:
  test_path             Specify the (parent) test directory to run. The command finds tests recursively by searching `native_posix_64.keymap`. Current directory by default.

options:
  -h, --help            show this help message and exit
  -d BUILD_DIR, --build-dir BUILD_DIR
                        Path to the ZMK build directory to output test artifacts. <west workspace root>/build by default.
  -m [EXTRA_MODULES ...], --extra-modules [EXTRA_MODULES ...]
                        Additional ZMK modules to include during testing. Useful when running test under your zmk-module to include your module itself by specifying zmk-module repository root.
  -v, --verbose         Enable verbose output for west itself and tests.
```

### west zmk-renode-test

Boot an **already-built** ZMK firmware ELF in the [Renode](https://renode.io/)
emulator, run a generic boot + core Studio RPC smoke test, then (optionally)
the module's own `tests/renode/*_test.py` files. Hardware-free — no J-Link, no
physical board. This command never builds firmware; the caller builds the ELF.

```bash
# Smoke test only (boot banner + core Studio GetDeviceInfo round trip)
$ west zmk-renode-test --elf build/renode/zephyr/zmk.elf

# Smoke test + the module's own custom-RPC tests
$ west zmk-renode-test tests/renode --elf build/renode/zephyr/zmk.elf

# Boot-banner only, for a module that does not enable Studio RPC
$ west zmk-renode-test --elf build/renode/zephyr/zmk.elf --no-rpc
```

```
usage: west zmk-renode-test [-h] --elf ELF [--renode-version RENODE_VERSION]
                            [--boot-timeout BOOT_TIMEOUT] [--skip-smoke] [--no-rpc]
                            [--real-binary] [--min-virtual MIN_VIRTUAL]
                            [--storage-addr ADDR] [--storage-size SIZE]
                            [tests_dir]
```

Renode is downloaded automatically on first use (a portable tarball, cached
under `$RENODE_ROOT`, default `~/.renode`). Each `tests_dir/*_test.py` file is
run non-recursively as `python3 <file> -v` with `ZMK_RENODE_ELF` set to the ELF
and the harness (`scripts/lib/renode/`) prepended to `PYTHONPATH`, so a test
file only needs `import renode_harness`.

#### Building a Renode-testable ELF

This repo is also a Zephyr module: it provides the `renode-studio-uart` snippet
and a Renode-only Studio RPC UART transport (both inert unless the snippet is
used — see `renode-test-module/Kconfig`). Real hardware carries Studio RPC over
USB-CDC, which Renode's nRF52840 USBD model cannot present; the snippet binds
Studio RPC + the console to real UART peripherals instead. Add a `build.yaml`
artifact that uses it:

```yaml
include:
  - artifact: renode
    board: xiao_ble//zmk           # an nRF52840 board (the checked-in .repl)
    shield: renode_tester
    cmake-args: -DCONFIG_ZMK_STUDIO=y   # for the core Studio RPC smoke check
    snippets:
      - renode-studio-uart
```

```bash
$ west zmk-build <your-zmk-config> -af renode
$ west zmk-renode-test tests/renode --elf build/renode/zephyr/zmk.elf
```

> `CONFIG_ZMK_STUDIO=y` build-asserts on a `zmk,physical-layout` (with
> `key_physical_attrs`) **and** the absence of a chosen `zmk,matrix-transform`.
> Give your shield/board a keys'd physical layout that references the transform
> directly — see the in-repo example
> `tests/zmk-config/boards/shields/renode_tester/renode_tester.overlay`.

> The HWv2 `xiao_ble//zmk` board (and the node labels the `renode-studio-uart`
> overlay disables) only exist on newer ZMK; this repo's own CI pins ZMK
> `main` for the Renode job (`scripts/west-test-renode.yml`) while the rest of
> the tests stay on `v0.3-branch`.

#### Real-binary mode (`--real-binary`)

The default flow above boots an ELF built with the `renode-studio-uart` snippet
(Studio RPC re-bound to a UART so Renode can drive it). `--real-binary` instead
boots a **real flashable image** — the exact artifact you would flash to a
board, built with ZMK's own `studio-rpc-usb-uart` snippet (USB CDC + QSPI NOR +
BLE all enabled), with **zero firmware-side deviation**:

```bash
# Boot a real xiao_ble image and verify it stays alive (no Zephyr fatal)
$ west zmk-renode-test --real-binary --elf build/zephyr/zmk.elf
```

Renode's nRF52840 has no USBD/QSPI/FICR models, so a real image would hang or
oops on stock Renode. The `xiao_nrf52840_real.repl` platform adds four things
(see that file and `scripts/lib/renode/platforms/models/`):

1. **QSPI stub** (`0x40029000`) — completes the `nrfx_qspi` busy-wait on
   `EVENTS_READY`; the JEDEC probe then mismatches so `nordic_qspi_nor` fails
   gracefully (`-ENODEV`) instead of hanging. The external NOR is not the
   settings backend, so this is harmless.
2. **USBD stub** (`0x40027000`) — returns `EVENTCAUSE.READY` so
   `nrf_usbd_common` enable completes, then reads 0 (no VBUS) so the driver
   idles like an unplugged cable.
3. **FICR model** (`0x10000000`) — serves real `CODEPAGESIZE`/`CODESIZE` (so
   `settings_nvs` sizes its partition instead of failing `-EDOM`) and a BLE
   identity address. Without it, settings never load, BT host init stalls, and
   the HCI Read-BD_ADDR times out into a `BT_ASSERT` oops around 10 s.
4. **NVS preload** — Renode zero-fills flash, but NVS needs erased sectors to
   read `0xFF`, so the storage partition is preloaded with `0xFF` (else
   `nvs_mount` fails `-EDEADLK`). Defaults to the **xiao_ble** `storage_partition`
   (`0xec000`, size `0x8000`); override with `--storage-addr`/`--storage-size`
   for other boards.

A real image speaks Studio RPC only over USB/BLE, so there is no UART transport
to drive. The smoke therefore becomes a **liveness check**: run `--min-virtual`
virtual seconds (default 20), then sample the CPU `PC` a few times and resolve
each symbol. It **fails** if any sample lands in a fatal frame
(`arch_system_halt` / `z_fatal_error` / `k_sys_fatal_error_handler` — a Zephyr
fatal parks the CPU spinning in `arch_system_halt`) and **passes** otherwise;
if the image happens to have a console (observation builds), its output is
captured and also checked for `FATAL ERROR` / `Halting system`, but console
output is not required. Module `tests/renode/*_test.py` still run afterwards
with `ZMK_RENODE_ELF` set plus `ZMK_RENODE_REAL=1` and
`ZMK_RENODE_STORAGE_ADDR`/`ZMK_RENODE_STORAGE_SIZE`, so a test can build its own
real machine via `renode_harness.boot_single_real(...)`.

Limitations (real-binary mode today):

- **No NVMC erase model.** Preloaded `0xFF` gets a clean NVS mount, but there is
  no flash-erase peripheral, so a long session with many settings writes will
  eventually fail NVS garbage collection.
- **Studio RPC is not reachable.** The USB/BLE transports are stubbed to idle,
  not driven — there is no Studio round trip in real mode yet (BLE-encryption /
  CCM work to reach it is a separate, parallel effort).
- **Radio TX is visible but not connectable.** BLE advertising is emitted (you
  can see it on ch 37/38/39), but a connection test needs a wireless medium and
  a peer, which this single-machine platform does not wire up.

#### Requirements

The smoke test's Studio RPC check compiles the workspace's `zmk-studio-messages`
protos, so it needs the python `protobuf` runtime and the `protoc` compiler.
Install the python side with `pip install -r requirements-test.txt` (`protoc` is
a system package, e.g. `apt-get install protobuf-compiler`). Pass `--no-rpc` to
skip this and check only the boot banner.

#### GitHub Action

A thin composite action wraps the command for CI (it installs protobuf/protoc,
caches Renode, and calls `west zmk-renode-test`). It assumes the caller already
ran checkout + `west init`/`west update` with `zmk-west-commands` in the
manifest:

```yaml
- uses: cormoran/zmk-west-commands/.github/actions/zmk-renode-test@main
  with:
    elf-path: build/renode/zephyr/zmk.elf
    tests: tests/renode          # optional
```

See `.github/actions/zmk-renode-test/README.md` for the full contract.

### west zmk-ble-test

Run a module's **BabbleSim (bsim) BLE tests** with no hardware: build the DUT
from the workspace ZMK app with your module added via `ZMK_EXTRA_MODULES`,
build any split peripherals and host ("computer") apps, launch them all under
the bsim 2G4 phy, and diff the filtered device output against a checked-in
snapshot. This is a Python port of the template repo's
`tests/ble/run-ble-test.sh`, kept byte-compatible with its
`sort | sed | diff` pass/fail pipeline.

```bash
# Run every case under tests/ble, with this module added via ZMK_EXTRA_MODULES
$ west zmk-ble-test tests/ble -m .

# A single case
$ west zmk-ble-test tests/ble/split/basic -m .

# Regenerate snapshots (also honors ZMK_TESTS_AUTO_ACCEPT=y)
$ west zmk-ble-test tests/ble -m . --auto-accept

# Run cases concurrently; each case's sim id isolates its phy
$ west zmk-ble-test tests/ble -m . -j 4
```

```
usage: west zmk-ble-test [-h] [-m MODULE] [--auto-accept] [--sim-prefix NAME]
                         [--bsim PATH] [-j PARALLEL] [-v] [tests_path]
```

A directory is a **test case** iff it contains `nrf52_bsim.keymap`; discovery
recurses from `tests_path` (default: current directory). Per-case files:

| File | Meaning |
|---|---|
| `nrf52_bsim.keymap` | marks the case; DUT keymap (needs a keys'd physical layout for Studio) |
| `nrf52_bsim.conf` | Kconfig shared by the DUT and peripherals (via `ZMK_CONFIG`) |
| `central.conf` | extra Kconfig applied to the DUT (central) only (via `EXTRA_CONF_FILE`) |
| `peripheral.conf` | extra Kconfig applied to peripheral builds only |
| `peripheral*.overlay` | one split-peripheral build each; presence ⇒ DUT built as a split central (`-DCONFIG_ZMK_SPLIT_ROLE_CENTRAL=y`) |
| `siblings.txt` | one command line per extra simulated device (`-d=2…`; `-d=0` is the DUT, `-d=1` the handbrake) |
| `studio_requests.json` | declarative `zmk.studio.Request` list (JSON DSL); if present, the shared `ble-studio-host` app is built for this case with these payloads embedded (see below) |
| `studio_requests.hex` | byte-exact escape hatch for the same (one framed request per hex line); mutually exclusive with the `.json` |
| `events.patterns` | `sed -E -n` script filtering the combined output log |
| `events.snapshot` | expected filtered output |
| `pending` | if present, a snapshot mismatch is PENDING instead of FAILED |

Builds land under `<west topdir>/build/ble/`; each case's `output.log`,
`filtered_output.log` and the aggregate `tests/pass-fail.log` are kept there.

**Placeholders in `siblings.txt`.** `--sim-prefix NAME` (default: the
sanitized module directory name) sets the bsim simulation id
(`<prefix>_<case>`) and the staged executable-name prefix. In `siblings.txt`:

- `{prefix}` expands to the active prefix. Lines without placeholders run
  unchanged, so existing case data keeps working.
- `{studio_host}` expands to the case's staged shared-host executable name
  (`<sim id>_studio_host.exe` — only meaningful for cases with a
  `studio_requests.json`/`.hex`).

Custom module host apps (`tests/ble/*_host/`, the documented convention; the
legacy `tests/ble/*_central/` is still auto-discovered for backward compat)
are staged as both `<prefix>_<appname>.exe` and a plain `<appname>.exe`
alias.

**Device numbering & asserting any device (incl. peripherals).** The runner
assigns `-d=0` to the DUT and `-d=1` to the bsim handbrake; **every other
device gets its id from its own `siblings.txt` line** (`-d=2`, `-d=3`, … as
written there — split peripherals are ordinary siblings: the runner stages
`<sim id>_<peripheral>.exe`, the case launches it). Each device prefixes its
stdout with `d_NN: @<sim time>`; the combined `output.log` captures the DUT
and all siblings (the handbrake is not captured). The evaluation pipeline's
stable `sort -t: -k1,1` groups lines per device (ascending id: the `d_00`
block, then `d_02`, `d_03`, …) while preserving each device's own
chronological order — so a snapshot lists one deterministic block per
asserted device, and **any device's lines can be asserted**, not just the
DUT/host. Relabel with a keyword-guarded substitution so only the intended
lines print (a substitution only prints when it matches, so the same keyword
appearing on another device's line is harmless), e.g. `split/basic`'s
peripheral rule:

```sed
/Welcome to ZMK!|security_changed: Security changed|kscan_process_msgq/s/^d_03: @[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}  .{19}/peripheral /p
```

Determinism guidance — bsim runs are deterministic per firmware build, but
prefer lines that stay stable across dependency bumps and avoid:
`*** Booting Zephyr OS build <hash> ***` (changes with every Zephyr/ZMK
revision), raw kscan-mock event encodings (`ev <number> …` — prefer the
decoded `…kscan_process_msgq: Row: …, pressed: …` lines), and HCI
version/build banner lines. Semantic lines (connection/security changes,
CCC subscriptions, position events) are stable and meaningful. When in
doubt, run the case at least twice (ideally with different `--sim-prefix`)
and confirm identical `filtered_output.log`.

**BabbleSim setup.** bsim is Linux-only and comes from ZMK's manifest. Fetch
and build it once, then point the command at it:

```bash
$ west config manifest.group-filter -- +babblesim
$ west update --narrow
$ make -C "$(west topdir)/dependencies/tools/bsim" everything -j"$(nproc)"
$ export BSIM_OUT_PATH="$(west topdir)/dependencies/tools/bsim"
$ export BSIM_COMPONENTS_PATH="$BSIM_OUT_PATH/components"
```

`BSIM_OUT_PATH`/`BSIM_COMPONENTS_PATH` (or `--bsim PATH`) select the compiled
tree; the command errors with these instructions if it is missing or
uncompiled.

**ZMK revision prerequisite.** The bsim BLE tests need two fixes not yet on
`zmkfirmware/zmk` main — a writable behavior local-id map section, and
`settings_subsys_init` before dynamic BLE handler registration (without them
the split central segfaults or never starts BLE on `nrf52_bsim`). Until they
land upstream, pin `cormoran/zmk@fffa339cf6f5c45366ab332d2b512f1c3c300753` in
your test manifest (this repo's `scripts/west-test-ble.yml` does exactly that,
with a TODO to unpin).

**Studio-over-BLE host app (no C, no Python in your module).** To exercise
Studio RPC over BLE (including while the split link is active), your case
ships **one data file**: `studio_requests.json`, an ordered list of
`zmk.studio.Request` messages in protobuf's canonical JSON mapping. A bytes
field (e.g. a custom-subsystem `Call.payload`) may be written as
`{"$type": "<full.message.name>", ...fields}` — the infrastructure resolves
the name against the workspace's Studio protos plus your module's own
`proto/` directory, encodes the message and substitutes the bytes
(recursively); `request_id` is auto-assigned (1-based) when omitted:

```json
[
  { "custom": { "listCustomSubsystems": {} } },
  { "custom": { "call": {
      "subsystemIndex": 0,
      "payload": { "$type": "your_name.template.Request",
                   "sample": { "value": 42 } } } } }
]
```

The runner converts the JSON at test time (needs python `protobuf` + `protoc`
— see `requirements-test.txt`; the CI action installs both), automatically
builds this repo's shared [`ble-studio-host/`](ble-studio-host/) app with the
payloads embedded, and stages it per case; reference it from `siblings.txt`
as `./{studio_host} -d=2`. See
[`ble-studio-host/README.md`](ble-studio-host/README.md) for the full DSL
spec and [`tests/ble/studio/core/`](tests/ble/studio/core/) for a complete
sample case. **Escape hatches:** a byte-exact `studio_requests.hex` (or the
programmatic API in `scripts/lib/ble/studio_requests.py`) for payloads the
JSON mapping cannot express, and a custom host app as
`tests/ble/<name>_host/` (legacy `tests/ble/<name>_central/` still
auto-discovered) for custom host-side logic. Prefer the shared app + JSON
whenever "send requests in order, snapshot the response hexdumps" is enough.

#### GitHub Action

A thin composite action wraps the command for CI (enables the `+babblesim`
group, builds and caches the bsim tree, exports `BSIM_OUT_PATH` /
`BSIM_COMPONENTS_PATH`, and calls `west zmk-ble-test`). It assumes the caller
already ran checkout + `west init`/`west update` with `zmk-west-commands` in
the manifest, and runs in the `zmkfirmware/zmk-build-arm:4.1` container:

```yaml
- uses: cormoran/zmk-west-commands/.github/actions/zmk-ble-test@main
  with:
    tests: tests/ble
    module: .
```

See `.github/actions/zmk-ble-test/README.md` for the full contract.

## Use case

TODO

###

## Development Guide

### Setup

There are two west workspace layout options.

**Option 1: Download dependencies in parent directory**

This option is west's standard way. Choose this option if you want to re-use dependent projects in other zephyr module development.

```bash
mkdir west-workspace
cd west-workspace
git clone https://github.com/cormoran/zmk-west-commands.git
west init -l . --mf scripts/west-test.yml
west update --narrow
west zephyr-export
```

The directory structure becomes as follows:

```
west-workspace
  - .west/config
  - build : build output directory
  - zmk-west-commands: this repository
  # other dependencies
  - zmk
  - zephyr
  - ...
  # You can develop other zephyr modules in this workspace
  - your-other-repo
```

**Option 2: Download dependencies in ./dependencies (Enabled in dev-container)**

Choose this option if you want to download dependencies under this directory (like `node_modules` in npm).
This option is useful for specifying cache target in CI. This layout is easier to understand if you want to isolate dependencies.

Note that `.west` is placed in the parent directory. Creating an empty parent directory is required like option 1 to avoid conflicts with other zephyr module development.

```bash
mkdir west-workspace
cd west-workspace
west init -l . --mf scripts/west-test-standalone.yml
# If you use dev container, start from the following commands. Above commands are executed
# automatically. (but directory name is /workspace instead of west-workspace for dev container)
west update --narrow
west zephyr-export
```

The directory structure becomes as follows:

```
west-workspace
  - .west/config
  - build : build output directory
  - zmk-west-commands: this repository
    - dependencies
      - zmk
      - zephyr
      - ...
```

#### Dev container

The dev container is configured for setup option 2. The container creates the following volumes to re-use resources among containers.

- zmk-dependencies: dependencies dir for setup option 2
- zmk-build: build output directory
- zmk-root-user: /root, the same as ZMK's official dev container

If you don't want to share resources, please rename the volume name in `devcontainer.json`.

### Test

The `./tests` directory contains zmk-config to test west commands.
You can try it with the following commands.

```bash
west zmk-test tests
west zmk-build tests/build_yaml
```

The following test script verifies the output of the above commands to detect regressions.

```bash
python -m unittest
```

### Linting & formatting

```bash
pip install -r requirements-dev.txt
ruff format .
ruff check .
```
