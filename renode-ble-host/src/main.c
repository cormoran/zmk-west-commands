/*
 * renode-ble-host: the simulated computer that drives a ZMK DUT's Studio RPC
 * service over an ENCRYPTED BLE link, for `west zmk-renode-test --ble`.
 *
 * Flow: scan -> connect to the DUT (advertiser whose name starts with
 * CONFIG_RENODE_BLE_HOST_TARGET_NAME) -> elevate security to L2 (LE SC Just
 * Works pairing) -> read the encryption-protected ZMK Studio RPC
 * characteristic. Every stage prints a stable "STAGE:" marker so the Renode
 * harness can grep the console for pass/fail (S4 = encrypted link up, S5 =
 * encrypted GATT read OK).
 *
 * The app plays the BLE *central* GAP role (the keyboard is the advertiser),
 * which is why the code below says "central" where technically accurate --
 * the user-facing name for this whole thing is "host".
 *
 * This proves the ZMK Studio characteristic's encrypted code paths run on the
 * real ARM binary under Renode. It is NOT a cryptographic test: Renode has no
 * AES-CCM, so both machines share a fake identity-CCM peripheral (see the
 * module README's Studio-over-BLE section / platforms/models/ccm.py).
 */

#include <zephyr/types.h>
#include <stddef.h>
#include <errno.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/sys/byteorder.h>

/* ZMK Studio RPC characteristic (zmk app/src/studio/uuid.h):
 *   base = xxxxxxxx-0196-6107-c967-c5cfb1c2482a
 *   RPC char = 00000001-...  (READ | WRITE | INDICATE, PERM_*_ENCRYPT)
 */
#define ZMK_STUDIO_UUID(num) BT_UUID_128_ENCODE(num, 0x0196, 0x6107, 0xc967, 0xc5cfb1c2482a)
static struct bt_uuid_128 rpc_chrc_uuid = BT_UUID_INIT_128(ZMK_STUDIO_UUID(0x00000001));

/* Name prefix of the DUT to connect to (Kconfig; default "Module"). */
#define TARGET_NAME CONFIG_RENODE_BLE_HOST_TARGET_NAME

static struct bt_conn *default_conn;
static bool tried_read;

static bool name_cb(struct bt_data *data, void *user_data)
{
	bool *is_dut = user_data;
	const size_t target_len = strlen(TARGET_NAME);

	if (data->type == BT_DATA_NAME_COMPLETE || data->type == BT_DATA_NAME_SHORTENED) {
		if (data->data_len >= target_len &&
		    memcmp(data->data, TARGET_NAME, target_len) == 0) {
			*is_dut = true;
			return false;
		}
	}
	return true;
}

static struct bt_gatt_read_params read_params;

static uint8_t gatt_read_cb(struct bt_conn *conn, uint8_t err,
			    struct bt_gatt_read_params *params, const void *data,
			    uint16_t length)
{
	if (err) {
		printk("STAGE:S5-GATT-READ FAIL att_err=0x%02x\n", err);
	} else {
		printk("STAGE:S5-GATT-READ OK len=%u (encrypted read succeeded)\n", length);
	}
	return BT_GATT_ITER_STOP;
}

static void do_encrypted_read(struct bt_conn *conn)
{
	int err;

	if (tried_read) {
		return;
	}
	tried_read = true;

	read_params.func = gatt_read_cb;
	read_params.handle_count = 0;
	read_params.by_uuid.uuid = &rpc_chrc_uuid.uuid;
	read_params.by_uuid.start_handle = 0x0001;
	read_params.by_uuid.end_handle = 0xffff;

	printk("STAGE:S5-GATT-READ START (reading ZMK Studio RPC char by UUID)\n");
	err = bt_gatt_read(conn, &read_params);
	if (err) {
		printk("STAGE:S5-GATT-READ FAIL bt_gatt_read err=%d\n", err);
	}
}

static void start_scan(void);

static void device_found(const bt_addr_le_t *addr, int8_t rssi, uint8_t type,
			 struct net_buf_simple *ad)
{
	char addr_str[BT_ADDR_LE_STR_LEN];
	bool is_dut = false;
	int err;

	if (default_conn) {
		return;
	}
	if (type != BT_GAP_ADV_TYPE_ADV_IND && type != BT_GAP_ADV_TYPE_ADV_DIRECT_IND) {
		return;
	}

	bt_data_parse(ad, name_cb, &is_dut);
	bt_addr_le_to_str(addr, addr_str, sizeof(addr_str));
	printk("STAGE:S1-ADV seen %s (rssi %d) dut=%d\n", addr_str, rssi, is_dut);
	if (!is_dut) {
		return;
	}

	if (bt_le_scan_stop()) {
		return;
	}

	err = bt_conn_le_create(addr, BT_CONN_LE_CREATE_CONN, BT_LE_CONN_PARAM_DEFAULT,
				&default_conn);
	if (err) {
		printk("STAGE:S2-CONNECT create-conn failed (%d)\n", err);
		start_scan();
	}
}

static void start_scan(void)
{
	int err;

	/* Active scan so the DUT's scan-response name is available. */
	err = bt_le_scan_start(BT_LE_SCAN_ACTIVE, device_found);
	if (err) {
		printk("STAGE:S1-SCAN start failed (err %d)\n", err);
		return;
	}
	printk("STAGE:S1-SCAN started (target name prefix \"%s\")\n", TARGET_NAME);
}

static void connected(struct bt_conn *conn, uint8_t err)
{
	char addr[BT_ADDR_LE_STR_LEN];
	int rc;

	bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

	if (err) {
		printk("STAGE:S2-CONNECT fail %s reason=0x%02x\n", addr, err);
		bt_conn_unref(default_conn);
		default_conn = NULL;
		start_scan();
		return;
	}
	if (conn != default_conn) {
		return;
	}

	printk("STAGE:S2-CONNECT OK %s\n", addr);

	rc = bt_conn_set_security(conn, BT_SECURITY_L2);
	if (rc) {
		printk("STAGE:S3-SECURITY request-failed err=%d\n", rc);
	} else {
		printk("STAGE:S3-SECURITY requested L2\n");
	}
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	char addr[BT_ADDR_LE_STR_LEN];

	if (conn != default_conn) {
		return;
	}
	bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));
	printk("STAGE:DISCONNECT %s reason=0x%02x\n", addr, reason);
	bt_conn_unref(default_conn);
	default_conn = NULL;
	tried_read = false;
	start_scan();
}

static void security_changed(struct bt_conn *conn, bt_security_t level,
			     enum bt_security_err err)
{
	char addr[BT_ADDR_LE_STR_LEN];

	bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));
	if (err) {
		printk("STAGE:S3-SECURITY-CHANGED FAIL %s level=%d err=%d\n", addr, level, err);
		return;
	}
	printk("STAGE:S4-SECURITY-CHANGED OK %s level=%d (encrypted link up)\n", addr, level);
	if (level >= BT_SECURITY_L2) {
		do_encrypted_read(conn);
	}
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = connected,
	.disconnected = disconnected,
	.security_changed = security_changed,
};

int main(void)
{
	int err;

	err = bt_enable(NULL);
	if (err) {
		printk("STAGE:BOOT bt_enable failed (err %d)\n", err);
		return 0;
	}
	printk("STAGE:BOOT bt ready\n");
	start_scan();
	return 0;
}
