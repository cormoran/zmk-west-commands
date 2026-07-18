# SPDX-License-Identifier: Apache-2.0
#
# Shared BLE "host" (simulated computer) app for `west zmk-ble-test`.
# The request payloads are injected per-case via the STUDIO_REQUESTS_HEX_FILE
# cache variable (a studio_requests.hex file), converted to a generated
# requests.inc by hex2inc.py at build time -- consumers never edit C here.

cmake_minimum_required(VERSION 3.20.0)

set(STUDIO_REQUESTS_HEX_FILE "" CACHE FILEPATH
    "Path to the studio_requests.hex payload file to embed (see README.md)")

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(ble_studio_host)

if(NOT STUDIO_REQUESTS_HEX_FILE)
  message(FATAL_ERROR
      "STUDIO_REQUESTS_HEX_FILE is not set. Pass "
      "-DSTUDIO_REQUESTS_HEX_FILE=<case dir>/studio_requests.hex (the "
      "`west zmk-ble-test` runner does this automatically for cases that "
      "contain a studio_requests.hex).")
endif()

set(requests_inc ${CMAKE_CURRENT_BINARY_DIR}/generated/requests.inc)
add_custom_command(
  OUTPUT ${requests_inc}
  COMMAND ${PYTHON_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/hex2inc.py
          ${STUDIO_REQUESTS_HEX_FILE} ${requests_inc}
  DEPENDS ${STUDIO_REQUESTS_HEX_FILE} ${CMAKE_CURRENT_SOURCE_DIR}/hex2inc.py
  COMMENT "Generating requests.inc from ${STUDIO_REQUESTS_HEX_FILE}"
)
add_custom_target(studio_requests_inc DEPENDS ${requests_inc})
add_dependencies(app studio_requests_inc)
target_include_directories(app PRIVATE ${CMAKE_CURRENT_BINARY_DIR}/generated)

FILE(GLOB app_sources src/*.c)
target_sources(app PRIVATE ${app_sources})
