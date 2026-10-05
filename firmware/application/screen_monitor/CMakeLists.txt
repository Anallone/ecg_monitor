# screen_monitor -- monitor screen (waveform + readouts)
idf_component_register(
    SRCS         "screen_monitor.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES sdcard realtime i18n lcd player ui_widgets waveform
)
