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

Then, you can use `west zmk-*` commands like below.

```bash
$ west update
$ west -h
...
extension commands from project manifest (path: zmk-west-commands):
  zmk-build:            Build ZMK firmware for given zmk-config
  zmk-test:             Run ZMK unit tests
  zmk-renode-test:      Run ZMK Renode emulator tests against a pre-built ELF
  zmk-ble-test:         Run ZMK module BabbleSim (bsim) BLE tests
...
# Optionally required to use interactive option
$ pip install -r <path to zmk-west-commands>/requirements.txt
```

| Command | What it does | Details |
|---|---|---|
| `west zmk-build` | Build (and flash) ZMK firmware for a zmk-config, driven by its `build.yaml`. | [docs/zmk-build.md](docs/zmk-build.md) |
| `west zmk-test` | Run ZMK's `native_sim` unit tests with the environment set up automatically. | [docs/zmk-test.md](docs/zmk-test.md) |
| `west zmk-renode-test` | Boot a pre-built firmware ELF in the Renode emulator and run boot + Studio smoke tests — no hardware. | [docs/renode-testing.md](docs/renode-testing.md) |
| `west zmk-ble-test` | Run a module's BabbleSim (bsim) BLE tests — no hardware. | [docs/zmk-ble-test.md](docs/zmk-ble-test.md) |

### west zmk-build

A small `west build` wrapper command for zmk modules. This command reads zmk's
`build.yaml` and automatically configures options for `west build`.

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

See **[docs/zmk-build.md](docs/zmk-build.md)** for flashing, the shortcut flags,
VSCode integration, and the extended `build.yaml` behavior.

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
  test_path             Specify the (parent) test directory to run. The command finds tests recursively by searching `native_sim.keymap`. Current directory by default.

options:
  -h, --help            show this help message and exit
  -d BUILD_DIR, --build-dir BUILD_DIR
                        Path to the ZMK build directory to output test artifacts. <west workspace root>/build by default.
  -m [EXTRA_MODULES ...], --extra-modules [EXTRA_MODULES ...]
                        Additional ZMK modules to include during testing. Useful when running test under your zmk-module to include your module itself by specifying zmk-module repository root.
  -v, --verbose         Enable verbose output for west itself and tests.
```

See **[docs/zmk-test.md](docs/zmk-test.md)** for the test-case directory layout.

### west zmk-renode-test

Boot an **already-built** ZMK firmware ELF in the [Renode](https://renode.io/)
emulator and run a boot + Studio smoke test. No hardware needed. The caller
builds the ELF, and this command only runs it.

There are **four modes** (`--mode`, default `ble`). `--elf` is the DUT (the
central half in `wired-split` / `ble-split`):

| Mode | What it proves |
|---|---|
| **`ble`** (default) | Boots the exact `studio-rpc-usb-uart` **hardware** image with no extra config. With `--host-elf`: LE pairing + an encrypted Studio GATT read. Without it: boot liveness. |
| **`usb`** | The same real image, driving Studio RPC over the emulated **USB CDC** (fast, no BLE pairing cost) — the natural choice for module RPC tests. |
| **`wired-split`** | A **wired-split** pair whose central still speaks Studio over the emulated **USB CDC**: a Studio round trip over USB, plus a keypress injected on the peripheral relayed over the wired split UART to the central. |
| **`ble-split`** | A **wireless split** end to end: the encrypted split link comes up, then the host does an encrypted Studio read *through* the central. |

```bash
# ble mode (default): build the exact hardware image, then smoke it
$ west zmk-build <your-zmk-config> -af <your studio-rpc-usb-uart artifact>
$ west zmk-renode-test --elf build/<artifact>/zephyr/zmk.elf
```

See **[docs/renode-testing.md](docs/renode-testing.md)** for per-mode recipes,
flags, the CI action, and troubleshooting.
**[docs/renode-internals.md](docs/design/renode-internals.md)** covers how a real image
boots under emulation.

### west zmk-ble-test

Run a module's **BabbleSim (bsim) BLE tests** with no hardware. It builds the
DUT (plus any split peripherals and host apps), runs them under the bsim 2G4
phy, and diffs the device output against a checked-in snapshot.

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

See **[docs/zmk-ble-test.md](docs/zmk-ble-test.md)** for the test-case directory
layout, BabbleSim setup, and the Studio-over-BLE host-app DSL.

## GitHub Actions

Thin composite actions wrap the commands above for CI, documented alongside each
command:

- **`zmk-renode-test`** — see [docs/renode-testing.md § GitHub Action](docs/renode-testing.md#github-action)
- **`zmk-ble-test`** — see [docs/zmk-ble-test.md § GitHub Action](docs/zmk-ble-test.md#github-action)

## Development Guide

See **[docs/development.md](docs/development.md)** for how to set up a west
workspace to develop and test `zmk-west-commands` itself.
