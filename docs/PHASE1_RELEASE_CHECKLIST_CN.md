# Phase 1 发布前检查清单

这份清单不是给“能跑就发”的。

它是给你在准备发 GitHub、发内测、或者准备给别人装之前，做最后一圈收口用的。

## 1. 代码与包装

发布前先确认：

- `config\nanobot.local.json` 没有被提交
- `runtime/` 没有被提交
- `desktop-configurator\node_modules\` 没有被提交
- `desktop-configurator\dist\` 没有被提交
- `desktop-configurator\src-tauri\target\` 没有被提交
- 仓库里没有真实 QQ OpenID、session key、message id
- 仓库里没有你机器专属的绝对路径

## 2. 自动化测试

至少跑这两组：

```powershell
python -m unittest discover -s tests -p "test_phase1_*.py"
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\powershell\Run-Phase1PowerShellTests.ps1
```

如果这两组没全绿，不建议发布。

## 3. 长稳压测

建议正式发布前至少做一次长时间运行验证：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-Phase1StabilityRun.ps1 -DurationHours 7
```

重点看：

- 有没有 gateway 假在线
- 有没有 worker 卡死
- 有没有 admin relay 异常
- 有没有 probe 超时
- 有没有 delivery warning

压测结果可以用这个脚本汇总：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Get-Phase1StabilityReport.ps1
```

## 4. Win11 干净环境回归

如果你准备面向更广用户发布，建议至少在一台真实 Win11 机器上跑一次：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Invoke-Phase1Win11CleanRegression.ps1
```

如果还想顺手验证桌面配置器构建：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Invoke-Phase1Win11CleanRegression.ps1 -BuildDesktopConfigurator
```

## 5. 手工回归

自动化全绿之后，至少做这几件人工验证：

1. 双击 `Open-Phase1Configurator.vbs` 能打开窗口
2. 配置器里 `检查环境` 输出可读
3. 配置器里 `启动整套链路` 能正常拉起
4. `Get-Phase1Status.ps1` 显示 gateway / worker 正常
5. 手机 QQ 发一条真实消息能收到回执
6. 文件回传链路至少手测一次

## 6. 可以发布的最低标准

至少满足下面这些，才建议说“可发”：

- 自动化测试全绿
- 无敏感数据残留
- 无本机绝对路径残留
- 7 小时长稳无阻断级问题
- Win11 干净环境回归通过或已明确补齐结果
- 手机 QQ 真消息回执通过

## 7. 不建议直接发布的情况

以下任一出现，都建议先别发：

- 偶发不回消息
- 偶发假在线
- worker 或 gateway 会静默挂死
- 配置器双击打不开
- 新用户必须手修脚本才能跑
- 运行结果依赖你自己机器上的专属路径
