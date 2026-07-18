# QSPI (0x40029000) stub for real-binary mode -- see xiao_nrf52840_real.repl.
#
# The stock xiao_ble build enables the on-board QSPI NOR flash. Renode has no
# nRF52840 QSPI model, so nrfx_qspi would busy-wait forever on EVENTS_READY.
# This stub lets that busy-wait complete: EVENTS_READY (0x100) reads 1 and
# STATUS (0x604) reads READY (bit 3); every other register (incl. the JEDEC ID
# data path) reads 0, so the JEDEC probe mismatches and nordic_qspi_nor init
# fails *gracefully* with -ENODEV. That is harmless here: the external NOR is
# not the settings backend (NVS lives in internal flash), it just must not hang.
if request.IsInit:
    pass
elif request.IsRead:
    if request.Offset == 0x100:      # EVENTS_READY
        request.Value = 1
    elif request.Offset == 0x604:    # STATUS: READY (bit 3)
        request.Value = 0x8
    else:
        request.Value = 0
# writes ignored
