# ecg_source — 真实心电采集前端（MAX30003）

> 状态存档：2026-09-17。本组件是「实时模式」的采集层，**尚未接入 main.c**。
> 选型依据、接线、寄存器细节见 [`MAX30003_硬件选型与集成.md`](../../../docs/references/MAX30003_硬件选型与集成.md)。
> 2026-09-17 起，数据手册与官方软件已到手（`docs/CJMCU-30003 资料/`），
> 之前标 `TODO(手册)` 的两处寄存器位与 FIFO 数据格式均已按手册落实（见第三节）。

## 一、现状（三句话）

1. **能编译**：ESP-IDF v5.5.5 下 `ecg_source.c` 与 `ecg_max30003.c` 均编译通过
   （产物见 `firmware/build/esp-idf/ecg_source/CMakeFiles/__idf_ecg_source.dir/*.obj`）。
   本组件早期头注释里写的「未编译」已过时。
2. **已链接**：`main` 的 `PRIV_REQUIRES` 已加入 `ecg_source`，并接入实时页
   `ST_REALTIME`（2026-09-18）。此前「编译了但未链接」的状态已结束。
3. **没上硬件**：SPI 时序、寄存器配置、中断驱动全部**未经实物验证**。

## 二、已验证 / 未验证清单

| 项 | 状态 | 说明 |
|---|---|---|
| `ecg_resampler_run()` 分块独立性 | ✅ 已验证 | 用 Python 逐行复刻，chunk=1..1024 与一次性处理结果一致（偏差 ~1e-10）。**仅算法**，未在 C 下运行 |
| 组件能否编译 | ✅ 已验证 | IDF v5.5.5，2026-09-11 |
| 寄存器位定义 / FIFO 数据格式 | ✅ 已按数据手册确认 | EINT=D[23]、EN_EINT=D[23]、18-bit 左对齐+ETAG、CNFG_ECG=512 SPS（见第三节） |
| SPI 时序 / 寄存器实际生效 | ❌ 未验证 | 需实物 |
| 中断驱动（INT1） | ⚠️ 已实现，未上硬件 | 下降沿中断唤醒任务、排空 FIFO 的逻辑已落地，但未接实物验证 |
| 归一化 scale 取值 | ❌ 未标定 | 需按实测信号幅度调 |

## 三、已按数据手册闭合的寄存器点（原阻塞项）

数据手册（`docs/CJMCU-30003 资料/datasheet_max30003.pdf`）已到手，以下三点已落地到 `ecg_max30003.c`：

1. **`STATUS`(0x01) 的 FIFO 就绪位 = EINT = D[23]**（首字节 `0x80`）。
   `m3_eint_active()` 读 STATUS 判 `st[0] & 0x80`，不再盲读空 FIFO。
2. **`EN_INT`(0x02) 的使能位 = EN_EINT = D[23]**（`0x800000`）。
   `CFG_EN_INT = 0x800003`（EN_EINT + INTB_TYPE=11 保留默认开漏+上拉），已在 `m3_configure()` 写寄存器。
3. **ECG FIFO 数据格式**：24-bit 字 = `ECG Sample Voltage Data[17:0]`（左对齐于 DO[23:6]，
   有符号）| `ETAG[2:0]`=DO[5:3] | `PTAG[2:0]`=DO[2:0]。
   旧实现把整 24-bit 当符号扩展是错的；`m3_read_sample()` 已改为取高 18 位并解 ETAG。

> 顺带修正：官方 Arduino 库默认 `CNFG_ECG=0x805000` 实为 RATE=10 → **128 SPS**，
> 与本项目「512 SPS 采集」方案不符；已按手册改为 `0x005000`（RATE=00 → 512 SPS）。

## 四、接入前置条件（✅ 已满足）

原硬约束：**realtime 引擎必须先做环形缓冲改造**（`rt_engine_t` 原先假设「整段信号常驻 RAM 且有限」，
`rt_init()` 一次性预计算 `integ[]`、绝对索引读 `e->sig`）。

**已完成**：`realtime` 新增 live 模式（`rt_init_live` / `rt_feed` / `rt_sample_at`，
有界环 + 增量阈值窗），回放路径行为不变。实时页 `ST_REALTIME` 已接入本组件：

```
ST_REALTIME 循环：ecg_source.read() → player_feed() → player_tick() → monitor_draw(true)
```

详见 `firmware/README.md`「实时采集（MAX30003）」。⚠️ 仍**未上硬件验证**。

## 五、采样率与归一化

- MAX30003 只有 **128 / 256 / 512 SPS**，与本项目 `RT_FS = 360` 不是整数倍关系。
  方案：**512 SPS 采集 → 驱动层插值重采样到 360**，上层零改动（`ecg_resampler_*` 已实现）。
  驱动已把 `CNFG_ECG` 固定为 `0x005000`（RATE=00 → 512 SPS），调用方 `fs_native` 应传 **512**。
- 归一化（18-bit 整数 → float）**放在采集驱动层**，保持 `realtime` 引擎「不再滤波」的既有约定。
  `m3_read_sample()` 已按数据手册把 DO[23:6] 解成 18-bit 符号值再 `feed()`。

## 六、接线与总线（与本板 LCD/SD/触摸不冲突）

```
MAX30003（独立总线 SPI3_HOST，避免与 LCD/SD 抢 SPI2）
  SCK=IO12   MOSI=IO11   MISO=IO13   CS=IO10   INT1=IO9
```

> ⚠️ **不要接 IO5** —— 那是本板 LCD 复位脚，接上直接黑屏。
> 官方文档那张 ESP32 接线表写 `CS0 → GPIO5`，**不可照抄**。

## 七、建议落地顺序

1. 买模块 + 一次性 Ag/AgCl 电极；先跑通 `readDeviceID()`，串口绘图器里看到 PQRST
   （此阶段用湿电极，排除电极变量）。
2. ~~查到手册的两位定义~~ ✅ 已完成：EINT=D[23]、EN_EINT=D[23]、18-bit+ETAG 均按数据手册落地。
3. ✅ 已用 INT1 下降沿中断替掉盲读（`m3_int1_init` / `m3_task`）；**未上硬件**，
   上电后需确认不丢样、不重样（看 `ecg_max30003_src_t::overflow`）。
4. 完成上面 §4 的引擎环形缓冲改造。
5. 512 → 360 重采样喂引擎，打通「实时模式」页面。
6. 最后才考虑干电极 / 手环结构件（接触阻抗、运动伪影、镍释放合规）。

## 八、组件内文件

| 文件 | 职责 | IDF 依赖 |
|---|---|---|
| `ecg_source.h` / `.c` | 采集源抽象 + 内存源 + 重采样器 + MAX30003 环形缓冲 | ❌ 无（可在主机单独编译测重采样） |
| `ecg_max30003.h` / `.c` | SPI/寄存器/INT1 时序，采到样本后 `feed()` | ✅ 有 |

把 ESP-IDF 相关代码单独放在 `ecg_max30003.*`，是为了让重采样逻辑能在主机上离线验证。
