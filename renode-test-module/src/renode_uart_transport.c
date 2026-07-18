/*
 * Copyright (c) 2026 The ZMK Contributors
 *
 * SPDX-License-Identifier: MIT
 *
 * A Studio RPC UART transport for Renode emulation testing. See the
 * CONFIG_ZMK_RENODE_STUDIO_UART_TRANSPORT Kconfig help text for the full
 * rationale: this is a copy of ZMK's own
 * dependencies/zmk/app/src/studio/uart_rpc_transport.c with exactly one
 * change — `ZMK_RPC_TRANSPORT(..., ZMK_TRANSPORT_USB, ...)` becomes
 * `ZMK_RPC_TRANSPORT(..., ZMK_TRANSPORT_NONE, ...)` — so it gets selected
 * without a working USB/BLE endpoint, which Renode's nRF52840 model cannot
 * ever provide. Everything else (framing, ring buffers, interrupt-driven
 * RX/TX) is identical and uses only public zmk/studio/rpc.h APIs.
 */

#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/ring_buffer.h>

#include <zephyr/logging/log.h>
#include <zmk/studio/rpc.h>

LOG_MODULE_DECLARE(zmk_studio, CONFIG_ZMK_STUDIO_LOG_LEVEL);

#define UART_DEVICE_NODE DT_CHOSEN(zmk_studio_rpc_uart)

static const struct device *const uart_dev = DEVICE_DT_GET(UART_DEVICE_NODE);

static void tx_notify(struct ring_buf *tx_ring_buf, size_t written, bool msg_done,
                      void *user_data) {
    if (msg_done || (ring_buf_size_get(tx_ring_buf) > (ring_buf_capacity_get(tx_ring_buf) / 2))) {
        uart_irq_tx_enable(uart_dev);
    }
}

static int start_rx(void) {
    uart_irq_rx_enable(uart_dev);
    return 0;
}

static int stop_rx(void) {
    uart_irq_rx_disable(uart_dev);
    return 0;
}

/* The only substantive change from ZMK's uart_rpc_transport.c: tagged
 * ZMK_TRANSPORT_NONE (auto-selected with no USB/BLE endpoint) instead of
 * ZMK_TRANSPORT_USB (gated behind a working USB HID connection). */
ZMK_RPC_TRANSPORT(renode_uart, ZMK_TRANSPORT_NONE, start_rx, stop_rx, NULL, tx_notify);

static void serial_cb(const struct device *dev, void *user_data) {
    if (!uart_irq_update(uart_dev)) {
        return;
    }

    if (uart_irq_rx_ready(uart_dev)) {
        uint32_t last_read = 0, len = 0;
        struct ring_buf *buf = zmk_rpc_get_rx_buf();
        do {
            uint8_t *buffer;
            len = ring_buf_put_claim(buf, &buffer, buf->size);
            if (len > 0) {
                last_read = uart_fifo_read(uart_dev, buffer, len);

                ring_buf_put_finish(buf, last_read);
            } else {
                LOG_ERR("Dropping incoming RPC byte, insufficient room in the RX buffer. Bump "
                        "CONFIG_ZMK_STUDIO_RPC_RX_BUF_SIZE.");
                uint8_t dummy;
                last_read = uart_fifo_read(uart_dev, &dummy, 1);
            }
        } while (last_read && last_read == len);

        zmk_rpc_rx_notify();
    }

    if (uart_irq_tx_ready(uart_dev)) {
        struct ring_buf *tx_buf = zmk_rpc_get_tx_buf();
        uint32_t len;
        while ((len = ring_buf_size_get(tx_buf)) > 0) {
            uint8_t *buf;
            uint32_t claim_len = ring_buf_get_claim(tx_buf, &buf, tx_buf->size);

            if (claim_len == 0) {
                continue;
            }

            int sent = uart_fifo_fill(uart_dev, buf, claim_len);

            ring_buf_get_finish(tx_buf, MAX(sent, 0));
        }
        /* Unlike dependencies/zmk/app/src/studio/uart_rpc_transport.c (which
         * leaves this commented out), we explicitly disable the TX IRQ once
         * the buffer drains. Under Renode, uart_irq_tx_ready() reads as a
         * level condition that stays true with nothing queued, so leaving
         * TX IRQ enabled after a full response drains starves the CPU in a
         * permanent interrupt storm and the studio_rpc_thread never gets
         * scheduled again -- the first RPC request/response round-trip
         * works, but every subsequent request silently times out. Disabling
         * here (and tx_notify() above re-enables it whenever there is
         * something to send) fixed a reproducible "only the first RPC
         * request ever gets a response" failure during Renode bring-up. */
        if (ring_buf_size_get(tx_buf) == 0) {
            uart_irq_tx_disable(uart_dev);
        }
    }
}

static int renode_uart_rpc_interface_init(void) {
    if (!device_is_ready(uart_dev)) {
        LOG_ERR("UART device not found!");
        return -ENODEV;
    }

    int ret = uart_irq_callback_user_data_set(uart_dev, serial_cb, NULL);

    if (ret < 0) {
        if (ret == -ENOTSUP) {
            printk("Interrupt-driven UART API support not enabled\n");
        } else if (ret == -ENOSYS) {
            printk("UART device does not support interrupt-driven API\n");
        } else {
            printk("Error setting UART callback: %d\n", ret);
        }
        return ret;
    }

    return 0;
}

SYS_INIT(renode_uart_rpc_interface_init, POST_KERNEL, CONFIG_KERNEL_INIT_PRIORITY_DEFAULT);
