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

## How payloads get in: `studio_requests.json`

The request sequence is **declarative per-case data**, not code. A test case
directory contains a `studio_requests.json`: a JSON array where each element
is one `zmk.studio.Request` in protobuf's **canonical JSON mapping**
(camelCase or original snake_case field names both work — the file is parsed
with `google.protobuf.json_format.ParseDict` against the real compiled
descriptors, so field names, types and enums are validated for free, and any
core/behaviors/keymap/custom request is expressible):

```json
[
  {
    "custom": { "listCustomSubsystems": {} }
  },
  {
    "custom": {
      "call": {
        "subsystemIndex": 0,
        "payload": {
          "$type": "your_name.template.Request",
          "sample": { "value": 42 }
        }
      }
    }
  }
]
```

Rules:

- **`$type` extension (nested-encoded bytes)**: a *bytes* field may be
  written as an object `{"$type": "<full.message.name>", ...fields}`. The
  named message is resolved against the compiled protos — the workspace's
  `zmk-studio-messages` plus **your module's own `proto/` directory**
  (everything under `<module>/proto` is compiled automatically, each file's
  directory used as its include root) — encoded, and substituted as the
  bytes value. `$type` objects nest recursively. Plain base64 strings
  (canonical JSON's native bytes form) still work for raw payloads.
- **`request_id`**: optional. If omitted (or 0), it is auto-assigned the
  request's 1-based position in the array.
- The requests are framed (SOF/ESC/EOF) and sent in array order, one per
  received response.

`west zmk-ble-test` converts the JSON at test time (shared code in
`scripts/lib/ble/studio_requests.py`; needs the python `protobuf` package +
`protoc`, see `requirements-test.txt` — the CI action installs both), builds
this app for the case (board `nrf52_bsim`, payload table embedded via
`-DSTUDIO_REQUESTS_HEX_FILE` → `hex2inc.py` → generated `requests.inc`), and
stages it as `<sim id>_studio_host.exe`. Reference it from `siblings.txt`
with the `{studio_host}` placeholder:

```
./{studio_host} -d=2
```

### Lower-level forms (when JSON is not enough)

- **`studio_requests.hex`** in the case dir: one hex-encoded, framed
  `zmk.studio.Request` per line (`#` comments allowed). Byte-exact escape
  hatch for payloads the JSON mapping cannot express. A case must have
  *either* the `.json` *or* the `.hex` — both at once is an error.
- **Programmatic API**: `scripts/lib/ble/studio_requests.py` exposes
  `generator_main()` / `render_hex()` / `load_workspace_studio_pb2()` /
  `compile_protos` / `frame` for scripts that build Request protos in Python
  and emit a `.hex` — useful when payloads are computed rather than written
  down.

## What a module ships for a Studio-over-BLE case

Only case data — no C, no Python, no app directory:

```
tests/ble/studio/<case>/
├── nrf52_bsim.keymap        # DUT keymap (keys'd physical layout for Studio)
├── nrf52_bsim.conf          # CONFIG_ZMK_STUDIO=y (+ your module's Kconfig)
├── studio_requests.json     # declarative request sequence (source of truth)
├── siblings.txt             # references {studio_host}, see above
├── events.patterns / events.snapshot
└── (peripheral*.overlay, central.conf, ... as usual)
```

See [`tests/ble/studio/core/`](../tests/ble/studio/core/) for a complete
minimal case.

## Escape hatch: custom host apps

If your test needs host-side logic this app cannot express (custom asserts,
reacting to notification content, multi-connection scenarios, ...), ship your
own Zephyr app as `tests/ble/<name>_host/` in your module — the runner
auto-discovers and builds every such app and stages it as
`<prefix>_<name>_host.exe` (plus a plain `<name>_host.exe` alias). The legacy
`tests/ble/<name>_central/` naming is still auto-discovered too, for backward
compat with existing case data. Prefer `studio_requests.json` + this shared
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
