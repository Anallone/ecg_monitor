"""BLE 接收线程：连接端侧设备（GATT 外设），解析推来的 ECG 波形流。

协议与固件 `firmware/components/ble_gatt/ble_gatt.h` 一一对应：
    帧：[0xAA][seq u8][type u8][len u8][payload][crc8]
         crc8：poly 0x07、init 0x00，覆盖 [seq,type,len,payload]
    type：0x01 波形（int16 小端）/ 0x02 会话开始 / 0x03 会话结束 / 0x04 状态心跳
波形样本缩放与 SD 卡 ECG1 一致：float = int16 / 2000。

线程模型：本模块在**工作线程**里跑一个独立 asyncio 事件循环（bleak 是 async 的），
解析出数据后通过 Qt Signal 交给主线程；GUI 不直接碰 asyncio。
"""
from __future__ import annotations

import asyncio
import threading

import numpy as np
from PySide6.QtCore import QThread, Signal

# ── UUID / 名称（与固件保持一致，改一处必须同步另一处）──
DEVICE_NAME = "ECG-Monitor"
SERVICE_UUID = "a1b20001-1234-5678-9abc-def012345678"
CHR_ECG_UUID = "a1b20002-1234-5678-9abc-def012345678"
CHR_STATUS_UUID = "a1b20003-1234-5678-9abc-def012345678"
CHR_CTRL_UUID = "a1b20004-1234-5678-9abc-def012345678"
CHR_INFO_UUID = "a1b20005-1234-5678-9abc-def012345678"

# ── 帧协议 ──
FRAME_MAGIC = 0xAA
TYPE_WAVE = 0x01
TYPE_SESSION_START = 0x02
TYPE_SESSION_END = 0x03
TYPE_STATUS = 0x04

MODE_DEMO = 0x01
MODE_LIVE = 0x02

SAMPLE_SCALE = 2000.0
FRAME_OVERHEAD = 5          # 0xAA + seq + type + len + crc8


def crc8(data: bytes) -> int:
    """CRC-8（poly 0x07、init 0x00），与固件 ble_gatt.c 的 crc8() 等价。"""
    crc = 0x00
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else ((crc << 1) & 0xFF)
    return crc


def parse_frame(buf: bytes):
    """解析一帧。返回 (seq, ftype, payload)；不完整或校验失败返回 None。"""
    if len(buf) < FRAME_OVERHEAD or buf[0] != FRAME_MAGIC:
        return None
    seq, ftype, plen = buf[1], buf[2], buf[3]
    total = FRAME_OVERHEAD + plen
    if len(buf) < total:
        return None
    if crc8(buf[1:4 + plen]) != buf[4 + plen]:
        return None
    return seq, ftype, buf[4:4 + plen]


class LossTracker:
    """按波形序号缺口统计丢包率（序号 u8 回绕）。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.last: int | None = None
        self.received = 0
        self.lost = 0

    def push(self, seq: int) -> None:
        if self.last is not None:
            gap = (seq - self.last - 1) & 0xFF
            self.lost += gap
        self.received += 1
        self.last = seq

    @property
    def rate(self) -> float:
        total = self.received + self.lost
        return (self.lost / total) if total else 0.0


class BleClientWorker(QThread):
    """后台连接 + 接收。信号都在工作线程发射，Qt 会自动排队到主线程。"""

    connected = Signal(str)              # 设备名
    disconnected = Signal()
    session_started = Signal(int, str)   # mode, name
    session_ended = Signal()
    samples = Signal(object)             # np.ndarray[float32]（已归一化）
    status = Signal(int, int, float)     # battery, sent_total, loss_rate
    failed = Signal(str)                 # 连接/运行期错误

    def __init__(self, device_name: str = DEVICE_NAME, parent=None) -> None:
        super().__init__(parent)
        self.device_name = device_name
        self._stop = threading.Event()
        self.loss = LossTracker()

    def stop(self) -> None:
        """请求停止（run() 里的循环会退出并断开连接）。"""
        self._stop.set()

    # ---- 工作线程 ----
    def run(self) -> None:  # noqa: D102
        try:
            import bleak  # noqa: F401  延迟导入：未装 bleak 时给出友好提示
        except ImportError:
            self.failed.emit("未安装 bleak，请先 pip install bleak")
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _main(self) -> None:
        from bleak import BleakClient, BleakScanner

        device = await BleakScanner.find_device_by_name(self.device_name, timeout=8.0)
        if device is None:
            self.failed.emit(f"未找到设备 {self.device_name}（确认设备已上电、未被他机占用）")
            return

        self.loss.reset()

        # 连接。Windows 会把设备的配对/缓存状态记下来，设备端重启后残留状态常让
        # 首次连接失败（表现就是「要先在系统里删掉设备才能连」）；第一次失败后先
        # 解除配对（等价于系统设置里的「删除设备」）再重连一次，把这一步自动化。
        client = None
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                client = BleakClient(device)
                await client.connect()
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                try:
                    if client is not None:
                        await client.unpair()
                except Exception:  # noqa: BLE001
                    pass
                client = None
                if attempt == 0:
                    await asyncio.sleep(1.0)

        if client is None or not client.is_connected:
            self.failed.emit(f"连接失败：{last_exc}")
            return

        # 校验两个通知特征都在（顺带确认服务发现已就绪），缺失时给明确提示。
        try:
            svcs = client.services
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"服务发现失败：{exc}")
            await client.disconnect()
            return
        if (svcs.get_characteristic(CHR_ECG_UUID) is None
                or svcs.get_characteristic(CHR_STATUS_UUID) is None):
            self.failed.emit("设备缺少 ECG/状态通知特征，请重新烧录固件")
            await client.disconnect()
            return

        try:
            await client.start_notify(CHR_ECG_UUID, self._on_ecg)
            await client.start_notify(CHR_STATUS_UUID, self._on_status)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"订阅通知失败：{exc}")
            await client.disconnect()
            return

        self.connected.emit(getattr(device, "name", None) or self.device_name)

        # 等待停止或对端断开
        while not self._stop.is_set() and client.is_connected:
            await asyncio.sleep(0.2)

        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.disconnected.emit()

    # ---- 通知回调（在事件循环里同步调用）----
    def _on_ecg(self, _sender, data: bytearray) -> None:
        self._dispatch(bytes(data))

    def _on_status(self, _sender, data: bytearray) -> None:
        self._dispatch(bytes(data))

    def _dispatch(self, raw: bytes) -> None:
        """统一的帧分发：固件把「波形/会话开始/会话结束」都发在 ECG 特征上，
        仅「状态心跳」走 Status 特征；这里按 type 分派，不依赖来自哪个特征。"""
        frame = parse_frame(raw)
        if frame is None:
            return
        seq, ftype, payload = frame

        if ftype == TYPE_WAVE:
            if len(payload) < 2:
                return
            self.loss.push(seq)
            arr = np.frombuffer(payload[:len(payload) // 2 * 2], dtype="<i2").astype(np.float32)
            self.samples.emit(arr / SAMPLE_SCALE)

        elif ftype == TYPE_SESSION_START:
            mode = payload[0] if payload else MODE_DEMO
            name = payload[1:].decode("utf-8", errors="replace") if len(payload) > 1 else ""
            self.session_started.emit(mode, name)

        elif ftype == TYPE_SESSION_END:
            self.session_ended.emit()

        elif ftype == TYPE_STATUS and len(payload) >= 6:
            battery = payload[1]
            sent = int.from_bytes(payload[2:6], "little")
            self.status.emit(battery, sent, self.loss.rate)


def build_wave_payload(samples) -> bytes:
    """把 float 样本编成波形帧载荷（int16 小端）——供离线测试/回放用。"""
    arr = np.asarray(samples, dtype=np.float32) * SAMPLE_SCALE
    arr = np.clip(np.rint(arr), -32768, 32767).astype("<i2")
    return arr.tobytes()


def build_frame(seq: int, ftype: int, payload: bytes = b"") -> bytes:
    """组一帧（供离线测试用；固件端对应 ble_gatt_notify）。"""
    body = bytes([seq & 0xFF, ftype & 0xFF, len(payload) & 0xFF]) + payload
    return bytes([FRAME_MAGIC]) + body + bytes([crc8(body)])
