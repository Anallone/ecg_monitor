# screen_settings -- settings screen (language / brightness)
idf_component_register(
    SRCS         "screen_settings.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES app_config i18n lcd ui_widgets
)
