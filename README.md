# 轻量级可穿戴 ECG 实时心律失常监测系统

综合设计 · 预选题第 13 题 · 端侧部署主线（微雪 ESP32-S3）

## 目标与最终结果

| 指标 | 目标 | 实际 |
|------|------|------|
| 心跳分类准确率（5 类 AAMI） | ≥ 90% | **94.30%**（跨患者测试集，见下） |
| 模型体积（int8） | < 50 KB | **38.79 KB** ✅（约束余量 22.4%） |
| 参数量 / FLOPs / 时延 | — | 37,445 / 878,597 / 2.43 ms |
| 实时分类 + 心率报警 | ✅ | 已实现 |
| PyQt GUI 演示 | ✅ | 已实现（浅莫兰迪配色，实时滚动波形，与端侧同构的因果流式引擎） |
| ESP32-S3 端侧部署 | ✅ | 固件构建通过并实机运行（438 KB bin） |

> **完整指标、混淆矩阵见 [`models/res_se_cnn_rr4_metrics.json`](models/res_se_cnn_rr4_metrics.json)；部署与实验说明见 [`docs/report/REPORT.md`](docs/report/REPORT.md)。**
>
> 说明：94.30% 由占测试集约 88% 的 N 类主导；S/F/Q 跨患者少数类召回率仍然偏低，
> 是轻量模型（37,445 参数、int8 38.79 KB）在 MIT-BIH 跨患者设置下的已知难点
> （详见报告 §4）。模型选型遵循任务书 <50 KB 硬约束：在该约束内用残差连接 +
> 通道注意力（SE）与 4 维 RR 特征把存储余量用足，故由早期 7.21 KB 的基线升级为
> 38.79 KB 的主线。

## 环境

本项目支持**两种**环境方式，任选其一。下文命令统一写作
`./runtime/python/python.exe`（便携方式）；若你用的是自建 venv，替换为
`./.venv/Scripts/python` 即可。Windows 下建议先双击 `check_env.bat` 自检，
它会自动识别便携运行时与本地 venv，并指出缺失项。

### 快速开始

1. 双击 `check_env.bat`，按提示补全 `runtime\` 或 `.venv`；
2. 双击 `run_gui.bat` 启动 GUI，或双击 `run_demo.bat` 运行命令行演示；
3. 若两处解释器都没有，联网双击 `setup_env_uv.bat` 重建核心环境；
   需要 TFLite/TensorFlow 导出时再加参数 `--export`。

### 方式 A：便携运行时（个人机推荐，免配置、免联网）

`runtime/` 是自包含的 Python 3.11 + 全部依赖（约 3.9 GB），解压即用：

```bash
./runtime/python/python.exe run.py gui --record 200
```

Windows 下可直接双击 `run_gui.bat` / `run_demo.bat`。
`runtime/` 体积过大**未纳入 git**，需从工作机拷贝一次
（见 [`docs/README_DEPLOY.md`](docs/README_DEPLOY.md)）。

### 方式 B：自建 venv（开发机）

```bat
rem 推荐：自动检测 uv / py / python，创建 Python 3.11 venv 并安装核心依赖
setup_env_uv.bat

rem 仅当需要导出 TFLite / 使用 TensorFlow 相关工具时再加 --export
setup_env_uv.bat --export
```

> 已实测：本机使用清华 pip 镜像（`pip config` 已配置）。`uv pip` 在本机偶发卡死，
> 因此安装命令统一走 `python -m pip`。
>
> 依赖已按用途拆分：`requirements.txt` 覆盖训练、推理、ONNX、GUI、BLE；
> `requirements-export.txt` 只包含体积较大的 TFLite/TensorFlow 导出链，
> 可用 `run_pip.bat install -r requirements-export.txt` 单独安装。

## 目录结构

```
├── runtime/          # 便携 Python 运行时（免配置，未入 git，需从工作机拷贝）
├── data/             # MIT-BIH 原始记录（wfdb 下载）
├── processed/        # 预处理后的 numpy 缓存（windows/labels/rrfeat）
├── models/           # 权重、指标 JSON
├── export/           # ONNX / int8 C 导出
├── docs/             # 文档/设计子项目（独立 git 仓库）：文档、报告、PPT、任务书、实物照片等
├── src/              # Python 源码
│   ├── config.py         # 全局配置 + AAMI 映射
│   ├── data_loader.py    # wfdb 读取 + 心拍提取 + RR 特征
│   ├── preprocessing.py  # 滤波 / Pan-Tompkins / 分割 / 归一化
│   ├── models.py         # res_se_cnn_rr4（主线）/ res_se_cnn / ds_cnn / std_cnn / binary_cnn
│   ├── dataset.py        # PyTorch Dataset（增强 / 平衡采样）
│   ├── prepare_data.py   # 下载 + 预处理 + 记录级划分
│   ├── train.py          # 训练 + 评估（支持 acc/f1/balanced 选模与可选 logit 校准）
│   ├── quantize.py       # FLOPs / 时延 / ONNX 导出
│   ├── export_c_model.py # int8 C 数组导出 + 量化精度验证（部署权威路径）
│   ├── export_tflite.py  # TFLite fp32/fp16/int8 导出（onnx2tf）+ 精度校验
│   ├── realtime.py       # 实时推理引擎 + 心率报警
│   ├── gui.py            # PySide6 GUI
│   └── demo.py           # 离线命令行演示
├── tools/            # embed_ecg.py 生成固件示例样本
├── packaging/        # ecg_monitor_entry.py GUI 打包入口 + ecg.ico 应用图标
├── firmware/         # ESP32-S3 端侧固件（ESP-IDF v5.5；main/ + application/ 模块化）
├── run.py            # 一键入口
├── _env.bat          # 公共解释器选择（被下方 .bat 脚本复用）
├── check_env.bat     # 环境自检
├── setup_env_uv.bat  # 联网重建 .venv（可加 --export）
├── run_gui.bat       # 一键启动 GUI（便携运行时）
├── run_demo.bat      # 一键命令行演示
├── run_pip.bat       # 便携环境内执行 pip
├── requirements.txt        # 核心依赖
└── requirements-export.txt # 可选 TFLite/TensorFlow 导出依赖
```

## 使用

```bash
# 1. 下载数据 + 预处理（首次约 8 分钟下载 48 条记录）
./runtime/python/python.exe src/prepare_data.py --download

# 2. 训练主线模型 res_se_cnn_rr4（残差 + SE + 4 维 RR 特征）
./runtime/python/python.exe src/train.py --model res_se_cnn_rr4 --metric acc --epochs 60 --processed-dir processed_svdb

# 3. int8 C 导出 + 验证（部署权威路径）
./runtime/python/python.exe src/export_c_model.py --model res_se_cnn_rr4

# 4. 量化指标汇总（FLOPs / 时延 / ONNX 体积）
./runtime/python/python.exe src/quantize.py --model res_se_cnn_rr4 --measure

# 4b. TFLite 导出（fp32 / fp16 / 动态范围 / 全整型 int8）
./runtime/python/python.exe src/export_tflite.py --model res_se_cnn_rr4 --verify

# 5. 离线演示 / GUI（默认已用主线模型 res_se_cnn_rr4）
./runtime/python/python.exe run.py demo --record 200
./runtime/python/python.exe run.py gui  --record 200

# 5b. 蓝牙设备源：GUI 左上「来源」选「蓝牙设备」→「连接设备」，
#     连上（设备名 ECG-Monitor）后在本机播放演示样本，波形即实时推来；
#     收到的样本默认落盘到 sdcard/ble_*.BIN，可用「本地数据集」离线复现。
#     协议与固件端点见 firmware/README.md「蓝牙上传（BLE）」。

# 6. 生成示例 ECG 样本（供固件）
./runtime/python/python.exe tools/embed_ecg.py --record 200 --start 0 --len 1800

# 7. 校验
./runtime/python/python.exe tools/verify_c_export.py --model res_se_cnn_rr4   # C 导出 vs PyTorch
./runtime/python/python.exe tools/verify_onnx.py --model res_se_cnn_rr4       # ONNX Runtime vs PyTorch
./runtime/python/python.exe tools/check_gui_contrast.py                  # GUI 配色对比度
./runtime/python/python.exe tools/make_figures.py                        # 报告插图（输出到 docs/report/figures）
```

## 仓库拆分

本仓库仅保留代码、数据、模型与打包配置。文档、报告、答辩 PPT 与设计隐私材料已统一
放入 `docs/` 这一独立子项目（独立 git 仓库，不入主仓库；如需可再接入为 git submodule）。

`docs/` 内含 `README_DEPLOY.md`、`report/`、`references/`、`CJMCU-30003 资料/`、
报告模板、设计计划、任务书、答辩 PPT，以及含姓名/学号/实物照片的隐私材料。
因含个人信息，`docs/` 远程仓库请保持私有；本仓库 `.gitignore` 已忽略该目录。

## 打包成 exe（Windows）

把 GUI 打成免 Python 环境的便携 exe（PyInstaller onedir，含 torch CPU / PySide6，
体积较大；打包机需先有 `runtime/` 便携环境）：

```bash
# 双击 build_exe.bat（首次会自动往 runtime 装 PyInstaller）
```

产物：
- `dist\ECGMonitor\ECGMonitor.exe` —— onedir 便携版（`models/`、`data/`、`sdcard/`
  由 `assemble_package.py` 拷贝到 exe 同级，`config.py` 检测 frozen 后从该处解析路径）
- `ECGMonitor.zip` —— 组装好的便携包

换机器分发：解压 zip，双击 `ECGMonitor\ECGMonitor.exe` 即可。

## 数据划分

- AAMI 推荐：DS1（22 条）训练、DS2（22 条）测试；4 条起搏记录并入训练集。
- DS1 内部再按记录粒度切 85% 训练 / 15% 验证，避免同记录泄漏。
- 非心拍标注（节律变化 `+`、信号质量 `~` 等）已在提取时过滤，不再污染 Q 类。

