"""
relay_manager.py — Relay 可用性探测与自建兜底。

策略：优先使用配置中的既有 Relay（官方/公共/用户自建）；探测失败时
自动在本机拉起 Skill 内置的 dglab_v4_relay.py，无需用户手动部署任何
外部服务。

用法：
    from relay_manager import ensure_relay
    handle = ensure_relay("ws://127.0.0.1:9998", speak=print)
    ...                                # Session 期间 handle.process 保持存活
    handle.stop()                      # 结束时终止自建 Relay（如有）

直接运行执行自测：python3 relay_manager.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

STATE_DIR = Path(__file__).resolve().parent.parent / "state"


def detect_lan_ip() -> str:
    """探测本机当前局域网 IP（UDP connect 技巧，不产生真实流量）。
    DHCP 漂移时每次启动拿到最新地址，失败回退 127.0.0.1。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def effective_url(url: str) -> str:
    """计算实际使用的 Relay URL：局域网地址的主机名替换为当前探测 IP
    （DHCP 漂移自愈，无需改配置）；127.0.0.1/localhost 保持不变。"""
    from urllib.parse import urlunsplit
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    if host in ("127.0.0.1", "localhost"):
        return url
    ip = detect_lan_ip()
    if ip == host:
        return url
    netloc = f"{ip}:{parts.port or 9998}"
    return urlunsplit((parts.scheme, netloc, parts.path,
                       parts.query, parts.fragment))


def probe(url: str, timeout: float = 3.0) -> bool:
    """探测 Relay 是否可用：能建连并收到 hello 帧即为可用。"""
    try:
        import websocket  # websocket-client（check_env 必需依赖）
        ws = websocket.create_connection(url, timeout=timeout)
        frame = json.loads(ws.recv())
        ws.close()
        return frame.get("type") == "hello"
    except Exception:
        return False


class RelayHandle:
    def __init__(self, url: str, process: Optional[subprocess.Popen]):
        self.url = url
        self.process = process       # None = 使用既有服务，无需清理

    @property
    def self_hosted(self) -> bool:
        return self.process is not None

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None


def ensure_relay(url: str, speak: Callable = print,
                 python_exe: Optional[str] = None,
                 relay_script: Optional[str] = None,
                 boot_timeout: float = 10.0) -> RelayHandle:
    """确保 Relay 可用。探测失败 → 自建；自建起不来 → 抛 RuntimeError。"""
    if probe(url):
        speak(f"Relay 服务已在运行：{url}")
        return RelayHandle(url, None)

    speak(f"未检测到 Relay 服务（{url}），正在启动自建 Relay……")
    python_exe = python_exe or sys.executable
    relay_script = relay_script or str(
        Path(__file__).parent / "dglab_v4_relay.py")
    parts = urlsplit(url)
    port = parts.port or 9998
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # 自建始终绑 0.0.0.0：二维码/客户端用探测到的局域网 IP 连接，
    # 避免配置写死的 IP 在 DHCP 漂移后 bind 失败（errno 49）
    proc = subprocess.Popen(
        [python_exe, relay_script, "--host", "0.0.0.0", "--port", str(port)],
        stdout=open(STATE_DIR / "relay.log", "a", encoding="utf-8"),
        stderr=subprocess.STDOUT)

    deadline = time.time() + boot_timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if probe(url, timeout=1.0):
            speak(f"自建 Relay 已启动：{url}（PID {proc.pid}，"
                  "Session 结束后自动关闭）")
            return RelayHandle(url, proc)
        time.sleep(0.2)

    proc.terminate()
    raise RuntimeError(
        f"自建 Relay 启动失败（端口 {port}）。引导：请确认端口未被占用、"
        "websockets 依赖已安装（check_env.py），或改用其他端口/地址。")


if __name__ == "__main__":
    # ---- 动态 IP 探测与 URL 重写 ----
    ip = detect_lan_ip()
    assert ip and "." in ip
    assert effective_url("ws://127.0.0.1:9998") == "ws://127.0.0.1:9998"
    rewritten = effective_url("ws://192.168.0.1:9998")
    assert rewritten.startswith(f"ws://{ip}:"), rewritten

    # ---- 自测：空闲端口上 ensure → 自建 → probe 成功 → stop ----
    test_url = "ws://127.0.0.1:19999"
    assert not probe(test_url, timeout=1.0), "测试端口应初始不可用"

    logs = []
    handle = ensure_relay(test_url, speak=logs.append)
    assert handle.self_hosted
    assert probe(test_url), "自建后应可用"
    assert any("自建 Relay 已启动" in s for s in logs)

    # hello 帧验证
    import websocket
    ws = websocket.create_connection(test_url, timeout=3)
    assert json.loads(ws.recv())["type"] == "hello"
    ws.close()

    handle.stop()
    time.sleep(0.3)
    assert not probe(test_url, timeout=1.0), "stop 后应不可用"
    print("relay_manager self-test OK: all assertions passed")
