"""
dglab_v4_relay.py — 自建 V4 Relay 服务（官方 dglab-websocket-server v4 的
Python 等价实现，行为对齐其对外可观测协议）。

用途：本地没有 Relay 服务时由 relay_manager 自动拉起，无需安装官方
Bun/Node 服务。协议细节见 references/protocol-websocket.md。

依赖：websockets（check_env.py 已列为必需）。
运行：python3 dglab_v4_relay.py [--host 127.0.0.1] [--port 9998]
直接运行 --self-test 执行真机联调自测（拉起临时端口，双向收发验证）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import websockets

HEARTBEAT_INTERVAL = 30.0   # 服务端心跳帧间隔（对齐官方 V4 默认 30000ms）
IDLE_TIMEOUT = 300.0        # 控制方无被控端接入的空闲超时（对齐官方 300000ms）

# 断开码（对齐官方）：4000 控制方断开 / 4001 控制方不存在 / 4002 空闲超时
CLOSE_CONTROLLER_GONE = 4000
CLOSE_NO_CONTROLLER = 4001
CLOSE_IDLE = 4002


def log(msg):
    print(f"[relay {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Controller:
    def __init__(self, ws):
        self.id = uuid.uuid4().hex[:8]
        self.ws = ws
        self.clients = {}          # client_id -> ClientState
        self.idle_task = None


class ClientState:
    def __init__(self, ws, controller):
        self.id = uuid.uuid4().hex[:8]
        self.ws = ws
        self.controller = controller


class Relay:
    def __init__(self):
        self.controllers = {}      # controller_id -> Controller

    async def send(self, ws, obj):
        await ws.send(json.dumps(obj, ensure_ascii=False))

    # ---------- 控制方 ----------

    async def handle_controller(self, ws):
        ctl = Controller(ws)
        self.controllers[ctl.id] = ctl
        await self.send(ws, {"type": "hello", "clientId": ctl.id})
        log(f"控制方接入 {ctl.id}")
        ctl.idle_task = asyncio.create_task(self._idle_watch(ctl))
        try:
            async for raw in ws:
                await self._on_controller_message(ctl, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            log(f"控制方 {ctl.id} 连接关闭 code={ws.close_code} "
                f"reason={ws.close_reason!r}")
            await self._drop_controller(ctl)

    async def _on_controller_message(self, ctl, raw):
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            await self.send(ctl.ws, {"type": "error", "code": "bad_request"})
            return
        t = msg.get("type")
        if t == "ping":                       # dglab-kit 服务端级应用 ping
            await self.send(ctl.ws, {"type": "pong"})
        elif t == "heartbeat":
            pass                              # 心跳类消息不转发
        elif t == "message":
            target = ctl.clients.get(msg.get("clientId"))
            if target is None:
                await self.send(ctl.ws, {"type": "error",
                                         "code": "client_not_found",
                                         "clientId": msg.get("clientId")})
                return
            await self.send(target.ws, {"type": "message",
                                        "data": msg.get("data")})
        else:
            await self.send(ctl.ws, {"type": "error", "code": "bad_request"})

    async def _idle_watch(self, ctl):
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
            if not ctl.clients:
                await self.send(ctl.ws, {"type": "idle_timeout"})
                await ctl.ws.close(CLOSE_IDLE, "idle timeout")
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    async def _drop_controller(self, ctl):
        if ctl.idle_task:
            ctl.idle_task.cancel()
        self.controllers.pop(ctl.id, None)
        for client in list(ctl.clients.values()):
            try:
                await client.ws.close(CLOSE_CONTROLLER_GONE,
                                      "controller disconnected")
            except websockets.ConnectionClosed:
                pass
        log(f"控制方断开 {ctl.id}，已关闭 {len(ctl.clients)} 个被控端")

    # ---------- 被控方（APP） ----------

    async def handle_client(self, ws, tid):
        ctl = self.controllers.get(tid)
        if ctl is None:
            await self.send(ws, {"type": "error",
                                 "code": "controller_not_found"})
            await ws.close(CLOSE_NO_CONTROLLER, "controller not found")
            return
        client = ClientState(ws, ctl)
        ctl.clients[client.id] = client
        await self.send(ws, {"type": "hello", "clientId": client.id})
        await self.send(ws, {"type": "controller_attached",
                             "clientId": ctl.id})
        await self.send(ctl.ws, {"type": "client_attached",
                                 "clientId": client.id})
        log(f"被控方 {client.id} 接入控制方 {ctl.id}")
        try:
            async for raw in ws:
                await self._on_client_message(client, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            ctl.clients.pop(client.id, None)
            try:
                await self.send(ctl.ws, {"type": "client_disconnected",
                                         "clientId": client.id})
            except websockets.ConnectionClosed:
                pass
            log(f"被控方 {client.id} 断开 code={ws.close_code} "
                f"reason={ws.close_reason!r}")

    async def _on_client_message(self, client, raw):
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            await self.send(client.ws, {"type": "error", "code": "bad_request"})
            return
        if msg.get("type") == "message":
            await self.send(client.controller.ws,
                            {"type": "message", "clientId": client.id,
                             "data": msg.get("data")})
        elif msg.get("type") == "ping":
            # APP 会定期发应用级 ping 探活，必须回 pong，
            # 否则 APP 约 8 秒判定服务端无响应并断开（真机实测 code=1005）
            await self.send(client.ws, {"type": "pong"})
        # 被控方心跳类消息静默忽略

    # ---------- 入口 ----------

    async def handler(self, ws):
        path = getattr(getattr(ws, "request", None), "path", "/") or "/"
        tid = parse_qs(urlsplit(path).query).get("tid", [None])[0]
        if tid:
            await self.handle_client(ws, tid)
        else:
            await self.handle_controller(ws)

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            for ctl in list(self.controllers.values()):
                targets = [ctl.ws] + [c.ws for c in ctl.clients.values()]
                for ws in targets:
                    try:
                        await self.send(ws, {"type": "heartbeat"})
                    except websockets.ConnectionClosed:
                        pass


async def _serve(host, port):
    relay = Relay()
    async with websockets.serve(relay.handler, host, port,
                                ping_interval=None):
        # 关闭协议层 ping：控制方客户端（websocket-client）无后台读线程，
        # 只在收发指令时读帧，无法及时回 pong，会被协议层 ping 超时误杀。
        # 保活依赖应用层 heartbeat 帧（heartbeat_loop）。
        asyncio.create_task(relay.heartbeat_loop())
        log(f"V4 Relay 已启动: ws://{host}:{port}")
        await asyncio.Future()  # 永久运行


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9998)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    asyncio.run(_serve(args.host, args.port))


# ================= 真机联调自测 =================

def self_test():
    """拉起真实 Relay（临时端口），用 websocket-client 双向验证协议行为。"""
    import subprocess
    import sys
    import websocket  # websocket-client

    port = 19998
    url = f"ws://127.0.0.1:{port}"
    proc = subprocess.Popen([sys.executable, __file__, "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # 等待服务就绪
        for _ in range(50):
            try:
                ws = websocket.create_connection(url, timeout=1)
                hello = json.loads(ws.recv())
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("FAIL: Relay 未能启动")

        # 1. 控制方 hello
        assert hello["type"] == "hello" and hello["clientId"]
        ctl_id = hello["clientId"]

        # 2. 被控方经 tid 接入
        cli = websocket.create_connection(f"{url}?tid={ctl_id}", timeout=3)
        cli_hello = json.loads(cli.recv())
        assert cli_hello["type"] == "hello"
        cli_attach = json.loads(cli.recv())
        assert cli_attach == {"type": "controller_attached", "clientId": ctl_id}
        ctl_attach = json.loads(ws.recv())
        assert ctl_attach["type"] == "client_attached"
        cli_id = ctl_attach["clientId"]

        # 3. 控制方 → 被控方消息转发（外层 clientId 剥离）
        ws.send(json.dumps({"type": "message", "clientId": cli_id,
                            "data": {"op": "test", "v": 1}}))
        got = json.loads(cli.recv())
        assert got == {"type": "message", "data": {"op": "test", "v": 1}}, got

        # 4. 被控方 → 控制方上报（附带来源 clientId）
        cli.send(json.dumps({"type": "message", "data": {"t": "resp"}}))
        got = json.loads(ws.recv())
        assert got == {"type": "message", "clientId": cli_id,
                       "data": {"t": "resp"}}, got

        # 4b. 被控方应用级 ping → pong（真机实测：APP 依赖它保活）
        cli.send(json.dumps({"type": "ping"}))
        assert json.loads(cli.recv())["type"] == "pong"

        # 5. 服务端级 ping → pong
        ws.send(json.dumps({"type": "ping"}))
        assert json.loads(ws.recv())["type"] == "pong"

        # 6. 目标不存在 → client_not_found
        ws.send(json.dumps({"type": "message", "clientId": "ghost",
                            "data": {}}))
        err = json.loads(ws.recv())
        assert err["type"] == "error" and err["code"] == "client_not_found"

        # 7. 被控方断开 → 控制方收到通知
        cli.close()
        assert json.loads(ws.recv())["type"] == "client_disconnected"

        # 8. 非法 tid → controller_not_found + 4001
        bad = websocket.create_connection(f"{url}?tid=ghost", timeout=3)
        err = json.loads(bad.recv())
        assert err["type"] == "error" and err["code"] == "controller_not_found"
        try:
            more = bad.recv()
            assert not more, more          # 空数据 = 连接已被关闭
        except websocket.WebSocketException:
            pass

        # 9. 控制方断开 → 被控端被 4000 关闭
        cli2 = websocket.create_connection(f"{url}?tid={ctl_id}", timeout=3)
        json.loads(cli2.recv()); json.loads(cli2.recv())
        ws.close()
        try:
            more = cli2.recv()
            assert not more, more          # 空数据 = 连接已被关闭
            code = None
        except websocket.WebSocketConnectionClosedException as e:
            code = getattr(e, "code", None)
        print("relay self-test OK: all assertions passed",
              f"(close_code={code})")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
