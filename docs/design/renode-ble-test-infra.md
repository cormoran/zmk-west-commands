# Design: Renode & BLE (BabbleSim) test infrastructure in zmk-west-commands

- Status: **Proposal** (for review)
- Date: 2026-07-18
- Related:
  - BLE (bsim) tests: [zmk-module-template-with-custom-studio-rpc PR #49](https://github.com/cormoran/zmk-module-template-with-custom-studio-rpc/pull/49)
  - Renode tests: [template `tests/renode`](https://github.com/cormoran/zmk-module-template-with-custom-studio-rpc/tree/main%2Bcustom-studio-protocol/tests/renode)
  - Current Renode infra: `cormoran/zmk-workspace` (`skills/test-zmk-renode/`, `.github/actions/zmk-renode-test/`, root `zephyr/module.yml`)

## 1. Goals

1. Any zmk-module — **including modules not based on the template repository** — can run
   Renode tests and BLE (BabbleSim) tests with a small amount of module-side code.
2. Remove the dependency on `cormoran/zmk-workspace` (west pin, `PYTHONPATH` into its
   skill scripts, and the cross-repo GitHub Action reference).
3. Keep flexibility: modules can bring their own test cases, host apps, platform files,
   and opt out of any generic step.

What the tests must cover (unchanged from today):

- **BLE (bsim)**: split central↔peripheral interaction; Studio RPC over BLE including
  operation while the split link is active (RPC relay path).
- **Renode**: central-only, closer-to-real-hardware boot test; Studio RPC against the
  central (core RPC + the module's custom subsystem RPC).

### Non-goals

- Moving hardware-rig tooling (J-Link, hw-lock, RTT skills) — stays in zmk-workspace.
- Fixing the ZMK fork pin the bsim tests currently need (two fixes on
  `cormoran/zmk` `fix/local-id-map-writable-section`); that is a module-manifest
  concern and should eventually be resolved upstream. This design only documents it as
  a prerequisite.
- Porting `build_fw.py` (raw-cmake role builds, heavily zmk-workspace-coupled). The CI
  path does not use it; it stays behind and can be retired with the skill.

## 2. Current state (what depends on what)

```
zmk-module-template (consumer)
├── tests/renode/renode_test.py ──imports──► zmk-workspace/skills/test-zmk-renode/scripts/
│                                            (renode_harness.py, rpc_client.py, platforms/*.resc)
├── west/west-dependency/west-test-dependency.yml
│     └── pins zmk-workspace@SHA  ──provides──► zephyr/module.yml
│                                               ├── renode-test-module (UART transport, ZMK_TRANSPORT_NONE)
│                                               └── snippet_root → renode-studio-uart snippet
├── .github/workflows/zmk-module.yml
│     └── uses: cormoran/zmk-workspace/.github/actions/zmk-renode-test@SHA
└── tests/ble/run-ble-test.sh (212 lines, vendored fork of zmk app/run-ble-test.sh)
      └── + tests/ble/studio_rpc_central/ (410-line BT central host app)
```

Key facts that shape the design:

- The Renode Python harness (`renode_harness.py`, `rpc_client.py`, `renode_smoke.py`,
  `install_renode.sh`, `platforms/`) derives all paths from `Path(__file__)`; the only
  hard-coded zmk-workspace path is the action's
  `$GITHUB_ACTION_PATH/../../../skills/test-zmk-renode/scripts`.
- The Renode-only Studio UART transport must be discoverable as a **Zephyr module at a
  west project root** (`zephyr/module.yml` is only looked up at project roots). Its
  Kconfig (`ZMK_RENODE_STUDIO_UART_TRANSPORT`) defaults to `n`, so it is inert unless
  the `renode-studio-uart` snippet enables it.
- Studio protos are **not** shipped by the infra; they are compiled at test time with
  `protoc` from the consumer's own west dependency (`zmk-studio-messages`).
- `run-ble-test.sh` is ~90% generic. The module-specific parts are: the
  `tmpl_ble_` exe-name prefix (duplicated in each case's `siblings.txt`), the
  hard-coded build of `tests/ble/studio_rpc_central/`, and the per-case data files.
- bsim itself comes from ZMK's manifest (`+babblesim` group) under
  `<topdir>/dependencies/tools/bsim` — no zmk-workspace involvement.

## 3. Proposed design

### 3.1 Overview

`zmk-west-commands` grows from "west commands only" into the single home for module
test infrastructure, with three additions:

1. A root `zephyr/module.yml` — the repo becomes a (Kconfig-gated, inert-by-default)
   **Zephyr module** providing the Renode UART transport and the `renode-studio-uart`
   snippet. Consumers that already have `zmk-west-commands` in their manifest get both
   for free; the separate `zmk-workspace` west pin disappears.
2. Two new west commands, consistent with the existing `zmk-build` / `zmk-test`:
   - `west zmk-renode-test` — install Renode, run the generic boot+RPC smoke test,
     then the module's own `tests/renode/*_test.py` against a built ELF.
   - `west zmk-ble-test` — the generic bsim orchestration (case discovery, builds,
     phy/handbrake, snapshot diffing) extracted from `run-ble-test.sh`.
3. Two thin composite GitHub Actions in this repo that wrap the west commands, so CI
   in a consumer is 1 step per test kind.

```
zmk-west-commands/
├── west.yml                        # unchanged (self: west-commands)
├── zephyr/module.yml               # NEW: name: zmk-west-commands
│                                   #   build.cmake/kconfig → renode-test-module/
│                                   #   settings.snippet_root: .
├── renode-test-module/             # NEW (moved as-is from zmk-workspace)
│   ├── CMakeLists.txt / Kconfig
│   ├── src/renode_uart_transport.c # ZMK_TRANSPORT_NONE transport + tx-irq fix
│   └── zephyr/module.yml           # nested manifest kept for ZMK_EXTRA_MODULES users
├── snippets/renode-studio-uart/    # NEW (moved): snippet.yml / .conf / .overlay
├── scripts/
│   ├── west-commands.yml           # + zmk-renode-test, zmk-ble-test
│   ├── zmk_renode_test.py          # NEW west command
│   ├── zmk_ble_test.py             # NEW west command
│   └── lib/
│       ├── renode/                 # NEW (moved; import names unchanged)
│       │   ├── renode_harness.py   # public API for module tests
│       │   ├── rpc_client.py
│       │   ├── renode_smoke.py
│       │   ├── install_renode.sh
│       │   └── platforms/          # single.resc, split_wired.resc, xiao_nrf52840.repl
│       └── ble/
│           ├── runner.py           # NEW: ported orchestration core
│           └── studio_requests.py  # NEW: payload-generator helper for modules
├── ble-studio-host/                # NEW: shared Studio-over-BLE host app
│                                   #   (payloads injected per case; see 3.5)
├── .github/actions/
│   ├── zmk-renode-test/action.yml  # NEW: thin wrapper
│   └── zmk-ble-test/action.yml     # NEW: thin wrapper
└── docs/design/                    # this document
```

Notes on layout choices:

- `snippets/` at the repo root follows the standard Zephyr convention
  (`snippet_root: .`). `renode-test-module/` keeps its current name and nested
  `zephyr/module.yml` so the legacy `ZMK_EXTRA_MODULES` path keeps working.
- The harness keeps its "scripts + platforms are siblings" grouping under
  `scripts/lib/renode/`, so `renode_harness.py` needs no logic change beyond its
  directory constants. **Import names stay the same** (`import renode_harness`) —
  existing module tests only need their fallback `sys.path` entry updated.
- `.resc` files use cwd-relative paths (Renode resolves `@path` against its cwd, and
  reconnecting to `CreateServerSocketTerminal` is impossible), so the harness keeps
  launching Renode with `cwd=<lib/renode>`.

### 3.2 Zephyr module manifest (removes the zmk-workspace west pin)

```yaml
# zephyr/module.yml
name: zmk-west-commands
build:
  cmake: renode-test-module
  kconfig: renode-test-module/Kconfig
  settings:
    snippet_root: .
```

Impact on existing consumers: every module that already lists `zmk-west-commands` in
its manifest will start registering this Zephyr module on the next `west update`.
This is safe because:

- `ZMK_RENODE_STUDIO_UART_TRANSPORT` is `default n` and `depends on ZMK_STUDIO_RPC`;
  no code is compiled into normal builds.
- A registered `snippet_root` is inert unless `-S renode-studio-uart` is passed.

Consumers then delete the `zmk-workspace` project from their test manifests and drop
the pinned-SHA TODOs.

### 3.3 `west zmk-renode-test`

```
west zmk-renode-test [tests_dir] --elf <path> [options]

positional:
  tests_dir             Directory with the module's own *_test.py files
                        (run non-recursively via `python3 <file> -v`).
                        Optional; omit to run only the generic smoke test.
options:
  --elf PATH            Path to the firmware ELF to test (required).
                        Built by the caller, typically a build.yaml artifact
                        with `snippets: [renode-studio-uart]`.
  --renode-version V    Default 1.16.1 (matches the checked-in .repl copy).
  --boot-timeout SEC    Default 20.
  --skip-smoke          Skip the generic smoke test.
  --no-rpc              Smoke test checks the boot banner only (for modules
                        that do not enable Studio RPC).
```

Behavior:

1. `find_or_install_renode()` — reuses `$RENODE_ROOT` (default `~/.renode`), so CI can
   cache it.
2. Generic smoke test (`renode_smoke.py` logic): boot the ELF on the
   `single.resc`/`xiao_nrf52840.repl` platform, wait for the ZMK boot banner, then
   (unless `--no-rpc`) compile Studio protos from the workspace and assert a
   `GetDeviceInfo` round trip.
   - Proto discovery is improved over today's hard-coded
     `dependencies/modules/msgs/zmk-studio-messages/proto/zmk`: resolve the
     `zmk-studio-messages` project via `west.manifest.Manifest` (same pattern
     `zmk_test.py` uses to find `zmk`), keeping the glob fallback.
3. For each `tests_dir/*_test.py`: run with `PYTHONPATH=<scripts/lib/renode>` and
   `ZMK_RENODE_ELF=<elf>` — the exact contract module tests use today.

The build stays the caller's responsibility (same as the current action contract);
`west zmk-build tests/zmk-config -af renode` already covers it for template-style
repos. A `--build` convenience flag can be added later without breaking anything.

**Module-side code for a minimal consumer: zero.** A build.yaml artifact with the
snippet + one CI step gives boot + core-RPC coverage. Custom-RPC tests are additive
`*_test.py` files using the harness API (`boot_single()`, `compile_protos()`,
`wait_for_text()`, `RpcSocket`).

### 3.4 `west zmk-ble-test`

```
west zmk-ble-test [tests_path] [options]

positional:
  tests_path            Case or parent directory. A directory is a test case iff
                        it contains `nrf52_bsim.keymap` (same discovery rule as
                        `west zmk-test`). Default: cwd.
options:
  -m, --module DIR      Module repo root to add via ZMK_EXTRA_MODULES
                        (default: enclosing git repo / cwd, like zmk-test's -m).
  --auto-accept         Regenerate events.snapshot (ZMK_TESTS_AUTO_ACCEPT).
  --sim-prefix NAME     bsim simulation-id / exe prefix. Default: derived from
                        the module directory name (sanitized).
  --bsim PATH           BSIM_OUT_PATH override; errors with guidance if bsim is
                        not found/compiled.
  -j, --parallel N      Run cases in parallel (default 1; upstream ZMK uses
                        xargs -P — enable once proven stable).
  -v, --verbose
```

Implementation: port the **orchestration** of `run-ble-test.sh` to Python
(`scripts/lib/ble/runner.py`), invoked by the west command. Rationale: the command
must resolve `zmk`/topdir from the manifest anyway (same as `zmk_test.py`), and
parameterizing prefix/host-apps/paths in bash is where the script is already
straining. The **pass/fail pipeline is kept byte-compatible** by shelling out to the
exact upstream pipeline (`sort -s -t: -k1,1 | sed -E -n -f events.patterns` then
`diff -auZ`), so existing `events.patterns`/`events.snapshot` files work unchanged
and stay diffable against upstream ZMK conventions.

Per-case file conventions (unchanged from PR #49, which follows upstream ZMK):

| File | Meaning |
|---|---|
| `nrf52_bsim.keymap` | marks the case; DUT keymap (needs keys'd physical layout for Studio) |
| `nrf52_bsim.conf` | shared DUT+peripheral Kconfig (via `ZMK_CONFIG`) |
| `central.conf` / `peripheral.conf` | role-specific extra conf |
| `peripheral*.overlay` | one split-peripheral build each; presence ⇒ DUT is split central |
| `siblings.txt` | one command line per extra simulated device |
| `events.patterns` / `events.snapshot` | sed filter + expected output |
| `pending` | mismatch ⇒ PENDING instead of FAILED |

Generalizations over the template's script:

1. **Exe prefix**: `tmpl_ble_` becomes `--sim-prefix` (default: module dir name).
   `siblings.txt` supports a `{prefix}` placeholder expanded by the runner
   (literal names keep working), removing the script↔case-data name coupling.
2. **Custom host apps**: instead of hard-coding `tests/ble/studio_rpc_central`, the
   runner builds every `tests/ble/*_central/CMakeLists.txt` app it finds under the
   module (board `nrf52_bsim`) and stages `<prefix>_<appname>.exe` into
   `$BSIM_OUT_PATH/bin`. ZMK's generic `ble_test_central.exe` (from
   `<zmk>/app/tests/ble/central`) is always built, as today.
3. **DUT app**: `$(west list -f '{abspath}' zmk)/app` with
   `-DZMK_EXTRA_MODULES=<module>` — already the approach in PR #49; unchanged.

**Module-side code for a split test: case data only** (keymap, conf, patterns,
snapshot — no shell, no C). For Studio-RPC-over-BLE tests the module additionally
ships a `tests/ble/<name>_central/` host app; see 3.5.

### 3.5 Studio-over-BLE host app: shared `ble-studio-host` (decided 2026-07-18)

The 410-line `studio_rpc_central/main.c` splits into:

- **Generic skeleton** (~85%): scan for HIDS advertiser → connect → `BT_SECURITY_L2`
  (Just-Works; the Studio characteristic requires encryption) → MTU exchange
  (`BT_L2CAP_TX_MTU=247` — Studio indications exceed the 23-byte default) → discover
  the ZMK Studio service by UUID → subscribe to indications → SOF/ESC/EOF de-framer
  with hexdump logging.
- **Module-specific** (~15%): the pre-encoded `zmk.studio.Request` payloads it writes
  and the snapshot lines asserting the responses.

Request payloads are **never hand-encoded**. The module checks in a small Python
generator script (the source of truth for the payloads), which the module author
runs ahead of time to (re)generate the encoded artifact that the host app embeds:

- The generator builds `zmk.studio.Request` messages in Python against the real
  protos — reusing the harness helpers (`compile_protos()`, `load_studio_pb2()`,
  the module's own proto) so encoding always matches the workspace's proto
  revision — applies the SOF/ESC/EOF framing, and writes the output artifact.
- The generated artifact is checked in next to the script (regenerable,
  diff-reviewable); CI does not need to run the generator.

Decided in review (originally sketched as an optional later phase, pulled into
Phase 2): the skeleton ships as a **shared app owned by this repo** —
`ble-studio-host/` — and modules copy **no C at all**. Naming note: "host", not
"central" — central is the device-side BLE role term; user-facing naming (dir,
staged exe names, docs) says host.

- Payloads are injected per-case from `studio_requests.hex` in the case dir (one
  hex-encoded framed Request per line, `#` comments allowed — diffable and
  reviewable), converted to a generated `requests.inc` at build time
  (`-DSTUDIO_REQUESTS_HEX_FILE=<file>` + `hex2inc.py`).
- The runner auto-detects `studio_requests.hex` in a case, builds the shared app
  with it embedded, and stages it as `<sim id>_studio_host.exe`; `siblings.txt`
  references it via the `{studio_host}` placeholder (alongside `{prefix}`).
- The app sends the requests in order (one per response), hexdumps every
  de-framed response in the same log shape as the original template app (so
  snapshot conventions carry over), then idles connected so split traffic keeps
  flowing.
- A module's checked-in `generate_requests.py` stays the source of truth; the
  `scripts/lib/ble/studio_requests.py` helper keeps it to ~30 lines.
- Escape hatch: modules needing custom host logic (arbitrary sequencing, custom
  asserts) still ship their own `tests/ble/*_central/` app via auto-discovery.

### 3.6 GitHub Actions (thin wrappers)

Both actions live in this repo and assume the caller has already run
checkout + west init/update (every consumer has an equivalent of the template's
`west-init` step). All test logic lives west-side, so the action ref (`@main`) and
the consumer's west revision for `zmk-west-commands` can move independently — the
actions only wrap environment setup:

```yaml
# consumer CI, Renode job (container: zmkfirmware/zmk-build-arm:stable)
- uses: cormoran/zmk-west-commands/.github/actions/zmk-renode-test@main
  with:
    elf-path: build/renode_smoke_test/zephyr/zmk.elf
    tests: tests/renode          # optional
```

`zmk-renode-test` action steps: cache `~/.renode` (keyed on renode-version) →
install python-protobuf + protoc (keep the progressive apt/pip fallbacks — the
zmk-build-arm container has no pip/sudo/curl) → `west zmk-renode-test tests/renode
--elf <elf-path>`.

```yaml
# consumer CI, BLE job (container: zmkfirmware/zmk-build-arm:4.1)
- uses: cormoran/zmk-west-commands/.github/actions/zmk-ble-test@main
  with:
    tests: tests/ble
```

`zmk-ble-test` action steps: `west config manifest.group-filter -- +babblesim` +
`west update --narrow` → `make everything` in `<topdir>/dependencies/tools/bsim`
(with its own cache keyed on the resolved zephyr/bsim revisions, instead of relying
on the consumer's whole-`dependencies/` cache) → `west zmk-ble-test <tests>` with
`BSIM_OUT_PATH`/`BSIM_COMPONENTS_PATH` set → upload `build/ble/**/*.log` on failure
is left to the caller (or an `upload-logs` input, TBD).

Container-path gotchas learned in zmk-workspace CI carry over verbatim: use
`$GITHUB_ACTION_PATH` / `$GITHUB_WORKSPACE` env vars, never the
`${{ github.action_path }}` / `${{ github.workspace }}` expressions (host vs
container path, actions/runner#716), and resolve relative inputs against
`$GITHUB_WORKSPACE`. Since the actions no longer need to locate sibling scripts,
the fragile `../../../skills/...` path disappears entirely.

### 3.7 Quickstart for a non-template module (goal #1 check)

A module repo that never saw the template needs:

1. **Manifest**: a test manifest with `zmk` (fork/pin as needed) and
   `zmk-west-commands` with `import: true`.
2. **Renode**: one `build.yaml` entry with `snippets: [renode-studio-uart]`
   (+ `CONFIG_ZMK_STUDIO=y` for the RPC smoke) → CI: build + the renode action.
   Optional `tests/renode/my_test.py` for custom-RPC assertions.
3. **BLE**: `tests/ble/<group>/<case>/` with the per-case files → CI: the ble action.
   A Studio-over-BLE case additionally checks in a `generate_requests.py`
   (built on `studio_requests.py`, ~30 lines) and the `studio_requests.hex` it
   generates, referencing the shared host app via `{studio_host}` in
   `siblings.txt` — no protobuf hand-encoding, no C at all.

No shell scripts, no harness code, no zmk-workspace anywhere.

## 4. Migration plan

Phased so each step is independently green:

- **Phase 1 — Renode into zmk-west-commands**
  1. This repo: add `zephyr/module.yml`, `renode-test-module/`, `snippets/`,
     `scripts/lib/renode/`, `west zmk-renode-test`, the renode action, and a CI job
     exercising the command against a minimal ELF (reuse `tests/zmk-config`).
  2. Template: drop the `zmk-workspace` project from `west-test-dependency.yml`,
     repoint CI to this repo's action, update `tests/renode/renode_test.py`'s
     fallback import path. Delete nothing else — the harness API is unchanged.
  3. zmk-workspace: mark the action + skill scripts deprecated with a pointer here
     (the skill's rig-specific parts stay).
- **Phase 2 — BLE into zmk-west-commands**
  1. This repo: `west zmk-ble-test` + runner + ble action + the shared
     `ble-studio-host/` app with per-case `studio_requests.hex` injection (3.5).
  2. Template PR #49: replace `tests/ble/run-ble-test.sh` with the command, switch
     `siblings.txt` to `{prefix}`/`{studio_host}`, replace `studio_rpc_central`
     with a `generate_requests.py` + `studio_requests.hex` (or keep it under the
     auto-discovered `*_central` escape hatch), `test.py`'s `test_ble` shells out
     to `west zmk-ble-test`.
- **Phase 3 (optional)** — `--build` convenience for `zmk-renode-test`; parallel
  case execution by default; CI freshness check for generated
  `studio_requests.hex` (`generate_requests.py --check` needs protoc in the job).

## 5. Risks & compatibility

- **All existing consumers of `zmk-west-commands@main` are affected by the Zephyr
  module-ification immediately** (most pin `revision: main`). Mitigated by the
  Kconfig gate / inert snippet, but this is the strongest argument for starting to
  tag releases (see open questions).
- **Version skew** between an action `@main` ref and the consumer's west pin of this
  repo: contract kept minimal (action = env setup + one west call) so old west-side
  code keeps working with new actions and vice versa.
- **Renode version coupling**: the checked-in `xiao_nrf52840.repl` is a copy of
  Renode 1.16.1's platform; bumping `--renode-version` default requires refreshing it.
- **bsim ZMK pin**: BLE tests need two fixes not yet on
  `main+custom-studio-protocol` (writable behavior local-id map section;
  `settings_subsys_init` before dynamic handler registration). Until upstreamed,
  consumer manifests must pin `cormoran/zmk@fffa339…` — documented in the ble
  command's README section, not enforced by this repo.
- **Python protobuf** becomes a runtime dependency of `zmk-renode-test` (proto
  compilation). Shipped as `requirements-test.txt`; the command fails with an
  actionable message when missing; the action installs it.
- **Known Renode limitation carried over**: large custom-subsystem RPC responses
  stall (TX ring never drains). The harness docs keep documenting it; modules assert
  small responses (the template even asserts the documented failure mode).

## 6. Open questions for review

1. ~~**BLE runner language**~~ — **decided (2026-07-18): Python** orchestration +
   shelled `sort|sed|diff` pipeline, as described in 3.4.
2. **Release tagging**: start tagging `zmk-west-commands` (e.g. `v0.x`) when the
   Zephyr module lands, and recommend consumers pin tags instead of `main`?
3. **Action repo**: are actions in `zmk-west-commands/.github/actions/*` acceptable,
   or should they be top-level `action.yml` repos for shorter `uses:` strings? (The
   subdirectory form works cross-repo and keeps everything in one place.)
4. **Smoke-test default**: is boot-banner + `GetDeviceInfo` the right default smoke
   for `zmk-renode-test`, with `--no-rpc` for non-Studio modules — or should the RPC
   check be opt-in?
5. ~~**`examples/` vs `templates/`** naming~~ — **decided (2026-07-18)**: no
   copyable app at all; shared `ble-studio-host/` owned by this repo ("host",
   not "central" — central is the device-side BLE role term).
