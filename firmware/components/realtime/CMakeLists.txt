# realtime — ECG 流式处理引擎（因果 R 峰检测 + 逐拍分类 + 心率报警）
idf_component_register(
    SRCS         "realtime.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES ecg_infer log
)
