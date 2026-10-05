/**
 * ecg_max30003.h — MAX30003 SPI 驱动入口（依赖 ESP-IDF）
 *
 * 与 ecg_source.h 分开，是为了让 ecg_source.h/.c 保持**不依赖 ESP-IDF**，
 * 可以在主机上单独编译测试重采样逻辑。
 *
 * 状态：编译通过（IDF v5.5.5），但**未上硬件验证**（SPI 时序 / INT1 中断 /
 *       归一化 scale 需接实物调）。寄存器位定义已按数据手册确认，见本目录 README.md。
 */
#ifndef ECG_MAX30003_H
#define ECG_MAX30003_H

#include "esp_err.h"

#include "ecg_source.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 初始化 SPI 总线、配置 MAX30003、启动采集任务
 * @param ctx 已由 ecg_src_max30003_init() 初始化的状态（生命周期需覆盖任务）
 * @return ESP_OK / ESP_ERR_NOT_FOUND（芯片无应答）/ 其他 ESP-IDF 错误
 *
 * @note 采样数据会通过 ecg_src_max30003_feed() 进入 ctx 的环形缓冲，
 *       上层用 ecg_source_t::read() 取（已重采样到 ecg_src_max30003_init 指定的 fs）。
 */
esp_err_t ecg_max30003_start(ecg_max30003_src_t* ctx);

/** @brief 停止采集任务并释放 SPI 总线 */
void ecg_max30003_stop(void);

#ifdef __cplusplus
}
#endif

#endif /* ECG_MAX30003_H */
