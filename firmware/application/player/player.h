/**
 * player.h — 样本回放（引擎持有者 + 样本目录）
 *
 * 本模块拥有实时引擎实例与当前样本，是「样本列表页 / 监测页 / 报警页」共用的数据源：
 *   - 样本目录：启动时扫描 SD 卡（无卡则为 0 条），列表页据此渲染，点选后交给 player_start
 *   - 引擎：持有 rt_engine_t，页面只通过 player_engine() 只读访问；
 *     推进/判断结束/复位统一走 player_tick / player_finished / player_reset_keep_hr，
 *     页面不直接调 rt_* —— 避免 UI 与状态机各自操作同一份引擎状态。
 *
 * 内置样本（firmware/application/player/ecg_sample.h，由 tools/embed_ecg.py 生成）
 * 恒编译：SD 卡不可用时菜单仍提供「内置样本」一项，便于无卡演示与调试。
 */
#ifndef PLAYER_H
#define PLAYER_H

#include <stdbool.h>

#include "realtime.h"

#ifdef __cplusplus
extern "C" {
#endif

/** player_start() 的特殊索引：回放内置样本（不占 SD 卡目录） */
#define PLAYER_BUILTIN (-1)

/** 回放节拍与倍速（与端侧实时引擎一致：20ms tick、4 倍速） */
#define PLAYER_TICK_MS 20
#define PLAYER_SPEED   4

/** 扫描 SD 卡样本（挂载 + 列目录）。返回样本数；无卡或失败返回 0。 */
int player_scan(void);

int         player_count(void);
const char* player_name(int idx);

/**
 * 开始回放第 idx 个样本（PLAYER_BUILTIN 为内置样本）。
 * 内部会先停止当前回放；失败时已回滚到停止态。
 */
bool player_start(int idx);

/** 停止回放并释放样本缓冲（可重复调用） */
void player_stop(void);

/* ─── 直播模式（实时采集）───
 * 与回放共用同一个引擎实例与 monitor 页面：player_engine()/player_tick() 不变，
 * 只是数据由采集前端经 player_feed() 边到边喂入。 */

/** 启动直播引擎（有界环形缓冲，无限流）。返回是否成功。 */
bool player_start_live(int fs);

/** 喂入一段已按 fs 重采样的实时样本（由采集任务/主循环调用）。 */
void player_feed(const float* samples, int n);

/** 当前样本名（内部静态缓冲；停止后为空串） */
const char* player_current_name(void);

/* ─── 引擎访问 ─── */
const rt_engine_t* player_engine(void);   /* 只读：页面取心率/报警/计数/波形数据 */
void player_tick(void);                   /* 推进一个 tick（TICK_MS） */
bool player_finished(void);               /* 是否回放结束 */
void player_reset_keep_hr(void);          /* 复位检测状态但保留上次心率（报警锁存期间续播） */
void player_flush(void);                  /* 流播完时冲刷末拍分类（直播引擎不会自动冲刷） */

/** 内置样本（供开机动画描记）。*out_len 非空时写入长度。 */
const float* player_builtin(int* out_len);

/* ─── 当前回放源（供 BLE 推流取数） ───
 * 与 player_engine() 指向同一份缓冲；回放停止后返回 NULL/0。
 * 生命周期由 player 持有：调用方须在 player_stop() 前用完（含推流任务）。 */

/** 当前回放的样本缓冲（未在回放时返回 NULL，*out_len 置 0） */
const float* player_active_sig(int* out_len);

/** 当前回放源的总采样点数（流式 SD 样本返回文件头 n；停止后返回 0）。 */
int player_active_len(void);

/** 当前回放进度（0..1）；SD 流式样本按已喂入进度，避免实时引擎前瞻导致的尾段不归 100%。 */
float player_progress(void);

/** 当前样本名（内置样本为 "BUILTIN"，SD 样本为文件名；停止后为空串） */
const char* player_active_name(void);

#ifdef __cplusplus
}
#endif

#endif /* PLAYER_H */
