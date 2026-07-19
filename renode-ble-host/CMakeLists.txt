# SPDX-License-Identifier: Apache-2.0
#
# renode-ble-host: the simulated computer / BLE host for `west zmk-renode-test
# --ble`. It boots as a second real ARM image alongside the ZMK DUT on one
# Renode BLE medium, pairs (Just Works), and does an encrypted Studio RPC read.
# Consumers never edit C here -- override the DUT name via
# CONFIG_RENODE_BLE_HOST_TARGET_NAME (see Kconfig / README.md).
#
# Build (from a consumer's west workspace):
#   west build -b nrf52840dk/nrf52840 -s <this repo>/renode-ble-host

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(renode_ble_host)

target_sources(app PRIVATE src/main.c)
