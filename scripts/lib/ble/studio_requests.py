"""Shared infrastructure for the `ble-studio-host` request payloads.

The primary, declarative form is a per-case **`studio_requests.json`**: an
ordered JSON array of `zmk.studio.Request` messages in protobuf's canonical
JSON mapping (validated against the real compiled descriptors via
`google.protobuf.json_format.ParseDict`), with one DSL extension -- a bytes
field may be written as an object `{"$type": "<full.message.name>", ...}`
whose fields are encoded as that message and substituted as the bytes value
(recursively). `west zmk-ble-test` converts the JSON to the framed payload
list at test time via `load_requests_json()`; modules check in *only* the
JSON -- no Python, no hex.

The lower-level pieces stay available for exotic cases:

- `studio_requests.hex` (one hex-encoded framed Request per line, `#`
  comments allowed) is the escape-hatch case file for byte-exact payloads
  the JSON mapping cannot express;
- the programmatic API (`generator_main()` / `render_hex()` /
  `load_workspace_studio_pb2()` / `compile_protos` / `frame`) lets a script
  build Request protos in Python and emit that hex file.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import sys
from pathlib import Path

# Old protoc (<3.19) generates descriptor code that only works with the
# pure-Python protobuf runtime. The switch is read when google.protobuf is
# first imported, so set it here -- before any json_format/descriptor_pool
# import below runs -- mirroring renode_harness.compile_protos (which sets it
# too, but only after this module may already have imported google.protobuf).
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

# The Renode harness (proto compilation + Studio framing) is a sibling lib.
_RENODE_LIB = Path(__file__).resolve().parent.parent / "renode"
sys.path.insert(0, str(_RENODE_LIB))
from renode_harness import (  # noqa: E402
    compile_protos,  # noqa: F401  (re-exported for module protos)
    find_studio_proto_dir,
    frame,
    load_studio_pb2,
)

__all__ = [
    "compile_protos",
    "frame",
    "load_workspace_studio_pb2",
    "compile_module_protos",
    "load_requests_json",
    "render_hex",
    "generator_main",
]


def load_workspace_studio_pb2():
    """Compile + import zmk-studio-messages' studio_pb2 from the enclosing
    west workspace (resolved via the west manifest, with a recursive-search
    fallback)."""
    from west.util import west_topdir

    topdir = Path(west_topdir())
    try:
        from west.manifest import Manifest

        for project in Manifest.from_topdir().projects:
            if project.name == "zmk-studio-messages":
                candidate = Path(project.abspath) / "proto" / "zmk"
                if candidate.is_dir():
                    return load_studio_pb2(candidate)
    except Exception:
        pass
    return load_studio_pb2(find_studio_proto_dir(topdir))


def compile_module_protos(module_dir: Path) -> None:
    """Compile every .proto under `<module_dir>/proto` and import the
    generated `*_pb2` modules so their messages register in protobuf's
    default descriptor pool (where `$type` names are resolved).

    Each proto file's own directory is used as its include root ("flat"
    generation, mirroring how the template's Renode test compiles its module
    proto), so sibling imports by bare filename work.
    """
    proto_root = Path(module_dir) / "proto"
    if not proto_root.is_dir():
        return
    proto_files = sorted(proto_root.rglob("*.proto"))
    if not proto_files:
        return
    include_dirs = sorted({str(p.parent) for p in proto_files})
    out_dir = compile_protos(proto_files, include_dirs=include_dirs)
    for gen in sorted(Path(out_dir).glob("*_pb2.py")):
        importlib.import_module(gen.stem)


def _message_class(type_name: str):
    """Resolve a full protobuf message name (e.g. `your_name.template.
    SampleRequest`) against the default descriptor pool. Compatible with
    both old and new python-protobuf message_factory APIs."""
    from google.protobuf import descriptor_pool, message_factory

    try:
        desc = descriptor_pool.Default().FindMessageTypeByName(type_name)
    except KeyError:
        raise ValueError(
            f'unknown $type "{type_name}" -- not found among the compiled protos '
            "(workspace zmk-studio-messages + the module's proto/ directory)"
        )
    if hasattr(message_factory, "GetMessageClass"):
        return message_factory.GetMessageClass(desc)
    return message_factory.MessageFactory().GetPrototype(desc)


def _expand_dollar_types(node):
    """Recursively replace `{"$type": name, ...fields}` objects with the
    base64 encoding of the serialized message -- protobuf's canonical JSON
    form for bytes fields, so the result feeds straight into ParseDict.
    Nested `$type` objects (a `$type` inside another's fields) work
    naturally because children are expanded first."""
    from google.protobuf import json_format

    if isinstance(node, dict):
        if "$type" in node:
            type_name = node["$type"]
            if not isinstance(type_name, str):
                raise ValueError(f"$type must be a string, got: {type_name!r}")
            fields = {k: _expand_dollar_types(v) for k, v in node.items() if k != "$type"}
            msg = json_format.ParseDict(fields, _message_class(type_name)())
            return base64.b64encode(msg.SerializeToString()).decode("ascii")
        return {k: _expand_dollar_types(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_dollar_types(v) for v in node]
    return node


def load_requests_json(path: Path, module_dir: Path | None = None):
    """Parse a `studio_requests.json` case file into the ordered list of
    (name, zmk.studio.Request) tuples the hex renderer / host app expect.

    File format: a JSON array; each element is one `zmk.studio.Request` in
    protobuf's canonical JSON mapping (camelCase or original field names),
    plus the `$type` extension for bytes fields (see `_expand_dollar_types`).
    If an element omits `request_id`/`requestId` (or sets it to 0), it is
    auto-assigned the element's 1-based position in the array.
    """
    from google.protobuf import json_format

    studio_pb2 = load_workspace_studio_pb2()
    if module_dir is not None:
        compile_module_protos(Path(module_dir))

    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError(f"{path}: top level must be a JSON array of zmk.studio.Request objects")

    named = []
    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: element {idx} is not a JSON object")
        expanded = _expand_dollar_types(entry)
        try:
            req = json_format.ParseDict(expanded, studio_pb2.Request())
        except json_format.ParseError as err:
            raise ValueError(f"{path}: element {idx}: {err}") from err
        if req.request_id == 0:
            req.request_id = idx + 1
        which = req.WhichOneof("subsystem") or "empty"
        named.append((f"request_id={req.request_id} ({which})", req))
    return named


def _normalize(requests) -> list[tuple[str, object]]:
    """Accept a list of Request messages or (name, Request) tuples."""
    named = []
    for i, entry in enumerate(requests):
        if isinstance(entry, tuple):
            named.append(entry)
        else:
            named.append((f"request_{i}", entry))
    return named


def render_hex(requests) -> str:
    """Render framed requests as a `studio_requests.hex` file body."""
    lines = [
        "# GENERATED studio_requests payload file -- DO NOT EDIT.",
        "# Edit the source (studio_requests.json, or your generator script)",
        "# and regenerate instead.",
        "#",
        "# One hex-encoded, framed (SOF/ESC/EOF) zmk.studio.Request per line;",
        "# the ble-studio-host app sends them in order, one per response.",
    ]
    for name, req in _normalize(requests):
        lines.append(f"# {name}")
        lines.append(frame(req.SerializeToString()).hex().upper())
    return "\n".join(lines) + "\n"


def parse_hex_file(path: Path) -> list[bytes]:
    """Parse a studio_requests.hex file into framed payload byte strings
    (used by ble-studio-host's hex2inc.py and available for tests)."""
    payloads = []
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        payloads.append(bytes.fromhex(line))
    return payloads


def generator_main(build_requests, generator_file, default_output=None) -> int:
    """CLI entry point for a module's generate_requests.py.

    `build_requests(studio_pb2)` returns the request sequence (Request
    messages or (name, Request) tuples). `generator_file` is the module
    generator's `__file__`; the default output is `studio_requests.hex` next
    to it (override with `default_output` or `-o`).
    """
    if default_output is None:
        default_output = Path(generator_file).resolve().parent / "studio_requests.hex"

    ap = argparse.ArgumentParser(
        description="Generate the studio_requests.hex payload file for ble-studio-host."
    )
    ap.add_argument(
        "-o",
        "--output",
        default=str(default_output),
        help=f"output path (default: {default_output})",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the output file is not byte-identical to what would be generated",
    )
    args = ap.parse_args()

    content = render_hex(build_requests(load_workspace_studio_pb2()))

    out = Path(args.output)
    if args.check:
        current = out.read_text() if out.is_file() else ""
        if current != content:
            print(f"{out} is out of date; re-run the generator", file=sys.stderr)
            return 1
        print(f"{out} is up to date")
        return 0

    out.write_text(content)
    print(f"wrote {out} ({len(content)} bytes)")
    return 0
