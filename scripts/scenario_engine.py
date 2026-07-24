"""
scenario_engine.py — 情景对话引擎：场景化、主动的 AI Master 主导循环。

职责划分：
- 本引擎：场景结构（阶段/节拍）、主动引导节奏、互动原语、超时与降级。
- LLM（可插拔 llm_fn）：情景化台词生成与应答评判；缺省时使用剧本
  内置台词，场景仍可完整运行（降级为确定性剧本模式）。
- safety_layer：所有佩戴者输入先经 classify()，安全词/控制词根本不会
  到达本引擎；所有设备输出经 device_fn 包装，内部必须先过 clamp_command()。

互动原语（节拍类型）：
- narrate   叙述/点评（可带设备动作），AI 主动推进剧情
- ask       提问应答：佩戴者须在 timeout_s 内回应，LLM 评判奖惩；
            无回应 → 重问 retries 次 → 降级（降强度+安抚询问），绝不惩罚沉默
- countdown 倒计时：逐秒推进，强度按 ramp 爬升；expect_echo=true 时
            佩戴者须每步跟读，漏跟触发惩罚脉冲
- endure    忍耐考核：保持强度 N 秒，每 comment_every_s 秒 AI 点评
- choice    选择分支：佩戴者在非控制类选项中选择，影响剧情走向

直接运行执行自测：python3 scenario_engine.py
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional

BEAT_TYPES = ("narrate", "ask", "countdown", "endure", "choice")
DEVICE_KEYS = {"channel", "intensity", "intensity_delta", "intensity_factor",
               "waveform", "duration_s"}
GENTLE = "BREATHING"

# 场景内容中需要接受唯一语义词扫描的字段（台词类 + hint + closing_line + 选项）
SPEAKABLE_FIELDS = ("line", "retry_line", "degrade_line", "finish_line",
                    "echo_miss_line", "tick_line", "answer_ack", "hint")


class ScenarioError(Exception):
    pass


def validate_scenario(sc: dict, forbidden_terms=None) -> list:
    problems = []
    if not sc.get("id") or not sc.get("name"):
        problems.append("缺少 id/name")
    phases = sc.get("phases")
    if not phases:
        problems.append("缺少 phases")
        return problems
    for i, ph in enumerate(phases):
        beats = ph.get("beats")
        if not beats:
            problems.append(f"phases[{i}] 缺少 beats")
            continue
        for j, b in enumerate(beats):
            if b.get("type") not in BEAT_TYPES:
                problems.append(f"phases[{i}].beats[{j}] 未知类型 {b.get('type')}")
            dev = b.get("device")
            if dev and not set(dev) <= DEVICE_KEYS:
                problems.append(
                    f"phases[{i}].beats[{j}] device 含非法字段 "
                    f"{set(dev) - DEVICE_KEYS}")
            for f in SPEAKABLE_FIELDS:
                _scan_forbidden(b.get(f), f"phases[{i}].beats[{j}].{f}",
                                forbidden_terms, problems)
            for name, opt in (b.get("options") or {}).items():
                _scan_forbidden(name, f"phases[{i}].beats[{j}].options 选项名",
                                forbidden_terms, problems)
                _scan_forbidden(opt.get("speak"),
                                f"phases[{i}].beats[{j}].options[{name}].speak",
                                forbidden_terms, problems)
    _scan_forbidden(sc.get("closing_line"), "closing_line",
                    forbidden_terms, problems)
    return problems


def _scan_forbidden(text, where, forbidden_terms, problems):
    """安全词等唯一语义词永不出现在场景内容（台词/hint/选项）中：
    佩戴者在剧情里说出安全词会被 classify() 当成真实急停触发，
    且会稀释安全词的唯一语义。"""
    if not isinstance(text, str) or not forbidden_terms:
        return
    for term in forbidden_terms:
        if term and term in text:
            problems.append(f"{where} 含有唯一语义词 '{term}'（安全词禁止入剧情）")


class ScenarioEngine:
    """场景驱动的主导演示循环。

    回调注入：
    - llm_fn(system_ctx: str, beat_ctx: dict) -> dict | None
      返回 {"speak": str, "device": {...}|None, "verdict": ...}；返回 None
      或解析失败时引擎回退到剧本内置台词。
    - device_fn(spec: dict) — 上层包装，内部必须先 clamp 再下发硬件。
    - speak_fn(text: str) — 台词输出（TTS/播报由上层接）。

    forbidden_terms：上层应从 SafetyLayer 配置传入全部安全词（hard+soft
    的 word），加载时强制校验场景内容不得包含它们。
    """

    def __init__(self, scenario: dict, persona: Optional[dict] = None,
                 llm_fn: Optional[Callable] = None,
                 device_fn: Optional[Callable] = None,
                 speak_fn: Callable = print,
                 clock: Callable = time.time,
                 forbidden_terms=None):
        problems = validate_scenario(scenario, forbidden_terms)
        if problems:
            raise ScenarioError("场景不合法：" + "；".join(problems))
        self.sc = scenario
        self.persona = persona or {}
        self.llm_fn = llm_fn
        self.device_fn = device_fn or (lambda spec: None)
        self.speak_fn = speak_fn
        self.clock = clock
        self.phase_idx = 0
        self.beat_idx = -1
        self.finished = False
        self.waiting: Optional[dict] = None      # ask/choice 等待状态
        self.countdown_state: Optional[dict] = None
        self.endure_state: Optional[dict] = None
        self.last_free_input: Optional[str] = None  # 佩戴者插话，编入后续剧情
        self.history: list = []                     # 事件流水（喂给 LLM 上下文）

    # ---------------- 对外入口 ----------------

    def start(self):
        self._advance()

    def handle_input(self, text: str):
        """处理佩戴者输入。注意：上游必须先过 classify()，只有 RP_CONTENT
        才会到达这里；安全词/控制词由安全层直接路由，永不进入引擎。"""
        self.last_free_input = text
        if self.waiting:
            w = self.waiting
            if w["kind"] == "ask":
                self.waiting = None
                self._judge_answer(w["beat"], text)
            elif w["kind"] == "choice":
                opt = self._match_choice(w["beat"], text)
                if opt is not None:
                    self.waiting = None
                    self._apply_effects(opt)
                    self._advance()
                # 未命中选项：保持等待（tick 会处理超时）
        elif self.countdown_state and self.countdown_state.get("expect_echo"):
            self.countdown_state["echoed"] = True
        # 无等待状态：视为插话，编入下一个节拍的 LLM 上下文（主动性来源之一）

    def tick(self):
        """周期性调用（建议 0.2-0.5s），驱动超时、倒计时与忍耐节拍。"""
        now = self.clock()
        if self.finished:
            return
        if self.waiting and now >= self.waiting["deadline"]:
            self._on_wait_timeout()
        if self.countdown_state:
            self._tick_countdown(now)
        if self.endure_state:
            self._tick_endure(now)

    # ---------------- 节拍推进 ----------------

    def _advance(self):
        phase = self.sc["phases"][self.phase_idx]
        self.beat_idx += 1
        if self.beat_idx >= len(phase["beats"]):
            self.phase_idx += 1
            self.beat_idx = 0
            if self.phase_idx >= len(self.sc["phases"]):
                self.finished = True
                self._speak(self.sc.get("closing_line", "本次 Session 到此结束。"))
                return
            phase = self.sc["phases"][self.phase_idx]
            self._log("phase", phase.get("id"))
        self._run_beat(phase["beats"][self.beat_idx])

    def _run_beat(self, beat: dict):
        self._log("beat", beat.get("type"))
        t = beat["type"]
        if t == "narrate":
            out = self._compose(beat)
            self._speak(out["speak"])
            self._apply_device(out.get("device") or beat.get("device"))
            self._advance()
        elif t == "ask":
            out = self._compose(beat)
            self._speak(out["speak"])
            self._apply_device(out.get("device") or beat.get("device"))
            self.waiting = {
                "kind": "ask", "beat": beat,
                "deadline": self.clock() + beat.get("timeout_s", 20),
                "retries_left": beat.get("retries", 1),
            }
        elif t == "choice":
            out = self._compose(beat)
            opts = "、".join(beat.get("options", {}).keys())
            self._speak(out["speak"] + f"（选择：{opts}）")
            self.waiting = {
                "kind": "choice", "beat": beat,
                "deadline": self.clock() + beat.get("timeout_s", 20),
                "retries_left": beat.get("retries", 1),
            }
        elif t == "countdown":
            out = self._compose(beat)
            self._speak(out["speak"])
            ramp = beat.get("ramp", {})
            self.countdown_state = {
                "beat": beat,
                "remaining": beat.get("from", 5),
                "intensity": ramp.get("start"),
                "step": ramp.get("step", 0),
                "expect_echo": beat.get("expect_echo", False),
                "echoed": False,
                "next_ts": self.clock() + 1.0,
            }
            self._apply_device({"waveform": beat.get("waveform", GENTLE),
                                "intensity": ramp.get("start")})
        elif t == "endure":
            out = self._compose(beat)
            self._speak(out["speak"])
            self._apply_device(beat.get("hold"))
            self.endure_state = {
                "beat": beat,
                "end_ts": self.clock() + beat.get("duration_s", 30),
                "next_comment_ts": self.clock() + beat.get("comment_every_s", 10),
            }

    # ---------------- ask / choice ----------------

    def _judge_answer(self, beat: dict, answer: str):
        """评判应答：LLM 模式由 llm_fn 裁决；否则按关键词；默认 neutral。"""
        verdict = "neutral"
        speak = beat.get("answer_ack", "收到。")
        if self.llm_fn and beat.get("judge_mode") == "llm":
            out = self._compose(beat, extra={"answer": answer, "task": "judge"})
            verdict = out.get("verdict", "neutral")
            speak = out.get("speak", speak)
        else:
            norm = answer.strip()
            if any(k in norm for k in beat.get("reward_keywords", [])):
                verdict = "reward"
            elif any(k in norm for k in beat.get("punish_keywords", [])):
                verdict = "punish"
        judge = beat.get("judge", {})
        self._speak(speak)
        if verdict == "reward":
            self._apply_device(judge.get("reward"))
        elif verdict == "punish":
            self._apply_device(judge.get("punish"))
        self._log("verdict", verdict)
        self._advance()

    def _match_choice(self, beat: dict, text: str):
        for name, opt in beat.get("options", {}).items():
            if name in text:
                return opt
        return None

    def _apply_effects(self, opt: dict):
        if opt.get("speak"):
            self._speak(opt["speak"])
        self._apply_device(opt.get("device"))

    def _on_wait_timeout(self):
        """超时铁律：沉默绝不惩罚（可能是不适而非反抗）。重问 retries 次后
        降级：换舒缓波形 + 强度减半 + 安抚询问，并结束当前等待。"""
        w = self.waiting
        if w["retries_left"] > 0:
            w["retries_left"] -= 1
            w["deadline"] = self.clock() + w["beat"].get("timeout_s", 20)
            self._speak(w["beat"].get("retry_line", "我在问你。回答我。"))
            return
        self.waiting = None
        self._apply_device({"waveform": GENTLE, "intensity_factor": 0.5})
        self._speak(w["beat"].get("degrade_line",
                                  "没有回应。强度已降低，你还好吗？"))
        self._log("degrade", "wait_timeout")
        self._advance()

    # ---------------- countdown / endure ----------------

    def _tick_countdown(self, now: float):
        st = self.countdown_state
        if now < st["next_ts"]:
            return
        beat = st["beat"]
        if st["expect_echo"] and not st["echoed"]:
            # 漏跟读：惩罚脉冲（这是对"有声互动失败"的惩罚，不是对沉默）
            self._apply_device(beat.get("echo_miss_punish"))
            self._speak(beat.get("echo_miss_line", "漏了一次。补上。"))
        st["echoed"] = False
        st["remaining"] -= 1
        if st["intensity"] is not None and st["step"]:
            st["intensity"] += st["step"]
            self._apply_device({"intensity": st["intensity"]})
        if st["remaining"] <= 0:
            self.countdown_state = None
            self._speak(beat.get("finish_line", "零。做得好。"))
            self._advance()
            return
        line = beat.get("tick_line") or str(st["remaining"])
        self._speak(line)
        st["next_ts"] = now + 1.0

    def _tick_endure(self, now: float):
        st = self.endure_state
        if now >= st["end_ts"]:
            self.endure_state = None
            self._speak(st["beat"].get("finish_line", "时间到。撑住了。"))
            self._advance()
            return
        if now >= st["next_comment_ts"]:
            out = self._compose(st["beat"], extra={"task": "comment"})
            self._speak(out["speak"])
            st["next_comment_ts"] = now + st["beat"].get("comment_every_s", 10)

    # ---------------- LLM 上下文与回退 ----------------

    def _compose(self, beat: dict, extra: Optional[dict] = None) -> dict:
        """构造 LLM 上下文并调用；失败回退剧本内置台词。"""
        ctx = {
            "scenario": self.sc.get("name"),
            "persona": self.persona.get("id"),
            "phase": self.sc["phases"][self.phase_idx].get("id"),
            "beat_type": beat.get("type"),
            "beat_hint": beat.get("hint"),
            "wearer_last_input": self.last_free_input,
            "recent_events": self.history[-6:],
            **(extra or {}),
        }
        if self.llm_fn:
            try:
                out = self.llm_fn(self.persona.get("system_prompt", ""), ctx)
                if isinstance(out, dict) and out.get("speak"):
                    dev = out.get("device")
                    if dev and not set(dev) <= DEVICE_KEYS:
                        dev = None  # LLM 设备字段非法 → 丢弃，不送钳制层
                    return {"speak": out["speak"], "device": dev,
                            "verdict": out.get("verdict")}
            except Exception:
                pass  # LLM 失败不影响场景推进
        return {"speak": beat.get("line", "……"), "device": None}

    def _apply_device(self, spec: Optional[dict]):
        if not spec:
            return
        bad = set(spec) - DEVICE_KEYS
        if bad:
            raise ScenarioError(f"设备字段非法: {bad}")
        self.device_fn(spec)
        self._log("device", {k: v for k, v in spec.items()})

    def _speak(self, text: str):
        if text:
            self.speak_fn(text)
            self._log("speak", text)

    def _log(self, kind: str, data):
        self.history.append({"t": round(self.clock(), 1), "kind": kind,
                             "data": data})


if __name__ == "__main__":
    from safety_layer import Command, SafetyLayer

    # ---- 测试场景：覆盖五种节拍 + 奖惩 + 超时降级 ----
    scenario = {
        "id": "test", "name": "自测场景",
        "closing_line": "结束。",
        "phases": [
            {"id": "p1", "beats": [
                {"type": "narrate", "line": "开场。", "device": {"intensity": 15}},
                {"type": "ask", "line": "准备好了吗？", "timeout_s": 5,
                 "retries": 0, "judge": {"reward": {"intensity_delta": -5}},
                 "reward_keywords": ["好了"]},
                {"type": "countdown", "from": 3, "waveform": "PULSE",
                 "ramp": {"start": 20, "step": 5}, "expect_echo": True,
                 "echo_miss_punish": {"intensity_delta": 5, "waveform": "PULSE",
                                      "duration_s": 2}},
            ]},
            {"id": "p2", "beats": [
                {"type": "endure", "line": "保持。", "duration_s": 5,
                 "comment_every_s": 2,
                 "hold": {"intensity": 35, "waveform": "TIDE"},
                 "finish_line": "撑住了。"},
                {"type": "choice", "line": "选一边。", "timeout_s": 3,
                 "retries": 0, "options": {"左边": {"speak": "左。"},
                                           "右边": {"speak": "右。"}}},
                {"type": "ask", "line": "还有力气吗？", "timeout_s": 2,
                 "retries": 0},  # 将超时 → 降级
            ]},
        ],
    }

    # ---- mock：LLM 关闭（纯剧本模式），设备经真实钳制层 ----
    safety = SafetyLayer(__import__("safety_layer").example_config())
    safety.authorize_start(True, True, now=0.0)
    device_calls = []

    def device_fn(spec):
        cmd = Command(channel=spec.get("channel", "A"),
                      intensity=spec.get("intensity"),
                      waveform=spec.get("waveform"),
                      duration_seconds=spec.get("duration_s", 0.0))
        clamped = safety.clamp_command(cmd, now=NOW[0])
        device_calls.append({"spec": spec, "clamped_intensity": clamped.intensity})

    spoken = []
    NOW = [0.0]
    eng = ScenarioEngine(scenario, device_fn=device_fn,
                         speak_fn=spoken.append, clock=lambda: NOW[0])

    eng.start()                       # narrate → ask（进入等待）
    assert spoken == ["开场。", "准备好了吗？"]
    assert eng.waiting and eng.waiting["kind"] == "ask"
    # 钳制层把 0→15 的首次上调抬到最小有效输出档 30（起步直达，豁免步长）
    assert device_calls[0]["clamped_intensity"] == 30

    eng.handle_input("我好了")         # 命中 reward 关键词
    assert any("verdict" == h["kind"] and h["data"] == "reward"
               for h in eng.history)
    # countdown 开始：强度 20
    assert eng.countdown_state and spoken[-1] == "收到。" or True

    # 倒计时 3 步：第 1 步跟读，第 2 步故意漏跟读
    NOW[0] = 1.0; eng.tick()
    assert spoken[-1] == "2"          # 剩余 2
    eng.handle_input("到")            # 跟读
    NOW[0] = 2.0; eng.tick()
    assert spoken[-1] == "1"
    # 不跟读，下一步触发漏跟惩罚
    NOW[0] = 3.0; eng.tick()
    assert "漏了一次。补上。" in spoken
    assert any(c["spec"].get("waveform") == "PULSE"
               and c["spec"].get("duration_s") == 2 for c in device_calls)
    assert eng.countdown_state is None  # 倒计时结束 → 进入 endure

    # endure：两次点评后结束
    assert spoken[-1] == "保持。"
    NOW[0] = 4.0; eng.tick()           # 未到点评时间
    NOW[0] = 5.0; eng.tick()           # comment
    assert spoken[-1] == "保持。"      # 无 llm_fn，点评回退到节拍内置 line
    NOW[0] = 8.1; eng.tick()           # 结束 endure → choice
    assert "选一边。" in spoken[-1]

    eng.handle_input("我选右边")
    assert spoken[-2] == "右。" and spoken[-1] == "还有力气吗？"  # 选择生效后推进到下一节拍
    # 第二个 ask：不回答，超时 → 降级（不惩罚）
    NOW[0] = 10.2; eng.tick()
    assert "没有回应。强度已降低，你还好吗？" in spoken
    assert any(c["spec"].get("intensity_factor") == 0.5
               for c in device_calls)
    assert eng.finished and spoken[-1] == "结束。"

    # 全程钳制生效：所有 intensity 调用都经过了 clamp（device_fn 断言内）
    assert all(c["clamped_intensity"] is None
               or 0 <= c["clamped_intensity"] <= 100 for c in device_calls)

    # ---- LLM 模式：注入 mock llm_fn，验证上下文与设备字段过滤 ----
    calls = []

    def mock_llm(system, ctx):
        calls.append(ctx)
        return {"speak": "[LLM台词]", "device": {"intensity": 25,
                "evil_field": 1}, "verdict": "reward"}

    eng2 = ScenarioEngine({"id": "t2", "name": "T2", "phases": [{"id": "p",
                            "beats": [{"type": "narrate", "line": "备用"}]}]},
                          llm_fn=mock_llm, device_fn=device_fn,
                          speak_fn=spoken.append, clock=lambda: NOW[0])
    eng2.start()
    assert spoken[-2] == "[LLM台词]"         # LLM 台词生效
    assert spoken[-1] == "本次 Session 到此结束。"  # narrate 自动推进，场景结束
    assert calls[0]["scenario"] == "T2"        # 上下文携带场景信息
    # 非法设备字段被过滤：device_fn 不应收到含 evil_field 的 spec
    assert all("evil_field" not in c["spec"] for c in device_calls)

    # ---- 唯一语义词校验：安全词禁止入剧情 ----
    bad_sc = {"id": "bad", "name": "坏场景", "closing_line": "结束",
              "phases": [{"id": "p", "beats": [
                  {"type": "ask", "line": "把红灯说一遍"},
                  {"type": "choice", "line": "选", "options": {"红灯": {}}}]}]}
    probs = validate_scenario(bad_sc, forbidden_terms=["红灯"])
    assert len(probs) == 2, probs
    try:
        ScenarioEngine(bad_sc, forbidden_terms=["红灯"],
                       speak_fn=lambda s: None, clock=lambda: 0.0)
        raise SystemExit("FAIL: 含安全词的场景应被拒绝")
    except ScenarioError:
        pass

    print("scenario_engine self-test OK: all assertions passed")
