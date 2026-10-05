/**
 * ble_gatt.c — BLE GATT 服务实现（NimBLE 外设）
 *
 * 结构参考 ESP-IDF 的 NimBLE bleprph 示例：nimble_port_init -> 注册 GATT 服务 ->
 * 广播；连接/断开/订阅事件在 gap_event_cb 里更新本模块状态，帧发送走
 * ble_gatts_notify_custom（服务端主动 Notify）。
 *
 * 线程约定：ble_gatt_notify() 可在任意任务上下文调用（内部只做组包 +
 * 交给 NimBLE，真正的空口发送在主机任务完成）。
 */
#include "ble_gatt.h"

#include <string.h>

#include "esp_log.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "host/ble_hs.h"
#include "host/ble_att.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "os/os_mbuf.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

static const char* TAG = "BLE";

#define DEVICE_NAME "ECG-Monitor"
#define INFO_STR    "ECG-Monitor v1.0"

/* 128-bit UUID：a1b2000X-1234-5678-9abc-def012345678
 * （BLE_UUID128_INIT 按小端字节序展开；上位机用字符串形式，见 src/ble_client.py） */
#define UUID_TAIL 0x78, 0x56, 0x34, 0x12, 0xf0, 0xde, 0xbc, 0x9a, 0x78, 0x56, 0x34, 0x12
static const ble_uuid128_t SVC_UUID       = BLE_UUID128_INIT(UUID_TAIL, 0x01, 0x00, 0xb2, 0xa1);
static const ble_uuid128_t CHR_ECG_UUID   = BLE_UUID128_INIT(UUID_TAIL, 0x02, 0x00, 0xb2, 0xa1);
static const ble_uuid128_t CHR_STATUS_UUID= BLE_UUID128_INIT(UUID_TAIL, 0x03, 0x00, 0xb2, 0xa1);
static const ble_uuid128_t CHR_CTRL_UUID  = BLE_UUID128_INIT(UUID_TAIL, 0x04, 0x00, 0xb2, 0xa1);
static const ble_uuid128_t CHR_INFO_UUID  = BLE_UUID128_INIT(UUID_TAIL, 0x05, 0x00, 0xb2, 0xa1);

static uint8_t  s_own_addr_type;
static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static uint16_t s_ecg_val_handle;
static uint16_t s_status_val_handle;
static uint16_t s_info_val_handle;
static volatile bool s_ecg_sub = false;
static volatile bool s_status_sub = false;
static ble_gatt_disconnect_cb_t s_on_disconnect = NULL;
static uint8_t s_seq_wave = 0;      /* 波形序号（上位机据其缺口算丢包） */
static uint8_t s_seq_status = 0;    /* 心跳序号（不参与丢包统计） */
static uint8_t s_seq_ctrl = 0;      /* 会话开始/结束序号（同样不参与丢包统计） */

/* ------------------------------------------------------------------------- */
/* CRC8：poly 0x07、init 0x00（与 src/ble_client.py 的 _crc8 等价）           */
/* ------------------------------------------------------------------------- */
static uint8_t crc8(const uint8_t* data, size_t len) {
    uint8_t crc = 0x00;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
        }
    }
    return crc;
}

/* ------------------------------------------------------------------------- */
/* GATT 访问回调                                                              */
/* ------------------------------------------------------------------------- */
static int gatt_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                          struct ble_gatt_access_ctxt* ctxt, void* arg) {
    (void)conn_handle;
    (void)arg;

    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        if (attr_handle == s_info_val_handle) {
            int rc = os_mbuf_append(ctxt->om, INFO_STR, strlen(INFO_STR));
            return rc == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
        }
        return BLE_ATT_ERR_UNLIKELY;
    }

    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        /* Control 特征：v1 仅记录，命令语义预留（默认连接即自动开始推流）。 */
        uint8_t buf[32];
        uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
        if (len > sizeof(buf)) len = sizeof(buf);
        if (len && os_mbuf_copydata(ctxt->om, 0, len, buf) == 0) {
            ESP_LOGI(TAG, "control write len=%u", (unsigned)len);
        }
        return 0;
    }

    return BLE_ATT_ERR_UNLIKELY;
}

static const struct ble_gatt_svc_def gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &SVC_UUID.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = &CHR_ECG_UUID.u,
                .access_cb = gatt_access_cb,
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_ecg_val_handle,
            },
            {
                .uuid = &CHR_STATUS_UUID.u,
                .access_cb = gatt_access_cb,
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_status_val_handle,
            },
            {
                .uuid = &CHR_CTRL_UUID.u,
                .access_cb = gatt_access_cb,
                .flags = BLE_GATT_CHR_F_WRITE,
            },
            {
                .uuid = &CHR_INFO_UUID.u,
                .access_cb = gatt_access_cb,
                .flags = BLE_GATT_CHR_F_READ,
                .val_handle = &s_info_val_handle,
            },
            { 0 },
        },
    },
    { 0 },
};

/* ------------------------------------------------------------------------- */
/* 广播与事件                                                                 */
/* ------------------------------------------------------------------------- */
static int gap_event_cb(struct ble_gap_event* event, void* arg);

static void start_advertising(void) {
    struct ble_hs_adv_fields fields = {0};
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    const char* name = ble_svc_gap_device_name();
    fields.name = (uint8_t*)name;
    fields.name_len = (uint8_t)strlen(name);
    fields.name_is_complete = 1;

    int rc = ble_gap_adv_set_fields(&fields);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv fields rc=%d", rc);
        return;
    }

    /* 扫描响应里带上完整 128-bit 服务 UUID：Windows 靠它在扫描阶段识别并缓存
     * GATT 服务。不广播 UUID 时 Windows 会把设备按「无服务」缓存，上位机往往得
     * 先删设备再重连才拿得到服务；放扫描响应（而非主广播）是为给 31 字节主广播
     * 留足名字空间。 */
    struct ble_hs_adv_fields rsp = {0};
    rsp.uuids128 = &SVC_UUID;
    rsp.num_uuids128 = 1;
    rsp.uuids128_is_complete = 1;
    rc = ble_gap_adv_rsp_set_fields(&rsp);
    if (rc != 0) {
        ESP_LOGW(TAG, "adv rsp fields rc=%d", rc);
    }

    struct ble_gap_adv_params adv_params = {0};
    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;

    rc = ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER,
                           &adv_params, gap_event_cb, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv start rc=%d", rc);
    }
}

static int gap_event_cb(struct ble_gap_event* event, void* arg) {
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            s_conn_handle = event->connect.conn_handle;
            ESP_LOGI(TAG, "connected");
            /* 主动发起 MTU 交换：把 ATT MTU 从默认 23 抬到 preferred（256）。
             * 否则演示推流只能 7 样本/包 ≈52 包/s，Windows 端 30ms 连接间隔
             * 一包一事件扛不住，会稳定丢 1/3；MTU 抬高后每包几十样本、包率
             * 降到个位数，丢包归零（见 ble_stream.c 按 MTU 定包长）。 */
            int rc = ble_gattc_exchange_mtu(s_conn_handle, NULL, NULL);
            if (rc != 0) ESP_LOGW(TAG, "mtu exchange start rc=%d", rc);
        } else {
            s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
            start_advertising();
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "disconnected reason=%d", event->disconnect.reason);
        s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        s_ecg_sub = false;
        s_status_sub = false;
        if (s_on_disconnect) s_on_disconnect();
        start_advertising();
        return 0;

    case BLE_GAP_EVENT_SUBSCRIBE:
        if (event->subscribe.attr_handle == s_ecg_val_handle) {
            s_ecg_sub = event->subscribe.cur_notify;
        } else if (event->subscribe.attr_handle == s_status_val_handle) {
            s_status_sub = event->subscribe.cur_notify;
        }
        ESP_LOGI(TAG, "subscribe h=%d notify=%d", event->subscribe.attr_handle,
                 event->subscribe.cur_notify);
        return 0;

    case BLE_GAP_EVENT_ADV_COMPLETE:
        start_advertising();
        return 0;

    default:
        return 0;
    }
}

/* ------------------------------------------------------------------------- */
/* 主机同步/复位                                                              */
/* ------------------------------------------------------------------------- */
static void on_sync(void) {
    if (ble_hs_util_ensure_addr(0) != 0) {
        ESP_LOGE(TAG, "no usable address");
        return;
    }
    ble_hs_id_infer_auto(0, &s_own_addr_type);
    start_advertising();
}

static void on_reset(int reason) { ESP_LOGW(TAG, "host reset, reason=%d", reason); }

static void host_task(void* param) {
    (void)param;
    nimble_port_run();
    nimble_port_freertos_deinit();
}

/* ------------------------------------------------------------------------- */
/* 对外接口                                                                   */
/* ------------------------------------------------------------------------- */
esp_err_t ble_gatt_init(void) {
    esp_err_t err = nimble_port_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nimble_port_init failed: %s", esp_err_to_name(err));
        return err;
    }

    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;

    ble_svc_gap_init();
    ble_svc_gatt_init();

    int rc = ble_gatts_count_cfg(gatt_svcs);
    if (rc != 0) {
        ESP_LOGE(TAG, "count_cfg rc=%d", rc);
        return ESP_FAIL;
    }
    rc = ble_gatts_add_svcs(gatt_svcs);
    if (rc != 0) {
        ESP_LOGE(TAG, "add_svcs rc=%d", rc);
        return ESP_FAIL;
    }

    ble_svc_gap_device_name_set(DEVICE_NAME);
    nimble_port_freertos_init(host_task);
    ESP_LOGI(TAG, "BLE ready, name=%s", DEVICE_NAME);
    return ESP_OK;
}

bool ble_gatt_connected(void) { return s_conn_handle != BLE_HS_CONN_HANDLE_NONE; }
bool ble_gatt_subscribed(void) { return s_ecg_sub; }

uint16_t ble_gatt_mtu(void) {
    if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE) return 23;
    uint16_t mtu = ble_att_mtu(s_conn_handle);
    return mtu ? mtu : 23;
}

void ble_gatt_set_on_disconnect(ble_gatt_disconnect_cb_t cb) { s_on_disconnect = cb; }

esp_err_t ble_gatt_notify(uint8_t type, const uint8_t* payload, uint8_t len) {
    if (!ble_gatt_connected()) return ESP_ERR_INVALID_STATE;
    if (type == BLE_TYPE_STATUS && !s_status_sub) return ESP_ERR_INVALID_STATE;

    /* 序号按类型独立计数：只有波形帧的序号参与上位机的丢包统计，
     * 若让「会话开始/结束」也占用同一个计数器，每开一次会话就会在波形流里
     * 顶出一个假的序号空洞，上位机会误报丢包。 */
    uint16_t handle;
    uint8_t* seqp;
    if (type == BLE_TYPE_STATUS) {
        handle = s_status_val_handle;
        seqp = &s_seq_status;
    } else if (type == BLE_TYPE_WAVE) {
        handle = s_ecg_val_handle;
        seqp = &s_seq_wave;
    } else {
        handle = s_ecg_val_handle;
        seqp = &s_seq_ctrl;
    }

    uint8_t  buf[5 + 255];
    size_t   k = 0;

    buf[k++] = BLE_FRAME_MAGIC;
    buf[k++] = (*seqp)++;
    buf[k++] = type;
    buf[k++] = len;
    if (len) {
        memcpy(&buf[k], payload, len);
        k += len;
    }
    buf[k] = crc8(&buf[1], 3 + len);   /* 覆盖 seq/type/len/payload */

    struct os_mbuf* om = ble_hs_mbuf_from_flat(buf, (uint16_t)(k + 1));
    if (om == NULL) return ESP_ERR_NO_MEM;

    /* ble_gatts_notify_custom 无论成功失败都会消费 mbuf（NimBLE 内部统一 free，
     * 头文件注释 "consumes supplied mbuf regardless of the outcome"）。
     * 调用方再释放会双 free 破坏堆——流控/缓冲满时该函数返回非零，此前这里又
     * free 一次，导致推流期间偶发重启。 */
    int rc = ble_gatts_notify_custom(s_conn_handle, handle, om);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}
