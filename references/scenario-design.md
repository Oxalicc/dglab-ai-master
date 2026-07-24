# 情景剧本创作指南 + LLM 提示词契约

`scripts/scenario_engine.py` 的配套文档。场景（Scenario）是 AI Master 的
"剧本骨架"：引擎保证节奏与结构，LLM 负责把每个节拍演绎成情景化台词。

## 主导循环（AI 主动，而非被动应答）

```
感知（佩戴者输入 + 设备状态 + 场景进度 + 最近事件）
  → 引擎推进节拍（narrate/ask/countdown/endure/choice）
  → LLM 情景化表达（台词 + 可选设备动作 + 可选裁决）
  → 设备互动（全部过 clamp_command 钳制）
  → 佩戴者回应 → 引擎路由（答案评判/跟读/选择/插话）
  → 回到感知
```

主动性来自三处：
1. **引擎驱动**：节拍自动推进，AI 不需要等佩戴者开口。
2. **插话编织**：佩戴者无等待状态下的发言记入 `last_free_input`，
   注入下一个节拍的 LLM 上下文，AI 将其"编进剧情"（如"刚才谁在求饶？"）。
3. **设备作为互动媒介**：提问有奖惩、倒计时要跟读、忍耐有考核——
   郊狼的输出本身就是对话的一部分。

## LLM 提示词契约

引擎调用 `llm_fn(system_ctx, beat_ctx)`：

### system_ctx（人格层，来自 personas.md 选定人格）

建议内容：人格语气定义 + 主导者行为准则：
- 你是场景的主导者：主动提问、布置任务、预告下一步、点评表现。
- 台词口语化、简短（单条 ≤ 40 字），符合当前场景氛围。
- 可以宣布强度变化的"剧情语义"（如"升到 60"），但实际数值由
  引擎与安全层决定；不得承诺超出红线的行为。
- 安全词播报、状态查询回答不使用人格演绎。

### beat_ctx（引擎注入）

```json
{
  "scenario": "场景名", "persona": "人格id", "phase": "阶段id",
  "beat_type": "ask", "beat_hint": "该节拍的剧情提示",
  "wearer_last_input": "佩戴者最近一句话",
  "recent_events": [{"t": 12.5, "kind": "verdict", "data": "punish"}],
  "task": "judge | comment（可选，评判/点评模式）",
  "answer": "佩戴者的回答（judge 模式）"
}
```

### 期望返回（JSON）

```json
{
  "speak": "台词（必填）",
  "device": {"intensity": 40, "waveform": "PULSE", "duration_s": 3},
  "verdict": "reward | punish | neutral（仅 judge 模式）"
}
```

- `device` 为 `null` 或省略 = 不动设备；字段只允许 channel/intensity/
  intensity_delta/intensity_factor/waveform/duration_s，**出现其他字段
  整体丢弃**（引擎已强制），随后一律过 `clamp_command()`。
- `speak` 必须基于人格改述措辞、保持语义；**不得直接照搬 `beat_hint`**，
  hint 只是剧情意图说明，不是台词。
- LLM 异常/超时/格式错误 → 引擎回退到剧本内置 `line`，场景不中断。

## 场景 JSON 结构

```json
{
  "id": "training_course", "name": "训练课程",
  "closing_line": "下课。",
  "phases": [
    {"id": "warmup", "beats": [ ... ]},
    {"id": "exam", "beats": [ ... ]}
  ]
}
```

### 节拍类型与字段

**narrate** — 叙述/点评，说完自动推进：
`{type, line(回退台词), hint(给LLM的剧情提示), device?}`

**ask** — 提问应答：
```
{type, line, hint, timeout_s=20, retries=1,
 judge_mode: "llm"（缺省=关键词裁决）,
 reward_keywords: [...], punish_keywords: [...],
 judge: {reward: {device spec}, punish: {device spec}},
 answer_ack: "收到。", retry_line: "...", degrade_line: "..."}
```
超时铁律：**沉默绝不惩罚**——重问 retries 次后降级（BREATHING +
强度减半 + 安抚询问）。惩罚只针对"有回答但答错/反抗"。

**countdown** — 倒计时（互动核心）：
```
{type, line, from: 5, waveform: "PULSE",
 ramp: {start: 20, step: 5},       # 每秒 +5
 expect_echo: true,                # 佩戴者每步须跟读
 echo_miss_punish: {device spec},  # 漏跟惩罚脉冲
 echo_miss_line: "...", tick_line: "...(缺省=剩余秒数)", finish_line: "..."}
```

**endure** — 忍耐考核：
`{type, line, duration_s: 30, hold: {device spec}, comment_every_s: 10, finish_line}`

**choice** — 选择分支（选项必须是非控制类剧情选项）：
```
{type, line, timeout_s, retries,
 options: {"左边": {speak, device}, "右边": {...}}}
```

### device spec 合法字段

`channel`("A"/"B")、`intensity`(绝对值)、`intensity_delta`(相对)、
`intensity_factor`(倍率，如 0.5=减半)、`waveform`(内置波形名)、
`duration_s`。其他字段非法，引擎抛错（剧本）或丢弃（LLM）。

## 创作准则

1. **安全词唯一语义（最高准则）**：安全词及其变体**永不出现**在场景的任何字段中——台词、hint、选项名、closing_line 都不行。佩戴者在剧情里说出安全词会被 `classify()` 当成真实急停，且稀释安全词的唯一语义。剧情中的"确认口令"必须使用与安全词完全不同的词（如"汪""到""在"）。引擎加载时用 `forbidden_terms` 强制校验，违规场景直接拒绝运行。
2. **hint 不输出**：`hint` 仅供 LLM 理解剧情意图，绝不直接播报；`line` 是语义基线——LLM 必须基于人格**改述措辞、保持语义**，不得照搬 hint（回退模式下才逐字播报 line）。
3. **节奏**：单场景 4-8 个节拍分 2-3 阶段；高强度段落（countdown/endure）
   之间必须有 narrate 缓冲。
4. **数值克制**：剧本里的强度一律视为"剧情意图"，反正会被钳制层截断；
   但写得越贴近红线内合理值，剧情与实际感受越一致。
5. **惩罚有据**：punish 只挂在 CONTROL_WORD（安全层路由）、答错
   （judge）、漏跟读（echo_miss）三类明确事件上。
6. **安全出口可见**：场景不得包含任何暗示"安全词无效"的台词；
   degrade_line 必须传递关心而非嘲讽。
7. **复用人格**：场景的 `hint` 写给 LLM 看，语气由人格决定，不要在
   hint 里重复人格定义。
8. **技法与话术取材**：惩罚节拍的设计优先取材 `references/playbook.md`
   （脉冲间歇 5~10s、高频峰值、归因/呼吸/计数/衔接四类话术语义模板），
   剧本只写语义与数值意图，口吻交给 LLM 按人格演绎。
