#
# Fake nRF52840 AES-CCM link-layer crypto peripheral for Renode BLE mode --
# see two_machine_ble.resc and renode_harness.boot_ble_pair().
#
# Renode has no CCM model, so a BLE-encrypted link can't come up: the
# controller writes plaintext to CCM.INPTR, triggers KSGEN/CRYPT, and reads
# back ciphertext (+MIC) from CCM.OUTPTR -- with no model those OUT buffers
# stay zero and pairing/encryption fails.
#
# This is NOT real AES-CCM -- it is an *identity* transform, deliberately.
# It only has to be self-consistent because BOTH endpoints (the ZMK DUT and
# the renode-ble-host app) run this exact same fake: "encrypt" appends 4 dummy
# MIC bytes and bumps the length; "decrypt" strips 4 trailing bytes and reports
# MIC-OK. This is fine for a *functional* BLE test (the encrypted code paths on
# both sides execute for real) but proves NOTHING about cryptography -- do not
# use it to validate crypto. See README.md's Studio-over-BLE section.
#
# An empty (len==0) PDU passes through unchanged in both directions (empty PDUs
# on an encrypted BLE link carry no MIC).
#
# Register map (base 0x4000F000), verified against nrfx mdk nrf52840.h:
#   TASKS_KSGEN 0x000  TASKS_CRYPT 0x004  TASKS_STOP 0x008
#   EVENTS_ENDKSGEN 0x100  EVENTS_ENDCRYPT 0x104  EVENTS_ERROR 0x108
#   SHORTS 0x200 (bit0 ENDKSGEN_CRYPT)  INTENSET/CLR 0x304/0x308
#   MICSTATUS 0x400 (1=pass)  ENABLE 0x500 (2=CCM,3=AAR)  MODE 0x504 (bit0
#   0=enc 1=dec)  CNFPTR 0x508  INPTR 0x50C  OUTPTR 0x510  SCRATCHPTR 0x514
#   MAXPACKETSIZE 0x518  RATEOVERRIDE 0x51C
#
# Two properties below are LOAD-BEARING -- each was a hard-won failure mode
# (see the failure-signature table in the module README / commit message):
#   1. EAGER transform in BOTH directions (not lazy-at-EVENTS-read) -- see
#      _schedule().
#   2. Payload at offset +3 (not +2) -- see _do_transform().
# Do not "simplify" either of these away.
#
# PythonPeripheral gotchas (Renode 1.16.1): request fields are Capitalized
# (IsInit/IsRead/IsWrite/Offset/Value); read/write scopes are separate; any
# uncaught exception wedges the monitor. Per-machine state (the same .py serves
# both machines' nodes) is keyed on the machine object's string in a
# module-global dict anchored on `sys`.

import sys

MIC_LEN = 4

TASKS_KSGEN = 0x000
TASKS_CRYPT = 0x004
TASKS_STOP = 0x008
TASKS_RATEOVERRIDE = 0x00C
EVENTS_ENDKSGEN = 0x100
EVENTS_ENDCRYPT = 0x104
EVENTS_ERROR = 0x108
SHORTS = 0x200
INTENSET = 0x304
INTENCLR = 0x308
MICSTATUS = 0x400
ENABLE = 0x500
MODE = 0x504
CNFPTR = 0x508
INPTR = 0x50C
OUTPTR = 0x510
SCRATCHPTR = 0x514
MAXPACKETSIZE = 0x518
RATEOVERRIDE = 0x51C


def _state(self):
    key = self.GetMachine().ToString()
    root = sys.__dict__.setdefault("_fakeccm", {})
    st = root.get(key)
    if st is None:
        st = {
            "regs": {},          # offset -> u32 (control/pointer regs)
            "endksgen": 0,
            "endcrypt": 0,
            "error": 0,
            "micstatus": 0,
            "pending": None,     # dict(mode, inptr, outptr) or None
        }
        root[key] = st
    return st


def _schedule(self, st):
    """Record a pending CRYPT op from the current register snapshot, then run
    the transform EAGERLY."""
    regs = st["regs"]
    st["pending"] = {
        "mode": regs.get(MODE, 0) & 0x1,   # 0=encrypt, 1=decrypt
        "inptr": regs.get(INPTR, 0),
        "outptr": regs.get(OUTPTR, 0),
    }
    # EAGER transform in BOTH directions (lazy-at-EVENTS-read is WRONG):
    #
    # TX/encrypt (KSGEN write with SHORTS ENDKSGEN_CRYPT): Renode's radio
    # builds the on-air frame from the CCM OUT buffer the moment the radio
    # starts, BEFORE firmware ever reads EVENTS_ENDCRYPT -- lazy leaves stale
    # bytes at OUTPTR and the radio transmits garbage (observed: 34-byte
    # stale payload -> Renode "Payload length (34) ... trimming" -> peer
    # MIC-fail disconnect 0x3d). The plaintext at INPTR is fully in RAM by
    # KSGEN time, so eager is safe.
    #
    # RX/decrypt (TASKS_CRYPT via PPI from the radio ADDRESS event): real
    # CCM hardware decrypts in parallel with reception, so the OUT buffer is
    # valid by the end of the packet. Zephyr's split LL relies on that:
    # lll_conn.c isr_rx_pdu() reads sn/nesn/len DIRECTLY from the OUT buffer
    # BEFORE it ever touches EVENTS_ENDCRYPT/MICSTATUS -- with a lazy
    # transform those reads see the PREVIOUS packet (observed: decrypted
    # LL_START_ENC_RSP never enqueued, peripheral enc_tx never set, 30s SMP
    # timeout -> security_changed err=9). Renode deposits the whole frame at
    # INPTR before the PPI CRYPT trigger fires, so eager is safe here too.
    _do_transform(self, st)


def _do_transform(self, st):
    op = st["pending"]
    if op is None:
        return
    st["pending"] = None
    inptr = op["inptr"]
    outptr = op["outptr"]
    decrypt = op["mode"] == 1

    try:
        if inptr == 0 or outptr == 0:
            self.Log(LogLevel.Warning, "fake-CCM: null INPTR/OUTPTR, skipping")
            st["endcrypt"] = 1
            st["error"] = 0
            st["micstatus"] = 1 if decrypt else 0
            return

        bus = self.GetMachine().SystemBus
        header = bus.ReadByte(inptr)
        length = bus.ReadByte(inptr + 1)

        # nRF52 CCM IN/OUT data structure (nRF52840 PS "CCM data structure"):
        #   [0] Header  [1] Length  [2] RFU/pad  [3..] Payload
        # The controller programs the radio with PCNF0.S1INCL=1 so the radio
        # RAM layout matches. Payload is at +3, NOT +2 -- using +2 shifts
        # every non-empty PDU by one byte (encrypts the RFU byte, hands the
        # peer a garbage LL opcode -> spec 0x3d MIC-failure termination,
        # ull_llcp_enc.c:1274).
        PAY = 3

        if length == 0:
            # Empty PDU: pass through unchanged, no MIC either direction.
            bus.WriteByte(outptr, header)
            bus.WriteByte(outptr + 1, 0)
            out_len_field = 0
        elif decrypt:
            # ciphertext(len) -> plaintext(len - MIC); report MIC OK.
            plain_len = length - MIC_LEN
            if plain_len < 0:
                plain_len = 0
            bus.WriteByte(outptr, header)
            bus.WriteByte(outptr + 1, plain_len)
            bus.WriteByte(outptr + 2, 0)
            for i in range(plain_len):
                bus.WriteByte(outptr + PAY + i, bus.ReadByte(inptr + PAY + i))
            out_len_field = plain_len
        else:
            # encrypt: plaintext(len) -> payload verbatim + 4 dummy MIC bytes.
            bus.WriteByte(outptr, header)
            bus.WriteByte(outptr + 1, length + MIC_LEN)
            bus.WriteByte(outptr + 2, 0)
            for i in range(length):
                bus.WriteByte(outptr + PAY + i, bus.ReadByte(inptr + PAY + i))
            for i in range(MIC_LEN):
                bus.WriteByte(outptr + PAY + length + i, 0xA5)
            out_len_field = length + MIC_LEN

        st["endcrypt"] = 1
        st["error"] = 0
        st["micstatus"] = 1 if decrypt else 0
        dump = " ".join(
            "%02x" % bus.ReadByte(inptr + PAY + i) for i in range(min(length, 8))
        )
        self.Log(
            LogLevel.Debug,
            "fake-CCM %s: hdr=0x%02x in_len=%d out_len=%d @in=0x%08x out=0x%08x in[3:]=%s"
            % ("DEC" if decrypt else "ENC", header, length, out_len_field, inptr, outptr,
               dump),
        )
    except Exception as e:
        st["error"] = 1
        st["endcrypt"] = 1
        self.Log(LogLevel.Error, "fake-CCM transform failed: %s" % str(e))


if request.IsInit:
    pass

elif request.IsRead:
    st = _state(self)
    off = request.Offset
    try:
        if off in (EVENTS_ENDCRYPT, MICSTATUS):
            _do_transform(self, st)

        if off == EVENTS_ENDKSGEN:
            request.Value = st["endksgen"]
        elif off == EVENTS_ENDCRYPT:
            request.Value = st["endcrypt"]
        elif off == EVENTS_ERROR:
            request.Value = st["error"]
        elif off == MICSTATUS:
            request.Value = st["micstatus"]
        else:
            request.Value = st["regs"].get(off, 0)
    except Exception as e:
        request.Value = 0
        self.Log(LogLevel.Error, "fake-CCM read @0x%03x failed: %s" % (off, str(e)))

elif request.IsWrite:
    st = _state(self)
    off = request.Offset
    val = request.Value
    try:
        if off == TASKS_KSGEN:
            st["endksgen"] = 1
            # ENDKSGEN->CRYPT shortcut (TX/encrypt path uses it).
            if st["regs"].get(SHORTS, 0) & 0x1:
                _schedule(self, st)
        elif off == TASKS_CRYPT:
            _schedule(self, st)
        elif off == TASKS_STOP:
            st["pending"] = None
        elif off in (EVENTS_ENDKSGEN, EVENTS_ENDCRYPT, EVENTS_ERROR):
            # Firmware clears events by writing 0 (honor any write value).
            if off == EVENTS_ENDKSGEN:
                st["endksgen"] = val
            elif off == EVENTS_ENDCRYPT:
                st["endcrypt"] = val
            else:
                st["error"] = val
        else:
            if off == ENABLE and (val & 0x3) == 0x3:
                self.Log(LogLevel.Warning, "fake-CCM: ENABLE=3 (AAR) requested -- not modeled")
            st["regs"][off] = val
    except Exception as e:
        self.Log(LogLevel.Error, "fake-CCM write @0x%03x failed: %s" % (off, str(e)))
