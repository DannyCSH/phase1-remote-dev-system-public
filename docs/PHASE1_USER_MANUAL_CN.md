# Phase 1 用户手册

## 1. 这是什么

这是一套把手机 QQ、NanoBot、Claude Code、Codex 串起来的本地远程开发系统。

你在手机 QQ 发需求后，系统会把任务送进本机，再由本地 Agent 链路处理：

`QQ -> NanoBot -> Claude Code -> Codex`

## 2. 安装前提

请先确认本机具备：

- Windows 10/11
- PowerShell 5.1+
- 官方 Python 3.11 或 3.12，并且自带 `venv` 模块
- Node.js 18+
- 可用的 QQ 机器人凭据
- 可联网安装 Python / npm 依赖

## 3. 一键初始化

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Bootstrap-Phase1.ps1
```

它会：

- 创建 `.venv`
- 安装 `nanobot-ai`
- 在缺失时安装 `claude` 和 `codex`
- 生成 `config\nanobot.local.json`

如果你只想装 Python 依赖，不想让脚本自动装全局 CLI，可以运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Bootstrap-Phase1.ps1 -SkipGlobalCliInstall
```

如果脚本提示“当前 Python 不能创建虚拟环境”，说明你现在指向的不是完整 CPython，或者缺少 `venv` 模块。请改用官方 Python 3.11/3.12 再运行。

## 4. 填写本地密钥

打开 `config\nanobot.local.json`，至少处理这几个值：

- `${QQ_APP_ID}`
- `${QQ_APP_SECRET}`
- `${QQ_OPENID}`
- `${MINIMAX_API_KEY}`

推荐做法有两种：

1. 直接把 `config\nanobot.local.json` 里的占位符改成真实值
2. 保留占位符，改为在系统环境变量里设置同名变量

不要把填好密钥的 `nanobot.local.json` 提交到公开仓库。

## 5. 登录 Claude Code 和 Codex

初始化后，再分别完成登录：

```powershell
claude login
codex login
```

如果命令不存在，先确认：

- `Bootstrap-Phase1.ps1` 是否已经跑完
- `node` / `npm` 是否可用
- npm 全局安装路径是否已经进入 PATH

## 6. 启动整套服务

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Phase1Stack.ps1
```

查看当前状态：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Get-Phase1Status.ps1
```

环境排查：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Check-Phase1Env.ps1
```

## 7. QQ 侧怎么用

正常情况下，你只需要在已授权 QQ 会话里发送自然语言任务。

适合的任务包括：

- 让 Agent 看代码、改代码、跑脚本
- 让 Agent 读取附件并继续工作
- 让 Agent 回传处理结果文件

## 8. 如果你想搜索电脑上其他目录里的文件

公开版默认不会直接开放整机搜索。

如果你确实需要“从 QQ 发自然语言，让 Agent 去电脑上找文件再发回手机”，请只对你自己的授权 QQ 号开启，并显式修改这些配置：

- `tools.restrictToWorkspace`
- `phase1.computerSearch.allowedRoots`
- `phase1.attachments.allowedRoots`
- `phase1.artifacts.allowedRoots`

建议先只开放少数明确目录，例如：

- `D:\Documents`
- `D:\Resumes`
- `D:\Pictures`

不要直接对所有陌生来源开放全盘搜索。

## 9. 常见问题

### Q1. QQ 发了消息没反应

先看状态脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Get-Phase1Status.ps1
```

重点看：

- gateway 是否 running
- worker 是否 running 或 idle
- queue 是否有积压
- `qq.enabled` 是否为 true

### Q2. Claude 或 Codex 找不到

先看：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Check-Phase1Env.ps1
```

再确认：

- `claude --version`
- `codex --version`
- `node --version`
- `npm --version`

### Q3. 想让管理员权限任务更稳定

看这份说明：

- [Codex 管理员宿主说明](./CODEX_ADMIN_HOST_SETUP.md)

## 10. 当前边界

- 当前公开版优先保证 Windows 原生链路可部署
- `WSL + tmux` 仍然是增强项，不是当前必需项
- 运行态目录 `runtime/` 属于本机状态，不应该提交到公开仓库
