# `ble-studio-central` example host app

A copyable, module-agnostic BLE **central** ("computer") that drives the ZMK
Studio RPC service over BLE, for BabbleSim BLE tests run by
[`west zmk-ble-test`](../../README.md#west-zmk-ble-test).

It scans for the DUT's HIDS advertisement, connects, pairs (Just Works),
exchanges a larger ATT MTU, discovers the ZMK Studio GATT service, subscribes
to its indications, and writes a sequence of framed `zmk.studio.Request`
messages -- hexdumping every de-framed response. Your test's `events.patterns`
/ `events.snapshot` assert on those hexdumps.

The **request payloads are never hand-encoded**. They come from a generated
`requests.inc` (a C table) produced by `generate_requests.py`, which builds
real protobuf messages against your workspace's `zmk-studio-messages` protos.
You edit and run the Python; the C file stays untouched in the common case.

## Files

| File | Purpose |
|---|---|
| `src/main.c` | Generic skeleton (scan/connect/encrypt/MTU/discover/subscribe/de-frame). Includes `requests.inc`. Rarely edited. |
| `generate_requests.py` | **Source of truth** for the request sequence. Edit `build_requests()`, then run it. |
| `requests.inc` | Generated, checked-in C payload table `#include`d by `main.c`. |
| `prj.conf` | BLE central config incl. the MTU/pairing settings Studio needs. |
| `CMakeLists.txt` | Standard Zephyr app; puts this dir on the include path so `requests.inc` is found. |

## Use it in your module

1. Copy this whole directory into your module as
   `tests/ble/<name>_central/` (the `*_central` suffix is what
   `west zmk-ble-test` auto-discovers and builds).

2. Edit `generate_requests.py`'s `build_requests()` to describe the requests
   your test should send. The default is a single core `ListCustomSubsystems`
   request (works for any module using the custom Studio RPC framework); the
   commented `SAMPLE` block shows how to add a custom-subsystem `Call`
   carrying your subsystem's own encoded protobuf payload.

3. Regenerate and commit the include (run from inside your west workspace so
   `west topdir` and the `zmk-studio-messages` project resolve):

   ```bash
   python3 tests/ble/<name>_central/generate_requests.py
   python3 tests/ble/<name>_central/generate_requests.py --check   # in CI/pre-commit
   ```

4. Reference the app from your case's `siblings.txt`. With
   `west zmk-ble-test`, module host apps are staged into `$BSIM_OUT_PATH/bin`
   as both `<prefix>_<name>_central.exe` and a plain `<name>_central.exe`
   alias, so either of these works:

   ```
   ./{prefix}_<name>_central.exe -d=2
   ./<name>_central.exe -d=2
   ```

   (`{prefix}` is expanded by the runner to the active `--sim-prefix`.)

## How the generator finds the helpers

`generate_requests.py` reuses `compile_protos` / `load_studio_pb2` / `frame`
from `zmk-west-commands`' `scripts/lib/renode/`. It locates that directory by,
in order: (1) walking up from the script for a `scripts/lib/renode/` (the
in-repo `examples/` layout); (2) resolving the `zmk-west-commands` project
from the west manifest (`Manifest.from_topdir()`); (3) a recursive search
under `west topdir`. So it works both inside `zmk-west-commands` and when
copied into a consumer module whose workspace lists `zmk-west-commands` as a
west project. It needs `protoc` and the Python `protobuf` package (the same
runtime dependency the Renode smoke test uses -- see `requirements-test.txt`).

## Gotchas (baked into `prj.conf`)

- **MTU**: Studio RPC indications exceed the 23-byte default ATT MTU. The app
  negotiates a larger MTU (`CONFIG_BT_L2CAP_TX_MTU=247`,
  `CONFIG_BT_BUF_ACL_RX_SIZE=251`) before discovery, like real Studio clients.
  Without this, indications are truncated and de-framing fails.
- **Encryption**: the Studio characteristic requires an encrypted link, so the
  app raises security to `BT_SECURITY_L2` (Just Works pairing) on connect and
  only starts discovery after `security_changed` succeeds.
