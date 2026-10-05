# screen_mode -- mode selection screen
idf_component_register(
    SRCS         "screen_mode.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES i18n ui_widgets
)
