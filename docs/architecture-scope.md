# AI-DataSeek 架构范围

## 产品边界

AI-DataSeek 只暴露以下产品能力：

1. 第三方数据集临时提交接口（`POST /api/v1/datasets/submissions`）与科学数据探查页面（`/dataset/seek/:datasetId`）。
2. 通用数据分析探查任务。
3. Skill、MCP、Renderer 插件。
4. 资源用量与配置、任务、MCP、技能系统管理能力。

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
- 独立数据集管理页面及其后台写入接口。
- 模型、SubAgent、执行节点、工作区、审批、审计、安全策略等管理页面。

沙箱内置浏览器、Shell、文件工具仍属于数据探查实现，继续保留。

## 部署隔离

项目只保留一套名为 `ai-dataseek` 的 Compose 部署；网络、镜像、数据库、MinIO bucket、数据卷、技能卷及 Sandbox 前缀均使用同一命名空间。前端只暴露 `7000` 端口，所有迭代和发布均更新这套服务，不再创建平行开发栈。

## 临时数据约束

第三方接口提交不会加入长期数据集目录。Backend 会在 MongoDB 中保存按所有者隔离的临时记录，并为其设置 24 小时绝对 TTL 索引。读取时会先检查过期时间，因此不依赖 MongoDB TTL 清理任务的执行延迟；同一 MongoDB 下的多进程或多副本 Backend 可共享未过期提交。真实服务器路径只保留在服务端，不进入响应、URL 或浏览器存储。
