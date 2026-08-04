# AI-DataSeek 架构范围

## 产品边界

AI-DataSeek 只暴露以下产品能力：

1. 数据集临时提交（`/dataset/setup`）与科学数据探查（`/dataset/seek/:datasetId`）。
2. 通用数据分析探查任务。
3. 数据集管理。
4. Skill、MCP、Renderer 插件。
5. 资源用量、任务、MCP、技能四项系统管理能力。

## 保留的运行链

```text
Dataset submission
  -> temporary dataset registry
  -> secure recursive directory inspection
  -> analysis session
  -> Agent planner/executor
  -> Docker sandbox
  -> read-only dataset mount
  -> analysis result files and Renderer previews
```

插件通过稳定接口接入分析运行时：Skill 提供提示词和脚本，MCP 提供外部工具，Renderer 负责数据成果预览。权限、安全审核、审计和 Token 用量是上述接口的内部实现依赖，不单独暴露管理页面。

## 已移除产品域

- OpenClaw/Claw。
- A2A Agent 网络。
- 科学站点发布及其独立运行时。
- 知识库和 Qdrant 索引服务。
- 客户端浏览器扩展。
- 智能化应用地域分布。
- 模型、SubAgent、执行节点、工作区、审批、审计、安全策略等管理页面。

沙箱内置浏览器、Shell、文件工具仍属于数据探查实现，继续保留。

## 部署隔离

Compose project、网络、镜像、数据库、MinIO bucket、数据卷、技能卷及 Sandbox 前缀均使用 `ai-dataseek` 命名，避免资源冲突。生产前端端口为 `7100`，隔离的开发与第三方联调前端端口为 `7000`。

## 临时数据约束

Setup 提交不会写入数据集数据库。临时数据集保存在 Backend 进程内，因此生产首版应保持单个 Uvicorn Worker；多进程或多副本部署需要先把临时注册表迁移为带 TTL 的共享存储。
