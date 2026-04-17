# Phase 1 架构与流程图

这份文档用 `GitHub 原生 Mermaid` 画出项目的核心结构。

优点是：

- 可以直接在 GitHub 页面渲染
- 你后续修改时只需要改 Markdown
- 不依赖第三方画图软件
- 适合比赛审阅、开源协作和后续版本迭代

## 1. 整体架构总览

```mermaid
flowchart LR
    subgraph UserSide["用户侧"]
        U1["手机 QQ 用户"]
    end

    subgraph QQSide["QQ / Bot 通道"]
        Q1["QQ 官方消息通道"]
        Q2["NanoBot 网关"]
    end

    subgraph Phase1["Phase 1 本地系统"]
        R1["scripts/Launch-Phase1Task.ps1"]
        R2["app/phase1_router_queue.py"]
        R3["runtime/inbox"]
        R4["runtime/queue/pending"]
        R5["app/phase1_worker.py"]
        R6["app/phase1_admin_relay.py"]
        R7["runtime/tasks"]
        R8["runtime/sessions"]
        R9["runtime/projects"]
        R10["runtime/logs + health.json"]
    end

    subgraph AIPath["AI 执行链路"]
        A1["Codex 规划 / 拆解"]
        A2["Claude Code 主执行"]
        A3["Codex 复审 / 对抗式审查"]
    end

    subgraph ScriptLayer["运维与验证脚本"]
        S1["Start-Phase1Stack.ps1"]
        S2["Get-Phase1Status.ps1"]
        S3["Check-Phase1Env.ps1"]
        S4["Start-Phase1StabilityRun.ps1"]
        S5["Watch-Phase1Gateway.ps1 / Watch-Phase1Worker.ps1"]
        S6["桌面配置器 / Open-Phase1Configurator.vbs"]
    end

    U1 --> Q1 --> Q2
    Q2 --> R3 --> R1 --> R2
    R2 -->|"本地命令可直接回答"| Q2
    R2 -->|"需要排队执行"| R4 --> R5
    R5 --> A1 --> A2 --> A3
    A3 --> R5
    R5 -->|"需要管理员权限"| R6 --> A2
    R5 --> R7
    R5 --> R8
    R5 --> R9
    R5 --> R10
    R5 --> Q2 --> Q1 --> U1

    S1 --> R2
    S1 --> R5
    S2 --> R10
    S3 --> R10
    S4 --> R2
    S4 --> R5
    S5 --> Q2
    S5 --> R5
    S6 --> S1
    S6 --> S2
    S6 --> S3
```

## 2. 一条消息从手机进来到结果回传的链路

```mermaid
sequenceDiagram
    autonumber
    participant User as 手机 QQ 用户
    participant QQ as QQ / Bot 通道
    participant Nano as NanoBot
    participant Router as phase1_router_queue.py
    participant Queue as runtime/queue/pending
    participant Worker as phase1_worker.py
    participant Codex as Codex
    participant Claude as Claude Code
    participant Status as status.json / logs / sessions

    User->>QQ: 发送消息 / 附件 / 控制命令
    QQ->>Nano: 推送事件
    Nano->>Router: 生成 Phase 1 任务

    Router->>Router: 统一会话键 / 去重 / project_root 校验
    Router->>Router: 判断是否为本地命令

    alt 本地即可处理
        Router->>QQ: 直接返回结果
        QQ->>User: 本地回复
    else 需要进入执行链路
        Router->>Queue: 写入 pending 队列
        Router->>QQ: 返回已收下任务
        QQ->>User: 已收下任务

        Worker->>Queue: Claim 下一个任务
        Worker->>Status: 写 task/status/session/project 状态

        alt 健康探针任务
            Worker->>Status: 秒级写入 health probe 结果
            Worker->>QQ: 回传探针完成
        else 显式发文件任务
            Worker->>Worker: 校验 allowed_roots 与文件大小
            Worker->>QQ: 回传本地文件
        else 普通开发任务
            Worker->>Codex: 规划 / 拆解
            Codex-->>Worker: 计划结果
            Worker->>Claude: 主执行
            Claude-->>Worker: 执行结果
            Worker->>Codex: 复审
            Codex-->>Worker: 审查结果

            alt 复审发现问题
                Worker->>Claude: 带反馈修正
                Claude-->>Worker: 修正后结果
            end

            alt 需要管理员权限
                Worker->>Worker: 触发 admin relay
                Worker->>Claude: 管理员链路执行
                Claude-->>Worker: 管理员执行结果
            end

            Worker->>Status: 打包 artifacts / 更新结果
            Worker->>QQ: 回传文本和文件
        end

        QQ->>User: 最终结果 / 文件 / 告警
    end
```

## 3. Router 的分流逻辑

```mermaid
flowchart TD
    A["收到原始任务 JSON"] --> B["解析 session_key / chat_id / sender_id"]
    B --> C["检查 message_id 去重"]
    C --> D["解析控制命令 / 自然语言意图"]
    D --> E["校验 project_root 是否在 allowlist"]

    E --> F{"是否本地命令?"}
    F -->|"切换项目 / 总结会话 / 停止 / 继续 / 浏览目录 / 读文件"| G["直接本地回复"]
    F -->|"否"| H{"是否显式发文件?"}

    H -->|"是"| I["构造 send_local_file system_action"]
    H -->|"否"| J{"是否 AI 全机搜索?"}

    J -->|"是"| K["构造 authorized_ai_file_search"]
    J -->|"否"| L{"是否健康探针?"}

    L -->|"是"| M["构造 health_probe 快路径任务"]
    L -->|"否"| N["按普通开发任务入队"]

    I --> O["写入 runtime/queue/pending"]
    K --> O
    M --> O
    N --> O
    G --> P["返回 router receipt"]
    O --> Q["返回 queued receipt"]
```

## 4. Worker 的执行逻辑

```mermaid
flowchart TD
    A["worker claim task"] --> B["绑定 task/session/project 运行态"]
    B --> C{"system_action 类型"}

    C -->|"health_probe"| D["走秒级健康探针快路径"]
    C -->|"send_local_file"| E["显式文件回传路径"]
    C -->|"authorized_ai_file_search"| F["受控全机搜索路径"]
    C -->|"普通任务"| G["常规开发路径"]

    D --> H["写 status.json + 回传 probe 完成"]

    E --> I["校验路径在 allowed_roots 内"]
    I --> J["校验大小 / QQ 上传限制"]
    J --> K["发送文件回 QQ"]

    F --> L["重算 trusted_search_roots"]
    L --> M["交给 Codex + Claude 执行"]

    G --> N["Codex 规划"]
    N --> O["Claude 主执行"]
    O --> P["Codex 复审"]
    P --> Q{"是否发现问题?"}
    Q -->|"是"| R["Claude 修正"]
    Q -->|"否"| S["进入结果打包"]
    R --> S

    M --> T["产出结果 / 文件 / 说明"]
    K --> U["写完成状态"]
    H --> U
    S --> U
    T --> U
    U --> V["回传 QQ 文本 / 文件 / 告警"]
```

## 5. 启动与部署流程

```mermaid
flowchart TD
    A["从 GitHub 下载仓库"] --> B["运行 Bootstrap-Phase1.ps1"]
    B --> C["生成 .venv / 安装依赖 / 生成本地配置"]
    C --> D["打开桌面配置器"]
    D --> E["填写 QQ / MiniMax / OpenID"]
    E --> F["选择权限模式"]
    F --> G["保存 config/nanobot.local.json"]
    G --> H["执行环境检查"]
    H --> I["启动 Start-Phase1Stack.ps1"]
    I --> J["启动 NanoBot / Gateway / Worker / Watchers"]
    J --> K["执行 Get-Phase1Status.ps1"]
    K --> L["手机 QQ 发送第一条验证消息"]
    L --> M["确认收到已收下任务或最终结果"]
```

## 6. 稳定性与健康探针流程

```mermaid
flowchart TD
    A["Start-Phase1StabilityRun.ps1"] --> B["定时采样 health.json"]
    A --> C["调用 Test-Phase1Pipeline.ps1 -HealthProbe"]
    C --> D["Router 标记 system_action = health_probe"]
    D --> E["Worker 走 fast-path"]
    E --> F["秒级写入 task/status"]
    F --> G["回传 probe 完成结果"]
    G --> H["稳定性脚本汇总 probes.jsonl / summary.md / state.json"]
```

## 7. 你后续修改这张图时最常改的地方

- 如果新增新的 `system_action`，改“Router 分流逻辑”和“Worker 执行逻辑”
- 如果新增新的 watcher / 自愈脚本，改“整体架构总览”
- 如果新增新的部署入口，改“启动与部署流程”
- 如果新增新的验证方案，改“稳定性与健康探针流程”

## 8. 推荐在 README 里怎么引用

可以直接在 README 文档列表里挂这个文件：

- `docs/PHASE1_ARCHITECTURE_FLOW_CN.md`

也可以在首页加一句：

“如果你想先看全局结构，请先读《Phase 1 架构与流程图》。”
