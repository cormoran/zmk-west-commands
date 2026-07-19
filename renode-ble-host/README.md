# `renode-ble-host` — Studio-over-BLE host app (Renode)

The shared, module-agnostic BLE **host** (simulated computer) for the Renode
Studio-over-BLE test, [`west zmk-renode-test --ble`](../README.md#studio-over-ble-testing-renode).
It boots as a **second real ARM image** alongside the ZMK DUT on one emulated
Renode BLE medium, so the encrypted Studio RPC path executes on the real
firmware — no BabbleSim, no host stack. This repo owns and builds it; **modules
do not copy any C code**.

It scans for the DUT's advertisement, connects, pairs (LE Secure Connections,
Just Works), elevates the link to security level 2, and does an **encrypted**
GATT read of the ZMK Studio RPC characteristic
(`00000001-0196-6107-c967-c5cfb1c2482a`). Each step prints a stable `STAGE:`
marker; the harness watches the host console for `STAGE:S4-SECURITY-CHANGED OK`
(encrypted link up) and `STAGE:S5-GATT-READ OK` (encrypted read). (Internally
the app plays the BLE *central* role — the keyboard is the advertiser — which
is why the code says "central" where technically accurate.)

> **Not a cryptographic test.** Renode has no AES-CCM engine, so both machines
> share a *fake* identity-CCM peripheral. The encrypted code paths on both
> sides run for real, but nothing here validates cryptography — see the module
> README's disclaimer.

## Which DUT it connects to

The app connects to the first advertiser whose local name **starts with**
`CONFIG_RENODE_BLE_HOST_TARGET_NAME` (default `"Module"`, matching the
zmk-module-template's default keyboard name `"Module Test"`). A module whose
DUT advertises a different name overrides it:

```
west build -b nrf52840dk/nrf52840 -s <this repo>/renode-ble-host \
    -- -DCONFIG_RENODE_BLE_HOST_TARGET_NAME='"My Keeb"'
```

## Building it

Built for the `nrf52840dk/nrf52840` board (a real flashable image, run under
the same `xiao_nrf52840_real.repl` real-binary platform as the DUT):

```
west build -b nrf52840dk/nrf52840 -s <this repo>/renode-ble-host
```

Then pass the resulting ELF to the test:

```
west zmk-renode-test --ble --elf <real DUT zmk.elf> \
    --host-elf build/zephyr/zephyr.elf
```

## The load-bearing data-length cap

`prj.conf` sets **`CONFIG_BT_CTLR_DATA_LENGTH_MAX=27`**. LE Data Length's
effective value is `min(local, remote)`, so capping the host caps **both**
directions — which is precisely why the ZMK DUT stays **unmodified**. Every
encrypted on-air PDU is `payload + 4-byte fake-CCM MIC`; `27 + 4 = 31` is
exactly Renode's `NRF52840_Radio` packet cap. Without the cap the DUT
negotiates larger PDUs and anything over `27+4` gets "trimmed" by the radio,
which breaks the encrypted link. Keep logging light, too: `BT_SMP` /
`BT_HCI_CORE` debug is fine, but `LOG_MODE_IMMEDIATE` (or heavier backends) can
crash the soft link-layer under Renode's coarse-quantum timing.

## Relationship to `ble-studio-host`

`ble-studio-host` is the BabbleSim ([`west zmk-ble-test`](../README.md#west-zmk-ble-test))
host — protocol-accurate POSIX, fast, drives a full Studio request/response
script. `renode-ble-host` runs the **real ARM binary** under Renode and only
proves the encrypted-link Studio read reaches the DUT (slow, ~6–7 min wall).
They are complementary; neither replaces the other.
