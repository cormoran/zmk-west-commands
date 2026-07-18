#!/usr/bin/env python3
"""ZMK Studio RPC client over a TCP socket (Renode UART terminal).

Renode exposes an emulated UART on a TCP port via
``emulation CreateServerSocketTerminal <port> "term" false`` + ``connector
Connect <uart> term``. This client speaks the ZMK Studio RPC framing over that
socket, so it is the emulation stand-in for talking to a USB-CDC/serial ZMK
Studio port.

Framing (same as hardware, see zmk studio transport):
    SOF=0xAB, ESC=0xAC, EOF=0xAD; ESC-escape any special byte inside a frame.

Payload is a length-delimited... no: ZMK studio frames carry a single protobuf
``Request``/``Response`` message body directly (no length prefix); the frame
delimiters mark the boundaries. This mirrors
skills/debug-zmk-jlink/scripts/zmk_studio_rpc_probe.py, which speaks the same
framing over pyserial.
"""

from __future__ import annotations

import socket
import time

SOF = 0xAB
ESC = 0xAC
EOF = 0xAD
SPECIAL = {SOF, ESC, EOF}


def frame(payload: bytes) -> bytes:
    out = bytearray([SOF])
    for byte in payload:
        if byte in SPECIAL:
            out.append(ESC)
        out.append(byte)
    out.append(EOF)
    return bytes(out)


class RpcSocket:
    """Minimal framed RPC transport over a Renode UART socket."""

    def __init__(self, host: str = "127.0.0.1", port: int = 3456, connect_timeout: float = 30.0):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._rx = bytearray()
        self._connect(connect_timeout)

    def _connect(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._sock = socket.create_connection((self.host, self.port), timeout=5.0)
                self._sock.settimeout(0.2)
                return
            except OSError as err:  # Renode may not have opened the port yet
                last_err = err
                time.sleep(0.5)
        raise TimeoutError(f"could not connect to Renode UART {self.host}:{self.port}: {last_err}")

    def send(self, payload: bytes) -> None:
        assert self._sock is not None
        self._sock.sendall(frame(payload))

    def read_frame(self, timeout: float = 10.0) -> bytes | None:
        """Return the next decoded frame payload, or None on timeout."""
        deadline = time.monotonic() + timeout
        in_frame = False
        escaped = False
        payload = bytearray()
        while time.monotonic() < deadline:
            byte = self._next_byte(deadline)
            if byte is None:
                continue
            if not in_frame:
                if byte == SOF:
                    in_frame = True
                    payload.clear()
                continue
            if escaped:
                payload.append(byte)
                escaped = False
            elif byte == ESC:
                escaped = True
            elif byte == EOF:
                return bytes(payload)
            elif byte == SOF:
                payload.clear()
            else:
                payload.append(byte)
        return None

    def _next_byte(self, deadline: float) -> int | None:
        while not self._rx:
            if time.monotonic() >= deadline:
                return None
            try:
                assert self._sock is not None
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            self._rx.extend(chunk)
        return self._rx.pop(0)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


if __name__ == "__main__":
    # Smoke helper: connect and dump any frames the device emits unsolicited.
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3456)
    ap.add_argument("--seconds", type=float, default=5.0)
    args = ap.parse_args()
    rpc = RpcSocket(port=args.port)
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        f = rpc.read_frame(timeout=1.0)
        if f is not None:
            print("frame:", f.hex())
    rpc.close()
