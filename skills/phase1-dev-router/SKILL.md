---
name: phase1-dev-router
description: Route development and system tasks into the local Claude/Codex worker instead of solving them inside NanoBot directly.
always: true
---

# Phase 1 Dev Router

在 QQ 频道里，只要用户提出下面这些内容，就优先走本地 Phase 1 路由，而不是在 NanoBot 里直接把整件事做完：

- 写代码
- 改文件
- 调试
- 配置环境
- 跑脚本
- 自动化
- 插件安装
- 文档落地到项目
- 系统级排障

## 路由原则

如果当前 `Channel: qq`，并且请求属于开发 / 配置 / 系统任务：

1. 在 `runtime/inbox/` 下创建任务文件。
2. 任务 JSON 至少写这些字段：

```json
{
  "task_id": "qq-20260414-210000",
  "channel": "qq",
  "chat_id": "<Chat ID from runtime context>",
  "sender_id": "<same as chat_id for QQ C2C if no better value is available>",
  "message_id": "<message id if available>",
  "session_key": "qq:<chat_id>",
  "project_id": "",
  "project_root": "",
  "attachments": [],
  "metadata": {},
  "user_request": "<copy the user request verbatim>",
  "received_at": "<Current Time from runtime context>"
}
```

3. 如果运行时上下文里能拿到附件信息，就把它们也写进来：

- 优先写入 `attachments`
- 同时保留 `metadata.attachments`
- 每个附件尽量带本地绝对路径

可接受的附件结构示例：

```json
[
  {
    "path": "${PROJECT_ROOT}\\runtime\\media\\qq\\inbound\\example.png",
    "name": "example.png",
    "kind": "image"
  }
]
```

4. 然后调用下面这个命令：

```text
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\Launch-Phase1Task.ps1" -TaskFile ".\runtime\inbox\<taskid>.json" -Config ".\config\nanobot.local.json"
```

5. 看命令输出里的 `ACTION=`、`REPLY=` 和 `REPLY_B64=`：

- 如果有 `REPLY_B64=`，优先按 UTF-8 Base64 解码后再回复用户
- 如果只有 `REPLY=`，把它当成单行预览或兼容回退
- 如果 `ACTION=enqueued`，但没有更具体内容，就回复：
  `已收下任务，正在按当前项目和会话排队处理，后续进度和结果会继续回到 QQ。`

## 控制命令

下面这些短命令也走这个技能，但优先按命令语义处理，不要当成普通开发需求：

- `切到项目：xxx`
- `新任务：xxx`
- `继续当前任务`
- `总结当前状态`
- `停止当前任务`
- `重置当前会话`

规则：

- `总结当前状态`
  直接运行 `Launch-Phase1Task.ps1`，优先取 `REPLY_B64=`，解码后回给用户；没有的话再退回 `REPLY=`。
- `停止当前任务`
  也是走 `Launch-Phase1Task.ps1`，优先取 `REPLY_B64=`，解码后回给用户；没有的话再退回 `REPLY=`。
- `切到项目 / 重置当前会话 / 继续当前任务`
  走 `Launch-Phase1Task.ps1`，优先返回 `REPLY_B64=`，没有的话再退回 `REPLY=`。

## 不要路由的情况

以下情况默认不要走后台 worker：

- 简单身份问题
- 闲聊
- 只是问当前模型是谁
- 很小的纯问答问题，明显不需要 Claude Code / Codex

## 重要要求

- 不要再发旧的机械回复 `Received. Working on it.`
- 不要把 shell 原始输出整段丢回给用户
- 对于开发类任务，后台 worker 才是权威执行路径
- 如果 QQ 消息里包含文件或图片，不要忽略；要尽量把本地下载路径透传进任务 JSON
