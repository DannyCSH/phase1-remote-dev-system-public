# Phase 1 GitHub 下载部署指南

这份文档按“第一次接触这个项目、并准备直接部署”的用户来写。

当前仓库不是演示壳子，而是可直接部署的公开发行版。

目标是让你从 GitHub 下载仓库后，按顺序完成：

1. 解压项目
2. 安装依赖
3. 填写本地密钥
4. 打开桌面配置器
5. 启动整套链路
6. 在手机 QQ 发第一条可验证消息

## 1. 先确认你的电脑环境

至少需要这些条件：

- Windows 10 或 Windows 11
- PowerShell 5.1+
- Python 3.11 或 3.12，并且带 `venv`
- Node.js 18+
- Rust 工具链
- Claude Code CLI 可登录
- Codex CLI 可登录
- 可用的 QQ 机器人凭据

如果你只是想先跑源码版桌面配置器，这些依赖是必须的。

## 2. 从 GitHub 下载项目

两种都可以：

1. `git clone <你的仓库地址>`
2. `Code -> Download ZIP`，然后解压

建议把项目放在一个稳定目录，例如：

`D:\YourFolder\phase1_remote_dev_system`

不建议放在桌面临时目录，也不建议反复换路径。

## 3. 一键初始化

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Bootstrap-Phase1.ps1
```

这一步会做几件事：

- 创建 `.venv`
- 安装 Python 依赖
- 在缺失时安装 `claude` 和 `codex`
- 生成 `config\nanobot.local.json`

如果你不想让脚本自动装全局 CLI，可以改用：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Bootstrap-Phase1.ps1 -SkipGlobalCliInstall
```

## 4. 填写你的本地密钥

你有两种方式。

### 方式 A：直接用桌面配置器

双击仓库根目录里的：

`Open-Phase1Configurator.vbs`

这是固定入口，不需要你手动先开终端。

第一次打开如果本机还没有配置器构建产物，脚本会先本地构建一次。
首次构建比后续打开慢，这是正常现象。

### 方式 B：手改本地配置

打开：

`config\nanobot.local.json`

至少需要填这几个值：

- `QQ_APP_ID`
- `QQ_APP_SECRET`
- `QQ_OPENID`
- `MINIMAX_API_KEY`

不要把填好密钥的 `nanobot.local.json` 提交回公开仓库。

## 5. 在桌面配置器里做什么

推荐顺序是：

1. 填 `QQ App ID`
2. 填 `QQ App Secret`
3. 填 `授权 QQ OpenID`
4. 填 `MiniMax API Key`
5. 选择权限模式
6. 点 `保存到 nanobot.local.json`
7. 点 `检查环境`
8. 点 `启动整套链路`
9. 点 `获取状态`

## 6. 权限模式怎么选

### 工作区模式

这是默认推荐模式。

特点：

- 只允许仓库和工作区内的内容
- 不默认开放整机搜索
- 不默认允许从任意目录把文件发回手机

适合第一次部署和公开版默认使用。

### 自定义目录模式

如果你只想放开少数目录，例如：

- `D:\Resumes`
- `D:\Pictures`
- `D:\Downloads`

那就用这个模式。

### 整个计算机模式

这个模式权限最大。

只建议在你完全清楚自己在做什么、并且 QQ 账号就是你自己可控账号时启用。

## 7. 登录 Claude 和 Codex

如果还没登录，执行：

```powershell
claude login
codex login
```

## 8. 启动链路

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Phase1Stack.ps1
```

然后查看状态：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Get-Phase1Status.ps1
```

如果你想先做一次全面环境检查，再启动，也可以先跑：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Check-Phase1Env.ps1
```

## 9. 第一次验收怎么做

建议至少做这 4 步：

1. 桌面配置器能正常打开
2. `Get-Phase1Status.ps1` 里 gateway / worker 都正常
3. 在手机 QQ 给授权账号发一条简单消息
4. 确认手机端至少能收到“已收下任务”或结果回复

## 10. 如果想做更严的发布验收

仓库里还提供了两类发布前验证入口：

- 长稳压测：`scripts\Start-Phase1StabilityRun.ps1`
- Win11 干净环境回归：`scripts\Invoke-Phase1Win11CleanRegression.ps1`

建议配合这份文档一起看：

- [发布前检查清单](./PHASE1_RELEASE_CHECKLIST_CN.md)

## 11. 需要记住的边界

- `config\nanobot.local.json` 是你的本地私有配置
- `runtime/` 是你的本机运行态目录
- 这两类内容都不应该提交回公开仓库
- 默认公开版是“工作区优先”的保守权限，不是默认全盘开放
