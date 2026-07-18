#!/usr/bin/env python3
"""Build-time converter: studio_requests.hex -> requests.inc (C table).

Invoked by this app's CMakeLists.txt with the file the caller points
STUDIO_REQUESTS_HEX_FILE at. Input format: one hex-encoded, framed
(SOF/ESC/EOF) zmk.studio.Request per line; blank lines and `#` comments are
ignored. Output: `studio_requests[]` table + STUDIO_REQUESTS_COUNT, included
by src/main.c.

Standalone on purpose (no imports from scripts/lib/) so the app builds in any
workspace without sys.path setup.
"""

from __future__ import annotations

import sys
from pathlib import Path


def parse_hex_file(path: Path) -> list[bytes]:
    payloads = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payloads.append(bytes.fromhex(line))
        except ValueError as err:
            raise SystemExit(f"{path}:{lineno}: not a hex line: {err}")
    return payloads


def c_array(name: str, data: bytes) -> str:
    rows = []
    for i in range(0, len(data), 12):
        rows.append("    " + ", ".join(f"0x{b:02X}" for b in data[i : i + 12]) + ",")
    return f"static const uint8_t {name}[] = {{\n" + "\n".join(rows) + "\n};"


def render(payloads: list[bytes], source: Path) -> str:
    lines = [
        "/*",
        f" * GENERATED from {source.name} by hex2inc.py -- DO NOT EDIT.",
        " * Framed zmk.studio.Request payloads (SOF/ESC/EOF), in send order.",
        " */",
        "",
    ]
    entries = []
    for idx, payload in enumerate(payloads):
        var = f"studio_request_{idx}"
        lines.append(c_array(var, payload))
        lines.append("")
        entries.append(f"    {{ {var}, sizeof({var}) }},")

    lines.append("static const struct {")
    lines.append("    const uint8_t *data;")
    lines.append("    size_t len;")
    lines.append("} studio_requests[] = {")
    lines.extend(entries)
    lines.append("};")
    lines.append("")
    lines.append(
        "#define STUDIO_REQUESTS_COUNT (sizeof(studio_requests) / sizeof(studio_requests[0]))"
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <studio_requests.hex> <requests.inc>", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    payloads = parse_hex_file(src)
    if not payloads:
        raise SystemExit(f"{src}: no request payloads found")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(render(payloads, src))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
