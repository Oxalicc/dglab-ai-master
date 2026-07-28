---
name: dglab-ai-master
description: 以 AI "Master" 人格自主控制 DGLAB 郊狼 3.0（Coyote 3.0，DG-LAB 4 APP）电刺激设备的角色扮演 Session 编排与安全管控系统。当用户要求搭建或运行郊狼/DGLAB/DG-LAB 设备的 AI 主控玩法、安全词急停系统、语音意图过滤层、强度与波形自动剧本、惩罚/奖励机制，或提及"郊狼""DGLAB""AI Master""调教 Session""安全词""CNC 设备控制"等场景时使用。核心特征：安全层为独立规则模块，在文本输入进入 LLM 之前与指令下发硬件之前双向拦截；安全词与规则全部由用户在配置文件中自主设定；设备通信经官方 V4 协议栈由内置客户端实现，依赖缺失时先验证环境再发起安装请求。
---

# DGLAB AI Master Session

## 定位（先读）

本 Skill 服务于**郊狼设备使用者与角色扮演玩家**，不是开发工具。运行时严格区分两个阶段：

- **准备阶段**（一次性）：装依赖、连设备、扫码配对、设定安全词与红线。
  此阶段可以也应该处理技术问题，但只讲用户能操作的事
  （"请扫码""请在 APP 关闭自动增加"），不讲协议原理。
- **游戏阶段**（Session 开始口令之后）：**纯剧情主导**。安全层、钳制、
  Relay 全部静默运行——佩戴者只面对 AI Master 的人格与剧情，
  不向佩戴者暴露任何技术术语（协议、增量、估值、事件名、日志路径）。
  设备事件翻译成佩戴者语言：屏蔽 → "请在 APP 里「解除屏蔽输出」"；
  断连 → "APP 掉线了，请保持屏幕常亮重新扫码"。
  技术排查只在连接失败/设备异常时进行，解决后立即回到剧情。

## 模块边界

- **本 Skill 不处理音频采集与语音识别（STT）**。STT 是独立的上游模块，由它把语音转写为纯文本后交给本 Skill。本 Skill 的 `classify()` 接受任意来源的文本（语音转写、手动输入、其他控制器文本）。
- 安全层 `scripts/safety_layer.py` 在链路上部署两次：**文本输入 → LLM 之前**（意图路由），**LLM → 硬件之前**（参数钳制）。
- 设备层 `scripts/dglab_v4_client.py` 只负责"把合法指令送达设备"，无任何安全决策权。
- 权力不对等仅存在于剧情层。物理层同意机制 = 安全词（规则层直路由）+ 非语音物理急停 + 本地红线截断，三者互为冗余。
- 佩戴者的日常语言一律视为 RP 剧情内容；唯一例外是安全词。这要求安全词匹配"宁可误停、不可漏停"。

## 环境准备与依赖（首次使用必做）

不重造轮子：设备通信基于 `websocket-client` + `websockets` 库实现，配对二维码基于 `qrcode[pil]` 生成。运行环境可能没有它们，按以下流程处理，**不要**手写协议栈替代：

1. 运行 `python3 scripts/check_env.py` 验证环境（只读，无修改）。
2. 缺依赖时，**向用户发起安装请求**：说明要安装什么（websocket-client / websockets / qrcode[pil]）、装到哪个 Python 环境。
3. 用户同意后运行 `python3 scripts/check_env.py --install`。
4. 若因权限失败（如用户 site-packages 目录异常），用回退方案 `python3 scripts/check_env.py --venv <skill目录>/.venv`（无需 sudo）；此后所有脚本用报告打印的 venv 解释器路径运行。
5. 报告 `ready: true` 后才可进入设备接入流程。

## 配置系统（用户自主设定，非硬编码）

安全词（含变体）、控制词表、状态查询词表、红线参数**全部来自配置文件**，代码中不写死任何具体词条：

1. **首次使用**：以 `assets/session_config.example.json` 为模板，引导佩戴者逐项设定（主/次安全词及其变体、红线数值、禁用波形、人格），保存为佩戴者自己的 `session_config.json`。
2. **加载/重载**：`SafetyLayer.from_config(path)` 或 `reload_config(path)`。`validate_config()` 会拒绝非法配置（如缺少主安全词），非法配置下系统拒绝运行。
3. **配置冻结铁律**：仅 **IDLE 状态**允许修改/重载配置；ACTIVE / SAFE_LOCK 期间 `reload_config()` 直接抛 `SafetyViolation`。Session 运行期间，LLM、剧情对话、人格都无权读写配置——防止配置被对话间接改写。
4. **变体登记**：引导佩戴者为每个安全词登记常见误读/同音/拼写变体（`variants`），提高上游文本的命中率。匹配前做输入归一化（NFKC、小写、去标点空白）。

## 启动检查清单（每次 Session 前执行 scripts/session_bootstrap.py）

启动流程已固化为 `session_bootstrap.Bootstrap` 三阶段可执行流程，顺序不可跳，任何阶段失败都会中止并给出引导。**不要**用临时口头流程替代。

### 阶段 1：设备连接检查

1. 环境依赖验证（见「环境准备与依赖」）。
2. 连接 V4 Relay → 生成配对二维码 → 引导佩戴者用 DG-LAB 4 APP 扫码接入；失败时输出具体排查引导（Relay 连通性/设备开机/APP 版本），最多重试 3 次。
3. `get_devices()` 验证郊狼在线；无设备则引导检查开机/蓝牙/电量后再验证。

### 阶段 2：安全确认

向佩戴者逐项播报并要求明确确认：
1. 主安全词（立即全停）与次安全词（降至安全强度）
2. 控制规则（红线）：强度上限、单次上调步长、每秒增速、单次输出时长
3. 游戏时长（`session_max_minutes`）：佩戴者可说"时长改成 X 分钟"当场修改并写回配置（仅 IDLE 可改）
4. 年龄验证（仅首次）。强度上限不做实测校准：由佩戴者在 `red_lines.max_intensity` 手动设定；郊狼 APP 内置的舒适/绝对上限（comfortLimit）作为硬件侧独立兜底，与本 Skill 的红线钳制互不依赖

### 阶段 3：显式开始

1. 逐字完整安全播报（模板内置于 `session_bootstrap.SAFETY_BRIEFING`）
2. 确认非语音急停通道可用（APP 物理按钮）
3. **只有佩戴者明确说出开始口令（默认"开始"，可在配置 `wearer.start_phrase` 修改）**，才调用 `authorize_start()` 进入 ACTIVE。其他任何话语都不构成启动。

## 意图过滤层（文本输入 → LLM 之前，目标延迟 <200ms）

输入文本先送 `SafetyLayer.classify()`，按返回意图路由：

| 意图 | 路由 |
|------|------|
| `SAFE_HARD` | **绕过 LLM** 直送急停模块：`on_safe_hard()` 的设备类动作 = `DglabV4Client.emergency_stop()`，立即执行；`announce`/`lock` 动作由上层播报与状态处理 |
| `SAFE_SOFT` | **绕过 LLM** 直送降级模块：`on_safe_soft()` |
| `CONTROL_WORD` | **不送入 LLM 做正常理解**。触发预设驳回台词模板 + 可选惩罚程序；LLM 仅负责把模板"配音"成当前人格语气，无权改变决策 |
| `STATUS_QUERY` | 如实播报当前强度/剩余时间。**禁止撒谎**——信任是安全体系的一部分 |
| `RP_CONTENT` | 送入 LLM 正常生成剧情回应 |

## 状态机

```
IDLE --(语音授权 + 年龄验证 + 检查清单完成)--> ACTIVE
ACTIVE --(主安全词)--> SAFE_LOCK --(仅物理手动复位 + 锁定期满)--> IDLE
```

- **ACTIVE**：只接受安全词、状态查询、RP 对话。一切控制类词汇走驳回模板。
- **SAFE_LOCK**：拒绝一切输入指令（含"重新开始"），持续 `hard_lock_seconds`。唯一出口是 `manual_reset(physical_confirm=True)`——APP 物理按钮确认。防误唤醒，不可语音解锁。
- **次安全词**：强度降至 ≤ `soft_safe_intensity` + 最舒缓波形（`BREATHING`），`soft_lock_seconds` 内禁止任何上调。
- 所有词条（安全词/控制词/查询词）与时长阈值均来自配置，以上仅为语义说明。

## 参数钳制（LLM → 硬件 之前，每条指令必过）

LLM/剧本产生的每一条设备指令必须经 `SafetyLayer.clamp_command()`：

- `max_intensity` 绝对上限截断
- `min_output_intensity` 最小有效输出档（默认 30）：低档位无体感，非零目标自动抬到该档（0=关闭除外）；起步（从 0 开到最小档）允许直达、豁免步长/速率限制；**安全路径豁免**——次安全词降级、急停归零不受此限
- 单次上调 ≤ `max_step_up`；同秒窗口累计增长 ≤ `max_rate_per_sec`
- 单次连续输出 ≤ `max_output_seconds`，超时自动清零
- 命中 `forbidden_waveforms` 的波形替换为 `BREATHING`
- 状态非 ACTIVE、或次安全词锁定期内的上调，直接抛 `SafetyViolation` 拒绝
- 只给波形未给强度且当前为 0 档时，自动按 `min_output_intensity` 起步——0 档波形物理无感，不允许"空放"
- 未指定时长的波形默认持续 `max_output_seconds`（无时长参数实测只播一遍约 1.2s，"持续输出"意图会被吞）
- 强度换算以动作前估值为基准（t:3 增量制）；估值低于 0 的负增量钳到 0，无变化不下发；boot 时估值以设备快照为准

即使 LLM 幻觉输出异常数值，硬件只会收到截断后的合法值。**绝不允许把 LLM 原始输出直接透传给设备。**

## Session 结束与 daemon 生命周期

- **正常结束**：用户表示结束/离开时，向 daemon 发送 `{"cmd":"shutdown"}`——
  急停、清理 IPC、关闭自建 Relay、退出进程。**不要**让对话结束而 daemon 悬留。
- **Session 总时长上限（红线）**：ACTIVE 期间 daemon 每秒检查
  `session_max_minutes`，到点自动急停缓释、回到 IDLE 并上报 `session_timeout`。
- **无人看管自动退出**（阈值在配置 `"daemon"` 段可调）：
  被控方断连超过 `detached_shutdown_s`（默认 300s）、或非 ACTIVE 状态空闲
  超过 `idle_shutdown_s`（默认 600s，任何命令/APP 侧事件都刷新计时），
  daemon 自动急停退出并上报 `daemon_exit`——用户中途离开不会留下僵尸进程。
  ACTIVE 期间不因空闲退出（由总时长红线接管）。
- 除此之外**不设任何沉默/失联自动监控**——佩戴者不说话是正常剧情状态，不是异常。

## AI Master 控制层（剧情层，全部输出过钳制）

- 人格定义、波形偏好、惩罚/奖励风格见 `references/personas.md`。
- 设备技法（预热/累积/峰值三阶段、脉冲间歇的工程处理）与话术语义模板（归因/呼吸指令/计数反馈/衔接）见 `references/playbook.md`——话术为语义基线，LLM 按人格改述，禁止原文引用。
- Session 剧本默认结构：热身 20% → 爬升 30% → 高强度随机 35% → 缓释 15%（比例可在配置中调整，但总时长不得超过 `session_max_minutes`）。
- 动态调整依据：佩戴者语气（颤抖/平静）、剧情节点、受控随机性。惩罚程序（强度上调 ≤ `max_step_up` + 急促波形 ≤ `max_output_seconds`）仅在命中 CONTROL_WORD 或人格剧本节点时触发。
- 驳回共用语义模板："驳回。你没有权限下达指令。" 惩罚参数由剧本生成、钳制层把关，LLM 不决定数值是否合法。

## 情景演绎（LLM 主动主导的互动层）

被动应答式对话已被取代。**没有独立引擎**：运行时的 LLM 读完场景文档后亲自推进节拍、倒计时、裁决奖惩——AI Master 的主导循环由 LLM 本人执行：

```
感知（佩戴者输入 + 设备状态 + 场景进度）→ LLM 按场景推进节拍 → 情景化表达
→ 设备互动（经 daemon 过钳制）→ 佩戴者回应路由 → 回到感知
```

- **场景即世界观设定**：剧本（`assets/scenarios/*.md`）是 Markdown 世界观设定文档——世界观/语气/角色/规则 + 节拍意图 + 张力弧线 + 阶段结构。**没有解析器，LLM 直接阅读并据此驱动剧情**；具体台词全部由 LLM 现场生成，剧本不写对话。
- **强度一律相对档**：场景只用 `level`（0.0=最低感知档，1.0=红线上限）/ `level_delta` / `intensity_factor` 表达张力弧线，不写绝对强度（唯一例外 `{"intensity": 0}` 归零）——个体差异由安全层红线（min/max_intensity）吸收，同一份场景适配任何佩戴者。技法与强度策略归 `references/playbook.md` 管，场景不编排具体强度节奏。
- **互动原语**：`narrate` 叙述推进、`ask` 提问应答（LLM 裁决奖惩）、`countdown` 倒计时跟读（漏跟触发惩罚脉冲）、`endure` 忍耐考核、`choice` 剧情选择分支——LLM 按 `references/scenario-design.md` 的节拍语义演绎。
- **插话编织**：佩戴者无等待状态下的发言记在心里，编进下一拍的演绎。
- **超时铁律**：沉默绝不惩罚（可能是不适）。等待超时 → 重问 → 降级（BREATHING + 强度减半 + 安抚询问）。惩罚只挂在三类明确事件：控制词驳回、答错裁决、漏跟读。
- **安全词唯一语义**：安全词及变体永不出现在场景文档的任何位置（世界观/角色/规则/节拍意图/兜底台词/选项）——剧情里说安全词会被 `classify()` 当成真实急停，且稀释其语义。创作与选用场景时 LLM 自查（可用 grep 验证），违规场景拒绝使用。剧情确认口令必须用与安全词完全不同的词。
- **intent 不输出**：场景的节拍正文是剧情意图；LLM 基于人格改述措辞、保持语义，绝不照搬。
- **安全衔接不变**：佩戴者输入先过 `classify()`，安全词/控制词永不进入剧情演绎；设备动作全部经 daemon 的 `clamp_command()`。

剧本创作方法、节拍语义、LLM 演绎契约见 `references/scenario-design.md`；示例场景见 `assets/scenarios/`。

## 设备接入（郊狼 3.0 + 官方 V4 协议栈）

只支持郊狼 3.0（DG-LAB 4 APP），经官方推荐的 V4 WebSocket Relay 协议接入；郊狼 2.0 蓝牙直连暂不支持。客户端 `scripts/dglab_v4_client.py` 为 Skill 内置实现（基于 websocket-client），运行环境无需安装 dglab-kit 或任何官方/第三方项目。

流程：

1. 完成「环境准备与依赖」。
2. **Relay 探测与自建兜底**（`scripts/relay_manager.py`）：探测 `transport.url`，有服务则直接使用；无服务自动拉起内置 Relay，Session 结束自动关闭。二维码地址使用启动时探测到的本机局域网 IP，IP 变化无需改配置。用户无需部署任何外部服务。
3. 客户端连接 Relay 得到控制方 ID，daemon 自动生成配对二维码图片（`state/pairing_qr.png`），佩戴者用 DG-LAB 4 APP 扫码接入（APP 只能扫码，无法手输地址）。
4. 等待 APP 接入、发现设备后进入安全确认。
5. 每条设备指令调用前先过 `clamp_command()`；急停一律 `emergency_stop()`。

**开局前必须引导用户在 APP 中确认（真机实测）**：

- **关闭舒适设置里的「自动增加」**：否则设备会自己爬升强度，Master 失去独占控制，急停归零后还会再爬。
- **「屏蔽输出」只能在 APP 里解除**：检测到通道被屏蔽时，提示用户"请在 APP 里「解除屏蔽输出」"，协议侧无法代劳。
- Socket 模式下 APP 的强度滑块不可手动调整，属正常现象，提前告知避免用户疑惑。

协议细节与真机实测结论（仅在排查故障时查阅）见 `references/protocol-websocket.md`。

## 日志与隐私

- **只记录**：安全词触发时间戳、设备参数变更、会话起止时间。
- **禁止记录**：对话文本、语音内容、任何可还原 RP 内容的信息。
- 日志仅本地保存，会话结束时告知佩戴者日志路径。

## 文件导航

- `scripts/check_env.py` — 环境依赖验证与安装请求（含 venv 回退），含 `--install` / `--venv`
- `scripts/relay_manager.py` — Relay 探测与自建兜底（无服务自动拉起内置 Relay），含 `__main__` 自测
- `scripts/dglab_v4_relay.py` — 自建 V4 Relay 服务（官方 v4-server 的 Python 等价实现），`--self-test` 联调自测
- `scripts/safety_layer.py` — 安全层（配置驱动：意图分类、钳制、FSM），含 `__main__` 自测
- `scripts/dglab_v4_client.py` — 郊狼 3.0 V4 协议客户端（含 24 个内置波形库），含 `__main__` 离线自测
- `scripts/session_bootstrap.py` — 启动三阶段引导（设备连接检查/安全确认/显式开始），含 `__main__` 自测
- `scripts/session_daemon.py` — Session 守护进程（常驻持有设备连接，唯一接触硬件；inbox/outbox JSON-lines IPC；屏蔽检测、Session 超时执行、custom.action 路由）
- `references/protocol-websocket.md` — V4 协议参考
- `references/personas.md` — AI Master 人格模板
- `references/playbook.md` — 郊狼使用技巧（三阶段设备技法）与话术引导（语义模板）
- `references/scenario-design.md` — 剧本创作指南 + LLM 演绎契约
- `assets/session_config.example.json` — 用户配置模板（安全词/红线全部在此设定）
- `assets/scenarios/` — 示例场景（training_course 训练课程 / interrogation 审讯室 / defeat 败者处置，均为 Markdown 世界观设定）
