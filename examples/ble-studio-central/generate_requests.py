#!/usr/bin/env python3
"""Generate `requests.inc` for the ble-studio-central example host app.

This is the *source of truth* for the framed `zmk.studio.Request` payloads
`main.c` writes to the ZMK Studio RPC characteristic. The module author edits
the `build_requests()` function below and runs this script ahead of time; the
generated `requests.inc` is checked in next to it (CI does not run this).

Requests are never hand-encoded: they are built as real protobuf messages
against the workspace's `zmk-studio-messages` protos -- reusing the harness
helpers from `zmk-west-commands`' `scripts/lib/renode/` -- so the wire bytes
always match the proto revision your firmware was built with. Each message is
serialized and wrapped in the ZMK Studio SOF/ESC/EOF framing before being
emitted as a C byte array.

Run it from within your west workspace (so `west topdir` and the
`zmk-studio-messages` project resolve):

    python3 generate_requests.py            # writes requests.inc next to this
    python3 generate_requests.py --check    # verify requests.inc is up to date
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# sys.path bootstrap: find zmk-west-commands' scripts/lib/renode/ so we can
# reuse its proto + framing helpers, whether this script runs inside the
# zmk-west-commands repo itself (examples/ble-studio-central/) or copied into
# a consumer module (tests/ble/<name>_central/) whose west workspace has
# zmk-west-commands as a project.
# --------------------------------------------------------------------------
def _locate_renode_lib() -> Path:
    marker = Path("scripts") / "lib" / "renode" / "renode_harness.py"

    # 1) Walking up from this file (covers the in-repo examples/ layout).
    for parent in [HERE, *HERE.parents]:
        if (parent / marker).is_file():
            return parent / "scripts" / "lib" / "renode"

    # 2) Resolve the zmk-west-commands project from the west manifest.
    try:
        from west.manifest import Manifest

        manifest = Manifest.from_topdir()
        for project in manifest.projects:
            if project.name == "zmk-west-commands":
                candidate = Path(project.abspath) / "scripts" / "lib" / "renode"
                if (candidate / "renode_harness.py").is_file():
                    return candidate
    except Exception:
        pass

    # 3) Last resort: recursive search under the west topdir.
    try:
        from west.util import west_topdir

        for hit in sorted(Path(west_topdir()).glob("**/" + str(marker))):
            return hit.parent
    except Exception:
        pass

    raise SystemExit(
        "could not locate zmk-west-commands' scripts/lib/renode/. Run this "
        "from inside a west workspace that has zmk-west-commands as a project."
    )


sys.path.insert(0, str(_locate_renode_lib()))
from renode_harness import (  # noqa: E402
    find_studio_proto_dir,
    frame,
    load_studio_pb2,
)


def _resolve_proto_dir() -> Path:
    from west.util import west_topdir

    return find_studio_proto_dir(Path(west_topdir()))


# --------------------------------------------------------------------------
# EDIT ME: describe the request sequence your host app should send.
# --------------------------------------------------------------------------
def build_requests(studio_pb2):
    """Return a list of (name, zmk.studio.Request) tuples, in send order.

    The default is a single core-only ListCustomSubsystems request, which any
    ZMK module built with the custom Studio RPC framework answers -- a good
    "does the Studio transport work over BLE" smoke.
    """
    requests = []

    list_req = studio_pb2.Request()
    list_req.request_id = 1
    # zmk.custom.Request.list_custom_subsystems (an empty message).
    list_req.custom.list_custom_subsystems.SetInParent()
    requests.append(("list_custom_subsystems", list_req))

    # ----------------------------------------------------------------------
    # SAMPLE: add a custom-subsystem Call. Uncomment and adapt for your
    # module. The payload is your subsystem's own protobuf Request, encoded
    # as bytes. Reference (the template's your_name.template subsystem):
    #
    #   from your_module_pb2 import Request as MyRequest  # your compiled proto
    #   my = MyRequest()
    #   my.sample.value = 42
    #
    #   call_req = studio_pb2.Request()
    #   call_req.request_id = 2
    #   call_req.custom.call.subsystem_index = 0
    #   call_req.custom.call.payload = my.SerializeToString()
    #   requests.append(("call_sample", call_req))
    # ----------------------------------------------------------------------

    return requests


# --------------------------------------------------------------------------
# Emit requests.inc
# --------------------------------------------------------------------------
def _c_array(name: str, data: bytes) -> str:
    rows = []
    for i in range(0, len(data), 12):
        chunk = ", ".join(f"0x{b:02X}" for b in data[i : i + 12])
        rows.append("    " + chunk + ",")
    body = "\n".join(rows)
    return f"static const uint8_t {name}[] = {{\n{body}\n}};"


def render_inc(named_requests) -> str:
    lines = [
        "/*",
        " * GENERATED by generate_requests.py -- DO NOT EDIT.",
        " *",
        " * Framed zmk.studio.Request payloads (SOF/ESC/EOF), in send order.",
        " * Regenerate with: python3 generate_requests.py",
        " */",
        "",
    ]
    entries = []
    for idx, (name, req) in enumerate(named_requests):
        framed = frame(req.SerializeToString())
        var = f"studio_request_{idx}_{name}"
        lines.append(_c_array(var, framed))
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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-o",
        "--output",
        default=str(HERE / "requests.inc"),
        help="output path (default: requests.inc next to this script)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the output file is not byte-identical to what would be generated",
    )
    args = ap.parse_args()

    studio_pb2 = load_studio_pb2(_resolve_proto_dir())
    content = render_inc(build_requests(studio_pb2))

    out = Path(args.output)
    if args.check:
        current = out.read_text() if out.is_file() else ""
        if current != content:
            print(f"{out} is out of date; re-run generate_requests.py", file=sys.stderr)
            return 1
        print(f"{out} is up to date")
        return 0

    out.write_text(content)
    print(f"wrote {out} ({len(content)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
