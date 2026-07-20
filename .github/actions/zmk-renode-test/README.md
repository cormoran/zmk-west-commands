# `zmk-renode-test` composite action

Thin wrapper around the `west zmk-renode-test` command (provided by
`zmk-west-commands`). It boots an **already-built** ZMK firmware ELF in the
[Renode](https://renode.io/) emulator, runs a boot + Studio smoke test, and
optionally the module's own `tests/renode/*_test.py` files. Four modes
(`mode: ble` default, `mode: usb`, `mode: split`, `mode: ble-split`) — see the
repo README's `west zmk-renode-test` section and `docs/renode-testing.md`.

## Contract

- **The caller builds the ELF.** This action does not build firmware. For the
  default `ble` mode — and for `usb` mode, which runs the **same** artifact —
  build the exact `studio-rpc-usb-uart` hardware image (no extra config) in an
  earlier step (see the repo README's `west zmk-renode-test` section). ble mode
  with `host-elf` also needs the `renode-ble-host` app built (see below).
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
| `elf-path` | yes | – | Path to the built DUT firmware ELF (relative paths resolve against `$GITHUB_WORKSPACE`). For the default `ble` mode **and** `usb` mode this is the real `studio-rpc-usb-uart` image (one build serves both). |
| `mode` | no | `ble` | `ble` (real hardware image over emulated BLE, no extra config), `usb` (the same real image, Studio RPC over the emulated USB CDC), `split` (wired split: `elf-path` central + `peripheral-elf` peripheral), or `ble-split` (wireless split: central + `peripheral-elf` + `host-elf`). |
| `host-elf` | no | `""` | ble / ble-split mode: the built `renode-ble-host` app ELF. ble: given → full S4/S5 smoke; omitted → boot-liveness only. Required for `mode: ble-split`. |
| `peripheral-elf` | no | `""` | split / ble-split mode: the built peripheral half's ELF (`elf-path` is the central). Required for `mode: split` / `ble-split`. |
| `tests` | no | `""` | Directory of the module's own `*_test.py` files, run after the smoke test. |
| `renode-version` | no | `1.16.1` | Renode portable release to install (must match the checked-in `.repl`). |
| `boot-timeout-seconds` | no | `20` | split/usb mode: seconds to wait for the boot banner. |

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
      - name: Build the real (studio-rpc-usb-uart) firmware
        run: west zmk-build tests/zmk-config -af ble
      - name: Build the renode-ble-host app
        # match the target-name prefix to your DUT's CONFIG_ZMK_KEYBOARD_NAME
        run: >-
          west build -b nrf52840dk/nrf52840 -d build/ble-host
          -s <zmk-west-commands checkout>/renode-ble-host
          -- -DCONFIG_RENODE_BLE_HOST_TARGET_NAME='"<your DUT name>"'
      - uses: cormoran/zmk-west-commands/.github/actions/zmk-renode-test@main
        with:
          # default mode is `ble`
          elf-path: build/ble/zephyr/zmk.elf
          host-elf: build/ble-host/zephyr/zephyr.elf  # optional (else liveness)
          tests: tests/renode                         # optional
```
