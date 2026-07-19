# NVMC (0x4001E000) flash-erase model for real-binary mode -- see
# xiao_nrf52840_real.repl.
#
# Renode's nRF52840 platform has no NVMC peripheral: reads of the 0x4001E000
# region return 0 (unmapped bus access), so nrfx's nrf_nvmc_ready_check()
# (polls READY at 0x400) never sees 1. A BLE-enabled real image can then
# spin-poll NVMC.READY forever the first time it touches flash -- an observed
# *silent* hang in two-machine runs. Modeling READY/READYNEXT=1 fixes the
# poll; modeling ERASEPAGE fixes NVS garbage collection, which needs a real
# page erase once a settings sector fills (Renode's MappedMemory has no erase
# concept, so without this a full sector never returns to 0xFF and GC fails).
#
# Erase path this SoC's driver takes (Zephyr soc_flash_nrf.c ->
# nrfx_nvmc_page_erase -> nrf_nvmc_page_erase_start), NRF52_SERIES branch:
#   1. CONFIG   (0x504) <- 2 (EEN / erase-enable)
#   2. ERASEPAGE(0x508) <- page base address
#   3. poll READY (0x400) until 1
#   4. CONFIG   (0x504) <- 0 (REN / read-only)
# The "write 0xFFFFFFFF directly into the page" alternative in nrfx's HAL is
# NRF53/91-only -- NOT used on nRF52840 -- so it is not modeled here.
#
# Register map (offsets from 0x4001E000), per nrfx mdk/nrf52840.h NRF_NVMC_Type:
#   0x400 READY              (RO) always 1 (erase/write "complete" instantly)
#   0x408 READYNEXT          (RO) always 1
#   0x504 CONFIG             (RW) 0=REN 1=WEN 2=EEN; stored, not enforced
#                                 (flash writes always "succeed" via
#                                 MappedMemory regardless of mode)
#   0x508 ERASEPAGE          (WO) write = page base addr -> fill 0x1000 bytes
#                                 with 0xFF
#   0x50C ERASEALL           (WO) no-op (would wipe program flash too; NVS
#                                 uses per-page erase)
#   0x510 ERASEPCR0          (WO) deprecated ERASEPAGE alias on nRF52840 --
#                                 implemented as an alias
#   0x514 ERASEUICR          (WO) no-op (no UICR peripheral modeled)
#   0x518 ERASEPAGEPARTIAL   (WO) treated the same as ERASEPAGE (a full erase
#                                 satisfies partial-erase for a test rig)
#   0x51C ERASEPAGEPARTIALCFG (RW) stored only, no-op
#
# PythonPeripheral gotchas (Renode 1.16.1): request fields are Capitalized
# (IsInit/IsRead/IsWrite/Offset/Value); the bare `machine` global is not
# reliably in scope here -- use self.GetMachine().SystemBus; any uncaught
# exception wedges the monitor, so everything is wrapped in try/except and
# reported via self.ErrorLog. State is persisted across calls via the `sys`
# module (anchored on sys.modules[__name__]).
#
# Erase writes 0xFF via WriteDoubleWord in a loop rather than
# SystemBus.WriteBytes: on this Renode build WriteBytes rejects plain Python
# bytes/bytearray ("expected Array[Byte], got bytes") and needs an explicit
# System.Array[System.Byte] marshal -- the WriteDoubleWord loop sidesteps that
# entirely and is plenty fast for a 4 KiB page in a test rig.

import sys

PAGE_SIZE = 0x1000

OFF_READY = 0x400
OFF_READYNEXT = 0x408
OFF_CONFIG = 0x504
OFF_ERASEPAGE = 0x508
OFF_ERASEALL = 0x50C
OFF_ERASEPCR0 = 0x510
OFF_ERASEUICR = 0x514
OFF_ERASEPAGEPARTIAL = 0x518
OFF_ERASEPAGEPARTIALCFG = 0x51C

_mod = sys.modules[__name__]
if not hasattr(_mod, "_nvmc_state"):
    _mod._nvmc_state = {"config": 0, "erasepagepartialcfg": 0}
_state = _mod._nvmc_state


def _erase_page(addr):
    # Align to the 4 KiB page base (NVS passes an aligned address already, but
    # be defensive) and fill the whole page with 0xFF.
    base = addr & ~(PAGE_SIZE - 1)
    bus = self.GetMachine().SystemBus
    for i in range(0, PAGE_SIZE, 4):
        bus.WriteDoubleWord(base + i, 0xFFFFFFFF)


try:
    if request.IsInit:
        _state["config"] = 0
        _state["erasepagepartialcfg"] = 0
    elif request.IsRead:
        if request.Offset in (OFF_READY, OFF_READYNEXT):
            request.Value = 1
        elif request.Offset == OFF_CONFIG:
            request.Value = _state["config"]
        elif request.Offset == OFF_ERASEPAGEPARTIALCFG:
            request.Value = _state["erasepagepartialcfg"]
        else:
            request.Value = 0
    elif request.IsWrite:
        if request.Offset == OFF_CONFIG:
            _state["config"] = request.Value
        elif request.Offset in (OFF_ERASEPAGE, OFF_ERASEPCR0, OFF_ERASEPAGEPARTIAL):
            _erase_page(request.Value)
        elif request.Offset == OFF_ERASEPAGEPARTIALCFG:
            _state["erasepagepartialcfg"] = request.Value
        elif request.Offset in (OFF_ERASEALL, OFF_ERASEUICR):
            pass  # harmless no-ops (ERASEALL would wipe program flash too)
        # else: unknown offset in the modeled range -- ignore silently
except Exception as e:
    try:
        self.ErrorLog(
            "nvmc: exception handling {0} at offset 0x{1:X}: {2}".format(
                request.Type, request.Offset, e
            )
        )
    except Exception:
        pass
