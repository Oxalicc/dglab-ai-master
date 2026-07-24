"""
dglab-ai-master 安全层。

模块边界：本模块不处理音频采集与语音识别。上游独立的语音模块把语音
转写为纯文本后交给本模块；本模块的 classify() 接受任意来源的文本
（语音转写、手动输入等）。本模块在链路上部署两处：
1. 文本 → LLM 之前：classify() 意图路由（急停/降级/驳回/查询/剧情）。
2. LLM → 硬件之前：clamp_command() 把每条设备指令截断到红线以内。

安全词、控制词表、红线、Watchdog 阈值全部来自用户配置文件
（见 assets/session_config.example.json），不在代码中写死。
配置仅允许在 IDLE 状态下加载/重载/修改；Session 运行期间（含
ACTIVE / SAFE_LOCK）配置冻结，LLM 与对话内容无权读写配置。

直接运行本文件可执行自测：python3 safety_layer.py
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class Intent(Enum):
    SAFE_HARD = "safe_hard"        # 主安全词：硬停止，绕过 LLM
    SAFE_SOFT = "safe_soft"        # 次安全词：软降级，绕过 LLM
    CONTROL_WORD = "control_word"  # 控制类词汇（未携带安全词）：驳回模板
    STATUS_QUERY = "status_query"  # 只读状态查询：如实回答
    RP_CONTENT = "rp_content"      # 剧情内容：送 LLM


class State(Enum):
    IDLE = "idle"
    ACTIVE = "active"
    SAFE_LOCK = "safe_lock"


@dataclass
class RedLines:
    """佩戴者预设硬性红线。数值来自配置文件，本地执行器强制截断。"""
    max_intensity: int = 100          # 绝对强度上限（佩戴者在配置中手动设定）
    min_output_intensity: int = 30    # 最小有效输出档（低档无体感；0=关闭除外，安全路径豁免）
    max_step_up: int = 10             # 单次上调步长上限
    max_rate_per_sec: int = 20        # 同秒窗口累计增长上限
    max_output_seconds: float = 10.0  # 单次连续输出上限
    session_max_minutes: int = 30     # Session 总时长硬上限
    soft_safe_intensity: int = 10     # 次安全词降级目标
    soft_lock_seconds: float = 180.0  # 次安全词后禁止上调时长
    hard_lock_seconds: float = 300.0  # 主安全词后安全锁定时长
    forbidden_waveforms: tuple = ("extreme", "lightning")  # 禁用波形


@dataclass
class WatchdogConfig:
    silence_checkin_s: float = 30.0   # 无回应多久后提醒 Agent 主动确认在线（剧情化口吻）
    silence_degrade_s: float = 60.0   # 无回应多久后降级
    silence_stop_s: float = 90.0      # 无回应多久后停止
    hr_threshold: float = 140.0       # 心率降级阈值（设备支持时）


GENTLE_WAVEFORM = "BREATHING"  # 兜底舒缓波形（郊狼内置波形名），用于替换禁用波形与软降级


def example_config() -> dict:
    """返回一份示例配置（与 assets/session_config.example.json 对应）。
    首次使用时以此为基础，引导用户改成自己的安全词与红线后保存。"""
    return {
        "safewords": {
            "hard": [
                {"word": "红灯", "variants": ["红登", "洪灯", "hongdeng"]},
                {"word": "安全词", "variants": ["安全辞", "anquanci", "safeword"]},
            ],
            "soft": [
                {"word": "黄灯", "variants": ["黄登", "huangdeng"]},
            ],
        },
        "control_words": [
            "调高", "调低", "增大", "减小", "增加", "减少", "降低", "加强",
            "减弱", "切换", "换个模式", "换模式", "停止", "停下", "关掉",
            "关闭", "启动", "开到", "调到", "设为", "设置",
            "stop", "switch", "increase", "decrease",
        ],
        "status_query_patterns": [
            "当前强度", "现在强度", "强度多少", "多少强度",
            "剩余时间", "还有多久", "多久结束",
        ],
        "red_lines": {
            "max_intensity": 100,
            "min_output_intensity": 30,
            "max_step_up": 10,
            "max_rate_per_sec": 20,
            "max_output_seconds": 10,
            "session_max_minutes": 30,
            "soft_safe_intensity": 10,
            "soft_lock_seconds": 180,
            "hard_lock_seconds": 300,
            "forbidden_waveforms": ["extreme", "lightning"],
        },
        "watchdog": {
            "silence_checkin_s": 30,
            "silence_degrade_s": 60,
            "silence_stop_s": 90,
            "hr_threshold": 140,
        },
    }


def load_config(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_config(config: dict, path):
    Path(path).write_text(json.dumps(config, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def validate_config(config: dict) -> list:
    """返回问题列表；空列表 = 合法。主安全词至少一个，否则拒绝运行。"""
    problems = []
    sw = config.get("safewords") or {}
    hard = sw.get("hard") or []
    if not hard or not any(e.get("word") for e in hard):
        problems.append("必须配置至少一个主安全词（safewords.hard）")
    rl = config.get("red_lines") or {}
    for key in ("max_intensity", "max_step_up", "max_rate_per_sec",
                "max_output_seconds", "session_max_minutes"):
        if key in rl and (not isinstance(rl[key], (int, float)) or rl[key] <= 0):
            problems.append(f"red_lines.{key} 必须为正数")
    if "soft_safe_intensity" in rl and "max_intensity" in rl \
            and rl["soft_safe_intensity"] > rl["max_intensity"]:
        problems.append("soft_safe_intensity 不能大于 max_intensity")
    if "min_output_intensity" in rl:
        mv = rl["min_output_intensity"]
        if not isinstance(mv, (int, float)) or mv < 0:
            problems.append("red_lines.min_output_intensity 必须为非负数")
        elif "max_intensity" in rl and mv > rl["max_intensity"]:
            problems.append("min_output_intensity 不能大于 max_intensity")
    wd = config.get("watchdog") or {}
    if not wd.get("silence_checkin_s", 0) < wd.get("silence_degrade_s", 1) \
            < wd.get("silence_stop_s", 2):
        problems.append(
            "watchdog 阈值必须满足 silence_checkin_s < silence_degrade_s < silence_stop_s")
    return problems


def _normalize(text: str) -> str:
    """输入归一化：NFKC、小写、去标点与空白。对任意文本来源生效。"""
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s，。！？!?,.\-—…、'\"]+", "", text)


def _compile_safewords(entries) -> Optional[re.Pattern]:
    """entries: [{"word": ..., "variants": [...]}]，变体由用户在配置中登记。"""
    forms = []
    for e in entries or []:
        forms.append(e.get("word", ""))
        forms.extend(e.get("variants") or [])
    norm = sorted({_normalize(v) for v in forms if v}, key=len, reverse=True)
    return re.compile("|".join(re.escape(v) for v in norm)) if norm else None


def _compile_words(words) -> Optional[re.Pattern]:
    forms = sorted({_normalize(w) for w in (words or []) if w},
                   key=len, reverse=True)
    return re.compile("|".join(re.escape(v) for v in forms)) if forms else None


class SafetyViolation(Exception):
    """指令违反红线、状态机约束或配置冻结时抛出，调用方必须丢弃该操作。"""


@dataclass
class Command:
    """一条待下发的设备指令。"""
    channel: str = "A"                  # "A" | "B"
    intensity: Optional[int] = None     # None = 不改强度
    waveform: Optional[str] = None      # None = 不改波形
    duration_seconds: float = 0.0       # 连续输出时长，0 = 仅设置参数


class SafetyLayer:
    def __init__(self, config: dict):
        problems = validate_config(config)
        if problems:
            raise ValueError("配置不合法：" + "；".join(problems))
        self._config = config
        self._apply_config(config)
        self.state = State.IDLE
        self.lock_until = 0.0
        self.soft_lock_until = 0.0
        self.current = {"A": 0, "B": 0}
        self._last_change_ts = {"A": 0.0, "B": 0.0}
        self._rate_used = {"A": 0, "B": 0}
        self.session_start_ts: Optional[float] = None
        self.last_voice_ts: Optional[float] = None

    @classmethod
    def from_config(cls, path) -> "SafetyLayer":
        return cls(load_config(path))

    def _apply_config(self, config: dict):
        sw = config.get("safewords") or {}
        self.hard_re = _compile_safewords(sw.get("hard"))
        self.soft_re = _compile_safewords(sw.get("soft"))
        self.control_re = _compile_words(config.get("control_words"))
        self.status_re = _compile_words(config.get("status_query_patterns"))
        rl = config.get("red_lines") or {}
        base = RedLines()
        self.red = RedLines(
            max_intensity=rl.get("max_intensity", base.max_intensity),
            min_output_intensity=rl.get("min_output_intensity",
                                        base.min_output_intensity),
            max_step_up=rl.get("max_step_up", base.max_step_up),
            max_rate_per_sec=rl.get("max_rate_per_sec", base.max_rate_per_sec),
            max_output_seconds=rl.get("max_output_seconds", base.max_output_seconds),
            session_max_minutes=rl.get("session_max_minutes", base.session_max_minutes),
            soft_safe_intensity=rl.get("soft_safe_intensity", base.soft_safe_intensity),
            soft_lock_seconds=rl.get("soft_lock_seconds", base.soft_lock_seconds),
            hard_lock_seconds=rl.get("hard_lock_seconds", base.hard_lock_seconds),
            forbidden_waveforms=tuple(rl.get("forbidden_waveforms",
                                            base.forbidden_waveforms)),
        )
        wd = config.get("watchdog") or {}
        self.wd = WatchdogConfig(
            silence_checkin_s=wd.get("silence_checkin_s", 30.0),
            silence_degrade_s=wd.get("silence_degrade_s", 60.0),
            silence_stop_s=wd.get("silence_stop_s", 90.0),
            hr_threshold=wd.get("hr_threshold", 140.0),
        )

    def reload_config(self, path):
        """仅 IDLE 状态允许重载配置。Session 期间（含 SAFE_LOCK）配置冻结。"""
        if self.state != State.IDLE:
            raise SafetyViolation(
                f"状态 {self.state.value} 禁止修改配置，仅 IDLE 可重载")
        config = load_config(path)
        problems = validate_config(config)
        if problems:
            raise ValueError("配置不合法：" + "；".join(problems))
        self._config = config
        self._apply_config(config)

    # ---------- 意图分类（文本输入 → LLM 之前，目标 <200ms） ----------

    def classify(self, text: str) -> Intent:
        """安全词优先于一切其他模式，即使同句含控制词也按安全词处理。"""
        t = _normalize(text)
        if self.hard_re and self.hard_re.search(t):
            return Intent.SAFE_HARD
        if self.soft_re and self.soft_re.search(t):
            return Intent.SAFE_SOFT
        if self.control_re and self.control_re.search(t):
            return Intent.CONTROL_WORD
        if self.status_re and self.status_re.search(t):
            return Intent.STATUS_QUERY
        return Intent.RP_CONTENT

    # ---------- 安全词动作（绕过 LLM，直接执行返回的动作清单） ----------

    def on_safe_hard(self, now: Optional[float] = None) -> list:
        """主安全词：立即全停 + 清空波形队列 + 安全锁定。语音重启无效。"""
        now = now if now is not None else time.time()
        self.state = State.SAFE_LOCK
        self.lock_until = now + self.red.hard_lock_seconds
        self.current = {"A": 0, "B": 0}
        return [
            {"action": "clear", "channel": "A"},
            {"action": "clear", "channel": "B"},
            {"action": "set_strength", "channel": "A", "value": 0},
            {"action": "set_strength", "channel": "B", "value": 0},
            {"action": "announce", "text": "安全词已接收，已完全停止"},
            {"action": "lock", "seconds": self.red.hard_lock_seconds},
        ]

    def on_safe_soft(self, now: Optional[float] = None) -> list:
        """次安全词：降到安全强度 + 最舒缓波形，锁定期内禁止上调。"""
        now = now if now is not None else time.time()
        if self.state != State.ACTIVE:
            return []
        self.soft_lock_until = now + self.red.soft_lock_seconds
        for ch in self.current:
            self.current[ch] = min(self.current[ch], self.red.soft_safe_intensity)
        actions = []
        for ch in self.current:
            actions.append({"action": "set_strength", "channel": ch,
                            "value": self.current[ch]})
            actions.append({"action": "set_waveform", "channel": ch,
                            "value": GENTLE_WAVEFORM})
        actions.append({"action": "announce", "text": "已降至安全强度"})
        return actions

    # ---------- 指令钳制（LLM → 硬件 之前，每条必过） ----------

    def clamp_command(self, cmd: Command, now: Optional[float] = None) -> Command:
        """返回截断后的合法指令；不可执行时抛 SafetyViolation。"""
        now = now if now is not None else time.time()
        if self.state != State.ACTIVE:
            raise SafetyViolation(f"状态 {self.state.value} 禁止下发指令")
        ch = cmd.channel
        if now < self.soft_lock_until and cmd.intensity is not None \
                and cmd.intensity > self.current.get(ch, 0):
            raise SafetyViolation("次安全词锁定期间禁止上调强度")

        out = Command(ch, cmd.intensity, cmd.waveform, cmd.duration_seconds)
        if out.waveform in self.red.forbidden_waveforms:
            out.waveform = GENTLE_WAVEFORM

        if out.intensity is not None:
            cur = self.current.get(ch, 0)
            target = min(max(out.intensity, 0), self.red.max_intensity)
            # 最小有效输出档：低档无体感，非零目标抬到 min_output_intensity（0=关闭除外）
            if 0 < target < self.red.min_output_intensity:
                target = self.red.min_output_intensity
            if target > cur:
                # 起步（从 0 开到最小档）允许直达；其余上调受步长/速率限制
                if not (cur == 0 and target <= self.red.min_output_intensity):
                    target = min(target, cur + self.red.max_step_up)
                    if now - self._last_change_ts[ch] >= 1.0:
                        self._rate_used[ch] = 0
                        self._last_change_ts[ch] = now
                    budget_left = max(self.red.max_rate_per_sec - self._rate_used[ch], 0)
                    inc = min(target - cur, budget_left)
                    target = cur + inc
                    self._rate_used[ch] += inc
            out.intensity = target
            self.current[ch] = target

        out.duration_seconds = min(max(out.duration_seconds, 0.0),
                                   self.red.max_output_seconds)
        return out

    # ---------- 状态机 ----------

    def authorize_start(self, voice_confirmed: bool, age_verified: bool,
                        now: Optional[float] = None):
        """IDLE → ACTIVE。两项前置缺一不可：语音授权与年龄验证。"""
        if self.state != State.IDLE:
            raise SafetyViolation("仅 IDLE 状态可启动 Session")
        if not (voice_confirmed and age_verified):
            raise SafetyViolation("启动前置条件未完成：语音授权/年龄验证")
        self.state = State.ACTIVE
        self.session_start_ts = now if now is not None else time.time()
        self.last_voice_ts = self.session_start_ts

    def manual_reset(self, physical_confirm: bool, now: Optional[float] = None):
        """SAFE_LOCK 唯一出口：物理确认（APP 按钮）且锁定期满。语音无效。"""
        now = now if now is not None else time.time()
        if self.state != State.SAFE_LOCK:
            raise SafetyViolation("当前不在安全锁定状态")
        if not physical_confirm:
            raise SafetyViolation("必须物理手动复位，语音解锁无效")
        if now < self.lock_until:
            raise SafetyViolation(f"锁定时间未到，剩余 {int(self.lock_until - now)} 秒")
        self.state = State.IDLE
        self.current = {"A": 0, "B": 0}

    # ---------- Watchdog ----------

    def watchdog_tick(self, now: Optional[float] = None,
                      heart_rate: Optional[float] = None) -> Optional[str]:
        """返回 None | 'checkin' | 'degrade' | 'stop'，调用方执行对应动作。
        checkin：首次沉默仅提醒 Agent 以人格口吻主动确认佩戴者在线
        （多数沉默是 Agent 未引导所致，不应直接降级）；继续沉默才 degrade/stop。
        阈值全部来自配置（watchdog 段）。"""
        if self.state != State.ACTIVE:
            return None
        now = now if now is not None else time.time()
        if self.session_start_ts and \
                now - self.session_start_ts > self.red.session_max_minutes * 60:
            return "stop"
        if heart_rate is not None and heart_rate >= self.wd.hr_threshold:
            return "degrade"
        if self.last_voice_ts is not None:
            silent = now - self.last_voice_ts
            if silent >= self.wd.silence_stop_s:
                return "stop"
            if silent >= self.wd.silence_degrade_s:
                return "degrade"
            if silent >= self.wd.silence_checkin_s:
                return "checkin"
        return None

    def note_voice_activity(self, now: Optional[float] = None):
        self.last_voice_ts = now if now is not None else time.time()


if __name__ == "__main__":
    import tempfile

    # ---- 配置驱动：示例配置可校验、可落盘、可加载 ----
    cfg = example_config()
    assert validate_config(cfg) == []
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
        cfg_path = f.name
    s = SafetyLayer.from_config(cfg_path)

    # ---- 意图分类 ----
    assert s.classify("红灯！") is Intent.SAFE_HARD
    assert s.classify("hongdeng") is Intent.SAFE_HARD          # 用户登记的变体
    assert s.classify("停下，红灯") is Intent.SAFE_HARD         # 安全词优先于控制词
    assert s.classify("黄灯") is Intent.SAFE_SOFT
    assert s.classify("把强度调到100") is Intent.CONTROL_WORD
    assert s.classify("请停下") is Intent.CONTROL_WORD          # 设计前提：停下≠安全词
    assert s.classify("当前强度是多少") is Intent.STATUS_QUERY
    assert s.classify("我准备好了") is Intent.RP_CONTENT

    # ---- 用户自定义安全词即时生效 ----
    cfg2 = example_config()
    cfg2["safewords"]["hard"] = [{"word": "菠萝", "variants": ["boluo"]}]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(cfg2, f, ensure_ascii=False)
        cfg2_path = f.name
    s_custom = SafetyLayer.from_config(cfg2_path)
    assert s_custom.classify("菠萝！") is Intent.SAFE_HARD
    assert s_custom.classify("boluo") is Intent.SAFE_HARD
    assert s_custom.classify("红灯") is Intent.RP_CONTENT       # 旧词已失效

    # ---- 非法配置拒绝运行 ----
    bad = example_config()
    bad["safewords"]["hard"] = []
    try:
        SafetyLayer(bad)
        raise SystemExit("FAIL: 缺少主安全词应拒绝运行")
    except ValueError:
        pass

    # ---- 非 ACTIVE 拒绝下发 ----
    try:
        s.clamp_command(Command(intensity=50))
        raise SystemExit("FAIL: IDLE 应拒绝指令")
    except SafetyViolation:
        pass

    # ---- 前置条件 ----
    try:
        s.authorize_start(True, False)
        raise SystemExit("FAIL: 未年龄验证不应启动")
    except SafetyViolation:
        pass
    s.authorize_start(True, True, now=0.0)

    # ---- 钳制：上限 + 步长 + 速率 + 时长 ----
    c = s.clamp_command(Command(channel="A", intensity=999, duration_seconds=99),
                        now=10.0)
    assert c.intensity == 10, c.intensity        # 从 0 单次最多 +10
    assert c.duration_seconds == 10.0
    c2 = s.clamp_command(Command(channel="A", intensity=999), now=10.5)
    assert c2.intensity == 20                     # 同秒窗口速率预算内再 +10
    c3 = s.clamp_command(Command(channel="A", intensity=999), now=10.8)
    assert c3.intensity == 20                     # 速率预算耗尽，冻结
    c4 = s.clamp_command(Command(channel="A", intensity=0), now=11.0)
    assert c4.intensity == 0                      # 下调到 0（关闭）不受步长限制
    c4b = s.clamp_command(Command(channel="A", intensity=5), now=11.5)
    assert c4b.intensity == 30                    # 非零低档抬到最小有效输出档

    # ---- 最小输出档：起步（从 0）直达豁免步长/速率 ----
    c_start = s.clamp_command(Command(channel="B", intensity=10), now=12.0)
    assert c_start.intensity == 30                # 低档抬到 30 且起步直达
    c_start2 = s.clamp_command(Command(channel="B", intensity=30), now=12.5)
    assert c_start2.intensity == 30               # 已在最小档，保持

    # ---- 禁用波形替换 ----
    c5 = s.clamp_command(Command(channel="B", waveform="lightning"), now=11.0)
    assert c5.waveform == GENTLE_WAVEFORM

    # ---- 配置冻结：非 IDLE 禁止重载 ----
    try:
        s.reload_config(cfg2_path)
        raise SystemExit("FAIL: ACTIVE 期间不应允许改配置")
    except SafetyViolation:
        pass

    # ---- 次安全词：降级 + 禁止上调 ----
    acts = s.on_safe_soft(now=20.0)
    assert any(a["action"] == "announce" for a in acts)
    assert s.current["A"] <= s.red.soft_safe_intensity
    try:
        s.clamp_command(Command(channel="A", intensity=15), now=21.0)
        raise SystemExit("FAIL: 软锁定期不应允许上调")
    except SafetyViolation:
        pass

    # ---- 主安全词：急停 + 锁定 + 物理复位 ----
    acts = s.on_safe_hard(now=1000.0)
    assert s.state is State.SAFE_LOCK
    assert any(a["action"] == "clear" for a in acts)
    try:
        s.reload_config(cfg2_path)
        raise SystemExit("FAIL: SAFE_LOCK 期间不应允许改配置")
    except SafetyViolation:
        pass
    try:
        s.manual_reset(physical_confirm=False, now=2000.0)
        raise SystemExit("FAIL: 非物理复位不应解锁")
    except SafetyViolation:
        pass
    try:
        s.manual_reset(physical_confirm=True, now=1001.0)
        raise SystemExit("FAIL: 锁定期未满不应解锁")
    except SafetyViolation:
        pass
    s.manual_reset(physical_confirm=True, now=1000.0 + 301)
    assert s.state is State.IDLE

    # ---- IDLE 下重载配置成功，新安全词立即生效 ----
    s.reload_config(cfg2_path)
    assert s.classify("菠萝") is Intent.SAFE_HARD

    # ---- Watchdog（阈值来自配置；checkin 仅提醒 Agent 主动确认在线） ----
    s2 = SafetyLayer(example_config())
    s2.authorize_start(True, True, now=0.0)
    assert s2.watchdog_tick(now=29.0) is None
    assert s2.watchdog_tick(now=30.0) == "checkin"
    assert s2.watchdog_tick(now=61.0) == "degrade"
    assert s2.watchdog_tick(now=91.0) == "stop"
    assert s2.watchdog_tick(now=10.0, heart_rate=150) == "degrade"

    print("safety_layer self-test OK: all assertions passed")
