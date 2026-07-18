# `zmk-renode-test` composite action

Thin wrapper around the `west zmk-renode-test` command (provided by
`zmk-west-commands`). It boots an **already-built** ZMK firmware ELF in the
[Renode](https://renode.io/) emulator, runs a generic boot + core Studio RPC
smoke test, and optionally the module's own `tests/renode/*_test.py` files.

## Contract

- **The caller builds the ELF.** This action does not build firmware. Build a
  `build.yaml` artifact with the `renode-studio-uart` snippet and
  `CONFIG_ZMK_STUDIO=y` in an earlier step (see the repo README's
  `west zmk-renode-test` section).
- **The caller sets up the west workspace.** The action assumes checkout +
  `west init`/`west update` have already run and that `zmk-west-commands` is in
  the manifest (that is where the `west zmk-renode-test` command comes from).
  The action deliberately does **not** reference its own checkout's scripts, so
  the action ref (`@main`) and the consumer's west pin of `zmk-west-commands`
  move independently.
- Runs in the `zmkfirmware/zmk-build-arm:stable` container (or any environment
  with `west` + `python3`). It installs the python `protobuf` runtime and
  `protoc` (with progressive apt/pip fallbacks, since the container has no
  pip/sudo/curl) and caches `~/.renode` keyed on the Renode version.

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `elf-path` | yes | – | Path to the built firmware ELF (relative paths resolve against `$GITHUB_WORKSPACE`). |
| `tests` | no | `""` | Directory of the module's own `*_test.py` files, run after the smoke test. |
| `renode-version` | no | `1.16.1` | Renode portable release to install (must match the checked-in `.repl`). |
| `boot-timeout-seconds` | no | `20` | Seconds to wait for the ZMK boot banner. |

## Usage

```yaml
jobs:
  renode:
    runs-on: ubuntu-latest
    container: zmkfirmware/zmk-build-arm:stable
    steps:
      - uses: actions/checkout@v4
      - name: Init west workspace
        run: |
          west init -l . --mf <your-test-manifest>.yml
          west update --narrow
          west zephyr-export
      - name: Build Renode-testable firmware
        run: west zmk-build tests/zmk-config -af renode
      - uses: cormoran/zmk-west-commands/.github/actions/zmk-renode-test@main
        with:
          elf-path: build/renode/zephyr/zmk.elf
          tests: tests/renode          # optional
```
