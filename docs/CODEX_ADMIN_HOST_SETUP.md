# Codex Admin Host Setup

## Goal

Give Codex a stable elevated host only when a task truly needs administrator rights.

## Why This Exists

普通开发任务不应该默认跑成管理员权限。

更稳妥的模式是：

- 平时用普通权限窗口
- 只有系统级任务才切到专门的管理员宿主

## One-Time Setup

在仓库根目录执行以下步骤。

1. 用“管理员身份”打开 Windows PowerShell
2. 运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Register-CodexAdminHostTask.ps1
```

脚本会尽量自动探测：

- 当前仓库路径
- VS Code 可执行文件

如果自动探测失败，可以手动指定：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Register-CodexAdminHostTask.ps1 -VsCodePath "C:\Path\To\Code.exe" -WorkspacePath "D:\Path\To\Repo"
```

## Later Usage

当你需要让 Codex 处理系统级任务时，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-CodexAdminHost.ps1
```

然后在新开的 VS Code / Codex 窗口里重新打开当前仓库。

## Verification

在管理员宿主里运行：

```powershell
[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent() | ForEach-Object {
  $_.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
```

如果输出为 `True`，说明当前窗口已经具备管理员权限。

## When To Use Elevated Codex

只在这些场景使用：

- WSL / DISM / Windows Features
- 系统服务、计划任务、受保护目录
- 机器级依赖安装
- 需要 UAC 的系统改动

不要因为任务“复杂”就默认切管理员。
