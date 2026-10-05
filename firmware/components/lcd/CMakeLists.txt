# lcd — ST7789 显示模块（复用 maincontrol 工程，已移除 initcall 依赖）
#
# font16.h 由 tools/gen_cjk_font.py 生成，必须显式列入 SRCS：
# 否则 ninja 不把它当依赖，重新生成字库后 lcd.c 不会重编，固件里仍是旧点阵，
# 新汉字在屏上会显示为实心方块（已踩过：字库改了、文案也进固件了，唯独点阵没更新）。
idf_component_register(
    SRCS         "lcd.c" "font16.h"
    INCLUDE_DIRS "."
    PRIV_REQUIRES esp_lcd esp_lcd_touch esp_lcd_touch_ft5x06 esp_lcd_touch_cst816s driver
    WHOLE_ARCHIVE
)
