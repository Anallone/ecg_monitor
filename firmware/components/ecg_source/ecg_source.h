/**
 * ecg_source.h — ECG 采集源抽象
 *
 * 目的：把「样本从哪来」和「流式处理」解耦。
 *   - 离线回放（flash 内置样本 / SD 载入样本）→ ecg_src_mem
 *   - 真实前端（MAX30003 over SPI）        → ecg_src_max30003
 * 两者对上层暴露同一个 read() 接口，产出**已归一化的 float 样本**。
 *
 * 归一化约定：本组件负责把原始整数样本转成 float 并做缩放对齐（scale），
 * 但**不做滤波、不做 z-score**——那仍是 realtime 引擎的约定（见 realtime.h）。
 *
 * 状态（2026-09-11）：已在 ESP-IDF v5.5.5 下**编译通过**，但**未上硬件验证**，
 * 且尚未被任何组件 REQUIRES（编译了但未链接）。详见本目录 README.md。
 */
#ifndef ECG_SOURCE_H
#define ECG_SOURCE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ───────────────────────── 通用接口 ───────────────────────── */

/** 采集源句柄。read() 是唯一的取数入口。 */
typedef struct ecg_source {
    const char* name;   /* 便于日志 */
    int         fs;     /* 本源对外输出的采样率 Hz（重采样后） */
    void*       ctx;    /* 具体实现的状态 */

    /**
     * @brief 拉取最多 max 个样本
     * @param out  输出缓冲（float，已归一化）
     * @param max  缓冲容量
     * @return 实际写入的样本数（>=0）；-1 表示错误
     * @note  非阻塞：无新数据时返回 0，调用方自行决定等待策略。
     */
    int  (*read)(struct ecg_source* self, float* out, int max);

    /** @brief 数据源是否已结束（流式源恒 false） */
    bool (*finished)(struct ecg_source* self);

    /** @brief 释放内部资源（不含 ctx 本身，ctx 由调用方持有） */
    void (*deinit)(struct ecg_source* self);
} ecg_source_t;

/* ───────────────────────── 内存源 ─────────────────────────
 * 包装一整段常驻缓冲，语义与当前 main.c 的 start_play() 一致：
 * 从 pos=0 顺序读出，读满 n 即 finished()。
 */

typedef struct {
    const float* sig;   /* 调用方保证生命周期（内置样本为 const flash，SD 样本为堆缓冲） */
    int          n;
    int          pos;
} ecg_mem_src_t;

/**
 * @brief 初始化内存源
 * @param src   待填充的接口
 * @param ctx   调用方持有的状态（生命周期需覆盖 src）
 * @param name  日志名
 * @param sig   样本数组
 * @param n     样本数
 * @param fs    采样率（本项目通常传 RT_FS=360）
 */
void ecg_src_mem_init(ecg_source_t* src, ecg_mem_src_t* ctx,
                      const char* name, const float* sig, int n, int fs);

/* ───────────────────────── 重采样 ─────────────────────────
 * MAX30003 只有 128/256/512 SPS，与 RT_FS=360 不是整数倍关系，
 * 必须重采样。这里用线性插值（512→360 时每输出点约 1.42 输入点，
 * 线性插值精度足够；若后续发现频响不足再换多相滤波）。
 *
 * 用法：每个输入块调一次 run()，phase 跨块保持，因此**块边界连续**。
 */

typedef struct {
    int    fs_in;
    int    fs_out;
    double phase;    /* 下一个输出点对应的输入位置（相对本块起点，可为小数） */
    float  prev;     /* 上一块最后一个输入样本（跨块插值用） */
    bool   has_prev;
} ecg_resampler_t;

/** @brief 初始化重采样器 */
void ecg_resampler_init(ecg_resampler_t* r, int fs_in, int fs_out);

/**
 * @brief 重采样一个块
 * @param in       输入样本
 * @param n_in     输入样本数
 * @param out      输出缓冲
 * @param out_cap  输出缓冲容量
 * @return 写入 out 的样本数（>=0）
 *
 * @note 输出缓冲容量建议 >= ceil(n_in * fs_out / fs_in) + 1。
 *       跨块状态保存在 r->phase / r->prev，调用方不要自行修改。
 */
int ecg_resampler_run(ecg_resampler_t* r, const float* in, int n_in,
                      float* out, int out_cap);

/* ───────────────────── MAX30003 采集源 ─────────────────────
 * 分工：
 *   - ecg_max30003.c（本组件内）负责 SPI/INT1 与寄存器时序，拿到原始样本后
 *     调用 ecg_src_max30003_feed() 投喂；
 *   - 本文件的环形缓冲负责生产者/消费者解耦，并把原生采样率重采样到目标采样率。
 *
 * 线程模型：单生产者（SPI 任务）、单消费者（引擎任务）。
 * feed() 必须在**任务上下文**调用，不要在 ISR 里直接调用（volatile 索引
 * 的 SPSC 环形缓冲在任务间是安全的，在 ISR 里则需要额外的内存屏障）。
 */

#define ECG_MAX30003_RING  4096                        /* 环形缓冲容量（原生采样率下的样本数） */
#define ECG_MAX30003_CHUNK 128                         /* 每次重采样的原生样本块大小 */
/* 重采样输出暂存。最坏情况：128 SPS 原生 → 360 升采样时，每 128 样本块最多输出
 * ceil(128*360/128)+1 = 361 点（正是 ecg_resampler_run 接口注释要求的容量下界）；
 * 旧的 CHUNK*2+2=258 装不下，块中途耗尽容量后 phase 被钳到 0，
 * 每块约丢 28% 的输出样本。CHUNK*3+2=386 覆盖全部文档化采样率（128/256/512）。 */
#define ECG_MAX30003_STAGE (ECG_MAX30003_CHUNK * 3 + 2)

typedef struct {
    volatile int head;    /* 写指针，仅生产者（feed）修改 */
    volatile int tail;    /* 读指针，仅消费者（read）修改 */
    float        ring[ECG_MAX30003_RING];
    int          fs_native;   /* AFE 原生采样率 */
    float        scale;       /* 原始 LSB → 归一化系数：输出 = raw * scale */
    ecg_resampler_t rs;       /* 原生 → 对外采样率 */
    float        tmp[ECG_MAX30003_CHUNK];   /* 消费者临时缓冲 */
    float        stage[ECG_MAX30003_STAGE]; /* 重采样输出暂存 */
    int          stage_len;
    int          stage_pos;
    volatile uint32_t overflow;  /* 环形缓冲溢出丢样计数（调试用，>0 说明消费太慢） */
} ecg_max30003_src_t;

/**
 * @brief 初始化 MAX30003 源
 * @param src        待填充的接口
 * @param ctx        调用方持有的状态
 * @param fs_native  AFE 原生采样率（128 / 256 / 512）
 * @param fs_out     对外输出采样率（本项目填 RT_FS=360）
 * @param scale      原始样本缩放系数：输出 = raw * scale
 *                   （18-bit 有符号满量程 2^17，通常取 1/131072.0f 归一到 ±1）
 *
 * @note src->fs 会被设为 fs_out。
 */
void ecg_src_max30003_init(ecg_source_t* src, ecg_max30003_src_t* ctx,
                           int fs_native, int fs_out, float scale);

/**
 * @brief 投喂一个原始样本（由 SPI 任务在 INT1 触发并读完 FIFO 后调用）
 * @param raw  MAX30003 ECG_FIFO 读回的 24-bit 字（符号扩展后的 int32）
 */
void ecg_src_max30003_feed(ecg_max30003_src_t* ctx, int32_t raw);

/* ───────────────────────── 未验证事项 ─────────────────────────
 * ✅ 已验证：ecg_resampler_run() 的算法正确性——用 Python 逐行复刻后实测
 *    「分块独立性」（chunk=1..1024 与一次性处理结果一致，偏差 ~1e-10），
 *    详见 ecg_source.c 顶部注释。该算法未在 C 下编译过。
 *
 * ⚠️ 仍未验证：
 *  1. 编译已通过（IDF v5.5.5）；但组件**未被链接进固件**——
 *     没有任何组件 REQUIRES 它。接入时需在 main 的 PRIV_REQUIRES 里加 ecg_source。
 *  2. ecg_max30003.c 的 SPI 时序、INT1 中断、寄存器配置**未上硬件验证**。
 *     寄存器位已按数据手册确认（STATUS 的 EINT=D[23]、EN_INT 的 EN_EINT=D[23]、
 *     ECG FIFO 的 18-bit 左对齐 + ETAG、CNFG_ECG 的 512 SPS）。
 *  3. ECG_MAX30003_RING 是否够大：512 SPS 下 4096 点仅 8 秒，
 *     若引擎任务可能长时间不 read()，需加大或改用 FreeRTOS StreamBuffer。
 *  4. scale 取值需按实测信号幅度调整（AFE 增益由 CNFG_ECG 决定，当前 20V/V）。
 *  5. 若要真正接引擎，还需先完成 realtime 引擎的环形缓冲改造
 *     （见 docs/references/MAX30003_硬件选型与集成.md §6.3）。
 */

#ifdef __cplusplus
}
#endif

#endif /* ECG_SOURCE_H */
