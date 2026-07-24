"""
dglab_v4_client.py — 郊狼 3.0（DG-LAB 4 APP）V4 协议客户端。

依赖：websocket-client。缺依赖时先运行 check_env.py 验证并发起安装。
协议参考：references/protocol-websocket.md（源自官方 dglab-websocket-server
与 dglab-kit，GPL-3.0；波形数据为郊狼设备内置波形）。

与安全的衔接：本模块只负责"把合法指令送达设备"。每一条指令必须由
safety_layer.SafetyLayer.clamp_command() 钳制后才能调用本模块；
急停直接调 emergency_stop()，等价于 SafetyLayer.on_safe_hard()
动作清单中的设备类动作（announce/lock 动作由上层处理）。

直接运行本文件执行离线自测：python3 dglab_v4_client.py
（自测不触碰网络；真实联机需要 V4 Relay 地址 + APP 扫码配对）
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional
from urllib.parse import quote

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

# ---------------- 协议常量 ----------------

CHANNEL_ID = {"A": 0, "B": 1}

ACTION_APPEND_PULSE = 0   # 裸波形数据任务
ACTION_ADD_INTENSITY = 3  # 相对增减强度
ACTION_SET_TEMP_INTENSITY = 4  # 临时强度（需 d 持续 ms）
ACTION_SET_MUTE = 5       # 通道静音开关（v:true/false，p:1；Socket 模式默认静音，须控制方解除）
ACTION_SET_INTENSITY = 7  # 绝对强度

DEFAULT_RESPONSE_TIMEOUT = 8.0   # 与 dglab-kit 一致
SERVER_PING_INTERVAL = 2.0       # 控制方 -> 服务端应用级 ping 间隔（纯保活，不做断连判定）

QR_PREFIX = "https://dungeon-lab.cn/s/?v=1&action=socket&url="

# ---------------- 郊狼内置波形库 ----------------
# 帧格式：16 位十六进制字符串 = 8 字节 = [频率x4][强度x4]，每帧 100ms。
# 数据来自 dglab-kit src/waveform/coyote.ts（GPL-3.0），为郊狼设备内置波形。
WAVEFORMS = {
    "EXTRUSTION": {"cn": "挤压", "raw": [
        "0A0A0A0A00000000", "0A0A0A0A64646464"]},
    "BUBBLE": {"cn": "气泡", "raw": [
        "2D2D2D2D00000000", "2D2D2D2D64646464"]},
    "RHYTHM": {"cn": "律动", "raw": [
        "0A0A0A0A00000000", "0A0A0A0A32323232", "0A0A0A0A64646464",
        "0A0A0A0A00000000", "0A0A0A0A32323232", "0A0A0A0A64646464",
        "1919191964646464", "1D1D1D1D64646464", "2222222264646464",
        "2626262664646464", "2B2B2B2B64646464", "0A0A0A0A00000000",
        "0A0A0A0A00000000"]},
    "AIR_WAVES": {"cn": "电波", "raw": [
        "0A0A0A0A64646464", "1717171764646464", "2424242464646464",
        "3232323264646464", "0A0A0A0A00000000", "0A0A0A0A64646464",
        "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
        "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A64646464",
        "0A0A0A0A00000000"]},
    "DANCE": {"cn": "舞步", "raw": [
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A64646464",
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A64646464",
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A64646464",
        "0A0A0A0A64646464", "0A0A0A0A64646464", "0A0A0A0A00000000",
        "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A64646464",
        "0A0A0A0A64646464"]},
    "CLIMB": {"cn": "攀登", "raw": [
        "3030303032323232", "282828283C3C3C3C", "2020202046464646",
        "1919191950505050", "111111115A5A5A5A", "0A0A0A0A64646464"]},
    "SHADE": {"cn": "树荫", "raw": [
        "6464646464646464", "6464646464646464"]},
    "PULSE": {"cn": "脉冲", "raw": [
        "0A0A0A0A64646464", "0D0D0D0D64646464", "1010101064646464",
        "1313131364646464", "1616161664646464", "1C1C1C1C64646464",
        "2525252564646464", "2E2E2E2E64646464", "3737373764646464",
        "4040404064646464", "4E4E4E4E64646464", "6C6C6C6C64646464",
        "7979797964646464", "8686868664646464", "9393939364646464",
        "A0A0A0A064646464"]},
    "BREATHING": {"cn": "呼吸", "raw": [
        "0A0A0A0A00000000", "0A0A0A0A14141414", "0A0A0A0A28282828",
        "0A0A0A0A3C3C3C3C", "0A0A0A0A50505050", "0A0A0A0A64646464",
        "0A0A0A0A64646464", "0A0A0A0A64646464", "0A0A0A0A00000000",
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A00000000"]},
    "TIDE": {"cn": "潮汐", "raw": [
        "0A0A0A0A00000000", "0B0B0B0B10101010", "0D0D0D0D21212121",
        "0E0E0E0E32323232", "1010101042424242", "1212121253535353",
        "1313131364646464", "151515155C5C5C5C", "1616161654545454",
        "181818184C4C4C4C", "1A1A1A1A44444444", "1A1A1A1A00000000",
        "1B1B1B1B10101010", "1D1D1D1D21212121", "1E1E1E1E32323232",
        "2020202042424242", "2222222253535353", "2323232364646464",
        "252525255C5C5C5C", "2626262654545454", "282828284C4C4C4C",
        "2A2A2A2A44444444", "0A0A0A0A00000000"]},
    "PULSATING": {"cn": "连击", "raw": [
        "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A64646464",
        "0A0A0A0A42424242", "0A0A0A0A21212121", "0A0A0A0A00000000",
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A64646464",
        "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A42424242",
        "0A0A0A0A21212121", "0A0A0A0A00000000", "0A0A0A0A00000000",
        "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
        "0A0A0A0A64646464", "0A0A0A0A42424242", "0A0A0A0A21212121",
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A00000000"]},
    "QUICK_RUB": {"cn": "快速按捏", "raw": ["0A0A0A0A" + ("00000000" if i % 2 == 0 else "64646464") for i in range(48)]},
    "GRADUALRUB": {"cn": "按捏渐强", "raw": [
        "0A0A0A0A00000000", "0A0A0A0A1C1C1C1C", "0A0A0A0A00000000",
        "0A0A0A0A34343434", "0A0A0A0A00000000", "0A0A0A0A49494949",
        "0A0A0A0A00000000", "0A0A0A0A57575757", "0A0A0A0A00000000",
        "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A00000000",
        "0A0A0A0A1C1C1C1C", "0A0A0A0A00000000", "0A0A0A0A34343434",
        "0A0A0A0A00000000", "0A0A0A0A49494949", "0A0A0A0A00000000",
        "0A0A0A0A57575757", "0A0A0A0A00000000", "0A0A0A0A64646464",
        "0A0A0A0A00000000"]},
    "HEARTBEAT": {"cn": "心跳节奏", "raw": [
        "7070707064646464", "7070707064646464", "7070707064646464",
        "7070707064646464", "7070707064646464", "7070707064646464",
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A00000000",
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A4B4B4B4B",
        "0A0A0A0A53535353", "0A0A0A0A5B5B5B5B", "0A0A0A0A64646464",
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A00000000",
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A00000000",
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A00000000",
        "0A0A0A0A00000000", "0A0A0A0A4B4B4B4B", "0A0A0A0A53535353",
        "0A0A0A0A5B5B5B5B", "0A0A0A0A64646464", "0A0A0A0A00000000",
        "0A0A0A0A00000000", "0A0A0A0A00000000", "0A0A0A0A00000000",
        "0A0A0A0A00000000"]},
    "COMPRESS": {"cn": "压缩", "raw": [
        "4A4A4A4A64646464", "4545454564646464", "4040404064646464",
        "3B3B3B3B64646464", "3636363664646464", "3232323264646464",
        "2D2D2D2D64646464", "2828282864646464", "2323232364646464",
        "1E1E1E1E64646464", "1A1A1A1A64646464"] + ["0A0A0A0A64646464"] * 10},
    "RHYTHMIC": {"cn": "节奏步伐", "raw": [
        "0A0A0A0A00000000", "0A0A0A0A14141414", "0A0A0A0A28282828",
        "0A0A0A0A3C3C3C3C", "0A0A0A0A50505050", "0A0A0A0A64646464",
        "0A0A0A0A00000000", "0A0A0A0A19191919", "0A0A0A0A32323232",
        "0A0A0A0A4B4B4B4B", "0A0A0A0A64646464", "0A0A0A0A00000000",
        "0A0A0A0A21212121", "0A0A0A0A42424242", "0A0A0A0A64646464",
        "0A0A0A0A00000000", "0A0A0A0A32323232", "0A0A0A0A64646464",
        "0A0A0A0A00000000", "0A0A0A0A64646464", "0A0A0A0A00000000",
        "0A0A0A0A64646464", "0A0A0A0A00000000", "0A0A0A0A64646464",
        "0A0A0A0A00000000"]},
    "GRAINY": {"cn": "颗粒摩擦", "raw": [
        "0A0A0A0A64646464", "0B0B0B0B64646464", "0D0D0D0D64646464",
        "0F0F0F0F00000000", "0F0F0F0F64646464", "1111111164646464",
        "1313131364646464", "1414141400000000", "1414141464646464",
        "1616161664646464", "1818181864646464", "1A1A1A1A00000000",
        "1A1A1A1A64646464", "1C1C1C1C64646464", "1D1D1D1D64646464",
        "1F1F1F1F00000000", "1F1F1F1F64646464", "2121212164646464",
        "2323232364646464", "2525252500000000", "2525252564646464",
        "2626262664646464", "2828282864646464", "2A2A2A2A00000000",
        "2A2A2A2A64646464", "2C2C2C2C64646464", "2E2E2E2E64646464",
        "3030303000000000"]},
    "BOUNCY": {"cn": "渐变弹跳", "raw": [
        "0A0A0A0A00000000", "0A0A0A0A21212121", "0B0B0B0B42424242",
        "0C0C0C0C64646464", "0C0C0C0C00000000", "0D0D0D0D21212121",
        "0E0E0E0E42424242", "0F0F0F0F64646464", "0F0F0F0F00000000",
        "0F0F0F0F21212121", "1010101042424242", "1111111164646464",
        "1111111100000000", "1212121221212121", "1313131342424242",
        "1414141464646464", "1414141400000000", "1414141421212121",
        "1515151542424242", "1616161664646464", "1616161600000000",
        "1717171721212121", "1818181842424242", "1919191964646464",
        "1919191900000000", "1919191921212121", "1A1A1A1A42424242",
        "1B1B1B1B64646464", "1B1B1B1B00000000", "1C1C1C1C21212121",
        "1D1D1D1D42424242", "1E1E1E1E64646464", "1E1E1E1E00000000",
        "1E1E1E1E21212121", "1F1F1F1F42424242", "2020202064646464",
        "2020202000000000", "2121212121212121", "2222222242424242",
        "2323232364646464", "2323232300000000", "2323232321212121",
        "2424242442424242", "2525252564646464", "2525252500000000",
        "2626262621212121", "2727272742424242", "2828282864646464",
        "0A0A0A0A00000000", "0A0A0A0A00000000"]},
    "RIPPLE": {"cn": "波浪涟漪", "raw": [
        "0A0A0A0A00000000", "0A0A0A0A32323232", "0A0A0A0A64646464",
        "0A0A0A0A49494949", "1111111100000000", "1111111132323232",
        "1111111164646464", "1111111149494949", "1919191900000000",
        "1919191932323232", "1919191964646464", "1919191949494949",
        "2121212100000000", "2121212132323232", "2121212164646464",
        "2121212149494949", "2828282800000000", "2828282832323232",
        "2828282864646464", "2828282849494949", "3030303000000000",
        "3030303032323232", "3030303064646464", "3030303049494949",
        "3838383800000000", "3838383832323232", "3838383864646464",
        "3838383849494949", "3F3F3F3F00000000", "3F3F3F3F32323232",
        "3F3F3F3F64646464", "3F3F3F3F49494949", "4747474700000000",
        "4747474732323232", "4747474764646464", "4747474749494949",
        "4F4F4F4F00000000", "4F4F4F4F32323232", "4F4F4F4F64646464",
        "4F4F4F4F49494949", "5656565600000000", "5656565632323232",
        "5656565664646464", "5656565649494949", "5E5E5E5E00000000",
        "5E5E5E5E32323232", "5E5E5E5E64646464", "5E5E5E5E49494949",
        "6464646400000000", "6464646432323232", "6464646464646464",
        "6464646449494949", "6666666600000000", "6666666632323232",
        "6666666664646464", "6666666649494949", "0A0A0A0A00000000"]},
    "RAINFALL": {"cn": "雨水冲刷", "raw":
        ["0E0E0E0E21212121", "0E0E0E0E42424242", "0E0E0E0E64646464"] * 15
        + ["3A3A3A3A64646464"] * 30
        + ["0A0A0A0A00000000"] * 3},
    "TEMPO_TAP": {"cn": "变速敲击", "raw": [
        "1818181864646464", "1818181864646464", "1818181864646464",
        "1818181800000000", "1818181800000000", "1818181800000000",
        "1818181800000000", "1818181864646464", "1818181864646464",
        "1818181864646464", "1818181800000000", "1818181800000000",
        "1818181800000000", "1818181800000000", "1818181864646464",
        "1818181864646464", "1818181800000000", "1818181800000000",
        "1818181800000000", "1818181800000000", "1818181864646464",
        "1818181864646464", "1818181864646464", "1818181800000000",
        "1818181800000000", "1818181800000000", "1818181864646464",
        "1818181864646464", "1818181864646464", "1818181800000000",
        "1818181800000000", "1818181800000000", "1818181800000000",
        "1818181864646464", "1818181864646464", "1818181864646464",
        "1818181800000000", "1818181800000000", "1818181800000000",
        "1818181800000000"] + ["7070707064646464"] * 39
        + ["0A0A0A0A00000000"] * 2},
    "SIGNAL": {"cn": "信号灯", "raw":
        ["BEBEBEBE64646464"] * 20 + [
        "0A0A0A0A00000000", "1010101021212121", "1717171742424242",
        "1E1E1E1E64646464"] * 5},
    "TEASE_1": {"cn": "挑逗1", "raw": [
        "0A0A0A0A00000000", "0C0C0C0C19191919", "0E0E0E0E32323232",
        "101010104B4B4B4B", "1212121264646464", "1515151564646464",
        "1717171764646464", "1919191900000000", "1B1B1B1B00000000",
        "1E1E1E1E00000000"] * 4
        + ["0A0A0A0A64646464" if i % 2 else "0A0A0A0A00000000" for i in range(22)]},
    "TEASE_2": {"cn": "挑逗2", "raw": [
        "2525252500000000", "222222220B0B0B0B", "2020202016161616",
        "1E1E1E1E21212121", "1C1C1C1C2C2C2C2C", "1919191937373737",
        "1717171742424242", "151515154D4D4D4D", "1313131358585858",
        "1111111164646464"] * 4 + [
        "0A0A0A0A00000000", "0B0B0B0B64646464", "0B0B0B0B00000000",
        "0C0C0C0C64646464", "0C0C0C0C00000000", "0D0D0D0D64646464",
        "0D0D0D0D00000000", "0E0E0E0E64646464", "0E0E0E0E00000000",
        "0F0F0F0F64646464", "0F0F0F0F00000000", "1010101064646464",
        "1010101000000000", "1111111164646464", "1111111100000000",
        "1212121264646464", "1212121200000000", "1313131364646464",
        "1313131300000000", "1414141464646464", "1414141400000000",
        "1515151564646464", "1515151500000000", "1616161664646464",
        "1616161600000000", "1717171764646464", "1717171700000000",
        "1818181864646464", "1818181800000000", "1919191964646464",
        "1919191900000000", "1A1A1A1A64646464", "1A1A1A1A00000000",
        "1B1B1B1B64646464", "1B1B1B1B00000000", "1C1C1C1C64646464",
        "1C1C1C1C00000000", "1D1D1D1D64646464", "1D1D1D1D00000000",
        "1E1E1E1E64646464", "0A0A0A0A00000000", "0A0A0A0A00000000"]},
}

# ---------------- 纯构造函数（离线可测） ----------------

def build_op(slot_id: str, channel, action: int, value,
             priority: Optional[int] = None,
             duration_ms: Optional[int] = None,
             immediate: Optional[bool] = None,
             version: Optional[int] = None) -> dict:
    """构造 device.op 指令数据（对应 dglab-kit V4DeviceOperate）。"""
    op = {"s": slot_id, "c": CHANNEL_ID[channel] if isinstance(channel, str)
          else channel, "t": action, "v": value}
    if priority is not None:
        op["p"] = priority
    if duration_ms is not None:
        op["d"] = duration_ms
    if immediate is not None:
        op["im"] = immediate
    if version is not None:
        op["ver"] = version
    return op


def build_rpc(req_id: str, method: str, data=None) -> dict:
    req = {"t": "req", "reqId": req_id, "m": method}
    if data is not None:
        req["data"] = data
    return req


def pairing_qr_url(ws_url: str, controller_id: str) -> str:
    """生成 DG-LAB 4 APP 扫码配对用的二维码内容（二维码本身由上层渲染）。"""
    sep = "&" if "?" in ws_url else "?"
    return QR_PREFIX + quote(f"{ws_url}{sep}tid={controller_id}", safe="")


def op_timeout(op: dict, default: float = DEFAULT_RESPONSE_TIMEOUT) -> float:
    """长持续任务的响应等待时间放宽到 d+1000ms，但封顶 8s。
    无封顶时 d=120s 的波形会让 rpc 阻塞 121s，冻结 daemon 主循环
    （连 ping 都无响应）；响应实际在波形下发后立即返回，无需等满时长。"""
    MAX_OP_TIMEOUT = 8.0
    d = op.get("d")
    if d is not None and d > default * 1000:
        return min(d / 1000 + 1.0, MAX_OP_TIMEOUT)
    return default


# ---------------- 客户端 ----------------

class DglabV4Error(Exception):
    pass


class DglabV4Client:
    """V4 控制方客户端（同步阻塞式，基于 websocket-client）。

    典型流程：
        client = DglabV4Client("ws://127.0.0.1:9998")
        cid = client.connect()                 # 控制方 ID
        print(client.pairing_qr_url())         # 渲染成二维码给 APP 扫
        client.wait_client()                   # 等待 APP 接入
        devices = client.get_devices()         # 发现设备 slotId
        client.set_intensity(slot, "A", 20)    # 指令须先过 clamp_command()
    """

    def __init__(self, url: str, response_timeout: float = DEFAULT_RESPONSE_TIMEOUT,
                 on_event: Optional[Callable[[dict], None]] = None):
        if websocket is None:
            raise DglabV4Error(
                "缺少依赖 websocket-client，请先运行 scripts/check_env.py "
                "验证环境并发起安装请求")
        self.url = url
        self.response_timeout = response_timeout
        self.on_event = on_event
        self.ws = None
        self.controller_id: Optional[str] = None
        self.client_id: Optional[str] = None
        self.devices: list = []
        self._req_counter = 0
        self._send_lock = threading.Lock()
        self._missed_pongs = 0
        self._closed = threading.Event()
        self._ping_thread: Optional[threading.Thread] = None

    # ---- 连接与配对 ----

    def connect(self, timeout: float = 10.0) -> str:
        self.ws = websocket.create_connection(self.url, timeout=timeout)
        frame = self._recv_json()
        if frame.get("type") != "hello":
            raise DglabV4Error(f"预期 hello 帧，收到: {frame}")
        self.controller_id = frame["clientId"]
        self._closed.clear()
        self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
        self._ping_thread.start()
        return self.controller_id

    def pairing_qr_url(self) -> str:
        if not self.controller_id:
            raise DglabV4Error("尚未 connect")
        return pairing_qr_url(self.url, self.controller_id)

    def wait_client(self, timeout: Optional[float] = 120.0) -> str:
        """阻塞等待 APP（被控方）接入，返回被控方 clientId。"""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            if deadline and time.time() > deadline:
                raise DglabV4Error("等待被控方接入超时")
            frame = self._recv_json()
            t = frame.get("type")
            if t == "client_attached":
                self.client_id = frame["clientId"]
                return self.client_id
            # 其余帧（pong/heartbeat/idle_timeout/error）统一路由：
            # 重置 missed-pong 计数，idle_timeout 与 error 在此抛错。
            self._handle_non_message(frame)

    # ---- RPC ----

    def rpc(self, method: str, data=None,
            timeout: Optional[float] = None):
        """发送 RPC 请求并阻塞等待响应。返回 result，失败抛 DglabV4Error。"""
        if not self.client_id:
            raise DglabV4Error("被控方尚未接入")
        self._req_counter += 1
        req_id = str(self._req_counter)
        self._send({"type": "message", "clientId": self.client_id,
                    "data": build_rpc(req_id, method, data)})
        wait = timeout if timeout is not None else self.response_timeout
        deadline = time.time() + wait
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise DglabV4Error(f"等待响应超时: {method}")
            frame = self._recv_json(timeout=remaining)
            if frame.get("type") != "message":
                self._handle_non_message(frame)
                continue
            payload = frame.get("data")
            if isinstance(payload, dict) and payload.get("t") == "resp" \
                    and (payload.get("reqId") == req_id
                         or payload.get("requestId") == req_id):
                if payload.get("error"):
                    raise DglabV4Error(f"{method} 失败: {payload['error']}")
                return payload.get("result")
            self._dispatch_event(payload)

    # ---- 设备指令（调用前必须过 SafetyLayer.clamp_command()） ----

    def get_devices(self) -> list:
        result = self.rpc("devices.get")
        self.devices = (result or {}).get("devices", [])
        return self.devices

    def _op(self, op: dict):
        return self.rpc("device.op", op, timeout=op_timeout(op))

    def set_intensity(self, slot_id: str, channel, value: int, **kw):
        return self._op(build_op(slot_id, channel, ACTION_SET_INTENSITY,
                                 value, **kw))

    def set_mute(self, slot_id: str, channel, muted: bool):
        """通道静音开关（官方协议 t:5，p:1）。Socket 模式下通道默认静音，
        需控制方显式解除后才能输出；APP 的舒适/绝对上限不受影响。"""
        return self._op({"s": slot_id,
                         "c": CHANNEL_ID[channel] if isinstance(channel, str)
                         else channel,
                         "t": ACTION_SET_MUTE, "p": 1, "v": bool(muted)})

    def add_intensity(self, slot_id: str, channel, delta: int, **kw):
        return self._op(build_op(slot_id, channel, ACTION_ADD_INTENSITY,
                                 delta, **kw))

    def set_temp_intensity(self, slot_id: str, channel, value: int,
                           duration_ms: int, **kw):
        return self._op(build_op(slot_id, channel, ACTION_SET_TEMP_INTENSITY,
                                 value, duration_ms=duration_ms, **kw))

    def append_pulse(self, slot_id: str, channel, frames: list,
                     duration_ms: Optional[int] = None, version: int = 2, **kw):
        return self._op(build_op(slot_id, channel, ACTION_APPEND_PULSE,
                                 frames, duration_ms=duration_ms,
                                 version=version, **kw))

    def send_waveform(self, slot_id: str, channel, name: str,
                      duration_ms: Optional[int] = None, **kw):
        """按内置波形名发送（如 "BREATHING"）。"""
        if name not in WAVEFORMS:
            raise DglabV4Error(f"未知波形: {name}（可选见 WAVEFORMS）")
        return self.append_pulse(slot_id, channel, WAVEFORMS[name]["raw"],
                                 duration_ms=duration_ms, **kw)

    def clear(self, slot_id: Optional[str] = None, channel=None):
        """清理任务：不传 slot_id 清理全部；传 channel 清理指定通道。"""
        data = None
        if slot_id:
            data = {"s": slot_id}
            if channel is not None:
                data["c"] = CHANNEL_ID[channel] if isinstance(channel, str) \
                    else channel
        return self.rpc("device.op.clear", data)

    # ---- 急停 ----

    def emergency_stop(self, slot_ids: Optional[list] = None):
        """急停：清理全部任务 + 所有设备双通道强度归零。
        尽最大努力执行，单个调用失败不中断后续动作。返回执行错误列表。"""
        errors = []
        try:
            self.clear()
        except Exception as e:  # noqa: BLE001 — 急停路径不挑异常
            errors.append(f"clear: {e}")
        slots = slot_ids or [d.get("slotId") for d in self.devices if d.get("slotId")]
        for slot in slots:
            for ch in (0, 1):
                try:
                    self._op({"s": slot, "c": ch, "t": ACTION_SET_INTENSITY,
                              "v": 0, "im": True, "p": 0})
                except Exception as e:  # noqa: BLE001
                    errors.append(f"zero {slot}/{ch}: {e}")
        return errors

    # ---- 内部 ----

    def _send(self, obj: dict):
        with self._send_lock:
            self.ws.send(json.dumps(obj, ensure_ascii=False))

    def _recv_json(self, timeout: Optional[float] = None) -> dict:
        if timeout is not None:
            self.ws.settimeout(max(timeout, 0.1))
        try:
            raw = self.ws.recv()
        except websocket.WebSocketTimeoutException:
            raise DglabV4Error("接收超时")
        except Exception as e:
            raise DglabV4Error(f"连接已断开: {e}")
        finally:
            if timeout is not None:
                self.ws.settimeout(self.response_timeout)
        if not raw:
            raise DglabV4Error("连接已关闭")
        return json.loads(raw)

    def _handle_non_message(self, frame: dict):
        t = frame.get("type")
        if t == "pong":
            self._missed_pongs = 0
        elif t == "heartbeat":
            pass  # 服务端心跳，控制方无需回复
        elif t == "idle_timeout":
            raise DglabV4Error("控制方空闲超时")
        elif t == "error":
            raise DglabV4Error(frame.get("message") or frame.get("code"))
        elif t == "client_disconnected":
            self.client_id = None
            raise DglabV4Error("被控方已断开")

    def _dispatch_event(self, payload):
        if isinstance(payload, dict) and payload.get("t") == "ev" and self.on_event:
            try:
                self.on_event(payload)
            except Exception:
                pass  # 事件回调异常不得影响主链路

    def _ping_loop(self):
        """应用层 ping 保活（对齐 dglab-kit 的 2s 间隔）。

        注意：本客户端没有后台读线程，pong 只在 rpc/wait_client 读帧时
        顺带处理，因此**不能**用"连续无 pong 就断连"的判活逻辑——空闲期
        pong 堆在 socket 缓冲区没人读，会把健康连接误杀。这里只做单向
        保活：发送失败说明连接真的死了，退出线程即可。"""
        while not self._closed.wait(SERVER_PING_INTERVAL):
            try:
                self._send({"type": "ping"})
            except Exception:
                return

    def close(self):
        self._closed.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def drain_events(self, max_frames: int = 20) -> int:
        """非阻塞排空 socket 缓冲区：把堆积的事件帧（t:"ev"）交给 on_event，
        其余帧走 _handle_non_message。供无主读线程的上层在主循环里轮询，
        保证 APP 主动上报（如 custom.action 安全词）能被及时处理。
        返回处理的帧数；连接类错误抛 DglabV4Error。"""
        if not self.ws:
            return 0
        n = 0
        while n < max_frames:
            self.ws.settimeout(0.02)
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            except Exception as e:
                raise DglabV4Error(f"连接已断开: {e}")
            finally:
                self.ws.settimeout(self.response_timeout)
            if not raw:
                raise DglabV4Error("连接已关闭")
            n += 1
            frame = json.loads(raw)
            if frame.get("type") == "message":
                self._dispatch_event(frame.get("data"))
            else:
                self._handle_non_message(frame)   # pong/heartbeat/断开通知
        return n


if __name__ == "__main__":
    # ---- 离线自测（不触碰网络与硬件） ----

    # 波形库完整性：24 个内置波形，每帧 16 位大写十六进制
    assert len(WAVEFORMS) == 24, len(WAVEFORMS)
    for name, wf in WAVEFORMS.items():
        assert wf["cn"] and wf["raw"], name
        for frame in wf["raw"]:
            assert len(frame) == 16 and all(
                ch in "0123456789ABCDEF" for ch in frame), (name, frame)

    # 指令构造
    op = build_op("slot-1", "A", ACTION_SET_INTENSITY, 20)
    assert op == {"s": "slot-1", "c": 0, "t": 7, "v": 20}, op
    op2 = build_op("slot-1", "B", ACTION_APPEND_PULSE,
                   WAVEFORMS["BREATHING"]["raw"], duration_ms=5000, version=2)
    assert op2["c"] == 1 and op2["t"] == 0 and op2["ver"] == 2
    assert op2["d"] == 5000 and len(op2["v"]) == 12
    op3 = build_op("slot-1", 1, ACTION_SET_TEMP_INTENSITY, 60,
                   duration_ms=3000, immediate=True, priority=0)
    assert op3["t"] == 4 and op3["d"] == 3000 and op3["im"] is True

    # RPC 构造
    req = build_rpc("1", "device.op", op)
    assert req == {"t": "req", "reqId": "1", "m": "device.op", "data": op}
    req2 = build_rpc("2", "devices.get")
    assert "data" not in req2

    # 二维码内容
    qr = pairing_qr_url("ws://127.0.0.1:9998", "abc-123")
    assert qr.startswith(QR_PREFIX)
    assert quote("ws://127.0.0.1:9998?tid=abc-123", safe="") in qr
    qr2 = pairing_qr_url("ws://127.0.0.1:9998/v4?x=1", "abc-123")
    assert quote("&tid=", safe="") in qr2  # 已有 query 时用 & 拼接

    # 响应超时策略（封顶 8s，防长波形冻结调用方主循环）
    assert op_timeout({"d": 3000}) == DEFAULT_RESPONSE_TIMEOUT
    assert op_timeout({"d": 7000}) == 8.0
    assert op_timeout({"d": 120000}) == 8.0
    assert op_timeout({}) == DEFAULT_RESPONSE_TIMEOUT

    # 依赖缺失时的指引（当前环境已装，验证导入路径存在）
    if websocket is None:
        print("提示: 缺 websocket-client，运行 check_env.py")
    else:
        c = DglabV4Client.__new__(DglabV4Client)  # 不连网，仅验证类可实例化
        assert callable(DglabV4Client.emergency_stop)

    print("dglab_v4_client self-test OK: all assertions passed")
