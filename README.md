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

# Filter build targets by artifact name (Build if artifact name=*mykbd*)
$ west zmk-build -a mykbd
```

You can also flash directly after the build. It internally executes `west flash -d <build dir>`.

```
# Using the default runner of the target board (e.g. UF2 for XIAO nrf52840)
$ west zmk-build --flash
# Specify runner (the same as west flash --runner XXXX)
$ west zmk-build --flash jlink
```

There are some useful shortcuts to specify cmake arguments:

```
# Erase persistent settings (e.g. BLE pairing setting) on restart
# It's the same as --cmake-args ' -DCONFIG_ZMK_SETTINGS_RESET_ON_START'
$ west zmk-build --reset
# Build with debug mode and enable RTT console for segger jlink
$ west zmk-build --debug-jlink
```

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

<details>

<summary>Full descriptions</summary>

```
$ west zmk-build -h
usage: west zmk-build [-h] [-d BUILD_DIR] [-m [EXTRA_MODULES ...]] [--extra-module-auto-discovery [{zmk-config,current,walk-up,none} ...]] [--build-yaml BUILD_YAML] [-b BOARD [BOARD ...]] [-s SHIELD [SHIELD ...]]
                      [-S SNIPPET [SNIPPET ...]] [-a ARTIFACT] [-as ARTIFACT_SUFFIX] [--cmake-args CMAKE_ARGS] [-q] [-n] [-i] [-P PARALLELISM] [-p {auto,always,never}] [--debug-jlink] [--reset] [--flash [FLASH]]
                      [config_path] [west_args ...]

Build ZMK firmware with specified zmk-config directory using west build. The command parses build.yaml to set up the build target automatically.

positional arguments:
  config_path           path to your zmk-config/config directory
  west_args             Additional arguments to pass to the `west build` command. Should be prepended with -- like `-- -p -n`

options:
  -h, --help            show this help message and exit
  -d BUILD_DIR, --build-dir BUILD_DIR
                        Path to the directory to output test artifacts. Artifact name is appended and the output results in `<build dir>/<artifact name>/`. `<west workspace root>/build` by default.
  -m [EXTRA_MODULES ...], --extra-modules [EXTRA_MODULES ...]
                        Additional ZMK modules to include. When building your zmk-config, root of the zmk-config should be specified.
  --extra-module-auto-discovery [{zmk-config,current,walk-up,none} ...]
                        Strategies to find extra modules automatically. 'zmk-config': add parent of config directory as extra module if zephyr/module.yml exists there 'current': add current working directory as extra module if
                        zephyr/module.yml exists there 'walk-up': walk up from parent of current directory to find zephyr/module.yml and add the first matched directory as extra module 'none': to disable auto discovery
  --build-yaml BUILD_YAML
                        Path to build.yaml file. By default, searched in order: <config_path>/../build.y[a]ml (zmk-config's official way) -> <config_path>/build.y[a]ml (this command's extension). In addition to ZMK's offical
                        definition, 'snippets' field is recognized to specify multiple snippets
  -b BOARD [BOARD ...], --board BOARD [BOARD ...]
                        Specify the target boards to build for. Prioritized over build.yaml setting (=works as filter if build.yaml found).
  -s SHIELD [SHIELD ...], --shield SHIELD [SHIELD ...]
                        Specify the shields to build for. Prioritized over build.yaml setting (=works as filter if build.yaml found).
  -S SNIPPET [SNIPPET ...], --snippet SNIPPET [SNIPPET ...]
                        Specify snippets to build for. Merged with build.yaml setting.
  -a ARTIFACT, --artifact ARTIFACT
                        Used for build directory naming. Prioritized over build.yaml setting. Works as filter for build.yaml records with artifact name. Artifact .uf2 file will be placed at <build dir>/<artifact name>/zephyr/zmk.uf2
                        zmk-config directory name by default if build target is only one. If multiple build targets are specified, board name and shield name are appended to artifact name. If --reset is specified, '_reset' is appended
                        to artifact name. If --debug-jlink is specified, '_debug' is appended to artifact name.
  -as ARTIFACT_SUFFIX, --artifact-suffix ARTIFACT_SUFFIX
                        Suffix to append to artifact name for build directory naming.
  --cmake-args CMAKE_ARGS
                        Additional arguments to pass like `west build -- <cmake-args>`. Merged with build.yaml. Need to be passed as string with white space like --cmake-args ' -D foo -D bar'
  -q, --quiet           Reduce output verbosity if build succeeds.
  -n, --no-run          Skip build and just outputs list of detected build targets.
  -i, --interactive     Interactively select build target from all detected candidates. Requires `pip install -r <path to zmk-west-commands>/requirements.txt` to work.
  -P PARALLELISM, --parallelism PARALLELISM
                        Number of parallel build jobs. Defaults to number of CPU cores.
  -p {auto,always,never}, --pristine {auto,always,never}
                        pristine build folder setting (the same to west build argument)
  --debug-jlink         Build for debug with Segger J-Link and RTT console.
  --reset               ' Build with reset settings on startup mode by specifying -DCONFIG_ZMK_SETTINGS_RESET_ON_START
  --flash [FLASH]       Flash the built firmware after successful build. Optional argument to specify the runner to flash to. (The same to west flash --runner)
```

</details>

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
