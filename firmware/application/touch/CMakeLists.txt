# touch -- touch sampling and tap detection (single lcd_touch_read caller)
idf_component_register(
    SRCS         "touch.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES lcd
)
