# Phase 1 架构与流程图

这份文档收录适合 GitHub 公开仓库展示的图片版架构图与流程图。
如果你后续要把这些图复用到 README、答辩 PPT 或项目介绍页，也可以直接引用下面各图。

## 1. 整体架构总览

![Phase 1 整体架构总览](./assets/flowcharts/architecture-overview.png)

## 2. 一条消息从手机进来到结果回传的链路

![一条消息从手机进来到结果回传的链路](./assets/flowcharts/message-roundtrip-sequence.png)

## 3. Router 的分流逻辑

![Router 的分流逻辑](./assets/flowcharts/router-control-flow.png)

## 4. Worker 的执行逻辑

![Worker 的执行逻辑](./assets/flowcharts/worker-execution-flow.png)

## 5. 启动与部署流程

![启动与部署流程](./assets/flowcharts/startup-deploy-flow.png)

## 6. 稳定性与健康探针流程

![稳定性与健康探针流程](./assets/flowcharts/stability-healthprobe-flow.png)

## 7. 你后续修改这张图时最常改的地方

- 如果新增 `system_action`，优先改 Router 图和 Worker 图。
- 如果新增 watcher、自愈、诊断或健康检查逻辑，优先改整体架构总览和稳定性图。
- 如果新增部署入口、配置器步骤或首次验收方式，优先改启动与部署流程图。
- 如果 GitHub 首页主图要换，建议同时检查 README 首页图和本文件第 1 张图是否仍然表达一致。

## 8. 推荐在 README 里怎么引用

- `README` 首页建议使用简化版总览图，先让读者快速看懂整套系统。
- 详细文档建议链接到本文件，方便读者继续查看时序、Router、Worker、部署和稳定性细节。
- 图片资源统一放在 `docs/assets/flowcharts/`，后续替换单张图片时不需要改目录结构。
