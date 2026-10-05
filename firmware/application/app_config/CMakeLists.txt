# app_config -- application settings (language / brightness) persisted in NVS
idf_component_register(
    SRCS         "app_config.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES i18n lcd nvs_flash
)
