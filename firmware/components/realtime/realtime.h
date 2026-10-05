/**
 * realtime.h — ECG 流式处理引擎（游标推进 + 因果 R 峰检测 + 逐拍分类 + 心率报警）
 *
 * 与 Python 端 `src/realtime.py` 的语义对齐，但改为**因果/流式**：
 *  - R 峰检测的移动平均用「尾随窗」（只看过去 54 点），不用未来点；
 *    代价是约 27 点（75ms）触发延迟，由 ±18 点原信号精修校正回真实峰位。
 *  - 阈值用「过去 1800 点（5 秒）滑动窗」的 mean + 0.5*std，而非整段全局统计。
 *  - mean_rr 用「已检测到的 RR 运行均值」，而非整段均值（因果近似）。
 *
 * 样本已由 Python 端滤波 + z-score（见 tools/export_samples.py），引擎不再滤波。
 *
 * 两种数据来源（共用同一套检测/分类代码，仅取数方式不同）：
 *   - 回放（rt_init）：整段常驻 RAM，绝对下标；样本为预滤波+zscore。
 *   - 直播（rt_init_live + rt_feed）：无限流 + 有界环形缓冲；样本来自 MAX30003，
 *     采集层已做数字滤波（0.5–30Hz 带通 + 50Hz 陷波，见 ecg_filter.h）后再喂入。
 */
#ifndef REALTIME_H
#define REALTIME_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 与 Python src/config.py 对齐 */
#define RT_FS        360
#define RT_PRE_R     64
#define RT_POST_R    122
#define RT_WIN_LEN   (RT_PRE_R + 1 + RT_POST_R)   /* 187 */

/* 心率报警阈值（bpm），与 src/realtime.py 的 HR_HIGH / HR_LOW 一致 */
#define RT_HR_HIGH   100
#define RT_HR_LOW    50
/* 心率平滑窗口：最近 N 个有效 RR 间期的中位数，与 src/realtime.py 的 HR_MEDIAN_N 一致 */
#define RT_HR_MED_N  7
/* 报警确认：心率连续越界多少拍才触发报警，与 src/realtime.py 的 HR_ALARM_CONFIRM 一致 */
#define RT_HR_ALARM_CONFIRM 3
/* 信号质量门限（导联脱落检测）：滤波后信号在 RT_SIG_WIN 内的峰峰幅低于此值判为无信号。
 * 与 src/realtime.py 的 SIG_WIN / SIG_P2P_MIN 一致；阈值按真实 BLE 记录标定，可按需再调。 */
#define RT_SIG_WIN      720    /* 2s @360Hz */
#define RT_SIG_P2P_MIN  0.008f

#define RT_MAX_BEATS 512

/** 单个心拍记录（供 UI 画类别竖线） */
typedef struct {
    int32_t r;    /* R 峰采样序号 */
    int8_t  cls;  /* 0..4 = N/S/V/F/Q；-1 = 尚未分类 */
    int8_t  alarm;/* 该拍的心率报警：0 无 / 1 过速 / 2 过缓 */
} rt_beat_t;

typedef struct {
    const float* sig;   /* 样本数据（不拷贝，调用方保证生命周期）；live 时指向 sig_ring */
    int      n;         /* 采样点数；live 时为「已喂入总数」（只增） */
    int      fs;        /* 采样率 */
    int      speed;     /* 加速倍率：每 tick 推进 speed * fs * tick_ms/1000 点 */

    float*   integ;     /* 因果积分信号（init 时预计算）；live 时为 NULL */
    int      pos;       /* 游标：已处理到的采样序号 */

    /* ── live（直播）模式：样本/积分存环形缓冲，下标用掩码映射 ──
     * 回放是「整段常驻 + 绝对下标」，直播是「无限流 + 有界环」，
     * 二者共用同一套检测/分类代码，仅取数方式不同（见 sig_at/integ_at）。 */
    bool     live;
    float*   sig_ring;
    float*   integ_ring;
    int      sig_cap;      /* 环形容量（2 的幂，用 i & (cap-1) 映射） */
    int      integ_cap;
    float    integ_acc;    /* 尾随 RT_MA_WIN 个 diff² 的运行和（live 增量算 integ） */

    /* 检测状态 */
    int      last_r;        /* 上一个 R 峰序号，-1 表示尚无 */
    float    thr;           /* 当前阈值 */
    int      thr_tick;      /* 阈值重算计数器 */
    float*   thr_win;       /* 滑动阈值窗口（环形），长度 RT_THR_WIN */
    /* live 时阈值窗跨 tick 增量维护（环形缓冲下无法回看 1800 点重建） */
    int      thr_idx;
    int      thr_cnt;
    double   thr_sum;
    double   thr_sumsq;

    /* RR / 心率 */
    float    rr_sum;        /* 已检测 RR 之和（秒） */
    int      rr_cnt;
    float    mean_rr;       /* 运行平均 RR（秒） */
    float    hr;            /* 最近一次心率（bpm），0=未知 */
    int      alarm;         /* 最近一次报警：0/1/2 */
    int      alarm_cand;    /* 报警候选方向（0/1/2） */
    int      alarm_cnt;     /* 该候选方向连续越界拍数 */
    /* 心率平滑：最近 RT_HR_MED_N 个有效 RR 间期的中位数窗口 */
    float    rr_win[RT_HR_MED_N];
    int      rr_win_idx;
    int      rr_win_cnt;
    /* 信号质量（live）：滤波后信号峰峰幅 */
    float    sig_min;
    float    sig_max;
    int      sig_cnt;
    bool     signal_ok;

    /* 心拍 */
    rt_beat_t beats[RT_MAX_BEATS];
    int      n_beats;       /* 已检测心拍数 */
    int      n_classified;  /* 已分类心拍数（<= n_beats，延迟一拍） */
    int      cls_count[5];
} rt_engine_t;

/** 滑动阈值窗口长度（5 秒 @360Hz） */
#define RT_THR_WIN 1800
/** 预热采样数：不足此长度不做检测（阈值不可靠） */
#define RT_WARMUP  720
/** 最小 R 峰间距 = 60/220*fs ≈ 97 点（220bpm 上限） */
#define RT_MIN_DIST 97
/**
 * 峰位精修邻域（非对称）：尾随移动平均使积分峰比真实 R 峰滞后约
 * (RT_MA_WIN-1)/2 ≈ 27 点，故向前多搜、向后少搜。
 * 实测（Python 复刻 + 与批处理 pan_tompkins 对比 4 个样本）：
 *   对称 ±18 -> 匹配 130、多检 12；back30/fwd10 -> 匹配 134、多检 8；
 *   完全不精修 -> 匹配仅 107（精修不可省）。
 */
#define RT_REFINE_BACK 30
#define RT_REFINE_FWD  10

/**
 * 直播模式的样本/积分环形缓冲容量（**必须是 2 的幂**，下标用掩码映射）。
 * 需覆盖：显示窗(1080) + 阈值窗(1800) + 分类后视(123) + 精修(30)。
 * 2048（5.7s@360Hz）满足；扩容只会多占堆，不影响正确性。
 */
#define RT_LIVE_CAP 2048

/**
 * @brief 初始化引擎（会预计算因果积分信号，malloc 内部缓冲）
 * @param sig   样本数据（已滤波+zscore），引擎不拷贝，调用方保证其生命周期
 * @param n     采样点数
 * @param fs    采样率（用 RT_FS）
 * @param speed 加速倍率（1=实时，4=4 倍速）
 * @return 0 成功，负值失败
 */
int rt_init(rt_engine_t* e, const float* sig, int n, int fs, int speed);

/**
 * @brief 初始化**直播**引擎（有界环形缓冲，供 MAX30003 等无限流使用）
 * @param fs    采样率（用 RT_FS）
 * @param speed 推进倍率（直播恒用 1）
 * @param cap   环形容量，必须为 2 的幂（用 RT_LIVE_CAP）
 * @return 0 成功，负值失败
 * @note 与 rt_init 的区别：不预计算，样本由 rt_feed() 边到边喂；
 *       e->n 为「已喂入总数」（只增），e->sig 指向内部环形，供显示判「有无数据」。
 */
int rt_init_live(rt_engine_t* e, int fs, int speed, int cap);

/**
 * @brief 向直播引擎喂入一段样本（已按 RT_FS 重采样）
 * @note 内部逐样本更新因果积分与阈值窗；再调 rt_tick() 做检测/分类。
 */
void rt_feed(rt_engine_t* e, const float* samples, int cnt);

/** @brief 取绝对下标 i 处的样本（回放=直接下标；直播=环形映射）。越界返回 0。 */
float rt_sample_at(const rt_engine_t* e, int i);

/** @brief 释放内部缓冲 */
void rt_deinit(rt_engine_t* e);

/**
 * @brief 按 tick_ms 推进游标并处理（检测 R 峰 + 对已满足条件的拍做分类）
 * @param tick_ms 距上次调用经过的毫秒数（用于计算推进步长，通常传 20）
 * @return 本次新分类完成的心拍数
 */
int rt_tick(rt_engine_t* e, int tick_ms);

/** @brief 是否已播放完整个样本 */
bool rt_finished(const rt_engine_t* e);

/** @brief 播放进度 0.0~1.0 */
float rt_progress(const rt_engine_t* e);

/** @brief 当前游标位置（采样序号） */
int rt_pos(const rt_engine_t* e);

/** @brief 重置到开头 */
void rt_reset(rt_engine_t* e);

/**
 * @brief 冲刷待分类的末拍（post-RR 用 mean_rr 回退），返回本次分类数量。
 * 直播引擎在流播完后由播放层显式调用，让最后一拍进入统计。幂等。
 */
int rt_flush(rt_engine_t* e);

#ifdef __cplusplus
}
#endif

#endif /* REALTIME_H */
