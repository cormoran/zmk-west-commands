# `west zmk-build` in depth

A small `west build` wrapper for zmk modules. It reads zmk's `build.yaml` and
automatically configures options for `west build`. For the quickstart (basic
build + target filtering) see the [README](../README.md#west-zmk-build); this
page covers flashing, the shortcut flags, VSCode integration, and the extended
`build.yaml` behavior.

## Flashing

You can flash directly after the build. It internally executes
`west flash -d <build dir> --skip-build`.

```bash
# Using the default runner of the target board (e.g. UF2 for XIAO nrf52840)
$ west zmk-build --flash
# Specify runner arguments with `+` prefix
$ west zmk-build --flash +r jlink
# Skip build
$ west zmk-build --flash -sb
```

## Useful shortcuts

There are some useful shortcuts to specify cmake arguments:

```bash
# Erase persistent settings (e.g. BLE pairing setting) on restart
# It's the same as --cmake-args ' -DCONFIG_ZMK_SETTINGS_RESET_ON_START'
$ west zmk-build --reset
# Enable USB logging by building with -S zmk-usb-logging
$ west zmk-build --debug-print
# Build with debug mode and enable RTT console for segger jlink
$ west zmk-build --debug-jlink
```

## VSCode integration

You can generate VSCode settings for IntelliSense and debugging:

```bash
# Generate .vscode/c_cpp_properties.json and .vscode/launch.json
$ west zmk-build --vscode
```

This creates:

- `.vscode/c_cpp_properties.json` - IntelliSense configuration using compile_commands.json
- `.vscode/launch.json` - Debugging configuration for Cortex-Debug extension with J-Link

Note: Currently only supports nRF52840 boards.

## Extended `build.yaml` behavior

As an extended behavior, this command recognizes the `snippets` field in
`build.yaml` to allow specifying multiple snippets.

```yaml:build.yaml
include:
  - artifact: foo
    # snippet: zmk-usb-logging # ZMK's official definition
    snippets:
      - zmk-usb-logging
      - studio-rpc-usb-uart
```
