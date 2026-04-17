# Phase 1 桌面配置器说明

## 它是什么

这是公开版新增的本地桌面配置器。

它的目标不是替代 `Phase 1` 主系统，而是把最容易劝退新用户的几步做成一个原生窗口：

- 填写 `QQ_APP_ID`、`QQ_APP_SECRET`、`QQ_OPENID`、`MINIMAX_API_KEY`
- 切换“只限工作区”或“放大到自定义目录”
- 一键读取当前配置
- 一键保存 `config\nanobot.local.json`
- 一键检查环境
- 一键启动整套链路
- 一键读取当前运行状态

## 它不是浏览器页面

这个配置器使用 `Tauri` 做桌面壳。

也就是说：

- 打开后是本地桌面窗口
- 前端界面是内嵌在本机 WebView2 里的
- 配置保存和脚本调用都在本地完成
- 默认不会把你的配置自动发到远程网页

## 当前版本的定位

当前仓库里提供的是“源码运行版”。

这意味着首次使用它时，机器上需要额外准备：

- Windows 10 或 Windows 11
- PowerShell 5.1+
- Node.js 18+
- Rust 工具链（含 `cargo`）
- Python 3.11 或 3.12，且必须带 `venv`
- 已安装并可运行的 `Claude Code CLI`
- 已安装并可运行的 `Codex CLI`

后续如果做 GitHub Releases，可以再额外发布预编译安装包。到那时，普通用户就不需要自己装 Node 和 Rust 了。

## 启动步骤

### 1. 先完成主系统基础依赖

在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Bootstrap-Phase1.ps1
```

这一步会优先处理 Python 虚拟环境和 `nanobot-ai` 依赖。

如果你的 `claude` 或 `codex` 还没准备好，也请先确保下面两个命令能正常运行：

```powershell
claude --version
codex --version
```

### 2. 打开桌面配置器

最推荐的固定打开方式是直接双击仓库根目录里的：

`Open-Phase1Configurator.vbs`

这个入口会在后台调用配置器启动脚本，不需要你先打开终端窗口。

如果你想看源码启动方式，也可以在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Phase1Configurator.ps1
```

第一次运行时，它会在 `desktop-configurator\` 目录里执行一次 `npm install`。

后续如果你只是想直接打开，可以加：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Phase1Configurator.ps1 -SkipInstall
```

### 3. 在窗口里填配置

重点先填这几项：

- `QQ App ID`
- `QQ App Secret`
- `授权 QQ OpenID`
- `MiniMax API Key`
- `默认模型`

然后点击：

`保存到 nanobot.local.json`

保存后，桌面配置器会把内容写入：

`config\nanobot.local.json`

如果原文件存在，还会先生成一份 `.bak` 备份。

### 4. 先做环境检查

点击：

`检查环境`

它实际调用的是仓库里的：

`scripts\Check-Phase1Env.ps1`

你会在右侧输出窗口里看到当前检测结果。

### 5. 启动整套链路

点击：

`启动整套链路`

它实际调用的是：

`scripts\Start-Phase1Stack.ps1`

### 6. 看当前状态

点击：

`获取状态`

它实际调用的是：

`scripts\Get-Phase1Status.ps1`

右侧会显示脚本输出，状态卡片也会同步更新。

## 权限模式怎么理解

### 工作区模式

这是公开版默认推荐模式。

特点：

- `tools.restrictToWorkspace = true`
- 主要允许仓库根目录和 `workspace\`
- 不默认放开整机搜索
- 不默认允许从陌生目录直接取文件回传

适合：

- 第一次部署
- 公开仓库读者
- 你还没决定要把哪些目录授权给 QQ

### 自定义目录模式

这个模式适合你已经知道自己要放开的目录。

配置器会把你选中的目录，同步写入这些配置项：

- `phase1.project.allowedRoots`
- `phase1.attachments.allowedRoots`
- `phase1.artifacts.allowedRoots`
- `phase1.computerSearch.allowedRoots`

同时会把：

- `tools.restrictToWorkspace`

改成 `false`。

适合：

- 想让授权 QQ 搜索“简历目录”“图片素材目录”“下载目录”
- 想让手机直接让电脑发某个授权目录里的文件
- 明确知道自己在放开什么权限

### 整个计算机模式

这个模式适合你已经明确接受“授权 QQ 可以搜索或回传整机本地磁盘里的文件”。

配置器会自动检测当前机器上的本地磁盘，并把它们写入：

- `phase1.project.allowedRoots`
- `phase1.attachments.allowedRoots`
- `phase1.artifacts.allowedRoots`
- `phase1.computerSearch.allowedRoots`

同时会把：

- `tools.restrictToWorkspace`

改成 `false`。

适合：

- 你就是想让这台私人电脑对授权 QQ 开整机搜索
- 你不想逐个选择目录
- 你已经清楚理解这比“工作区模式”和“自定义目录模式”权限更大

## 如果你想做预编译版本

当前也可以直接在本机打包：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Phase1Configurator.ps1 -Build
```

这会执行：

`npm run tauri build`

说明：

- 当前仓库主要先保证“源码运行版”可用
- 如果你后面要正式发 GitHub Release，建议再补应用图标、签名和安装包说明

## 目前这版最适合谁

最适合两类人：

- 想把公开版部署起来，但又不想手改 JSON 的人
- 想在公开版默认安全边界内，渐进式放大权限的人

如果你后面决定把它继续产品化，下一步最自然的升级就是：

- 增加“首次安装向导”
- 增加“环境缺失项一键跳转”
- 增加“导入/导出配置”
- 发布 Windows 预编译安装包
