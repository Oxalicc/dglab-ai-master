"""
session_bootstrap.py — Session 启动前三阶段引导流程（可执行的启动检查清单）。

三阶段，顺序不可跳：
1. 设备连接检查：环境依赖 → Relay 连接 → APP 扫码配对 → 设备在线验证；
   任何一步失败都给出具体引导并允许重试，绝不带着故障设备进入下一步。
2. 安全确认：向佩戴者逐项播报并确认 安全词 / 控制规则（红线） /
   游戏时长；IDLE 状态下支持语音修改时长并写回配置。
3. 显式开始：完整安全播报后，只有佩戴者明确说出开始口令（默认"开始"）
   才调用 authorize_start() 进入 ACTIVE。

回调注入（便于接真实语音链路，也便于离线自测）：
- speak_fn(text)  播报/显示引导语
- hear_fn() -> str  获取佩戴者一句输入（阻塞）
- client_factory(ws_url) -> 已 connect() 的客户端（默认用 DglabV4Client）
- env_checker() -> bool（默认调 check_env.check()）

直接运行执行自测：python3 session_bootstrap.py
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, Optional

from safety_layer import SafetyLayer, SafetyViolation, save_config, load_config

DEFAULT_START_PHRASE = "开始"
MAX_CONNECT_ATTEMPTS = 3

SAFETY_BRIEFING = (
    "Session 即将开始。请确认：主安全词【{hard}】会立即完全停止一切输出；"
    "次安全词【{soft}】会把强度降至安全水平。除安全词外，你的任何话语都会"
    "被视为剧情内容，不会被当作真实指令。请确认设备贴片位置正确、皮肤无破损。"
    "你随时可以使用 APP 物理按钮急停。如感不适，立即说出安全词。"
)


class BootstrapError(Exception):
    pass


class Bootstrap:
    def __init__(self, config_path: str,
                 speak_fn: Callable = print,
                 hear_fn: Optional[Callable] = None,
                 client_factory: Optional[Callable] = None,
                 env_checker: Optional[Callable] = None,
                 relay_ensurer: Optional[Callable] = None,
                 clock: Callable = time.time):
        self.config_path = config_path
        self.speak = speak_fn
        self.hear = hear_fn or (lambda: "")
        self.clock = clock
        # 延迟导入，缺依赖时的报错由 env_checker 阶段先拦截
        if client_factory is None:
            from dglab_v4_client import DglabV4Client

            def client_factory(url):
                c = DglabV4Client(url)
                c.connect()
                return c
        self.client_factory = client_factory
        if env_checker is None:
            import check_env
            env_checker = lambda: check_env.check()["ready"]
        self.env_ok = env_checker
        if relay_ensurer is None:
            import relay_manager
            relay_ensurer = lambda url, speak: relay_manager.ensure_relay(
                url, speak=speak)
        self.relay_ensurer = relay_ensurer
        self.relay_handle = None
        self.config = load_config(config_path)
        self.safety = SafetyLayer(self.config)
        self.client = None
        self.devices = []

    # ================= 主流程 =================

    def run(self) -> dict:
        """执行完整三阶段引导，返回 ready 上下文。任何阶段失败抛 BootstrapError。"""
        self._step_env()
        self._step_device_connect()
        self._step_device_verify()
        self._step_safety_review()
        self._step_final_confirm()
        return {
            "client": self.client,
            "devices": self.devices,
            "config": self.config,
            "safety": self.safety,
        }

    # ================= 阶段一：设备连接检查 =================

    def _step_env(self):
        self.speak("第 1/3 阶段：设备连接检查。先验证运行环境……")
        if not self.env_ok():
            raise BootstrapError(
                "运行环境缺依赖。请先运行 scripts/check_env.py 验证，"
                "并向用户发起安装请求（--install 或 --venv 回退）。")
        self.speak("环境就绪。")

    def _step_device_connect(self):
        url = (self.config.get("transport") or {}).get("url")
        if not url:
            raise BootstrapError("配置缺少 transport.url（V4 Relay 地址）")
        # Relay 探测与自建兜底：无服务时自动拉起 Skill 内置 Relay
        try:
            self.relay_handle = self.relay_ensurer(url, self.speak)
        except Exception as e:
            raise BootstrapError(f"Relay 不可用且自建失败：{e}")
        for attempt in range(1, MAX_CONNECT_ATTEMPTS + 1):
            try:
                self.client = self.client_factory(url)
                qr = self.client.pairing_qr_url()
                self.speak("请用 DG-LAB 4 APP 扫描配对二维码接入"
                           f"（二维码内容：{qr}）")
                self.speak("等待 APP 接入……")
                self.client.wait_client(timeout=120)
                self.speak("APP 已接入。")
                return
            except Exception as e:
                self.speak(f"第 {attempt} 次连接失败：{e}")
                if attempt < MAX_CONNECT_ATTEMPTS:
                    self.speak("引导：请确认 ① Relay 地址可连通 ② 郊狼已开机 "
                               "③ APP 已更新到 DG-LAB 4 并扫描上方二维码。"
                               "准备好后说'重试'。")
                    answer = self.hear()
                    if "重试" not in answer and "retry" not in answer.lower():
                        raise BootstrapError("用户放弃设备连接。")
        raise BootstrapError("多次连接失败，放弃。请检查 Relay 服务与设备后重试。")

    def _step_device_verify(self):
        self.devices = self.client.get_devices() or []
        if not self.devices:
            self.speak("未检测到任何郊狼设备。引导：请在 APP 中确认设备已开机、"
                       "蓝牙已连接、电量充足。确认后说'好了'。")
            answer = self.hear()
            if "好了" not in answer:
                raise BootstrapError("设备未就绪。")
            self.devices = self.client.get_devices() or []
            if not self.devices:
                raise BootstrapError("仍未检测到设备。")
        names = "、".join(d.get("name", d.get("slotId", "?")) for d in self.devices)
        self.speak(f"设备在线：{names}。")

    # ================= 阶段二：安全确认 =================

    def _step_safety_review(self):
        sw = self.config.get("safewords") or {}
        hard = "、".join(e["word"] for e in sw.get("hard", []))
        soft = "、".join(e["word"] for e in sw.get("soft", [])) or "（未设置）"
        while True:
            rl = self.safety.red
            self.speak(
                "第 2/3 阶段：安全确认。请逐项确认——\n"
                f"① 主安全词（立即全停）：{hard}\n"
                f"② 次安全词（降至安全强度）：{soft}\n"
                f"③ 控制规则：强度上限 {rl.max_intensity}，"
                f"每 {rl.step_up_cooldown_seconds:.0f} 秒最多上调 {rl.max_step_up} 档（下调可回补额度），"
                f"单次输出 ≤{rl.max_output_seconds:.0f} 秒\n"
                f"④ 游戏时长：{rl.session_max_minutes} 分钟\n"
                "全部确认请说'确认'；修改时长请说'时长改成 X 分钟'。"
            )
            answer = self.hear()
            if "确认" in answer:
                self.speak("安全设置已确认并锁定，Session 期间不可修改。")
                return
            m = re.search(r"时长改成?\s*(\d+)\s*分钟?", answer)
            if m:
                minutes = int(m.group(1))
                if not 1 <= minutes <= 120:
                    self.speak("时长需在 1-120 分钟之间，请重说。")
                    continue
                self._update_duration(minutes)
                self.speak(f"时长已改为 {minutes} 分钟。请重新确认全部设置。")
                continue
            self.speak("没有听懂。请说'确认'，或'时长改成 X 分钟'。")

    def _update_duration(self, minutes: int):
        """IDLE 下修改时长并写回配置（reload_config 的冻结规则保障非 IDLE 不可改）。"""
        self.config.setdefault("red_lines", {})["session_max_minutes"] = minutes
        save_config(self.config, self.config_path)
        self.safety.reload_config(self.config_path)

    # ================= 阶段三：显式开始 =================

    def _step_final_confirm(self):
        sw = self.config.get("safewords") or {}
        hard = "、".join(e["word"] for e in sw.get("hard", []))
        soft = "、".join(e["word"] for e in sw.get("soft", [])) or "（未设置）"
        start_phrase = (self.config.get("wearer") or {}).get(
            "start_phrase", DEFAULT_START_PHRASE)
        self.speak("第 3/3 阶段：最终确认。")
        self.speak(SAFETY_BRIEFING.format(hard=hard, soft=soft))
        self.speak(self._button_briefing())
        self.speak(f"全部准备就绪。说'{start_phrase}'正式启动 Session。")
        while True:
            answer = self.hear().strip()
            if answer == start_phrase:
                break
            self.speak(f"等待你的明确口令：'{start_phrase}'。")
        self.safety.authorize_start(
            voice_confirmed=True,
            age_verified=bool((self.config.get("wearer") or {}).get("age_verified_at")),
        )
        self.speak("Session 启动。")

    def _button_briefing(self):
        """APP 十个按键（界面显示为字母 A~J）的含义说明。
        A/F 为硬编码安全词语义；其余字母的语义取自配置 custom_actions，
        只代表剧情意图。介绍一律用字母（APP 界面没有数字）。"""
        mapping = self.config.get("custom_actions") or {}
        rp = []
        for n in range(1, 10):
            letter = chr(ord("A") + n)
            if letter == "F":
                continue
            sem = (mapping.get(str(n)) or {}).get("semantic")
            if sem:
                rp.append(f"{letter}={sem}")
        text = ("APP 里的十个按键：A=主安全词，立即完全停止；"
                "F=次安全词，把强度降到安全水平。")
        if rp:
            text += ("其余是剧情互动按键：" + "、".join(rp) +
                     "。剧情按键只表达角色扮演的意图，不代表你的真实意愿，"
                     "我会结合剧情回应，但不会因此交出控制权。")
        return text


if __name__ == "__main__":
    import tempfile

    # ---- mock 环境 ----
    class FakeClient:
        def __init__(self, url):
            self.url = url
            self.fail_once = False

        def pairing_qr_url(self):
            return "QR://test"

        def wait_client(self, timeout=None):
            if self.fail_once:
                self.fail_once = False
                raise TimeoutError("等待超时")
            return "client-1"

        def get_devices(self):
            return [{"slotId": "slot-1", "name": "郊狼3.0", "type": "coyote"}]

        def set_intensity(self, slot, ch, v, **kw):
            applied.append(v)

    spoken, applied = [], []

    class FakeRelayHandle:
        def stop(self):
            pass

    relay_calls = []

    def fake_ensurer(url, speak):
        relay_calls.append(url)
        speak(f"未检测到 Relay 服务（{url}），正在启动自建 Relay……")
        return FakeRelayHandle()

    def make_bootstrap(hear_script, fail_once=False):
        cfg = json.loads(json.dumps(__import__("safety_layer").example_config()))
        cfg["transport"] = {"url": "ws://127.0.0.1:9998"}
        cfg["wearer"] = {"age_verified_at": "2026-01-01T00:00:00"}
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        json.dump(cfg, tmp, ensure_ascii=False)
        tmp.close()

        def factory(url):
            c = FakeClient(url)
            c.fail_once = fail_once
            return c

        answers = iter(hear_script)
        return Bootstrap(tmp.name, speak_fn=spoken.append,
                         hear_fn=lambda: next(answers),
                         client_factory=factory, env_checker=lambda: True,
                         relay_ensurer=fake_ensurer)

    # ---- 路径 1：完整 happy path（含时长修改） ----
    bs = make_bootstrap([
        "确认",                    # 安全确认？—— 不，先修改时长
    ])
    # 重排：第一次安全确认时改时长，第二次确认
    bs = make_bootstrap(["时长改成 20 分钟", "确认",
                         "继续", "开始"])            # 先说错口令，再说"开始"
    ctx = bs.run()
    assert bs.safety.red.session_max_minutes == 20   # 时长修改生效
    assert bs.safety.state.name == "ACTIVE"          # 显式口令后授权
    assert ctx["devices"][0]["slotId"] == "slot-1"
    assert any("安全确认" in s for s in spoken)
    assert any("说'开始'正式启动" in s for s in spoken)
    assert relay_calls == ["ws://127.0.0.1:9998"]    # Relay 兜底已执行

    # ---- 路径 2：连接失败一次 → 引导 → 重试成功 ----
    spoken.clear()
    shared = FakeClient("ws://x")
    shared.fail_once = True

    def factory2(url):
        return shared

    cfg2 = json.loads(json.dumps(__import__("safety_layer").example_config()))
    cfg2["transport"] = {"url": "ws://x"}
    cfg2["wearer"] = {"age_verified_at": "2026-01-01T00:00:00"}
    tmp2 = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                       encoding="utf-8")
    json.dump(cfg2, tmp2, ensure_ascii=False)
    tmp2.close()
    answers2 = iter(["重试", "确认", "开始"])
    bs2 = Bootstrap(tmp2.name, speak_fn=spoken.append,
                    hear_fn=lambda: next(answers2),
                    client_factory=factory2, env_checker=lambda: True,
                    relay_ensurer=fake_ensurer)
    bs2.run()
    assert any("引导" in s for s in spoken)          # 失败后有连接引导

    # ---- 路径 3：缺年龄验证 → 授权被拒 ----
    cfg_tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
    bad_cfg = json.loads(json.dumps(__import__("safety_layer").example_config()))
    bad_cfg["transport"] = {"url": "ws://x"}
    bad_cfg["wearer"] = {"age_verified_at": None}
    json.dump(bad_cfg, cfg_tmp, ensure_ascii=False)
    cfg_tmp.close()
    answers3 = iter(["确认", "开始"])   # 正常走到最终授权才被拒
    bs3 = Bootstrap(cfg_tmp.name, speak_fn=lambda s: None,
                    hear_fn=lambda: next(answers3),
                    client_factory=lambda url: FakeClient(url),
                    env_checker=lambda: True,
                    relay_ensurer=fake_ensurer)
    try:
        bs3.run()
        raise SystemExit("FAIL: 未年龄验证不应通过")
    except SafetyViolation:
        pass  # authorize_start 拒绝，符合预期

    print("session_bootstrap self-test OK: all assertions passed")
