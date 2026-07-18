# `zmk-ble-test` composite action

Thin wrapper around the `west zmk-ble-test` command (provided by
`zmk-west-commands`). It fetches and builds [BabbleSim](https://babblesim.github.io/),
caches the built tree, and runs a ZMK module's bsim BLE tests.

## Contract

- **The caller sets up the west workspace.** The action assumes checkout +
  `west init`/`west update` have already run and that `zmk-west-commands` is in
  the manifest (that is where the `west zmk-ble-test` command comes from). The
  action deliberately does **not** reference its own checkout's scripts, so the
  action ref (`@main`) and the consumer's west pin of `zmk-west-commands` move
  independently.
- **Runs in the `zmkfirmware/zmk-build-arm:4.1` container** (BabbleSim is
  Linux-only; the `4.1` tag matches the Zephyr the bsim board targets).
- It enables the `+babblesim` west group, `west update --narrow`s to fetch the
  bsim sources, `make everything` in the resolved bsim tree, caches that tree
  keyed on the `bsim` project revision, exports `BSIM_OUT_PATH` /
  `BSIM_COMPONENTS_PATH`, and runs `west zmk-ble-test`.
- **ZMK revision prerequisite**: the bsim BLE tests need two fixes not yet on
  `zmkfirmware/zmk` main (writable behavior local-id map section;
  `settings_subsys_init` before dynamic BLE handler registration). Until they
  land upstream, pin `cormoran/zmk@fffa339…` in your test manifest (see the
  repo README's `west zmk-ble-test` section).
- Log upload is left to the caller (`build/ble/**/*.log` under the west topdir).

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `tests` | no | `tests/ble` | BLE tests dir or a single case (relative paths resolve against `$GITHUB_WORKSPACE`). |
| `module` | no | `.` | Module repo root added via `ZMK_EXTRA_MODULES`. |
| `extra-args` | no | `""` | Passed through to `west zmk-ble-test` (e.g. `--sim-prefix mymod -j 2 -v`). |

## Usage

```yaml
jobs:
  ble:
    runs-on: ubuntu-latest
    container: zmkfirmware/zmk-build-arm:4.1
    steps:
      - uses: actions/checkout@v4
      - name: Init west workspace
        run: |
          west init -l . --mf <your-test-manifest>.yml
          west update --narrow
          west zephyr-export
      - uses: cormoran/zmk-west-commands/.github/actions/zmk-ble-test@main
        with:
          tests: tests/ble
          module: .
      - if: always()
        uses: actions/upload-artifact@v4
        with:
          name: ble-test-logs
          path: build/ble/**/*.log
```
