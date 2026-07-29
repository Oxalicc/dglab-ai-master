"""
session_daemon.py — DGLAB AI Master Session 守护进程。

常驻后台持有设备连接，是唯一接触硬件的进程。与上层（LLM Agent）
通过 state/ 目录下的 JSON-lines 文件协议通信：

  上层 → state/inbox.jsonl   （命令，守护进程处理后立即清空已读行）
  上层 ← state/outbox.jsonl  （事件，只追加）

命令（inbox，每行一个 JSON）：
  {"cmd":"ping"}
  {"cmd":"input","text":"..."}            佩戴者文本输入（先过 classify 路由）
  {"cmd":"device","channel":"A","intensity":40,"waveform":"PULSE",
   "duration_seconds":3}                  设备指令（强制过 clamp_command）
      可选 "duration_rel":0.5            相对时长（0.0–1.0 = 单次上限的几成，
                                          推荐；与 duration_seconds 同时给时优先）
      可选 "delay_seconds":N（≤30）      延迟 N 秒执行（台词/动作时序对齐；
                                          执行时重新校验连接/屏蔽/钳制）
  {"cmd":"device","channel":"A","level":0.5}  相对档位：0.0=最低感知档 …
                                            1.0=红线上限（推荐，个体差异由红线吸收）
                                            ⚠ level 0.0 ≠ 关闭！关机只能显式
                                            intensity=0（level 映射不含 0 档）
      可选 "verify":true                  下发后主动查询设备真实状态并回报
                                          device_state（估值 vs 实际比对）。
                                          关机（intensity=0 / delta 降到 0）与
                                          次安全词软降级默认自动对账，无需显式
  {"cmd":"device","channel":"A","delta":5}  相对增减强度 t:3（红线内联校验）
  {"cmd":"device","channel":"A","level_delta":0.25}  相对增减（档位区间比例）
  {"cmd":"query_device"}                  主动查询设备真实状态（devices.get），
                                          回报 device_state 并按实际值回写估值
  {"cmd":"authorize_start"}               上层已核实开始口令后调用
  {"cmd":"manual_reset"}                  SAFE_LOCK 解锁（锁定期满 + 用户已确认）
  {"cmd":"meta","key":"scenario","value":"interrogation"}
                                          写入场次元数据（play_history 用，纯键值）
  {"cmd":"shutdown"}                      急停 + 清理 + 退出

事件（outbox，每行一个 JSON，含 ts/event 字段）：
  pairing / devices / ready / output_shielded / output_unshielded / started /
  intent_safe_hard / intent_safe_soft / intent_control_reject /
  intent_status / intent_rp / custom_action / device_event /
  device_scheduled / device_applied / device_rejected / device_state /
  waiting_reattach / client_attached(reattach) / client_detached /
  session_timeout / daemon_exit / locked_guidance / lock_notice / lock_status /
  reset_ok / shutdown / error / pong
  （pong/lock_status 负载为 _status() 全量仪表盘：state/current/up_budget/
  双锁定剩余秒/shielded/client_connected/relay/session_remaining_s）

安全不变量（本进程内强制，不依赖上层）：
  - 佩戴者输入一律先过 SafetyLayer.classify；安全词绕过 LLM 直接执行。
  - APP custom.action：0=主安全词 / 5=次安全词（唯一语义，硬编码），
    其余编号按配置 custom_actions 作为剧情互动上报上层。
  - 「屏蔽输出」为 APP 本地功能（用户确认），协议无法解除（实测 t:5 被拒）；
    检测到 isMuted:true 只提示佩戴者在 APP 中「解除屏蔽输出」。
  - 每条 device 命令强制过 clamp_command；SafetyViolation 一律丢弃。
  - 延迟待发指令（delay_seconds）在任何安全事件（急停/次安全词/超时/退出）
    发生时整队作废，不带着旧剧情节奏进入新状态。
  - ACTIVE 期间每秒检查 Session 总时长，到点自动缓释并结束。
  - 生命周期：被控方断连超时（默认 300s）或非 ACTIVE 空闲超时
    （默认 600s，配置 "daemon" 段可调）自动急停退出，不遗留进程。
  - 日志只记安全词时间戳/参数变更/会话起止，不记对话文本。
  - inbox 每轮处理后清空已处理行，对话内容不持久化。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"
INBOX = STATE_DIR / "inbox.jsonl"
OUTBOX = STATE_DIR / "outbox.jsonl"
LOG_FILE = STATE_DIR / "session.log"
PID_FILE = STATE_DIR / "daemon.pid"
CONFIG_PATH = SKILL_DIR / "session_config.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from safety_layer import (  # noqa: E402
    SafetyLayer, SafetyViolation, Command, Intent, State,
)
from dglab_v4_client import DglabV4Client, DglabV4Error  # noqa: E402
import relay_manager  # noqa: E402


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Daemon:
    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        OUTBOX.touch(exist_ok=True)
        INBOX.touch(exist_ok=True)
        self.sl = SafetyLayer.from_config(CONFIG_PATH)
        self.client = None
        self.relay = None
        self.slot_ids: list = []
        self.running = True
        self.shielded = {"A": False, "B": False}  # APP 侧「屏蔽输出」状态跟踪
        # 生命周期：无人看管时自动退出（对话结束/用户离开/APP 断连），
        # 阈值可在配置 "daemon" 段调整
        dg = (self.sl._config.get("daemon") or {})
        self.idle_shutdown_s = float(dg.get("idle_shutdown_s", 600))
        self.detached_shutdown_s = float(dg.get("detached_shutdown_s", 300))
        self.last_activity = time.time()   # 任何命令/设备事件/接入都刷新
        self._detached_since = None        # 被控方断连起点（boot 前不跟踪）
        self._lock_notice_stage = 0        # SAFE_LOCK 播报进度（0 未锁/1 已锁/2 过半已报/3 届满已报）
        self._safeword_events = {"hard": 0, "soft": 0}  # 场次安全词计数（play_history 用）
        self._session_meta = {}            # 上层经 meta 命令写入的场次元数据（场景名等）
        self._end_reason = "unknown"       # daemon 退出原因（play_history 用）
        self._pending = []                 # 延迟待发设备指令 [{at, msg}]（台词/动作时序对齐）

    # ---------- 输出 ----------

    def _status(self) -> dict:
        """设备状态仪表盘：一条 ping 回答"现在设备处于什么状态"。"""
        now = time.time()
        remaining = None
        if self.sl.session_start_ts and self.sl.state == State.ACTIVE:
            total = self.sl.red.session_max_minutes * 60
            remaining = max(0, int(total - (now - self.sl.session_start_ts)))
        return {
            "state": self.sl.state.value,
            "current": dict(self.sl.current),
            "up_budget": {ch: int(self.sl._up_budget[ch]) for ch in ("A", "B")},
            "soft_lock_remaining_s": max(0, int(self.sl.soft_lock_until - now)),
            "hard_lock_remaining_s": max(0, int(self.sl.lock_until - now)),
            "shielded": dict(self.shielded),
            "client_connected": bool(self.client and self.client.client_id),
            "relay": self.relay.url if self.relay else None,
            "session_remaining_s": remaining,
        }

    def emit(self, event: str, **data):
        rec = {"ts": time.time(), "time": now_iso(), "event": event}
        rec.update(data)
        with OUTBOX.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def log(self, kind: str, detail: str):
        """安全日志：只记安全词/参数变更/起止，不记对话。"""
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{now_iso()}\t{kind}\t{detail}\n")

    # ---------- 设备接入 ----------

    def boot(self):
        cfg = self.sl._config
        raw_url = (cfg.get("transport") or {}).get("url", "ws://127.0.0.1:9998")
        # DHCP 漂移自愈：二维码与连接统一用启动时探测到的局域网 IP
        url = relay_manager.effective_url(raw_url)
        self.relay = relay_manager.ensure_relay(url, speak=lambda s: None)
        self.client = DglabV4Client(url)
        self.client.on_event = self.handle_device_event
        controller_id = self.client.connect()
        # 二维码给手机扫：loopback 必须换成局域网 IP（本机连接仍用原 url）
        from dglab_v4_client import pairing_qr_url as _mk_qr
        qr = _mk_qr(relay_manager.lan_url(url), controller_id)
        qr_png = SKILL_DIR / "state" / "pairing_qr.png"
        try:
            import qrcode
            qrcode.make(qr).save(str(qr_png))
            qr_path = str(qr_png)
        except Exception:
            qr_path = None
        self.emit("pairing", qr_content=qr, qr_png=qr_path,
                  relay=self.relay.url, self_hosted=self.relay.self_hosted,
                  controller_id=controller_id)

        wearer_id = self.client.wait_client(timeout=600.0)
        self.emit("client_attached", wearer_id=wearer_id)

        devices = self.client.get_devices()
        self.slot_ids = [d.get("slotId") for d in devices if d.get("slotId")]
        if not self.slot_ids:
            self.emit("error", where="boot",
                      message="未发现郊狼设备。请确认设备已开机、蓝牙已连接、电量充足。")
            raise RuntimeError("no devices")
        self.emit("devices", devices=devices, slot_ids=self.slot_ids,
                  state=self.sl.state.value)
        self.log("session", f"设备接入 slots={self.slot_ids}")
        # 初始状态对齐设备快照：估值以 props.intensityA/B 为准（上次 Session
        # 残留/APP 侧调整都可能导致设备非 0），屏蔽状态从 slotState 读取
        for dev in devices:
            props = dev.get("props") or {}
            for key, ch in (("intensityA", "A"), ("intensityB", "B")):
                if isinstance(props.get(key), (int, float)):
                    self.sl.current[ch] = int(props[key])
            self._update_shield(dev.get("slotState") or {})
        self.emit("ready", state=self.sl.state.value)

    # ---------- 设备动作执行 ----------

    def _update_shield(self, slot_state: dict):
        """跟踪 APP 侧「屏蔽输出」状态（isMuted）。屏蔽是 APP 本地功能，
        协议无法解除（实测 t:5 被拒），只能提示佩戴者在 APP 中解除。"""
        for key, ch in (("channelA", "A"), ("channelB", "B")):
            info = slot_state.get(key) or {}
            if not isinstance(info.get("isMuted"), bool):
                continue
            was = self.shielded.get(ch)
            self.shielded[ch] = info["isMuted"]
            if info["isMuted"] and was is not True:
                self.emit("output_shielded", channel=ch,
                          hint="设备通道被屏蔽输出，请在 APP 中「解除屏蔽输出」")
                self.log("session", f"通道 {ch} 被屏蔽输出")
            elif not info["isMuted"] and was is True:
                self.emit("output_unshielded", channel=ch)
                self.log("session", f"通道 {ch} 解除屏蔽")

    def _ensure_client(self) -> bool:
        """设备指令前置：被控方掉线后（息屏/切后台）等待 APP 重新接入。
        client_attached 帧可能已堆在缓冲区，wait_client 会立即取到。"""
        if self.client and self.client.client_id:
            return True
        self.emit("waiting_reattach",
                  hint="APP 已断开。请保持屏幕常亮，重新扫码接入。")
        try:
            wearer_id = self.client.wait_client(timeout=180.0)
        except Exception as e:  # noqa: BLE001
            self.emit("error", where="wait_reattach", message=str(e))
            return False
        self.emit("client_attached", wearer_id=wearer_id, reattach=True)
        self.log("session", "被控方重新接入")
        self.last_activity = time.time()
        # 重接后屏蔽状态以 APP 快照为准，由后续 slots 事件刷新
        return True

    def _apply_strength(self, channel: str, value: int, base: int = None):
        """绝对目标强度 → 换算为相对增量下发（t:3）。
        实测本 APP 版本拒绝 t:7 绝对赋值（invalid_operate），
        官方 SDK 惯用路径也是 reset/add/reduce。

        base = 换算基准估值。clamp_command()/on_safe_soft() 会先行把
        sl.current 改成目标值，若直接以 sl.current 为基准，delta 恒为 0、
        指令被静默吞掉（真机事故：全程 0 档空放波形）。调用方必须传入
        动作前的估值作基准。"""
        if base is None:
            base = self.sl.current.get(channel, 0)
        delta = value - base
        if delta == 0:
            return
        for sid in self.slot_ids:
            self.client.add_intensity(sid, channel, delta)
        self.sl.current[channel] = value
        self.log("param", f"set_strength {channel}={value} (delta {delta:+d})")

    def _apply_waveform(self, channel: str, name: str, duration_s: float = 0.0):
        # 未指定时长 = 持续到单次输出上限（无 d 的波形实测只播一遍 ~1.2s，
        # 剧情里"持续输出"的意图会被静默吞掉）
        if duration_s <= 0:
            duration_s = float(self.sl.red.max_output_seconds)
        ms = int(duration_s * 1000)
        for sid in self.slot_ids:
            try:
                self.client.send_waveform(sid, channel, name, duration_ms=ms)
            except DglabV4Error as e:
                # 长任务的 APP 确认会延迟到任务结束才返回（实测 d=60s 时 8s 内无响应），
                # 指令本身已送达设备；rpc 按 reqId 匹配，迟到的响应会被安全丢弃
                if "超时" in str(e):
                    self.log("param",
                             f"waveform {channel}={name} dur={duration_s}s ack 延迟确认（忽略）")
                else:
                    raise
        self.log("param", f"waveform {channel}={name} dur={duration_s}s")

    def _estop(self, reason: str):
        # 清理任务并归零（屏蔽输出是 APP 本地功能，协议侧无法联动，只做归零）
        self._clear_pending(reason)
        errors = []
        try:
            errors += self.client.emergency_stop()
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
        self.sl.current = {"A": 0, "B": 0}
        self.log("estop", f"{reason} errors={errors or '无'}")
        return errors

    # ---------- 命令处理 ----------

    MAX_DELAY_SECONDS = 30.0   # 延迟上限：过远的"未来指令"是失控隐患
    MAX_PENDING = 8            # 待发队列上限，防上层失控堆积

    def _dispatch_device(self, msg: dict):
        """时序对齐入口：带 delay_seconds 的指令进入待发队列，到点在主循环
        执行；执行时仍走完整 handle_device（连接/屏蔽/钳制全部届时重校验）。
        典型用法：先发延迟指令再输出台词，让电流卡在台词说到一半时命中。"""
        try:
            delay = float(msg.get("delay_seconds") or 0.0)
        except (TypeError, ValueError):
            delay = 0.0
        if delay <= 0:
            self.handle_device(msg)
            return
        delay = min(delay, self.MAX_DELAY_SECONDS)
        if len(self._pending) >= self.MAX_PENDING:
            self.emit("device_rejected", reason="待发队列已满，延迟指令过多")
            return
        msg = dict(msg)
        msg.pop("delay_seconds", None)
        self._pending.append({"at": time.time() + delay, "msg": msg})
        self.emit("device_scheduled", execute_in=delay,
                  channel=msg.get("channel", "A"))

    def fire_pending(self):
        """主循环每次迭代调用：执行所有到点的待发指令。"""
        now = time.time()
        due = [p for p in self._pending if p["at"] <= now]
        if not due:
            return
        self._pending = [p for p in self._pending if p["at"] > now]
        for p in due:
            try:
                self.handle_device(p["msg"])
            except Exception as e:  # noqa: BLE001
                self.emit("error", where="pending", message=str(e))

    def _clear_pending(self, reason: str):
        """安全事件（急停/次安全词/超时/退出）清空待发队列：
        队列里的指令是基于旧剧情节奏排的，安全状态变了就必须作废。"""
        if self._pending:
            n = len(self._pending)
            self._pending = []
            self.log("session", f"清空待发指令 x{n}：{reason}")

    def handle_input(self, text: str):
        intent = self.sl.classify(text)
        if intent is Intent.SAFE_HARD:
            self._safe_hard(source="voice")
            return
        # SAFE_LOCK 期间：除主安全词（幂等重确认）外不路由降级/驳回/剧情。
        # 设备没有物理复位按键——锁定期满后由 daemon 主动询问，用户说出
        # 解锁口令（wearer.unlock_phrase，默认"确认解锁"）才解除锁定；
        # 其他任何输入统一回复中性指引，每句都应得到明确可操作的回应
        if self.sl.state == State.SAFE_LOCK:
            phrase = (self.sl._config.get("wearer") or {}).get(
                "unlock_phrase", "确认解锁")
            expired = time.time() >= self.sl.lock_until
            if expired and phrase in text:
                self._do_reset(source="voice_unlock")
            elif expired:
                self.emit("locked_guidance",
                          announce=f"锁定期已满。如需解锁，请说'{phrase}'；"
                                   "想继续保持锁定，无需任何操作。",
                          state=self.sl.state.value)
            else:
                self.emit("locked_guidance",
                          announce="已完全停止，设备处于安全锁定。"
                                   "锁定期满后会询问你是否解锁。",
                          state=self.sl.state.value)
            return
        if intent is Intent.SAFE_SOFT:
            self._safe_soft(source="voice")
            return
        if intent is Intent.CONTROL_WORD:
            self.emit("intent_control_reject",
                      reject_semantic="驳回。你没有权限下达指令。")
            return
        if intent is Intent.STATUS_QUERY:
            remaining = None
            if self.sl.session_start_ts:
                total = self.sl.red.session_max_minutes * 60
                remaining = max(0, int(total - (time.time() - self.sl.session_start_ts)))
            self.emit("intent_status", current=dict(self.sl.current),
                      remaining_seconds=remaining, state=self.sl.state.value)
            return
        self.emit("intent_rp", state=self.sl.state.value)

    # ---------- 安全词执行（语音 / APP custom.action 共用） ----------

    def _safe_hard(self, source: str):
        errors = self._estop(f"safe_hard:{source}")
        self.sl.on_safe_hard()
        self._safeword_events["hard"] += 1
        self._lock_notice_stage = 1  # 开启锁定播报（过半/届满各一次）
        self.log("safeword", f"HARD 触发 source={source}")
        self.emit("intent_safe_hard", announce="安全词已接收，已完全停止",
                  source=source,
                  lock_seconds=self.sl.red.hard_lock_seconds,
                  estop_errors=errors or None,
                  state=self.sl.state.value)

    def _safe_soft(self, source: str):
        self._clear_pending("safe_soft")  # 软锁期间禁止上调，待发指令全部作废
        prev = dict(self.sl.current)  # on_safe_soft 会先行更新估值，先留基准
        actions = self.sl.on_safe_soft()
        for a in actions:
            if a["action"] == "set_strength":
                self._apply_strength(a["channel"], a["value"],
                                     base=prev.get(a["channel"], 0))
            elif a["action"] == "set_waveform":
                self._apply_waveform(a["channel"], a["value"])
        self.log("safeword", f"SOFT 触发 source={source}")
        self._safeword_events["soft"] += 1
        self.emit("intent_safe_soft", announce="已降至安全强度",
                  source=source,
                  current=dict(self.sl.current),
                  soft_lock_seconds=self.sl.red.soft_lock_seconds,
                  state=self.sl.state.value)
        if any(a["action"] in ("set_strength", "set_waveform") for a in actions):
            # 软降级默认对账：漂移（自动增加/随机挑逗/硬件手动操作）
            # 会让"已降至安全强度"成为谎报，必须确认设备真的降下去了
            self.handle_query_device(verify_of="safe_soft")

    # ---------- APP 主动上报事件（custom.action 等） ----------

    # 固定映射：0=主安全词（硬停止）5=次安全词（软降低），唯一语义不可改作他用；
    # 其余编号语义由配置 custom_actions 决定（剧情互动）。
    ACTION_SAFE_HARD = 0
    ACTION_SAFE_SOFT = 5

    def handle_device_event(self, payload: dict):
        if not isinstance(payload, dict):
            return
        self.last_activity = time.time()  # APP 侧任何动静都算活动
        ev = (payload.get("ev") or "").strip()
        if ev == "custom.action":
            try:
                action = int(payload.get("action"))
            except (TypeError, ValueError):
                return
            if action == self.ACTION_SAFE_HARD:
                self._safe_hard(source="custom_action")
                return
            if action == self.ACTION_SAFE_SOFT:
                self._safe_soft(source="custom_action")
                return
            mapping = (self.sl._config.get("custom_actions") or {})
            letter = chr(ord("A") + action) if 0 <= action <= 9 else "?"
            entry = mapping.get(str(action)) or {}
            # 配置 {"type":"lock_query"} 的按键 = 锁定状态查询（不上报剧情）
            if entry.get("type") == "lock_query":
                self.emit("lock_status", letter=letter, **self._status())
                return
            semantic = entry.get("semantic", "未定义")
            self.log("session", f"custom.action {letter}({action}) {semantic}")
            self.emit("custom_action", action=action, letter=letter,
                      semantic=semantic, state=self.sl.state.value)
        elif ev in ("devices.snapshot", "devices.patch", "slots.patch"):
            # 设备快照/增量：携带负载供上层诊断（截断防爆）
            self.emit("device_event", ev=ev,
                      payload=json.dumps(payload, ensure_ascii=False)[:600])
            # APP 上报的真实强度回写估值（设备/APP 侧可能自行衰减或清零）
            for slot in payload.get("slots") or []:
                self._update_shield(slot.get("slotState") or {})
                props = slot.get("props") or {}
                for key, ch in (("intensityA", "A"), ("intensityB", "B")):
                    if isinstance(props.get(key), (int, float)):
                        self.sl.current[ch] = int(props[key])

    def handle_device(self, msg: dict):
        if not self._ensure_client():
            return
        # 相对增减（t:3，官方 SDK 惯用原语）：红线内联校验
        if "delta" in msg or "level_delta" in msg:
            self.handle_delta(msg)
            return
        channel = msg.get("channel", "A")
        if self.shielded.get(channel):
            self.emit("output_shielded", channel=channel,
                      hint="设备通道被屏蔽输出，请在 APP 中「解除屏蔽输出」")
            return
        intensity = msg.get("intensity")
        if intensity is None and msg.get("level") is not None:
            # 相对档位（0.0=最低感知档 … 1.0=红线上限），个体差异由红线吸收
            intensity = self.sl.resolve_level(msg.get("level"))
        # 持续时长：duration_rel（0.0–1.0 = 单次上限的几成，推荐）优先；
        # duration_seconds 绝对秒兼容保留
        if msg.get("duration_rel") is not None:
            duration_s = self.sl.resolve_duration(msg.get("duration_rel"))
        else:
            duration_s = float(msg.get("duration_seconds") or 0.0)
        cmd = Command(
            channel=channel,
            intensity=intensity,
            waveform=msg.get("waveform"),
            duration_seconds=duration_s,
        )
        # 开局/0 档保护：只给波形未给强度且当前为 0 时，波形在 0 档物理无感，
        # 按最小有效输出档自动起步（走 clamp，软锁定期等限制自然生效）
        if cmd.waveform and cmd.intensity is None \
                and self.sl.current.get(channel, 0) == 0:
            cmd.intensity = self.sl.red.min_output_intensity
        prev = self.sl.current.get(channel, 0)  # clamp 会先行更新估值，先留基准
        try:
            clamped = self.sl.clamp_command(cmd)
        except SafetyViolation as e:
            self.emit("device_rejected", reason=str(e))
            return
        if clamped.intensity is not None:
            self._apply_strength(clamped.channel, clamped.intensity, base=prev)
        if clamped.waveform:
            self._apply_waveform(clamped.channel, clamped.waveform,
                                 clamped.duration_seconds)
        self.emit("device_applied", channel=clamped.channel,
                  intensity=clamped.intensity, waveform=clamped.waveform,
                  duration_seconds=clamped.duration_seconds,
                  current=dict(self.sl.current))
        if clamped.intensity == 0 or msg.get("verify"):
            # 命令后确认：关机（intensity=0）默认对账——设备「自动增加」/
            # 随机挑逗/硬件手动操作都可能让通道假关闭（实机捕获过漂移）；
            # 其余命令按 verify:true 触发
            self.handle_query_device(verify_of="device")

    def handle_query_device(self, verify_of: str = "manual"):
        """主动查询设备真实状态（devices.get rpc），回报 device_state：
        估值（sl.current）vs 设备实际值比对，不一致时按实际值回写估值。
        用于命令后确认（verify）与上层对账——估值基于增量累加，
        设备侧衰减/APP 侧操作都会造成漂移。"""
        if not self._ensure_client():
            return
        try:
            devices = self.client.get_devices()
        except Exception as e:  # noqa: BLE001
            self.emit("error", where="query_device", message=str(e))
            return
        actual = {"A": None, "B": None}
        for dev in devices:
            props = dev.get("props") or {}
            for key, ch in (("intensityA", "A"), ("intensityB", "B")):
                if isinstance(props.get(key), (int, float)):
                    actual[ch] = int(props[key])
            self._update_shield(dev.get("slotState") or {})
        estimated = dict(self.sl.current)
        mismatch = {ch: {"estimated": estimated[ch], "actual": actual[ch]}
                    for ch in ("A", "B")
                    if actual[ch] is not None and actual[ch] != estimated[ch]}
        # 按实际值回写估值（与 slots.patch 回写语义一致）
        for ch in ("A", "B"):
            if actual[ch] is not None:
                self.sl.current[ch] = actual[ch]
        self.emit("device_state", verify_of=verify_of,
                  estimated=estimated, actual=actual,
                  mismatch=mismatch or None,
                  shielded=dict(self.shielded),
                  state=self.sl.state.value,
                  hint=("设备实际状态与下发指令不一致——请确认 APP 已关闭"
                        "「自动增加」与「随机挑逗」，且未在硬件上手动操作"
                        if mismatch else None))
        if mismatch:
            self.log("param", f"估值校正 {mismatch}（verify_of={verify_of}）")

    def handle_delta(self, msg: dict):
        """相对增减强度（协议 t:3，官方 SDK 惯用原语）。
        红线：仅 ACTIVE；|delta| ≤ max_step_up；结果估值落在 [0, max_intensity]。"""
        if self.sl.state != State.ACTIVE:
            self.emit("device_rejected",
                      reason=f"状态 {self.sl.state.value} 禁止下发指令")
            return
        channel = msg.get("channel", "A")
        if self.shielded.get(channel):
            self.emit("output_shielded", channel=channel,
                      hint="设备通道被屏蔽输出，请在 APP 中「解除屏蔽输出」")
            return
        try:
            if msg.get("level_delta") is not None:
                # 相对增减（档位区间比例），换算后走同一套红线校验
                delta = self.sl.resolve_level_delta(msg.get("level_delta"))
            else:
                delta = int(msg.get("delta"))
        except (TypeError, ValueError):
            self.emit("device_rejected", reason="delta 非法")
            return
        red = self.sl.red
        cur = self.sl.current.get(channel, 0)
        estimate = cur + delta
        # 负增量防护：估值低于 0 钳到 0（"0 档还在减"无意义且空耗指令）
        if estimate < 0:
            estimate = 0
            delta = -cur
        if delta == 0:
            self.emit("device_applied", channel=channel, delta=0,
                      estimated=cur, note="档位无变化，未下发",
                      current=dict(self.sl.current))
            return
        # 最小有效输出档：非零结果抬到 min_output_intensity（0=关闭除外）
        if 0 < estimate < red.min_output_intensity:
            estimate = red.min_output_intensity
            delta = estimate - cur
        # 起步（从 0 直达最小档）豁免步长限制；其余受 max_step_up 约束
        if not (cur == 0 and estimate <= red.min_output_intensity) \
                and abs(delta) > red.max_step_up:
            self.emit("device_rejected",
                      reason=f"单步 |{delta}| 超过红线 max_step_up={red.max_step_up}")
            return
        if estimate > red.max_intensity:
            self.emit("device_rejected",
                      reason=f"增减后估值 {estimate} 超过上限 {red.max_intensity}")
            return
        for sid in self.slot_ids:
            self.client.add_intensity(sid, channel, delta)
        self.sl.current[channel] = estimate
        self.log("param", f"add_strength {channel}{delta:+d} -> ~{estimate}")
        self.emit("device_applied", channel=channel, delta=delta,
                  estimated=estimate, current=dict(self.sl.current))
        if estimate == 0:
            # delta 路径降到 0 同样默认对账（假关机漂移防护）
            self.handle_query_device(verify_of="delta")

    def handle_authorize_start(self):
        cfg = self.sl._config
        age_ok = bool((cfg.get("wearer") or {}).get("age_verified_at"))
        try:
            self.sl.authorize_start(voice_confirmed=True, age_verified=age_ok)
        except SafetyViolation as e:
            self.emit("error", where="authorize_start", message=str(e))
            return
        self.log("session", "Session 开始")
        self.emit("started", state=self.sl.state.value,
                  session_max_minutes=self.sl.red.session_max_minutes)

    def _do_reset(self, source: str):
        try:
            self.sl.manual_reset()
        except SafetyViolation as e:
            self.emit("error", where="manual_reset", message=str(e),
                      hint="锁定期满后说出'确认解锁'才会解除锁定。")
            return
        self.log("session", f"解锁（{source}），回到 IDLE")
        self.emit("reset_ok", state=self.sl.state.value)

    def handle_manual_reset(self):
        self._do_reset(source="manual_reset")

    # ---------- Session 总时长上限（红线，ACTIVE 期间每秒检查） ----------

    def check_session_timeout(self):
        if self.sl.state != State.ACTIVE:
            return
        now = time.time()
        if self.sl.session_start_ts and \
                now - self.sl.session_start_ts > self.sl.red.session_max_minutes * 60:
            self._estop("session_timeout")
            self.sl.state = State.IDLE
            self.log("session", "session_timeout 自动结束")
            self.emit("session_timeout", state=self.sl.state.value)

    # ---------- SAFE_LOCK 可见性（每秒检查） ----------

    def check_lock_notices(self):
        """锁定期过半与届满各播报一次中性指引。设备没有物理复位按键：
        届满播报即"向用户询问是否解锁"——说'确认解锁'才解除，
        不答则保持锁定。"""
        if self.sl.state != State.SAFE_LOCK:
            self._lock_notice_stage = 0
            return
        remaining = self.sl.lock_until - time.time()
        if self._lock_notice_stage < 2 and \
                remaining <= self.sl.red.hard_lock_seconds / 2:
            self._lock_notice_stage = 2
            self.emit("lock_notice", phase="half",
                      announce="设备保持停止。锁定期满后会询问你是否解锁。",
                      hard_lock_remaining_s=max(0, int(remaining)))
        if remaining <= 0 and self._lock_notice_stage < 3:
            self._lock_notice_stage = 3
            phrase = (self.sl._config.get("wearer") or {}).get(
                "unlock_phrase", "确认解锁")
            self.emit("lock_notice", phase="expired",
                      announce=f"锁定期已满。是否解锁？如需解锁，请说'{phrase}'；"
                               "想继续保持锁定，无需任何操作。")

    # ---------- 生命周期（无人看管自动退出，每秒检查） ----------

    def check_lifecycle(self):
        """退出路径（除 shutdown 命令外）：被控方断连超时 / 非 ACTIVE 空闲超时。
        ACTIVE 期间不因空闲退出——Session 由 session_max_minutes 红线封顶，
        到点回 IDLE 后空闲规则自然接管。"""
        now = time.time()
        if self.client and self.client.client_id:
            self._detached_since = None
        elif self._detached_since is None:
            self._detached_since = now
        if self._detached_since is not None and \
                now - self._detached_since > self.detached_shutdown_s:
            self._auto_exit(f"被控方断连超过 {int(self.detached_shutdown_s)}s")
            return
        if self.sl.state != State.ACTIVE and \
                now - self.last_activity > self.idle_shutdown_s:
            self._auto_exit(f"空闲超过 {int(self.idle_shutdown_s)}s")

    def _auto_exit(self, reason: str):
        self.log("session", f"自动退出：{reason}")
        self._end_reason = f"auto_exit:{reason}"
        self.emit("daemon_exit", reason=reason, state=self.sl.state.value)
        self._estop(f"auto_exit:{reason}")
        self.running = False

    # ---------- 退出归档（F5 审计 / F7 多周目记忆） ----------

    def _archive_outbox(self):
        """退出前把 outbox 技术事件归档到 state/archive/session-<ts>.jsonl。
        隐私：丢弃 intent_rp 事件，并剔除任何 text 字段——归档只含
        设备/错误/生命周期等纯技术事件，不含对话内容。"""
        try:
            lines = OUTBOX.read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            return
        kept = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "intent_rp":
                continue
            rec.pop("text", None)
            kept.append(json.dumps(rec, ensure_ascii=False))
        if not kept:
            return
        archive = STATE_DIR / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        path = archive / f"session-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    def _write_play_history(self):
        """多周目记忆钩子：state/play_history.json 追加本场次纯元数据
        （时间/ACTIVE 时长/安全词类型计数/结束原因 + 上层 meta），
        不含任何对话内容。配置 logging.play_history=false 可关闭。"""
        if (self.sl._config.get("logging") or {}).get("play_history") is False:
            return
        if not self.sl.session_start_ts:
            return  # 未进入过 ACTIVE，无场次可记
        rec = {
            "ts": now_iso(),
            "active_minutes": round(
                (time.time() - self.sl.session_start_ts) / 60, 1),
            "safeword_events": dict(self._safeword_events),
            "end_reason": self._end_reason,
        }
        rec.update(self._session_meta)
        path = STATE_DIR / "play_history.json"
        try:
            history = json.loads(path.read_text(encoding="utf-8")) \
                if path.exists() else []
        except Exception:  # noqa: BLE001
            history = []
        history.append(rec)
        path.write_text(json.dumps(history[-50:], ensure_ascii=False, indent=2),
                        encoding="utf-8")

    # ---------- 主循环 ----------

    def drain_inbox(self) -> list:
        if not INBOX.exists():
            return []
        lines = INBOX.read_text(encoding="utf-8").splitlines()
        msgs = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                msgs.append(json.loads(ln))
            except json.JSONDecodeError:
                self.emit("error", where="inbox", message="无法解析的命令行")
        # 处理后立即清空（对话文本不持久化）
        INBOX.write_text("", encoding="utf-8")
        if msgs:
            self.last_activity = time.time()  # 上层任何命令都算活动
        return msgs

    def run(self):
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        self.log("session", "daemon 启动")
        try:
            self.boot()
        except Exception as e:  # noqa: BLE001
            self.emit("error", where="boot", message=str(e))
            self.log("session", f"boot 失败: {e}")
            self._end_reason = "boot_failure"
            self.cleanup()
            return

        last_tick = 0.0
        drain_fail_since = None   # 控制面（daemon↔Relay socket）连续失败起点
        last_drain_error_emit = 0.0
        while self.running:
            # 先排空设备侧上报（custom.action 安全词/剧情动作等）
            if self.client and self.client.ws:
                try:
                    self.client.drain_events()
                    drain_fail_since = None
                except DglabV4Error as e:
                    if "被控方已断开" in str(e):
                        self.emit("client_detached",
                                  hint="APP 已断开，等待重新接入")
                    else:
                        drain_fail_since = drain_fail_since or time.time()
                        if time.time() - last_drain_error_emit >= 30:
                            last_drain_error_emit = time.time()
                            self.emit("error", where="drain", message=str(e))
                except Exception as e:  # noqa: BLE001
                    drain_fail_since = drain_fail_since or time.time()
                    if time.time() - last_drain_error_emit >= 30:
                        last_drain_error_emit = time.time()
                        self.emit("error", where="drain", message=str(e))
            # 控制面连续失败 ≈ 连接已死：client_id 不会自动清空，
            # 断连计时对这种情况是盲的（真机事故：socket 死亡后僵尸进程
            # 刷了 7.5 小时错误日志）。按被控方断连同等处理。
            if drain_fail_since is not None and \
                    time.time() - drain_fail_since > self.detached_shutdown_s:
                self._auto_exit(
                    f"控制面连接连续失败超过 {int(self.detached_shutdown_s)}s")
                break
            for msg in self.drain_inbox():
                cmd = msg.get("cmd")
                try:
                    if cmd == "ping":
                        self.emit("pong", **self._status())
                    elif cmd == "input":
                        self.handle_input(str(msg.get("text", "")))
                    elif cmd == "device":
                        self._dispatch_device(msg)
                    elif cmd == "meta":
                        # 上层写入场次元数据（场景名等，play_history 用），纯键值
                        self._session_meta[str(msg.get("key"))[:50]] = \
                            str(msg.get("value"))[:100]
                    elif cmd == "authorize_start":
                        self.handle_authorize_start()
                    elif cmd == "manual_reset":
                        self.handle_manual_reset()
                    elif cmd == "query_device":
                        self.handle_query_device()
                    elif cmd == "shutdown":
                        self._estop("shutdown")
                        self._end_reason = "shutdown"
                        self.log("session", "Session 结束（shutdown）")
                        self.emit("shutdown")
                        self.running = False
                    else:
                        self.emit("error", where="cmd", message=f"未知命令: {cmd}")
                except Exception as e:  # noqa: BLE001
                    self.emit("error", where=cmd or "?",
                              message=str(e),
                              trace=traceback.format_exc()[-500:])
            now = time.time()
            if now - last_tick >= 1.0:
                last_tick = now
                try:
                    self.check_session_timeout()
                    self.check_lifecycle()
                    self.check_lock_notices()
                except Exception as e:  # noqa: BLE001
                    self.emit("error", where="session_timeout", message=str(e))
            self.fire_pending()  # 到点的延迟指令（0.2s 循环精度足够台词同步）
            time.sleep(0.2)
        self.cleanup()

    def cleanup(self):
        try:
            if self.client:
                self.client.close()
        except Exception:  # noqa: BLE001
            pass
        if self.relay:
            self.relay.stop()
        # 退出前归档：技术事件审计（F5）+ 场次元数据（F7），均在清空前完成
        self._archive_outbox()
        self._write_play_history()
        # 清理 IPC 文件，对话内容不留存
        for p in (INBOX, OUTBOX):
            try:
                p.write_text("", encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        self.log("session", "daemon 退出")


if __name__ == "__main__":
    Daemon().run()
