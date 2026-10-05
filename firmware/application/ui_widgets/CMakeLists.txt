# ui_widgets -- shared UI primitives (background / title / button / row / panel / marquee)
idf_component_register(
    SRCS         "ui_widgets.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES lcd
)
