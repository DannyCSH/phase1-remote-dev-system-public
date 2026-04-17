# Phase 1 Remote Dev System

一个把手机 QQ、NanoBot、本地 Windows 环境、Claude Code 和 Codex 串起来的远程开发系统。

主链路：

`QQ -> NanoBot -> Phase 1 Router/Worker -> Claude Code -> Codex`

这个公开仓库提供的是可公开审阅的 GitHub 版本：

- 默认按“工作区优先、最小权限”运行
- 带桌面配置器，不要求用户一直手改 JSON
- 带 PowerShell 启动、状态检查、诊断、稳定性验证脚本
- 适合作为公开演示版、Beta 版、比赛审阅版

当前这版不是“客户装完就完全不用管”的最终商用版定位，更准确地说是：

- 可以公开发布
- 可以安装体验
- 可以做技术审阅
- 仍建议继续做更长时间的稳定性回归

## 这套系统能做什么

- 从手机 QQ 给电脑发开发请求
- 在本地维持项目上下文、会话和队列
- 让 Claude Code 负责主执行
- 让 Codex 负责拆解、复审和兜底
- 接收 QQ 附件
- 把结果文本或文件回传到 QQ
- 在必要时切到管理员中继，而不是默认全程高权限

## 公开版默认边界

这个仓库默认不是“给陌生 QQ 账号开全盘权限”的版本。

默认行为是：

- 优先限制在仓库目录和 `workspace\`
- 不默认开放整机文件搜索
- 不默认允许从任意目录向手机回传文件
- 不默认暴露你的本机运行态数据、会话日志和本地密钥

如果你确实需要放大权限，可以：

- 在桌面配置器里切换到“自定义目录模式”
- 在桌面配置器里切换到“整个计算机模式”
- 或手动编辑 `config\nanobot.local.json`

## 快速开始

### 1. 环境要求

- Windows 10 或 Windows 11
- PowerShell 5.1+
- Python 3.11 或 3.12，且带 `venv`
- Node.js 18+
- Rust 工具链
- Claude Code CLI 已登录
- Codex CLI 已登录
- 可用的 QQ 机器人凭据

### 2. 下载仓库

可以用这两种方式：

- `git clone <repo-url>`
- GitHub 页面 `Code -> Download ZIP`

建议把仓库放在稳定目录，例如：

`D:\YourFolder\phase1_remote_dev_system`

### 3. 一键初始化

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Bootstrap-Phase1.ps1
```

这一步会完成：

- 创建 `.venv`
- 安装 Python 依赖
- 在缺失时安装 `claude` / `codex`
- 生成本地配置文件 `config\nanobot.local.json`

### 4. 打开桌面配置器

固定双击入口：

`Open-Phase1Configurator.vbs`

如果你想看源码启动方式，也可以运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Phase1Configurator.ps1
```

桌面配置器是本地 `Tauri` 窗口，不是浏览器页面。

### 5. 填写最关键的本地配置

至少需要填这几个值：

- `QQ_APP_ID`
- `QQ_APP_SECRET`
- `QQ_OPENID`
- `MINIMAX_API_KEY`

模板文件是：

`config\nanobot.example.json`

你的本地私有配置文件是：

`config\nanobot.local.json`

不要把填好密钥的 `nanobot.local.json` 提交回公开仓库。

### 6. 启动链路

```powershell
claude login
codex login
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Phase1Stack.ps1
```

查看状态：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Get-Phase1Status.ps1
```

如果想先检查环境，再启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Check-Phase1Env.ps1
```

## 桌面配置器里建议怎么点

推荐顺序：

1. 填 `QQ App ID`
2. 填 `QQ App Secret`
3. 填 `授权 QQ OpenID`
4. 填 `MiniMax API Key`
5. 选择权限模式
6. 点“保存配置”
7. 点“检查环境”
8. 点“启动整套链路”
9. 点“获取状态”

## 第一次验收建议

至少做这 4 步：

1. 配置器能正常打开
2. `Get-Phase1Status.ps1` 能看到 gateway / worker 状态
3. 授权 QQ 能发进来一条简单消息
4. 手机端能收到“已收下任务”或最终结果

## 仓库结构

- `app/`：核心 router / worker / runtime / admin relay 代码
- `scripts/`：启动、检查、诊断、稳定性验证脚本
- `desktop-configurator/`：本地桌面配置器
- `config/`：配置模板
- `tests/`：Python 和 PowerShell 测试
- `docs/`：安装、部署、配置器和发布说明

## 已包含的发布前能力

- PowerShell 自动化测试
- Python 单元测试
- 健康探针快路径
- 诊断包脱敏导出
- 长稳脚本入口
- Win11 干净环境回归脚本入口

## 重要说明

- `runtime/`、本地配置、会话数据、日志都不应该提交到公开仓库
- 公开版默认偏保守，不默认给 QQ 开全盘搜索和全盘发文件
- 如果你要做更严格的客户级稳定性签收，仍建议补更长时间的连续实机测试

## 文档入口

- [GitHub 下载部署指南](./docs/PHASE1_GITHUB_DEPLOY_GUIDE_CN.md)
- [发布前检查清单](./docs/PHASE1_RELEASE_CHECKLIST_CN.md)
- [用户手册](./docs/PHASE1_USER_MANUAL_CN.md)
- [桌面配置器说明](./docs/PHASE1_DESKTOP_CONFIGURATOR_CN.md)
- [Codex 管理员宿主说明](./docs/CODEX_ADMIN_HOST_SETUP.md)
- [WSL 后续路线](./docs/PHASE1_WSL_NEXT_STEPS_CN.md)
