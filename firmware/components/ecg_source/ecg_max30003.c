/**
 * ecg_max30003.c — MAX30003 SPI 采集驱动
 *
 * 寄存器位定义与初始化序列依据（2026-09-17 更新）：
 *   - Analog Devices MAX30003 数据手册（`docs/CJMCU-30003 资料/datasheet_max30003.pdf`）
 *   - ProtoCentral 官方 Arduino 示例（`docs/CJMCU-30003 资料/software/`）
 *
 * 状态：编译通过（IDF v5.5.5），但仍**未上硬件验证**——SPI 时序、INT1 中断、
 *       归一化 scale 都需接实物调。
 *
 * 本文件早期版本顶部的 TODO(手册) 已全部闭合：
 *   1. STATUS「ECG FIFO 就绪」= EINT = D[23]（首字节 0x80），见 m3_eint_active()
 *   2. EN_INT「ECG FIFO 事件使能」= EN_EINT = D[23]（0x800000），见 CFG_EN_INT
 *   3. ECG FIFO 数据格式：18-bit 电压**左对齐**于 DO[23:6]，ETAG[2:0]=DO[5:3]，
 *      PTAG[2:0]=DO[2:0]，见 m3_read_sample()（旧实现把 24-bit 字当整体符号扩展是错的）
 *
 * 接线（与本项目 LCD/SD/触摸不冲突，见 docs/references/MAX30003_硬件选型与集成.md §7）：
 *      SCK=IO12  MOSI=IO11  MISO=IO13  CS=IO10  INT1=IO9
 *   ⚠️ 不要接 IO5：那是本板 LCD 复位脚。
 */
#include "ecg_max30003.h"

#include <string.h>

#include "driver/gpio.h"
#include "driver/gptimer.h"
#include "driver/spi_master.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char* TAG = "M3";

/* ───────────── 引脚（按需修改） ───────────── */
#define M3_PIN_SCLK GPIO_NUM_12
#define M3_PIN_MOSI GPIO_NUM_11
#define M3_PIN_MISO GPIO_NUM_13
#define M3_PIN_CS   GPIO_NUM_10
#define M3_PIN_INT1 GPIO_NUM_9
/* 某些 CJMCU-30003 兼容模块板上没有可工作的 32.768kHz 时钟（或晶振虚焊），
 * 需要由 ESP32 提供一个 FCLK。IO6 未被本板 LCD/SD/触摸/PSRAM 占用。 */
#define M3_PIN_FCLK GPIO_NUM_6

/* 独立总线：LCD + SD 占 SPI2_HOST，MAX30003 用 SPI3_HOST，采集与显示互不抢总线。
 * 选 SPI3 还有个附带好处：改造前 LCD/SD 就跑在 SPI3 上，说明该总线在本板
 * 已被实证可用（ESP32-S3 SOC_SPI_PERIPH_NUM=3，SPI2/SPI3 均为通用 SPI）。 */
#define M3_SPI_HOST SPI3_HOST

/* ───────────── 寄存器（数据手册「User Command and Register Map」） ───────────── */
#define REG_STATUS   0x01
#define REG_EN_INT   0x02
#define REG_SW_RST   0x08
#define REG_SYNCH    0x09
#define REG_FIFO_RST 0x0A
#define REG_INFO     0x0F
#define REG_CNFG_GEN 0x10
#define REG_CNFG_CAL 0x12
#define REG_CNFG_EMUX 0x14
#define REG_CNFG_ECG 0x15
#define REG_CNFG_RTOR1 0x1D
#define REG_ECG_FIFO 0x21

#define SPI_WREG 0x00
#define SPI_RREG 0x01

/* ───────────── 配置值 ───────────── */

/* CNFG_GEN / CNFG_CAL / CNFG_EMUX / CNFG_RTOR1 与官方 begin() 一致 */
#define CFG_GEN    0x081007u
#define CFG_CAL    0x720000u
/* CNFG_EMUX(0x14)：OPENP/OPENN=0（ECGP/ECGN 接入 AFE），CALP_SEL/CALN_SEL=00
 * （不接校准源）。官方 Arduino 例程的 0x0B0000 实为 CALP=VCALP/CALN=VCALN，
 * 那是内部校准/自检波形，不是电极输入，照抄会导致采不到人体信号。 */
#define CFG_EMUX   0x000000u
#define CFG_RTOR1  0x3FC600u

/* CNFG_ECG(0x15)：D[23:22]=RATE[1:0]，D[17:16]=GAIN[1:0]，D[14]=DHPF，D[13:12]=DLPF[1:0]。
 * CNFG_GEN 默认 FMSTR=00（fMSTR=32768Hz）时：
 *     RATE=00 → 512 SPS，01 → 256，10 → 128。
 * 本项目按「512 SPS 采集 → 驱动层重采样到 360」方案（见组件 README §5），故取 RATE=00。
 * GAIN=00(20V/V)、DHPF=1(0.5Hz 高通)、DLPF=01(40Hz 低通) 与官方示例一致。
 *
 * ⚠️ 官方 Arduino 库 v2.0.0 的默认值 0x805000 实为 RATE=10 → **128 SPS**，
 *    与本项目 512 SPS 方案不符，这里按数据手册修正为 0x005000。 */
#define CFG_ECG 0x005000u

/* EN_INT(0x02)：D[23]=EN_EINT（ECG FIFO 中断使能），D[1:0]=INTB_TYPE[1:0]。
 * 0x800003 = EN_EINT=1 + INTB_TYPE=11（保留上电默认：开漏 + 内部 125kΩ 上拉）。
 * INTB 所有模式均为**低有效**：EINT 置位时 INTB 拉低，读空 FIFO 后恢复高。 */
#define CFG_EN_INT 0x800003u

/* ETAG[2:0] = DO[5:3]（数据手册 Table 33「ECG FIFO Data Tags」） */
#define ETAG_VALID    0x0u   /* 有效样本：电压 + 时间步均有效 */
#define ETAG_FAST     0x1u   /* FAST 恢复期：电压无效、时间步有效 */
#define ETAG_EOF      0x2u   /* 最后一份有效样本（EOF） */
#define ETAG_FAST_EOF 0x3u   /* 最后一份 FAST 样本（EOF） */
#define ETAG_EMPTY    0x6u   /* 读到空 FIFO 的无效样本 */
#define ETAG_OVERFLOW 0x7u   /* FIFO 溢出：数据已损坏 */

static spi_device_handle_t s_dev;
static TaskHandle_t        s_task;
static gptimer_handle_t    s_fclk_timer;
static bool                s_int1_added = false;   /* INT1 中断是否已挂上（未挂时不得摘除，否则报 ISR 服务未安装） */
static bool                s_fclk_level;
static volatile bool       s_stop = false;         /* 采集任务退出标志（stop 后重入 start 时清零） */

/* 输出 ~31.25kHz 方波到模块 FCLK（16us 半周期，GPTimer 中断翻转）。
 * 不采用 LEDC：ESP32-S3 的 LEDC 低速定时器共享同一全局时钟源，与 LCD 背光
 * 的 APB 时钟源冲突（32768Hz 会要求 XTAL 源）。GPTimer 独立，避开该限制。 */
static bool IRAM_ATTR m3_fclk_alarm_cb(gptimer_handle_t timer,
                                       const gptimer_alarm_event_data_t* edata,
                                       void* user_ctx) {
    (void)timer; (void)edata; (void)user_ctx;
    s_fclk_level = !s_fclk_level;
    gpio_set_level(M3_PIN_FCLK, s_fclk_level ? 1 : 0);
    return false;
}

static void m3_fclk_init(void) {
    gpio_config_t io = {0};
    io.pin_bit_mask = 1ULL << M3_PIN_FCLK;
    io.mode = GPIO_MODE_OUTPUT;
    gpio_config(&io);
    gpio_set_level(M3_PIN_FCLK, 0);

    gptimer_config_t cfg = {
        .clk_src = GPTIMER_CLK_SRC_DEFAULT,
        .direction = GPTIMER_COUNT_UP,
        .resolution_hz = 1 * 1000 * 1000,   /* 1MHz -> 1us 计数分辨率 */
    };
    gptimer_new_timer(&cfg, &s_fclk_timer);

    gptimer_alarm_config_t alarm = {
        .alarm_count = 16,                   /* 每 16us 翻转一次 -> ~31.25kHz 方波 */
        .reload_count = 0,
        .flags.auto_reload_on_alarm = true,
    };
    gptimer_set_alarm_action(s_fclk_timer, &alarm);

    gptimer_event_callbacks_t cbs = { .on_alarm = m3_fclk_alarm_cb };
    gptimer_register_event_callbacks(s_fclk_timer, &cbs, NULL);
    gptimer_enable(s_fclk_timer);
    gptimer_start(s_fclk_timer);
    ESP_LOGI(TAG, "FCLK out on IO%d @~31.25kHz", M3_PIN_FCLK);
}

/* ------------------------------------------------------------------------- */
/* 低层 SPI                                                                   */
/* ------------------------------------------------------------------------- */

/* 低层 SPI 读/写：总线错误重试（上电/导联切换瞬间偶发传输失败）。
 * 失败时读函数把 out3 清零，调用方按「全 0」处理——probe 判不在线、EINT 判
 * 无数据、样本解码判 FIFO 空，不会误用上一次的残留值。 */
#define M3_SPI_RETRY 3

static esp_err_t m3_write_reg(uint8_t reg, uint32_t val) {
    uint8_t tx[4] = {
        (uint8_t)((reg << 1) | SPI_WREG),
        (uint8_t)((val >> 16) & 0xFF),
        (uint8_t)((val >> 8) & 0xFF),
        (uint8_t)(val & 0xFF),
    };
    spi_transaction_t t = {0};
    t.length = 8 * sizeof(tx);
    t.tx_buffer = tx;
    esp_err_t err = ESP_FAIL;
    for (int i = 0; i < M3_SPI_RETRY; i++) {
        err = spi_device_transmit(s_dev, &t);
        if (err == ESP_OK) return ESP_OK;
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    ESP_LOGW(TAG, "SPI 写寄存器 0x%02X 失败: %s", reg, esp_err_to_name(err));
    return err;
}

static esp_err_t m3_read_reg(uint8_t reg, uint8_t* out3) {
    uint8_t tx[4] = { (uint8_t)((reg << 1) | SPI_RREG), 0, 0, 0 };
    uint8_t rx[4] = {0};
    spi_transaction_t t = {0};
    t.length = 8 * sizeof(tx);
    t.tx_buffer = tx;
    t.rx_buffer = rx;
    esp_err_t err = ESP_FAIL;
    for (int i = 0; i < M3_SPI_RETRY; i++) {
        memset(rx, 0, sizeof(rx));   /* 每次重试前清空，失败时不残留半帧 */
        err = spi_device_transmit(s_dev, &t);
        if (err == ESP_OK) {
            memcpy(out3, &rx[1], 3);   /* 第 1 字节是命令回显，数据在后 3 字节（MSB 在前） */
            return ESP_OK;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    ESP_LOGW(TAG, "SPI 读寄存器 0x%02X 失败: %s", reg, esp_err_to_name(err));
    memset(out3, 0, 3);
    return err;
}

/** @brief 读 INFO 寄存器校验芯片在线：(buf[0] & 0xF0) == 0x50 */
static bool m3_probe(void) {
    uint8_t b[3] = {0};
    if (m3_read_reg(REG_INFO, b) != ESP_OK) return false;
    return ((b[0] & 0xF0) == 0x50);
}

/* ------------------------------------------------------------------------- */
/* FIFO 就绪判定                                                              */
/* ------------------------------------------------------------------------- */

/**
 * @brief STATUS(0x01) 的 EINT=D[23]：ECG FIFO 中未读样本数达到 EFIT 阈值
 *
 * 数据手册：EINT 表示「达到/超过 ECG FIFO Interrupt Threshold (EFIT) 的样本
 * 可供读回」，读空到低于阈值后自动清除。默认 EFIT=16（MNGR_INT D[23:19]=01111）。
 * 字节序：m3_read_reg() 返回的 out3[0] 是 D[23:16]，故 EINT = 0x80。
 */
static bool m3_eint_active(void) {
    uint8_t st[3] = {0};
    if (m3_read_reg(REG_STATUS, st) != ESP_OK) return false;   /* 读失败按「无数据」处理 */
    return (st[0] & 0x80) != 0;
}

/* ------------------------------------------------------------------------- */
/* 样本解码                                                                    */
/* ------------------------------------------------------------------------- */

/**
 * @brief 读一个 ECG FIFO 字并解码
 * @param out 18-bit 电压（符号扩展到 int32）
 * @return ETAG[2:0]
 *
 * 数据手册「ECG FIFO Data Structure」：24-bit 字 = ECG Sample Voltage Data[17:0]
 * （左对齐于 DO[23:6]，two's complement）| ETAG[2:0]=DO[5:3] | PTAG[2:0]=DO[2:0]。
 * 因此有效数据是**高 18 位**，低 6 位是标签——旧实现「整 24-bit 符号扩展」是错的。
 */
static int m3_read_sample(int32_t* out) {
    uint8_t b[3] = {0};
    if (m3_read_reg(REG_ECG_FIFO, b) != ESP_OK) {
        *out = 0;
        return ETAG_EMPTY;   /* 传输失败按「FIFO 空」处理，由上层下一轮重试 */
    }
    uint32_t w = ((uint32_t)b[0] << 16) | ((uint32_t)b[1] << 8) | b[2];

    uint32_t v18 = (w >> 6) & 0x3FFFFu;   /* 18-bit 左对齐 → 低 18 位 */
    int32_t v = (int32_t)v18;
    if (v & 0x20000) v -= 0x40000;        /* 18-bit 符号扩展 */

    *out = v;
    return (int)((w >> 3) & 0x07u);       /* ETAG[2:0] = DO[5:3] */
}

/** @brief 排空 FIFO 并投喂；FIFO 深度 32。 */
static void m3_drain_fifo(ecg_max30003_src_t* ctx) {
    if (!m3_eint_active()) return;   /* 无数据（< EFIT 份未读），避免盲读空 FIFO */

    for (int i = 0; i < 32; i++) {
        int32_t raw = 0;
        int etag = m3_read_sample(&raw);

        if (etag == ETAG_EMPTY) return;              /* 读空了 */
        if (etag == ETAG_OVERFLOW) {                 /* 溢出：数据损坏，复位恢复 */
            ESP_LOGW(TAG, "ECG FIFO 溢出，执行 FIFO_RST + SYNCH");
            m3_write_reg(REG_FIFO_RST, 0x000000);
            vTaskDelay(pdMS_TO_TICKS(5));
            m3_write_reg(REG_SYNCH, 0x000000);
            return;
        }
        if (etag == ETAG_VALID || etag == ETAG_EOF) {
            ecg_src_max30003_feed(ctx, raw);
        }
        /* ETAG_FAST / ETAG_FAST_EOF：电压无效但时间步有效；bring-up 阶段暂丢弃，
         * 代价是丢一个时间步。上硬件后可改成补插值，保证连续。 */
        if (etag == ETAG_EOF || etag == ETAG_FAST_EOF) return;   /* EOF：本次读完 */
    }
}

/* ------------------------------------------------------------------------- */
/* 中断（INTB 低有效 → 下降沿唤醒采集任务）                                    */
/* ------------------------------------------------------------------------- */

static void IRAM_ATTR m3_int1_isr(void* arg) {
    (void)arg;
    if (s_task) {
        BaseType_t hpw = pdFALSE;
        vTaskNotifyGiveFromISR(s_task, &hpw);
        portYIELD_FROM_ISR(hpw);
    }
}

/* ------------------------------------------------------------------------- */
/* 初始化与配置                                                                */
/* ------------------------------------------------------------------------- */

static esp_err_t m3_bus_init(void) {
    spi_bus_config_t bus = {0};
    bus.mosi_io_num = M3_PIN_MOSI;
    bus.miso_io_num = M3_PIN_MISO;
    bus.sclk_io_num = M3_PIN_SCLK;
    bus.quadwp_io_num = -1;
    bus.quadhd_io_num = -1;
    bus.max_transfer_sz = 16;

    esp_err_t err = spi_bus_initialize(M3_SPI_HOST, &bus, SPI_DMA_DISABLED);
    if (err != ESP_OK) return err;

    spi_device_interface_config_t dev = {0};
    dev.clock_speed_hz = 2 * 1000 * 1000;   /* 官方库用 2MHz */
    dev.mode = 0;                            /* SPI Mode 0 */
    dev.spics_io_num = M3_PIN_CS;
    dev.queue_size = 4;
    err = spi_bus_add_device(M3_SPI_HOST, &dev, &s_dev);
    if (err != ESP_OK) spi_bus_free(M3_SPI_HOST);   /* 设备挂载失败：回收已初始化的总线 */
    return err;
}

/** @brief 装好 INT1 的 GPIO 下降沿中断（须在使能 EINT 之前调用） */
static esp_err_t m3_int1_init(void) {
    gpio_config_t io = {0};
    io.pin_bit_mask = 1ULL << M3_PIN_INT1;
    io.mode = GPIO_MODE_INPUT;
    io.intr_type = GPIO_INTR_NEGEDGE;    /* INTB 低有效：EINT 置位时拉低 */
    io.pull_up_en = GPIO_PULLUP_ENABLE;  /* 兼容开漏 INTB_TYPE=11 */
    esp_err_t err = gpio_config(&io);
    if (err != ESP_OK) return err;

    /* 全局 ISR 服务只需装一次；若其它组件已装，忽略 INVALID_STATE。 */
    err = gpio_install_isr_service(0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) return err;

    err = gpio_isr_handler_add(M3_PIN_INT1, m3_int1_isr, NULL);
    if (err == ESP_OK) s_int1_added = true;
    return err;
}

static void m3_configure(void) {
    m3_write_reg(REG_SW_RST, 0x000000);
    vTaskDelay(pdMS_TO_TICKS(100));

    m3_write_reg(REG_CNFG_GEN,  CFG_GEN);
    vTaskDelay(pdMS_TO_TICKS(50));
    m3_write_reg(REG_CNFG_CAL,  CFG_CAL);
    vTaskDelay(pdMS_TO_TICKS(50));
    m3_write_reg(REG_CNFG_EMUX, CFG_EMUX);
    vTaskDelay(pdMS_TO_TICKS(50));
    m3_write_reg(REG_CNFG_ECG,  CFG_ECG);
    vTaskDelay(pdMS_TO_TICKS(50));
    m3_write_reg(REG_CNFG_RTOR1, CFG_RTOR1);
    vTaskDelay(pdMS_TO_TICKS(50));

    /* 使能 ECG FIFO 中断（EINT → INTB）；INT1 中断已在 m3_int1_init() 装好。 */
    m3_write_reg(REG_EN_INT, CFG_EN_INT);

    m3_write_reg(REG_SYNCH, 0x000000);
    vTaskDelay(pdMS_TO_TICKS(50));
}

/* ------------------------------------------------------------------------- */
/* 采集任务                                                                    */
/* ------------------------------------------------------------------------- */

static void m3_task(void* arg) {
    ecg_max30003_src_t* ctx = (ecg_max30003_src_t*)arg;
    ESP_LOGI(TAG, "采集任务启动，原生 %d Hz（INT1 中断驱动）", ctx->fs_native);

    /* 复位/配置期间可能已有积压样本，先排空一次再进入轮询等待。 */
    m3_drain_fifo(ctx);

    while (!s_stop) {
        /* 20ms 轮询 STATUS 排空 FIFO。相比单纯等 INT1 更稳：即使 INTB 边沿
         * 因重新上电/导联状态变化而丢失，也能在下一个轮询周期把数据取走。 */
        vTaskDelay(pdMS_TO_TICKS(20));
        m3_drain_fifo(ctx);
    }

    /* 主动退出：先清句柄再自删除。m3_release() 看到 s_task==NULL 后才释放 SPI 总线，
     * 避免「总线已初始化 / 设备仍在传输」的竞态，保证停止后能安全重入。 */
    s_task = NULL;
    vTaskDelete(NULL);
}

/* ------------------------------------------------------------------------- */
/* 对外入口                                                                    */
/* ------------------------------------------------------------------------- */

static void m3_release(void);   /* 回收 start() 可能已获取的资源，供失败路径与 stop() 共用 */

/**
 * @brief 初始化 MAX30003 硬件与采集任务
 * @param ctx 已由 ecg_src_max30003_init() 初始化的状态
 * @return ESP_OK 成功
 *
 * @note 需要先调用 ecg_src_max30003_init()。
 *       上电自检失败（芯片无应答）时返回 ESP_ERR_NOT_FOUND。
 *       任一环节失败都会回收已获取的资源，不留下泄漏的总线/定时器/中断。
 */
esp_err_t ecg_max30003_start(ecg_max30003_src_t* ctx) {
    if (ctx == NULL) return ESP_ERR_INVALID_ARG;
    s_stop = false;   /* 每次启动都清退出标志，支持停止后重入 */

    esp_err_t err = m3_bus_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPI 总线初始化失败: %s", esp_err_to_name(err));
        return err;
    }

    if (!m3_probe()) {
        ESP_LOGE(TAG, "芯片无应答：检查供电、CS/SCLK/MOSI/MISO 接线（注意勿接 IO5=LCD RST）");
        m3_release();
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "MAX30003 在线");

    m3_fclk_init();   /* 在配置 AFE 前提供采样主时钟 */

    err = m3_int1_init();   /* 先装好 INT1 中断，再使能 EINT（见 m3_configure） */
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "INT1 中断初始化失败: %s", esp_err_to_name(err));
        m3_release();
        return err;
    }

    m3_configure();

    if (xTaskCreate(m3_task, "max30003", 4096, ctx, 5, &s_task) != pdPASS) {
        ESP_LOGE(TAG, "采集任务创建失败");
        m3_release();
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

/**
 * @brief 停止采集并释放资源
 * @note ISR 服务是全局的，这里只摘掉本引脚的中断，不卸载服务（其它组件可能复用）。
 */
void ecg_max30003_stop(void) {
    m3_release();
}

/* 各步骤都做了 NULL/状态守卫，因此在 start() 任一步骤后调用都安全：
 * 未创建的任务、未初始化的定时器/设备都跳过，未初始化的总线/中断只返回错误码。 */
static void m3_release(void) {
    if (s_task) {
        s_stop = true;
        /* 等采集任务自行退出（任务每 20ms 一个循环，这里最多等 ~200ms），
         * 任务退出时会把 s_task 置 NULL；随后再释放总线，避免释放时仍有传输。 */
        for (int i = 0; i < 40 && s_task != NULL; i++) {
            vTaskDelay(pdMS_TO_TICKS(5));
        }
        if (s_task) {                 /* 兜底：任务卡住则强制删除 */
            vTaskDelete(s_task);
            s_task = NULL;
        }
    }
    /* 停 FCLK：GPTimer 不随任务/SPI 总线自动释放，不删会以 ~62.5kHz 的中断率
     * 空转下去（并持续翻转 IO6）。顺序按 IDF 惯例：stop → disable → del_timer。 */
    if (s_fclk_timer) {
        gptimer_stop(s_fclk_timer);
        gptimer_disable(s_fclk_timer);
        gptimer_del_timer(s_fclk_timer);
        s_fclk_timer = NULL;
        gpio_reset_pin(M3_PIN_FCLK);
    }
    if (s_int1_added) {
        gpio_isr_handler_remove(M3_PIN_INT1);   /* 仅在实际装过时摘除 */
        s_int1_added = false;
    }
    gpio_reset_pin(M3_PIN_INT1);
    if (s_dev) {
        spi_bus_remove_device(s_dev);
        s_dev = NULL;
    }
    spi_bus_free(M3_SPI_HOST);
}
