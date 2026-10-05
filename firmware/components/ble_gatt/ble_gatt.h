/**
 * ble_gatt.h — BLE GATT 服务（NimBLE 外设）：演示样本流 + 状态心跳
 *
 * 与上位机 `src/ble_client.py` 遵守同一套帧协议，UUID 与枚举**必须两边同步改**。
 *
 * 帧格式（二进制，小端）：
 *   [0xAA][seq u8][type u8][len u8][payload(len 字节)][crc8]
 *   crc8：poly 0x07、init 0x00，覆盖 [seq,type,len,payload]（不含 0xAA 与校验本身）。
 *   seq 由本模块自增，且**按类型独立计数**：只有波形帧的 seq 参与上位机丢包统计，
 *   心跳与会话帧各有自己的计数器，避免在波形流里顶出假的序号空洞。
 *
 * GATT 结构（服务 UUID a1b20001-…）：
 *   ECG Data (a1b20002) Notify  波形数据包（type 0x01）
 *   Status   (a1b20003) Notify  状态心跳（type 0x04）
 *   Control  (a1b20004) Write   上位机命令（v1 预留）
 *   Info     (a1b20005) Read    设备名/固件版本
 */
#ifndef BLE_GATT_H
#define BLE_GATT_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ── 帧协议（与 src/ble_client.py 保持一致） ── */
#define BLE_FRAME_MAGIC 0xAA

#define BLE_TYPE_WAVE          0x01   /* payload = N 个 int16 小端样本 */
#define BLE_TYPE_SESSION_START 0x02   /* payload = [mode u8][name UTF-8] */
#define BLE_TYPE_SESSION_END   0x03   /* payload 空 */
#define BLE_TYPE_STATUS        0x04   /* payload = [mode u8][battery u8][sent u32][overflow u32] */

/* 会话模式：演示样本已滤波+z-score，实时样本为原始值（本轮仅实现演示） */
#define BLE_MODE_DEMO 0x01
#define BLE_MODE_LIVE 0x02

/** 波形样本线格式：float -> int16，与 SD 卡 ECG1 / tools/export_samples.py 的 2000 一致 */
#define BLE_SAMPLE_SCALE 2000.0f

/** 断开回调（在 NimBLE 主机任务上下文触发，回调内只置标志，不要做重活） */
typedef void (*ble_gatt_disconnect_cb_t)(void);

/** @brief 初始化 NimBLE 并注册 GATT 服务/开始广播。失败返回非 ESP_OK。 */
esp_err_t ble_gatt_init(void);

/** @brief 当前是否有中心设备已连接 */
bool ble_gatt_connected(void);

/** @brief 中心设备是否已订阅波形通知（未订阅时推流无意义） */
bool ble_gatt_subscribed(void);

/** @brief 当前连接的 ATT MTU（未连接/未协商时返回 23） */
uint16_t ble_gatt_mtu(void);

/**
 * @brief 发送一帧（自动填 seq 与 crc8）
 * @param type    见 BLE_TYPE_*
 * @param payload 载荷（可为 NULL，此时 len 必须为 0）
 * @param len     载荷长度（<= 250）
 */
esp_err_t ble_gatt_notify(uint8_t type, const uint8_t* payload, uint8_t len);

/** @brief 注册断开回调（覆盖旧值；传 NULL 取消） */
void ble_gatt_set_on_disconnect(ble_gatt_disconnect_cb_t cb);

#ifdef __cplusplus
}
#endif

#endif /* BLE_GATT_H */
