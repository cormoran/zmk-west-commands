#!/usr/bin/env python3
"""Regression repro for the usb-mode device->host delivery wedge (issue #50).

Boots a studio-rpc-usb-uart image on the usb platform, attaches the
DualCdcAcmBridge, then drives many core GetDeviceInfo round trips over the
emulated USB CDC. MODE=serial (default) is the realistic ZMK Studio client
pattern (one request, wait for its response, repeat -- the JS client's
rpcMutex.runExclusive); before the device->host pump fix, only the FIRST reply
per session reaches the host and this wedges at request 2. MODE=pipeline blasts
all N requests while reading (an unrealistic host->device flood that also
overruns cdc_acm's rx ring -- a separate robustness case).

Usage: python repro_race.py [ELF] [N]   (env: MODE=serial|pipeline,
REPRO_PROTO_DIR=<zmk-studio-messages proto/zmk>). ELF must be a BLE-off (or
low-radio) studio-rpc-usb-uart image so USB enumeration is not starved.
"""
import os
import random
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts" / "lib" / "renode"))

import renode_harness  # noqa: E402
from renode_smoke import USB_REPL_TEMPLATE, _mon_flag  # noqa: E402

ELF = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    "/tmp/usbonly/zephyr/zmk.elf"  # BLE-off USB-only image (reliable enumeration)
)
# zmk-studio-messages' proto/zmk dir. Override with REPRO_PROTO_DIR; the default
# is an example path from the author's workspace. renode_harness.find_studio_proto_dir
# can locate it under a west topdir if you prefer.
PROTO_DIR = Path(os.environ.get(
    "REPRO_PROTO_DIR",
    "/home/ubuntu/zmk-workspace/zmk-feature-custom-settings/dependencies/"
    "modules/msgs/zmk-studio-messages/proto/zmk",
))
LOGFILE = "/tmp/racelog.txt"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 150
BRIDGE = "bridge"


def main() -> int:
    renode_path = renode_harness.find_or_install_renode()
    assert renode_path, "renode not found"
    studio_pb2 = renode_harness.load_studio_pb2(PROTO_DIR)

    # Whole-emulation retry: boot/attach is wall-clock paced and can lose the
    # race under host load (esp. with the BLE-on image whose radio hogs the
    # emulated CPU), exactly like run_usb_smoke. A genuine wedge fails on the
    # stress step (after sanity), which we do NOT retry.
    last_setup_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            return _attempt(studio_pb2, renode_path)
        except _SetupError as e:
            last_setup_err = e
            print(f"[setup attempt {attempt}/3 failed: {e}; retrying fresh]", file=sys.stderr)
    print(f"gave up after setup failures: {last_setup_err}", file=sys.stderr)
    return 2


class _SetupError(Exception):
    pass


def _attempt(studio_pb2, renode_path) -> int:
    port_base = random.randint(26000, 40000)
    print(f"booting {ELF.name} on usb platform (port_base={port_base})...", file=sys.stderr)
    try:
        session, console, rpc = renode_harness.boot_single_real(
            renode_path, ELF, port_base=port_base, repl_template=USB_REPL_TEMPLATE
        )
    except (OSError, TimeoutError) as e:
        raise _SetupError(f"boot failed: {e!r}")
    mon = session.mon
    assert mon is not None
    cdc: list = []
    try:
        # Silence the BLE-radio / SVD warning flood (this is a real xiao_ble
        # image, so its BLE stack spams the log); keep usbd + bridge at Info for
        # the RACEDBG lines. Then capture to a logFile.
        for p in ("sysbus.radio", "sysbus", "sysbus.timer0", "sysbus.timer1"):
            mon.execute(f"logLevel 3 {p}")
        mon.execute(f"logFile @{LOGFILE}")

        t0 = time.monotonic()
        while time.monotonic() - t0 < 10.0:
            renode_harness.drain_text(console._sock, timeout=0.5)

        cdc = list(renode_harness.attach_dual_cdc_bridge(session, port_base + 4, port_base + 5))
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            if _mon_flag(mon, f"sysbus.{BRIDGE}_cdc0 IsWired"):
                break
        else:
            raise _SetupError("cdc0 never wired")
        dual = bool(_mon_flag(mon, f"sysbus.{BRIDGE}_cdc1 IsWired"))
        time.sleep(2.0)
        studio = cdc[1] if dual else cdc[0]

        # sanity round trip
        req = studio_pb2.Request()
        req.request_id = 1
        req.core.get_device_info = True
        studio.send(req.SerializeToString())
        fr = studio.read_frame(timeout=10.0)
        if fr is None:
            raise _SetupError("sanity GetDeviceInfo timed out")
        print("sanity GetDeviceInfo OK; starting pipelined stress...", file=sys.stderr)

        # Stress. MODE=serial (default): the realistic ZMK Studio client pattern
        # -- one request at a time, wait for its response before the next (the JS
        # client's rpcMutex.runExclusive). MODE=pipeline: blast all N requests
        # from a sender thread while reading (an unrealistic host->device flood
        # that overruns cdc_acm's 1024B rx_ringbuf -- a separate robustness case).
        mode = os.environ.get("MODE", "serial")
        mon.execute("log \"RACEDBG STRESS-START\"")
        send_errors = []
        sent_count = [0]
        got = 0
        wedged = False

        if mode == "pipeline":
            def sender():
                for i in range(N):
                    r = studio_pb2.Request()
                    r.request_id = 100 + i
                    r.core.get_device_info = True
                    try:
                        studio.send(r.SerializeToString())
                        sent_count[0] = i + 1
                    except OSError as e:
                        send_errors.append(e)
                        return
            th = threading.Thread(target=sender, daemon=True)
            th.start()
            for i in range(N):
                fr = studio.read_frame(timeout=4.0)
                if fr is None:
                    wedged = True
                    break
                got += 1
            th.join(timeout=2.0)
        else:
            for i in range(N):
                r = studio_pb2.Request()
                r.request_id = 100 + i
                r.core.get_device_info = True
                studio.send(r.SerializeToString())
                sent_count[0] = i + 1
                fr = studio.read_frame(timeout=4.0)
                if fr is None:
                    wedged = True
                    break
                got += 1
        if wedged:
            print(f"WEDGE: no response after {got}/{N} (mode={mode}, sent {sent_count[0]})",
                  file=sys.stderr)
        print(f"mode={mode} sender sent {sent_count[0]}/{N}", file=sys.stderr)
        mon.execute("log \"RACEDBG STRESS-END\"")
        time.sleep(0.5)
        mon.execute("logFile")  # flush/close

        print(f"RESULT: received {got}/{N} responses; wedged={wedged}; "
              f"send_errors={send_errors}", file=sys.stderr)
        return 1 if wedged else 0
    finally:
        for s in cdc:
            s.close()
        rpc.close()
        console.close()
        session.stop()


if __name__ == "__main__":
    sys.exit(main())
