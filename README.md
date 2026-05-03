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

You can also flash directly after the build. It internally executes `west flash -d <build dir>`.

```bash
# Using the default runner of the target board (e.g. UF2 for XIAO nrf52840)
$ west zmk-build --flash
# Specify runner (the same as west flash --runner XXXX)
$ west zmk-build --flash jlink
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
