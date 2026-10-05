# screen_demo -- demo sample list screen (scrolling)
idf_component_register(
    SRCS         "screen_demo.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES touch realtime i18n lcd player sdcard ui_widgets
)
