# USBD (0x40027000) stub for real-binary mode -- see xiao_nrf52840_real.repl.
#
# The stock xiao_ble build talks Studio RPC over USB CDC. Renode has no
# nRF52840 USBD model, so nrf_usbd_common's usbd_enable() would busy-wait
# forever on EVENTCAUSE.READY after writing ENABLE. Return that READY bit
# (bit 11, 0x800) always so enable() completes; every other register reads 0,
# which the driver reads as "no VBUS" -- so afterwards it just idles, exactly
# like a board powered without a USB cable plugged in. (Studio RPC is therefore
# not reachable over this transport yet; that is a separate work item.)
if request.IsInit:
    pass
elif request.IsRead:
    if request.Offset == 0x400:      # EVENTCAUSE
        request.Value = 0x800        # READY (bit 11)
    else:
        request.Value = 0
# writes ignored
