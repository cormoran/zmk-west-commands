# FICR (0x10000000) model for real-binary mode -- see xiao_nrf52840_real.repl.
#
# Renode normally Tags FICR as a read-0 region, which is fine for the
# renode-studio-uart images but fatal for a real xiao_ble build: settings_nvs
# reads CODEPAGESIZE/CODESIZE to size its partition and fails -EDOM (-33) when
# they read 0, so settings never load, BT host init stalls, and the HCI
# Read-BD_ADDR times out into a BT_ASSERT kernel oops around vt=10s. This model
# serves real hardware-like values for every FICR word ZMK/Zephyr/nrfx touches
# at boot (flash geometry, device id, ER/IR, BLE identity address); everything
# else reads 0xFFFFFFFF, like real erased/unused FICR words. Read-only.
#
# DEVICEADDR[0]/[1] hold the BLE identity (a static-random address:
# C0:E7:E7:E7:E7:E7 by default -- the top two bits of the MSB must be 0b11).
# They are pulled out as named constants below so the harness can materialize a
# *per-machine* copy of this model with a distinct address: two machines in one
# emulation must not share a BLE address. renode_harness._materialize_ficr()
# rewrites these two lines (matched by their `DEVICEADDR0 = ` / `DEVICEADDR1 = `
# prefix); keep them on their own line as `NAME = 0x...`.
DEVICEADDR0 = 0xE7E7E7E7  # FICR DEVICEADDR[0] = low 32 bits of the BLE address
DEVICEADDR1 = 0x0000C0E7  # FICR DEVICEADDR[1] = high 16 bits (MSB 0xC0 = static)

vals = {
    0x010: 0x00001000,  # CODEPAGESIZE = 4 KiB
    0x014: 0x00000100,  # CODESIZE = 256 pages (1 MiB)
    0x060: 0x12345678,  # DEVICEID[0]
    0x064: 0x9ABCDEF0,  # DEVICEID[1]
    0x080: 0xA1A2A3A4,  # ER[0]
    0x084: 0xB1B2B3B4,  # ER[1]
    0x088: 0xC1C2C3C4,  # ER[2]
    0x08C: 0xD1D2D3D4,  # ER[3]
    0x090: 0x11121314,  # IR[0]
    0x094: 0x21222324,  # IR[1]
    0x098: 0x31323334,  # IR[2]
    0x09C: 0x41424344,  # IR[3]
    0x0A0: 0x00000001,  # DEVICEADDRTYPE = random
    0x0A4: DEVICEADDR0,  # DEVICEADDR[0]
    0x0A8: DEVICEADDR1,  # DEVICEADDR[1]
    0x100: 0x00052840,  # INFO.PART
    0x104: 0x41414130,  # INFO.VARIANT "AAA0"
    0x108: 0x00002004,  # INFO.PACKAGE
    0x10C: 0x00000100,  # INFO.RAM = 256 KiB
    0x110: 0x00000400,  # INFO.FLASH = 1024 KiB
}

if request.IsInit:
    pass
elif request.IsRead:
    request.Value = vals.get(request.Offset, 0xFFFFFFFF)
elif request.IsWrite:
    pass  # FICR is read-only; ignore writes silently
