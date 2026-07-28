# DGLAB V4 协议参考（郊狼 3.0 / DG-LAB 4 APP）

本文档是 `scripts/dglab_v4_client.py` 的协议背景说明，**不是运行时依赖**——
客户端已在 Skill 内实现，此处仅供排查问题与扩展时查阅。
协议来源：官方 dglab-websocket-server 与 dglab-kit（均为 GPL-3.0）。

## 拓扑与 Relay

```
控制端（本 Skill） <--WebSocket--> V4 Relay 服务 <--WebSocket--> DG-LAB 4 APP（被控方）
```

- V4 支持 1 控制方 : N 被控方，默认端口 9998。Relay 默认由 Skill 自建
  （绑定 `0.0.0.0`，二维码/连接使用启动时探测到的本机局域网 IP，
  DHCP 漂移无需改配置）；配置指向既有 Relay 时探测可用则直接使用
- `data` 为业务透传层，服务端只关心外层 `type` / `clientId` / `data`，不解析设备指令

## 配对流程

1. 控制方连接 `ws://host:9998`（不带 tid）→ 服务端回 `{"type":"hello","clientId":"控制方ID"}`
2. 生成配对二维码内容：
   `https://dungeon-lab.cn/s/?v=1&action=socket&url=<urlencode(ws地址 + (?|&)tid=控制方ID)>`
   （已有 query 的地址用 `&` 拼接 tid）→ APP 扫码接入
3. 控制方收到 `{"type":"client_attached","clientId":"被控方ID"}` = 配对完成

## 帧类型（服务端 → 控制方）

| type | 含义 | 处理 |
|------|------|------|
| hello | 连接建立，含控制方 clientId | 保存 ID |
| client_attached | 被控方接入 | 记录被控方 ID |
| client_disconnected | 被控方断开 | 终止待响应指令 |
| heartbeat | 服务端心跳 | 无需回复 |
| pong | 应用级 ping 响应 | 重置 miss 计数 |
| idle_timeout | 控制方空闲超时，随后断开 | 报错 |
| error | 业务错误 {code, message?} | 报错 |
| message | 业务消息 {clientId, data} | 见 RPC/事件 |

错误码：`bad_request` / `client_not_found` / `controller_not_found`。
断开码：4000 控制方断开 / 4001 控制方不存在 / 4002 空闲超时。

## 服务端级应用 ping

控制方每 2s 发 `{"type":"ping"}`，服务端回 `{"type":"pong"}`；连续 3 次未收 pong 判定断连并关闭（与 dglab-kit 行为一致）。

## RPC（data 负载内）

请求：`{"t":"req","reqId":"1","m":"方法","data":{...}}`，经外层
`{"type":"message","clientId":"被控方ID","data":<请求>}` 发送。
响应：`{"t":"resp","reqId":"1","result":...}` 或 `{"t":"resp","reqId":"1","error":"..."}`。
默认响应超时 5s，封顶 8s。**实测：t:0 波形指令的确认帧会延迟到波形任务
结束才返回**（d=60s 时 8s 内必无响应），指令本身已送达设备；客户端按
reqId 匹配响应，迟到的确认会被安全丢弃，因此长波形按"发后不管"处理，
超时仅记日志不算失败。

方法：

| 方法 | data | 说明 |
|------|------|------|
| devices.get | 无 | → `{devices:[{slotId,name,type}]}` |
| device.op | 见下 | 设备指令 |
| device.op.clear | `{s,c?}` 或不传 | 清理任务：不传 s 清全部，传 c 清指定通道 |
| ping | 无 | 被控方连通性检查 |

## device.op 指令格式

```json
{"s":"slotId","c":0,"t":7,"v":20,"p":0,"d":3000,"im":true}
```

| 字段 | 含义 |
|------|------|
| s | 设备 slotId（来自 devices.get） |
| c | 通道：0=A，1=B |
| t | 动作：0=裸波形，3=相对增减强度，4=临时强度，7=绝对强度 |
| v | 值：强度数值 / 波形帧数组 |
| p | 优先级 0/1/2（可选） |
| d | 持续时间 ms（t=4 必填；t=0 可选） |
| im | 是否替换同类任务（可选） |
| ver | 波形数据版本：2=十六进制帧（本 Skill 使用），3/省略=V3 帧 |

**真机实测（DG-LAB 4 APP + 郊狼 3.0，2026-07）**：

- **t:7 绝对赋值 v>0 被拒**（`invalid_operate`），仅 `v:0` 归零可用；
  强度控制一律用 **t:3 相对增减**（官方 SDK 惯用路径也是 reset/add/reduce），
  本 Skill 内部把"目标值→增量"自动换算。
- **t:5 SetMute 两个方向均被拒**。「屏蔽输出」是 APP 本地功能，协议无法
  解除，只能通过 `slotState.channelX.isMuted` 检测并提示用户在 APP
  「解除屏蔽输出」。
- **无波形输出时强度自动衰减**（约 1 档/秒直至 0）。正确控制顺序：
  先下波形（带时长或持续喂），再调强度；无 `d` 的波形只播一小段。
- **`slotState.channelX.comfortLimit.autoIncr` 是硬件「强度保护」的上限自适应提升**，
  不是 APP 强度自适应里的「自动增加」（2026-07 实机对照实验确认）：它只让
  强度**上限** `intensityMax` 随输出时长自适应上调（`totalIncr` 累计提升次数，
  约每 10s +1），**不会自行爬升输出强度**，不构成控制权争夺；APP 侧开关
  状态变化不会实时推送到 socket（comfortLimit 仅接入时快照下发），开局前
  看到的快照值可能是历史残留，勿据此误判 APP 设置未生效。
- **APP 强度自适应「自动增加」会与 Master 争夺控制权**（与上面的
  comfortLimit.autoIncr 是两个不同功能）：设备自行爬升强度，急停归零后
  仍会再爬。Socket 模式开局前必须让用户在 APP 关闭「自动增加」。
- Socket 模式下 APP 的强度滑块不可手动调整（仅能协议控制），属正常。
- APP 热身机制 `warmUpScale` 从 0 缓爬，刚解除屏蔽后短暂输出偏弱属正常。

急停映射：`device.op.clear`（不传 s）+ 每设备双通道 `t:7, v:0, im:true, p:0`。

## 波形帧编码（ver=2）

- 每帧 = 16 位十六进制字符串 = 8 字节 = `[频率×4][强度×4]`，按 100ms/tick 消费
- 24 个内置波形已内置于客户端 `WAVEFORMS`（BREATHING/PULSE/TIDE/RIPPLE/HEARTBEAT/SIGNAL/TEMPO_TAP 等，含中文名）
- 新波形覆盖旧任务可用 `im:true`

## 事件（被控方上报，data 内 `t:"ev"`）

| ev | 含义 |
|----|------|
| devices.snapshot | 全量设备列表（slotState 直接挂在设备对象上） |
| devices.patch | 设备增删 {added?, removed?} |
| slots.patch | 设备属性/插槽状态增量（slots 列表项含 props/slotState；props.intensityA/B 为设备真实强度，slotState.channelX.isMuted 为屏蔽状态） |
| custom.action | 被控方自定义动作 0-9，**APP 界面显示为字母 A~J**（A=0 … J=9）。A=主安全词、F=次安全词为硬编码唯一语义；其余字母语义由配置 custom_actions 定义，仅为 RP 意图 |
