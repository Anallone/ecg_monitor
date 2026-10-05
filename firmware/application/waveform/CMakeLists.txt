# waveform -- ECG waveform frame-buffer renderer
idf_component_register(
    SRCS         "waveform.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES lcd realtime ui_widgets
)
