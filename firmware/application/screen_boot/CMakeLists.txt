# screen_boot -- boot animation screen
idf_component_register(
    SRCS         "screen_boot.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES realtime lcd i18n player ui_widgets waveform
)
