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
  {"cmd":"device","channel":"A","delta":5}  相对增减强度 t:3（红线内联校验）
  {"cmd":"authorize_start"}               上层已核实开始口令后调用
  {"cmd":"manual_reset"}                  SAFE_LOCK 解锁（上层已核实物理确认）
  {"cmd":"shutdown"}                      急停 + 清理 + 退出

事件（outbox，每行一个 JSON，含 ts/event 字段）：
  pairing / devices / ready / output_shielded / output_unshielded / started /
  intent_safe_hard / intent_safe_soft / intent_control_reject /
  intent_status / intent_rp / custom_action / device_event /
  device_applied / device_rejected /
  waiting_reattach / client_attached(reattach) / client_detached /
  watchdog_checkin / watchdog_degrade / watchdog_stop / session_timeout / locked /
  reset_ok / shutdown / error / pong

安全不变量（本进程内强制，不依赖上层）：
  - 佩戴者输入一律先过 SafetyLayer.classify；安全词绕过 LLM 直接执行。
  - APP custom.action：0=主安全词 / 5=次安全词（唯一语义，硬编码），
    其余编号按配置 custom_actions 作为剧情互动上报上层。
  - 「屏蔽输出」为 APP 本地功能（用户确认），协议无法解除（实测 t:5 被拒）；
    检测到 isMuted:true 只提示佩戴者在 APP 中「解除屏蔽输出」。
  - 每条 device 命令强制过 clamp_command；SafetyViolation 一律丢弃。
  - ACTIVE 期间每秒 watchdog_tick；degrade/stop 自动执行物理动作。
  - 日志只记安全词时间戳/参数变更/Watchdog 事件/会话起止，不记对话文本。
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
    GENTLE_WAVEFORM,
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
        self.client: DglabClientType = None  # type: ignore
        self.relay = None
        self.slot_ids: list = []
        self.running = True
        self._wd_degraded = False  # 每次失联只降级一次
        self._wd_checkin = False   # 每次失联只提醒一次在线确认
        self.shielded = {"A": False, "B": False}  # APP 侧「屏蔽输出」状态跟踪

    # ---------- 输出 ----------

    def emit(self, event: str, **data):
        rec = {"ts": time.time(), "time": now_iso(), "event": event}
        rec.update(data)
        with OUTBOX.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def log(self, kind: str, detail: str):
        """安全日志：只记安全词/参数变更/Watchdog/起止，不记对话。"""
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
        qr = self.client.pairing_qr_url()
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
        errors = []
        try:
            errors += self.client.emergency_stop()
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
        self.sl.current = {"A": 0, "B": 0}
        self.log("estop", f"{reason} errors={errors or '无'}")
        return errors

    # ---------- 命令处理 ----------

    def handle_input(self, text: str):
        intent = self.sl.classify(text)
        if intent is Intent.SAFE_HARD:
            self._safe_hard(source="voice")
            return
        if intent is Intent.SAFE_SOFT:
            self._safe_soft(source="voice")
            return
        # 其余意图都算佩戴者活动，喂给 Watchdog
        self.sl.note_voice_activity()
        self._wd_degraded = False
        self._wd_checkin = False
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
        self.log("safeword", f"HARD 触发 source={source}")
        self.emit("intent_safe_hard", announce="安全词已接收，已完全停止",
                  source=source,
                  lock_seconds=self.sl.red.hard_lock_seconds,
                  estop_errors=errors or None,
                  state=self.sl.state.value)

    def _safe_soft(self, source: str):
        prev = dict(self.sl.current)  # on_safe_soft 会先行更新估值，先留基准
        actions = self.sl.on_safe_soft()
        for a in actions:
            if a["action"] == "set_strength":
                self._apply_strength(a["channel"], a["value"],
                                     base=prev.get(a["channel"], 0))
            elif a["action"] == "set_waveform":
                self._apply_waveform(a["channel"], a["value"])
        self.log("safeword", f"SOFT 触发 source={source}")
        self.emit("intent_safe_soft", announce="已降至安全强度",
                  source=source,
                  current=dict(self.sl.current),
                  soft_lock_seconds=self.sl.red.soft_lock_seconds,
                  state=self.sl.state.value)

    # ---------- APP 主动上报事件（custom.action 等） ----------

    # 固定映射：0=主安全词（硬停止）5=次安全词（软降低），唯一语义不可改作他用；
    # 其余编号语义由配置 custom_actions 决定（剧情互动）。
    ACTION_SAFE_HARD = 0
    ACTION_SAFE_SOFT = 5

    def handle_device_event(self, payload: dict):
        if not isinstance(payload, dict):
            return
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
            semantic = mapping.get(str(action), {}).get("semantic", "未定义")
            letter = chr(ord("A") + action) if 0 <= action <= 9 else "?"
            self.sl.note_voice_activity()
            self._wd_degraded = False
            self._wd_checkin = False
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
        if "delta" in msg:
            self.handle_delta(msg)
            return
        channel = msg.get("channel", "A")
        if self.shielded.get(channel):
            self.emit("output_shielded", channel=channel,
                      hint="设备通道被屏蔽输出，请在 APP 中「解除屏蔽输出」")
            return
        cmd = Command(
            channel=channel,
            intensity=msg.get("intensity"),
            waveform=msg.get("waveform"),
            duration_seconds=float(msg.get("duration_seconds") or 0.0),
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

    def handle_authorize_start(self):
        cfg = self.sl._config
        age_ok = bool((cfg.get("wearer") or {}).get("age_verified_at"))
        try:
            self.sl.authorize_start(voice_confirmed=True, age_verified=age_ok)
        except SafetyViolation as e:
            self.emit("error", where="authorize_start", message=str(e))
            return
        self._wd_degraded = False
        self._wd_checkin = False
        self.log("session", "Session 开始")
        self.emit("started", state=self.sl.state.value,
                  session_max_minutes=self.sl.red.session_max_minutes)

    def handle_manual_reset(self):
        try:
            self.sl.manual_reset(physical_confirm=True)
        except SafetyViolation as e:
            self.emit("error", where="manual_reset", message=str(e))
            return
        self.log("session", "物理复位，回到 IDLE")
        self.emit("reset_ok", state=self.sl.state.value)

    # ---------- Watchdog ----------

    def watchdog(self):
        if self.sl.state != State.ACTIVE:
            return
        now = time.time()
        # 会话总时长到点 → 缓释并结束
        if self.sl.session_start_ts and \
                now - self.sl.session_start_ts > self.sl.red.session_max_minutes * 60:
            self._estop("session_timeout")
            self.sl.state = State.IDLE
            self.log("watchdog", "session_timeout 自动结束")
            self.emit("session_timeout", state=self.sl.state.value)
            return
        action = self.sl.watchdog_tick(now)
        if action == "checkin" and not self._wd_checkin:
            self._wd_checkin = True
            self.log("watchdog", "checkin 沉默在线确认")
            self.emit("watchdog_checkin",
                      hint="佩戴者长时间无回应。请以当前人格口吻主动确认其在线"
                           "（剧情化表达，不点破机制）；仍无回应将自动降级")
        elif action == "degrade" and not self._wd_degraded:
            self._wd_degraded = True
            for ch in ("A", "B"):
                nv = self.sl.current.get(ch, 0) // 2
                if nv != self.sl.current.get(ch, 0):
                    self._apply_strength(ch, nv)   # delta 制
            for sid in self.slot_ids:
                self.client.send_waveform(sid, "A", GENTLE_WAVEFORM)
            self.log("watchdog", f"degrade current={self.sl.current}")
            self.emit("watchdog_degrade", current=dict(self.sl.current))
        elif action == "stop":
            self._estop("watchdog_stop")
            self.sl.on_safe_hard(now)  # 转入锁定流程
            self.log("watchdog", "stop 失联停机并锁定")
            self.emit("watchdog_stop", announce="长时间无回应，已完全停止并锁定",
                      lock_seconds=self.sl.red.hard_lock_seconds,
                      state=self.sl.state.value)

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
        return msgs

    def run(self):
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        self.log("session", "daemon 启动")
        try:
            self.boot()
        except Exception as e:  # noqa: BLE001
            self.emit("error", where="boot", message=str(e))
            self.log("session", f"boot 失败: {e}")
            self.cleanup()
            return

        last_wd = 0.0
        while self.running:
            # 先排空设备侧上报（custom.action 安全词/剧情动作等）
            if self.client and self.client.ws:
                try:
                    self.client.drain_events()
                except DglabV4Error as e:
                    if "被控方已断开" in str(e):
                        self.emit("client_detached",
                                  hint="APP 已断开，等待重新接入")
                    else:
                        self.emit("error", where="drain", message=str(e))
                except Exception as e:  # noqa: BLE001
                    self.emit("error", where="drain", message=str(e))
            for msg in self.drain_inbox():
                cmd = msg.get("cmd")
                try:
                    if cmd == "ping":
                        self.emit("pong", state=self.sl.state.value,
                                  current=dict(self.sl.current))
                    elif cmd == "input":
                        self.handle_input(str(msg.get("text", "")))
                    elif cmd == "device":
                        self.handle_device(msg)
                    elif cmd == "authorize_start":
                        self.handle_authorize_start()
                    elif cmd == "manual_reset":
                        self.handle_manual_reset()
                    elif cmd == "shutdown":
                        self._estop("shutdown")
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
            if now - last_wd >= 1.0:
                last_wd = now
                try:
                    self.watchdog()
                except Exception as e:  # noqa: BLE001
                    self.emit("error", where="watchdog", message=str(e))
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


DglabClientType = DglabV4Client

if __name__ == "__main__":
    Daemon().run()
