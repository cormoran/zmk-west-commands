# Development Guide

How to set up a west workspace to develop and test `zmk-west-commands` itself.

## Setup

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

### Dev container

The dev container is configured for setup option 2. The container creates the following volumes to re-use resources among containers.

- zmk-dependencies: dependencies dir for setup option 2
- zmk-build: build output directory
- zmk-root-user: /root, the same as ZMK's official dev container

If you don't want to share resources, please rename the volume name in `devcontainer.json`.

## Test

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

## Linting & formatting

```bash
pip install -r requirements-dev.txt
ruff format .
ruff check .
```
