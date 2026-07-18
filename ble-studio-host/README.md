# `ble-studio-host` — shared Studio-over-BLE host app

The shared, module-agnostic BLE **host** (simulated computer) that drives the
ZMK Studio RPC service over BLE, for BabbleSim BLE tests run by
[`west zmk-ble-test`](../README.md#west-zmk-ble-test). This repo owns and
builds it — **modules do not copy any C code**.

It scans for the DUT's HIDS advertisement, connects, pairs (Just Works),
exchanges a larger ATT MTU, discovers the ZMK Studio GATT service, subscribes
to its indications, writes a sequence of framed `zmk.studio.Request` messages
(one per received response), hexdumps every de-framed response, and then
idles so e.g. split traffic keeps flowing. Your test's `events.patterns` /
`events.snapshot` assert on those hexdumps. (Internally the app plays the BLE
*central* role — the keyboard is the advertiser — which is why the code says
"central" where technically accurate.)

## How payloads get in: `studio_requests.hex`

The request sequence is **per-case data**, not code. A test case directory
may contain a `studio_requests.hex` file: one hex-encoded, framed
(SOF/ESC/EOF) `zmk.studio.Request` per line, `#` comments allowed —
diffable and reviewable. At build time, `CMakeLists.txt` runs `hex2inc.py`
to convert the file `STUDIO_REQUESTS_HEX_FILE` points at into a generated
`requests.inc` table that `src/main.c` includes.

`studio_requests.hex` itself is **generated, never hand-written**: the module
checks in a small `generate_requests.py` next to it that builds real
`zmk.studio.Request` protobufs against the workspace's `zmk-studio-messages`
protos and emits the hex lines via the `generator_main()` helper in
`scripts/lib/ble/studio_requests.py`. See
[`tests/ble/studio/core/generate_requests.py`](../tests/ble/studio/core/generate_requests.py)
for a complete ~30-line sample (including a commented custom-subsystem `Call`
example). Re-run it whenever you change the sequence; CI can verify with
`--check`.

## What a module ships for a Studio-over-BLE case

Only case data — no C, no app directory:

```
tests/ble/studio/<case>/
├── nrf52_bsim.keymap        # DUT keymap (keys'd physical layout for Studio)
├── nrf52_bsim.conf          # CONFIG_ZMK_STUDIO=y (+ your module's Kconfig)
├── generate_requests.py     # checked-in generator (source of truth)
├── studio_requests.hex      # generated, checked-in payload file
├── siblings.txt             # references the staged host exe, see below
├── events.patterns / events.snapshot
└── (peripheral*.overlay, central.conf, ... as usual)
```

When `west zmk-ble-test` finds `studio_requests.hex` in a case dir, it
automatically builds this app for that case (board `nrf52_bsim`, with
`-DSTUDIO_REQUESTS_HEX_FILE=<case>/studio_requests.hex`) and stages it as
`<sim id>_studio_host.exe`. Reference it from `siblings.txt` with the
`{studio_host}` placeholder:

```
./{studio_host} -d=2
```

## Escape hatch: custom host apps

If your test needs host-side logic this app cannot express (custom asserts,
reacting to notification content, multi-connection scenarios, ...), ship your
own Zephyr app as `tests/ble/<name>_host/` in your module — the runner
auto-discovers and builds every such app and stages it as
`<prefix>_<name>_host.exe` (plus a plain `<name>_host.exe` alias). The legacy
`tests/ble/<name>_central/` naming is still auto-discovered too, for backward
compat with existing case data. Prefer `studio_requests.hex` + this shared
app whenever a fixed request-in-order / hexdump-responses flow is enough.

## Gotchas (baked into `prj.conf`)

- **MTU**: Studio RPC indications exceed the 23-byte default ATT MTU. The app
  negotiates a larger MTU (`CONFIG_BT_L2CAP_TX_MTU=247`,
  `CONFIG_BT_BUF_ACL_RX_SIZE=251`) before discovery, like real Studio clients.
  Without this, indications are truncated and de-framing fails.
- **Encryption**: the Studio characteristic requires an encrypted link, so the
  app raises security to `BT_SECURITY_L2` (Just Works pairing) on connect and
  only starts discovery after `security_changed` succeeds.
- **Log module**: responses appear as `<dbg> ble_studio_host: ...` lines —
  write your `events.patterns`/`events.snapshot` against that name.
