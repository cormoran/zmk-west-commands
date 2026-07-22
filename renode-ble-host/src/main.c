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
static bool tried_rpc;

/*
 * S6: a REAL framed Studio GetDeviceInfo round trip over the encrypted BLE link
 * (not just the S5 raw GATT read). The ZMK Studio BLE transport frames RPC
 * exactly like the UART transport (msg_framing.c: SOF=0xAB, ESC=0xAC, EOF=0xAD;
 * ESC-escape any special byte). The host WRITEs the framed request to the RPC
 * characteristic and the DUT streams the framed response back as one or more
 * INDICATIONs on the same characteristic (gatt_rpc_transport.c). So S6:
 *   1. discovers the RPC char value handle + its CCC descriptor handle,
 *   2. subscribes to indications,
 *   3. writes the framed GetDeviceInfo request, and
 *   4. prints every indication chunk's raw bytes as `STAGE:S6-RPC-CHUNK <hex>`
 *      and `STAGE:S6-RPC-DONE` once an unescaped EOF (0xAD) closes the frame.
 * The Renode harness reassembles the chunks, strips the framing and parses the
 * protobuf Response (it owns studio_pb2), asserting a non-empty device name --
 * so this C app needs no protobuf/parse code, only framing-aware reassembly.
 *
 * Framed Request{request_id:1, core:{get_device_info:true}} == ab08011a020801ad
 * (verified against zmk-studio-messages' studio.proto; see the harness).
 */
#define FRAME_SOF 0xAB
#define FRAME_ESC 0xAC
#define FRAME_EOF 0xAD
static const uint8_t rpc_get_device_info_req[] = {0xAB, 0x08, 0x01, 0x1A,
						  0x02, 0x08, 0x01, 0xAD};

static uint16_t rpc_value_handle;
static struct bt_gatt_discover_params rpc_disc_params;
static struct bt_gatt_subscribe_params rpc_sub_params;
static struct bt_gatt_write_params rpc_write_params;
static bool rpc_frame_open;   /* inside a response frame (SOF seen, EOF not yet) */
static bool rpc_prev_escape;  /* previous response byte was an unescaped ESC */

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
		/* Chain the real framed GetDeviceInfo round trip (S6). */
		do_rpc_roundtrip(conn);
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

/* --- S6: framed Studio GetDeviceInfo round trip (see the block comment above) --- */

static void rpc_scan_response_chunk(const uint8_t *data, uint16_t length)
{
	/* Dump the raw indication bytes for the harness to reassemble, and track
	 * the msg-framing state so we can announce the end of the response frame. */
	char hex[2 * 27 + 1];
	uint16_t n = 0;

	for (uint16_t i = 0; i < length && n + 2 < sizeof(hex); i++) {
		static const char digits[] = "0123456789abcdef";
		hex[n++] = digits[(data[i] >> 4) & 0xf];
		hex[n++] = digits[data[i] & 0xf];
	}
	hex[n] = '\0';
	printk("STAGE:S6-RPC-CHUNK %s\n", hex);

	for (uint16_t i = 0; i < length; i++) {
		uint8_t b = data[i];

		if (rpc_prev_escape) {
			rpc_prev_escape = false;
			continue;
		}
		if (b == FRAME_ESC) {
			rpc_prev_escape = true;
		} else if (b == FRAME_SOF) {
			rpc_frame_open = true;
		} else if (b == FRAME_EOF && rpc_frame_open) {
			rpc_frame_open = false;
			printk("STAGE:S6-RPC-DONE (framed GetDeviceInfo response received)\n");
		}
	}
}

static uint8_t rpc_notify_func(struct bt_conn *conn, struct bt_gatt_subscribe_params *params,
			       const void *data, uint16_t length)
{
	if (!data) {
		/* Unsubscribed. */
		return BT_GATT_ITER_STOP;
	}
	rpc_scan_response_chunk(data, length);
	return BT_GATT_ITER_CONTINUE;
}

static void rpc_write_cb(struct bt_conn *conn, uint8_t err, struct bt_gatt_write_params *params)
{
	if (err) {
		printk("STAGE:S6-RPC-WRITE FAIL att_err=0x%02x\n", err);
	} else {
		printk("STAGE:S6-RPC-WRITE OK (framed GetDeviceInfo request sent)\n");
	}
}

static void rpc_write_request(struct bt_conn *conn)
{
	int err;

	rpc_write_params.func = rpc_write_cb;
	rpc_write_params.handle = rpc_value_handle;
	rpc_write_params.offset = 0;
	rpc_write_params.data = rpc_get_device_info_req;
	rpc_write_params.length = sizeof(rpc_get_device_info_req);

	err = bt_gatt_write(conn, &rpc_write_params);
	if (err) {
		printk("STAGE:S6-RPC-WRITE FAIL bt_gatt_write err=%d\n", err);
	}
}

static void rpc_subscribed(struct bt_conn *conn, uint8_t err,
			   struct bt_gatt_subscribe_params *params)
{
	if (err) {
		printk("STAGE:S6-RPC-SUBSCRIBE FAIL att_err=0x%02x\n", err);
		return;
	}
	printk("STAGE:S6-RPC-SUBSCRIBE OK (indications enabled)\n");
	rpc_write_request(conn);
}

static uint8_t rpc_discover_func(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				 struct bt_gatt_discover_params *params)
{
	int err;

	if (!attr) {
		printk("STAGE:S6-RPC-DISCOVER FAIL (RPC characteristic/CCC not found)\n");
		return BT_GATT_ITER_STOP;
	}

	if (params->type == BT_GATT_DISCOVER_CHARACTERISTIC) {
		/* Found the RPC characteristic declaration; its value handle is the
		 * next handle. Now discover its CCC descriptor to subscribe. */
		rpc_value_handle = bt_gatt_attr_value_handle(attr);
		rpc_disc_params.uuid = BT_UUID_GATT_CCC;
		rpc_disc_params.start_handle = attr->handle + 2;
		rpc_disc_params.end_handle = 0xffff;
		rpc_disc_params.type = BT_GATT_DISCOVER_DESCRIPTOR;
		err = bt_gatt_discover(conn, &rpc_disc_params);
		if (err) {
			printk("STAGE:S6-RPC-DISCOVER FAIL ccc-discover err=%d\n", err);
		}
		return BT_GATT_ITER_STOP;
	}

	/* CCC descriptor: subscribe to indications, then write the request. */
	rpc_sub_params.notify = rpc_notify_func;
	rpc_sub_params.subscribe = rpc_subscribed;
	rpc_sub_params.value = BT_GATT_CCC_INDICATE;
	rpc_sub_params.value_handle = rpc_value_handle;
	rpc_sub_params.ccc_handle = attr->handle;
	err = bt_gatt_subscribe(conn, &rpc_sub_params);
	if (err && err != -EALREADY) {
		printk("STAGE:S6-RPC-SUBSCRIBE FAIL bt_gatt_subscribe err=%d\n", err);
	}
	return BT_GATT_ITER_STOP;
}

static void do_rpc_roundtrip(struct bt_conn *conn)
{
	int err;

	if (tried_rpc) {
		return;
	}
	tried_rpc = true;

	printk("STAGE:S6-RPC-START (discovering ZMK Studio RPC characteristic)\n");
	rpc_disc_params.uuid = &rpc_chrc_uuid.uuid;
	rpc_disc_params.func = rpc_discover_func;
	rpc_disc_params.start_handle = 0x0001;
	rpc_disc_params.end_handle = 0xffff;
	rpc_disc_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;
	err = bt_gatt_discover(conn, &rpc_disc_params);
	if (err) {
		printk("STAGE:S6-RPC-START FAIL bt_gatt_discover err=%d\n", err);
	}
}

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
	tried_rpc = false;
	rpc_frame_open = false;
	rpc_prev_escape = false;
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
