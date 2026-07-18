#!/usr/bin/env python3
"""Generate this case's `studio_requests.hex` for the shared ble-studio-host
app -- the sample module-side generator (copy this file into your own case).

Run from inside the west workspace, then check in the regenerated file:

    python3 generate_requests.py            # writes studio_requests.hex
    python3 generate_requests.py --check    # CI: verify it is up to date
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _find_lib() -> Path:
    """Locate zmk-west-commands' scripts/lib/ble: walk up from this file
    (in-repo layout), else resolve the project from the west manifest."""
    marker = Path("scripts") / "lib" / "ble" / "studio_requests.py"
    for parent in [HERE, *HERE.parents]:
        if (parent / marker).is_file():
            return parent / marker.parent
    from west.manifest import Manifest

    for project in Manifest.from_topdir().projects:
        if project.name == "zmk-west-commands":
            return Path(project.abspath) / marker.parent
    raise SystemExit("zmk-west-commands not found (run inside a west workspace listing it)")


sys.path.insert(0, str(_find_lib()))
from studio_requests import generator_main  # noqa: E402


def build_requests(studio_pb2):
    """Return the zmk.studio.Request sequence, in send order."""
    requests = []

    # Core-only smoke: list the custom Studio subsystems. Works for any
    # firmware with CONFIG_ZMK_STUDIO=y, even with zero registered subsystems.
    list_req = studio_pb2.Request()
    list_req.request_id = 1
    list_req.custom.list_custom_subsystems.SetInParent()
    requests.append(("list_custom_subsystems", list_req))

    # ----------------------------------------------------------------------
    # SAMPLE: add a custom-subsystem Call. The payload is your subsystem's
    # own protobuf Request, compiled + encoded here (never hand-encoded).
    # Reference (the template repo's your_name.template subsystem):
    #
    #   from studio_requests import compile_protos
    #   compile_protos(
    #       ["<module>/proto/your-name/template/template.proto"],
    #       include_dirs=["<module>/proto/your-name/template"],
    #   )
    #   import template_pb2
    #
    #   my = template_pb2.Request()
    #   my.sample.value = 42
    #
    #   call_req = studio_pb2.Request()
    #   call_req.request_id = 2
    #   call_req.custom.call.subsystem_index = 0
    #   call_req.custom.call.payload = my.SerializeToString()
    #   requests.append(("call_sample", call_req))
    # ----------------------------------------------------------------------

    return requests


if __name__ == "__main__":
    raise SystemExit(generator_main(build_requests, __file__))
