# 端侧部署（微雪 ESP32-S3 2.8 寸电容触控屏）

## 硬件

- 微雪 ESP32-S3 2.8 寸电容触控屏开发板（LX7 双核 240MHz，ST7789V 240×320）
- 引脚（复用 `maincontrol` 工程已验证的配置）：
  - LCD：SCLK=IO39，MOSI=IO38，MISO=IO40，DC=IO42，CS=IO45，RST=IO5，BL=IO1
  - 触摸（I2C0）：SDA=IO48，SCL=IO47
- **触摸 IC 实测为 CST816S（I2C 0x15），非 FT6336U（0x38）**。
  固件在 `lcd_init()` 里扫描 I2C 总线后按结果自动选择驱动：
  命中 0x38 用 `esp_lcd_touch_ft5x06`，命中 0x15 用 `esp_lcd_touch_cst816s`，
  两者都无则跳过触摸初始化（不再盲试刷错误日志）。

## 固件目录

```
firmware/
├── CMakeLists.txt           # 根构建（依赖环境变量 IDF_PATH，运行 IDF export 脚本设置）
├── sdkconfig.defaults       # esp32s3 / 240MHz / 8MB flash
├── partitions.csv
├── main/
│   ├── main.c               # 主链路：R峰检测 + 推理 + 屏显
│   └── ecg_sample.h         # 示例 ECG（tools/embed_ecg.py 生成）
└── components/
    ├── lcd/                 # ST7789 显示驱动（复用 maincontrol，已去 initcall）
    └── ecg_infer/           # res_se_cnn_rr4 手写前向推理（读 model_data.h/model_config.h）
```

## 模型嵌入流程

1. 训练 + 导出 int8 权重（在项目根目录）：

   ```bash
   ./runtime/python/python.exe run.py train --model res_se_cnn_rr4
   ./runtime/python/python.exe src/export_c_model.py --model res_se_cnn_rr4
   ```

2. 把导出的头文件复制进推理组件：

   ```bash
   cp export/c_model/model_data.h   firmware/components/ecg_infer/
   cp export/c_model/model_config.h firmware/components/ecg_infer/
   ```

3. 生成示例 ECG 样本（可选，替换占位正弦波）：

   ```bash
   ./runtime/python/python.exe tools/embed_ecg.py --record 100 --len 1800
   ```

   > 生成后 `main.c` 中 `ECG_SAMPLE_PROVIDED` 需定义（或在 CMakeLists 加
   > `-DECG_SAMPLE_PROVIDED`），否则默认走占位正弦波。

4. 生成中文字库（**改动界面中文文案后必须重跑**）：

   ```bash
   ./runtime/python/python.exe tools/gen_cjk_font.py
   ```

   工具会扫描 `main/main.c` 里所有会显示在屏上的字符串，取出用到的汉字
   （ASCII 直接内置全集），用系统字体渲染成 16x16 点阵写入
   `components/lcd/font16.h`。只嵌入实际用到的字（当前约 63 个汉字 + 95 个 ASCII，
   4 KB），而不是整本 GB2312（267 KB）。

   > 三点约定：
   > 1. 汉字与 ASCII 同源同尺寸渲染；ASCII 用 21pt 以补偿 CJK 字体里
   >    拉丁字形偏小的问题，使中英混排视觉等重。觉得数字偏大/偏小就调
   >    `tools/gen_cjk_font.py` 里的 `ASCII_PT`。
   > 2. 每字符步进恒为 16px，故排版可按「字符数 × 16 × scale」精确计算，
   >    单行最多 15 字符（scale=1）。超宽会被画出屏外。
   > 3. 新增了汉字却忘了重跑工具时，屏上该字会显示为**实心方块**——
   >    这是刻意设计的占位，便于一眼发现遗漏。
   > 4. `font16.h` 已显式列入 `components/lcd/CMakeLists.txt` 的 `SRCS`。
   >    这一步不能省：它是脚本生成的，若不列入，ninja 不把它当依赖，
   >    重新生成字库后 `lcd.c` 不会重编，固件里仍是旧点阵——表现就是
   >    「文案更新了、字库也生成了，屏上却还是方块」。改完字库若不确定，
   >    可 `touch components/lcd/lcd.c` 强制重编。

## 端侧推理核对（改 ecg_infer.c 后必做）

`tools/verify_c_export.py` 只校验「导出数据 + 算法语义」，它用规整的 numpy 数组
模拟前向，**看不到 C 实现的内存布局问题**。曾经就有一次：`maxpool_inplace`
池化后没把通道步长从旧长度收紧到新长度，导致第 2 层起逐通道读错位（只有通道 0
恰好正确），当时该脚本照样报「完全一致」，而端侧输出全错。

凡改动 `ecg_infer.c` 的缓冲/索引逻辑，按下面步骤在真机上核对：

1. 临时在 `ecg_infer.c` 里加探针：每层结束打印 `out[0..2]`、层循环后打印
   `pooled[0..4]`；在 `realtime.c` 的 `classify_beat` 末尾打印
   `r / cls / logits / rr_feat`。
2. 屏上播内置样本（演示模式 → `BUILTIN`），抓串口日志。
3. 在主机上用**同一窗口**算出参考值：取 `g_ecg_sample`，窗口
   `sig[r-64 : r+123]`，`zscore_normalize` 后按 `export_c_model.extract_layers`
   逐层前向（注意数组按 `(ch, len)` 行主序，`out[0..2]` 是**通道 0 的前 3 个时间
   点**，不是前 3 个通道）。

合格标准：逐层中间值与 Python 一致，逐拍 `cls` 与 Python 完全一致，`logits`
差 ≲0.07（float32 累加误差），R 峰位置与 RR 特征逐位相同。

## 编译

在 Windows 下通过 `cmd.exe` 构建（**不要用 Git Bash/MSys 直接调 export.bat**，IDF 会拒绝）。
官方离线安装器会在 `D:\Espressif\` 下生成 `idf_cmd_init.bat`，先 `call` 它再 `idf.py build`；
本仓库自带的引导脚本更省事（见下）。

更省事的做法是用仓库自带的引导脚本 `firmware\idf.bat`（cmd）——它内部设置
`IDF_TOOLS_PATH`、国内下载镜像、IDF 专用 Python 3.11，再调用官方 `export.bat`：

```bat
cd /d D:\ecg_monitor\firmware
idf.bat build
```

PowerShell 等价：`powershell -ExecutionPolicy Bypass -File firmware\idf.ps1 build`；
VS Code 里直接 `Ctrl+Shift+B`（任务名 "ESP-IDF: build"）。

> 示例安装路径：ESP-IDF 位于 `D:\esp\v5.5.1\esp-idf`，工具链与 IDF 的 Python
> 环境位于 `D:\esp\.espressif`（xtensa-esp-elf 14.2.0）。
> `firmware\idf.bat` / `firmware\idf.ps1` 会自动探测，换机器无需改脚本。
> 构建产物 `build/ecg_monitor.bin` ≈ 698 KB（app 分区 1 MB，余量 32%）。

## 烧录与监视

```bash
idf.py -p COMx flash monitor
```

> 本机的板子（ESP32-S3，走 USB-Serial/JTAG）枚举为 **COM3**；
> 用引导脚本时可直接 `firmware\idf.bat -p COM3 flash monitor`。

## 实时采集（MAX30003）

「实时模式」已接到 MAX30003 前端：进入实时页时启动采集，链路为
`ecg_source`（512 SPS → 重采样 360）→ live 环形引擎（`rt_init_live` / `rt_feed`）
→ 屏上实时波形 / 心率 / 报警 / 心拍分类。**未接模块时不报错**，实时页显示「等待前端信号」。

### 接线

| MAX30003 模块 | ESP32-S3 本板 | 说明 |
|---|---|---|
| SCLK | **IO12** | 独立 SPI3_HOST |
| MOSI (SDI/DIN) | **IO11** | |
| MISO (SDO/DOUT) | **IO13** | |
| CS | **IO10** | 任意 GPIO 均可，改 `M3_PIN_CS` 即可 |
| INT1 (INTB) | **IO9** | 下降沿中断，低有效 |
| GND | GND | 共地必须 |
| VIN / 3V3 | 3V3 | 供电与电平以模块原理图为准 |

- 引脚定义在 `components/ecg_source/ecg_max30003.c` 顶部的 `M3_PIN_*`，与你的板子冲突就改这里。
- ⚠️ **绝不要接 IO5**：那是本板 LCD 复位脚，接上直接黑屏。IO9~IO13 与本板
  LCD(38~42,45)/SD(41)/触摸 I2C(47,48) 均不冲突。
- 独立 SPI3_HOST，与 LCD/SD 的 SPI2 互不抢总线。
- 模块板载 32.768kHz 晶振（X1），**无需外部 FCLK**（官方示例的 Timer 外部时钟是无晶振变体的接法）。
- 电极：LA(红)/RA(黑)/RL(绿)，三电极单导联；RL 不可省。

### 端侧行为与待调项

- 采集任务由 INT1 唤醒（FIFO 达到 EFIT=16 时 INTB 拉低），排空 FIFO 后经重采样投喂引擎；
  20ms 一轮 ≈7 点，与 360Hz 实时速率匹配。
- 归一化 `scale = 1/131072`（18-bit 满量程 → ±1），心电幅度不合适时改 `main.c` 里
  `ecg_src_max30003_init(..., 1.0f/131072.0f)` 的 scale。
- 实时会话**不做数字滤波**，只有 AFE 的硬件 DHPF(0.5Hz)/DLPF(40Hz)；
  工频哼声可能影响检测，后续可在采集层补 50Hz 陷波。
- 实时模式暂不进入报警锁存页（只在监测页把心率/报警文字变红）。
- ⚠️ **未上硬件验证**：SPI 时序、INT1 中断、scale 都需实测确认。

## 蓝牙上传（BLE）

演示模式回放的样本会经 **BLE GATT Notify** 推给上位机（`run.py gui` 选「蓝牙设备」数据源），
用于无线实时分析/显示/落盘。独立于屏上 4× 回放：BLE 侧按**真实 360Hz**推送。

- 组件：`components/ble_gatt/`（NimBLE GATT 服务）+ `application/ble_stream/`（推流任务）。
- 广播名 **`ECG-Monitor`**，栈选 **NimBLE**（本板无 PSRAM，NimBLE 占用远小于 Bluedroid）。
- 帧格式与 UUID **两端共用**，改一处必须同步另一处：
  `components/ble_gatt/ble_gatt.h` ↔ 上位机 `src/ble_client.py`。
  ```
  [0xAA][seq u8][type u8][len u8][payload][crc8]     crc8: poly 0x07 / init 0x00
  type: 0x01 波形(int16 小端) / 0x02 会话开始 / 0x03 会话结束 / 0x04 状态心跳
  ```
- 未连接/未订阅时推流自动 no-op，设备本地演示不受影响；连接中断会停止回放并回模式选择页。
- 需 `CONFIG_BT_ENABLED` + `CONFIG_BT_NIMBLE_ENABLED`（见 `sdkconfig.defaults`）。
  开启后二进制约 715 KB（1 MB 分区余约 30%）、DIRAM 约 76%（余 ~80 KB）。
- ⚠️ **未上硬件验证**：SPI/时序类验证同 `ecg_source`；BLE 链路需实物联调（nRF Connect 或上位机）。

### 怎么测（BLE）

1. 烧录：`firmware\idf.bat -p COMx flash monitor`，串口应出现 `BLE ready, name=ECG-Monitor`。
2. 手机装 **nRF Connect** → 扫描到 `ECG-Monitor` → 连接，应看到 4 个特征（UUID 见上）。
3. 设备端进「演示模式 → 选一个样本 → 播放」；在 nRF Connect 里订阅 **ECG Data**
   （`a1b20002-…`），应看到 `AA …` 十六进制帧流：波形帧（type 01）约 50 帧/秒，
   每秒夹一帧状态（type 04）。这就是「数据确实在推」的最快验证。
4. 上位机：`runtime\python\python.exe run.py gui --record 200`，左上「来源」选
   「蓝牙设备」→「连接设备」。连上后设备播放样本，GUI 应实时出波形/心拍/心率，
   状态卡第二行显示「蓝牙已连接 · 丢包 x% · 电量」，底部 `BLE: x%`。
5. 断开验证：把设备关机或拉远，设备端应停止回放并回到模式选择页；GUI 变回未连接。
6. 收尾：会话结束后 `sdcard/ble_*.BIN` 落盘，用「来源=本地数据集」可离线复现该段。

## 主线目标 vs 现状

| 目标 | 状态 |
|------|------|
| 模型 int8 嵌入固件（<50KB） | ✅ 导出 38.79 KB int8（`model_data.h`+`model_config.h` 已复制进 `components/ecg_infer/`） |
| 端侧 R 峰检测 + 逐拍分类 | ✅ 代码完成，含 4 维 RR 特征前向（`ecg_infer(input, rr_feat, logits)`） |
| 板载屏显示波形/心率/分类 | ✅ 代码完成（复用已验证 LCD 驱动） |
| 心率异常报警 | ✅ 代码完成 |
| 蓝牙上传演示样本（BLE Notify） | ✅ 代码完成、编译通过；链路未上硬件验证 |
| 真实采集前端（MAX30003 over SPI） | ✅ 已接入实时页（live 环形引擎 + 512→360 重采样）；未上硬件验证 |

## 与 maincontrol 参考工程的关系

- LCD 驱动 `components/lcd/lcd.c` 直接复用参考工程 `maincontrol-fixture_esp`
  中的 `maincontrol/application/lcd/lcd.c`，
  仅移除 `initcall.h` 依赖与 `SERVICE_INITCALL` 注册，改为由 `main.c` 显式调用
  `lcd_init()`。
- 触摸芯片型号、屏初始化时序（RGB 顺序、小端、RST 受控复位）均已在该工程实测，
  无需重查数据手册。
- ESP-IDF v5.5.1 工具链已就绪，引导脚本 `firmware\idf.bat` / `firmware\idf.ps1`
  会自动定位安装位置（详见 `../docs/README_DEPLOY.md` 第 4 节）。
