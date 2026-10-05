# screen_alarm -- alarm screen (latched, tap to acknowledge)
idf_component_register(
    SRCS         "screen_alarm.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES realtime i18n lcd player ui_widgets
)
