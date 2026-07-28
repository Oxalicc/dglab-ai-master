# DGLAB AI Master

> 测试版（Beta），锐意开发中。欢迎提 Issue、交 PR，或者单纯来聊聊。

让 AI Master 人格接管你的 DG-LAB 郊狼 3.0（Coyote 3.0 / DG-LAB 4 APP），开启一场沉浸式角色扮演 Session。控制权交给 AI，安全词永远留在你手里。

## 下载

👉 **[点击下载 dglab-ai-master.skill（v1.0.0）](https://github.com/Oxalicc/dglab-ai-master/releases/download/v1.0.0/dglab-ai-master.skill)**

其他版本见 [Releases 页面](https://github.com/Oxalicc/dglab-ai-master/releases)。`.skill` 本质上是个 zip 压缩包，不用解压，直接交给 AI 安装。

## 它是什么

一个可以装进本地 AI Agent 应用的 Skill 技能包。装上之后，对 AI 说一句"启动 dglab 主控 session"，AI 就会：

- 带你走完安全确认（安全词、强度红线、时长，全部说人话）
- 生成二维码，用 DG-LAB 4 APP 一扫即连
- 以 Master 人格临场演绎剧情，倒计时、忍耐考核、奖惩裁决每一局都不一样
- 全程被双向安全防线盯着——喊出安全词，绕开 AI 直送急停，宁可误停，绝不漏停

**你需要准备**：郊狼 3.0 设备 🐺 + 手机上的 DG-LAB 4 APP + 电脑上的本地 AI Agent 应用（如 Kimi 桌面版）。网页版聊天 AI 装不了 Skill，用不了。

## 安装 / 导入

> 第一次接触 Skill？强烈建议先读 [新手安装问答](https://github.com/Oxalicc/dglab-ai-master/wiki/Install-FAQ)，三分钟搞懂 Skill、平台、环境的关系。

三步走，全程用人话指挥 AI，不用碰命令行：

1. **装平台**：下载安装支持 Skill 的本地 Agent 应用（如 **Kimi 桌面版**），登录
2. **装 Skill**：在聊天里告诉 AI——"我在 ~/下载/dglab-ai-master.skill 下载了一个 skill，帮我安装"
3. **装环境**：再对 AI 说——"帮我检查这个 skill 的运行环境，缺什么装什么"。看到"环境就绪"就完成了

装完后新开一个对话，说"启动 dglab 主控 session"，按向导走完安全确认、扫码连接，等你明确说"开始"才进入正片。

## 使用前必读（安全）

- 本 Skill 涉及真实电刺激设备，**仅供成年用户用于双方自愿同意的角色扮演情景**
- 安全词是你最后的防线，但你不是只有这最后一道：APP 按钮（A 键主安全词 / F 键次安全词）、APP「屏蔽输出」开关、本地红线截断，一个失效还有别的
- 强度从最低开始，随时保持你能直接断电 / 摘设备
- APP 息屏会断连，请保持屏幕常亮；APP 端「自动增加」记得手动关掉

## 找到作者

- Email: oxalics@qq.com
- QQ: 793787351

有 bug 要报、有剧本要投、或者单纯想聊聊郊狼玩法，随便加，不用客气。

## 开发者 / 贡献者

运行架构、安全设计、模块说明、场景创作指南、贡献规范都已移入 Wiki：

📖 **[DGLAB AI Master Wiki](https://github.com/Oxalicc/dglab-ai-master/wiki)**

- [开发者指南](https://github.com/Oxalicc/dglab-ai-master/wiki/Developer-Guide) — 架构、状态机、项目结构、已知限制
- [安全设计](https://github.com/Oxalicc/dglab-ai-master/wiki/Safety-Design) — 七道防线的完整说明
- [参与贡献](https://github.com/Oxalicc/dglab-ai-master/wiki/Contributing) — Issue / PR / 写剧本 / 当小白鼠

## 项目动态

| 计划 | 状态 | 说明 |
|------|------|------|
| 负鼠（Possum）支持 | 规划中 | 与郊狼并列接入 |
| 多设备自动识别 | 规划中 | 插上自动识别型号与协议 |
| 多平台 agent 适配 | 进行中 | 目前已测试 Kimi，欢迎反馈其他平台表现 |

---

*安全词机制再完善，也不能替代现实中的沟通与信任。Play safe, have fun.*
