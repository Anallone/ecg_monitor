/**
 * ble_stream.h — 演示会话推流：把 player 当前样本按 360Hz 经 BLE Notify 发给上位机
 *
 * 与屏上回放解耦：屏上按 4 倍速推进 rt 引擎，本模块按**真实 360Hz**独立推送，
 * 这样上位机拿到的是实时流（而非 4 倍速），其「实时分析」才有意义。
 *
 * 未连接/未订阅时 begin() 直接 no-op，设备本地回放不受影响。
 */
#ifndef BLE_STREAM_H
#define BLE_STREAM_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** @brief 注册断开回调（内部转成标志，供主循环轮询）。app_main 里调用一次。 */
void ble_stream_init(void);

/**
 * @brief 开始一个推流会话
 * @param mode  BLE_MODE_DEMO / BLE_MODE_LIVE
 * @param name  会话名（样本名；可为 NULL）
 * @param sig   样本缓冲（调用方保证生命周期覆盖整个推流过程）
 * @param n     样本点数
 * @note 内部会先结束上一个会话；未连接/未订阅时直接返回。
 */
void ble_stream_begin(uint8_t mode, const char* name, const float* sig, int n);

/** @brief 开始一个“边播边推”会话（用于 SD 流式回放）。 */
void ble_stream_begin_feed(uint8_t mode, const char* name, int fs, int total);

/** @brief 喂入一段样本并立即按 BLE 帧发送。 */
void ble_stream_feed(const float* samples, int n);

/** @brief 当前是否处于“边播边推”会话中（供实时模式按需 begin）。 */
bool ble_stream_feed_active(void);

/** @brief 结束当前会话（发「会话结束」并等待推流任务退出；可重复调用） */
void ble_stream_end(void);

/** @brief 取出并清除「连接已断开」标志（主循环轮询：为真则停止回放回模式页） */
bool ble_stream_disconnect_pending(void);

#ifdef __cplusplus
}
#endif

#endif /* BLE_STREAM_H */
