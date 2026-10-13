# Data Skill 包规范 v1：第一批实现

本批实现 LAB-01 的包加载、发布身份和运行读取锁定基础。独立发布仓库、验证结果登记、草稿编辑器以及跨实例并发发布事务仍待后续实现；不能将本批状态理解为 LAB-01 或整个实验室升级全部完成。

目录中包含 SKILL.md 和 skill.yaml 时按 v1 包解析；只有 SKILL.md 时继续按 legacy 处理。存在但无效的 skill.yaml 会拒绝整个包，不降级为 legacy。manifest 是方法元数据的唯一来源；metadata.json 继续保存本地所有权信息，不参与发布摘要。

```yaml
schema_version: 1
package_id: dataseek/netcdf-quality
version: 1.0.0
title: NetCDF 结构与质量检查
description: 检查变量、维度、单位和缺失值约定。
authors: [DataSeek]
license: MIT
category: quality
domains: [geoscience]
triggers: [NetCDF, 数据质量]
inputs:
  type: object
  properties:
    dataset_version_id: {type: string, minLength: 1}
  required: [dataset_version_id]
  additionalProperties: false
outputs:
  type: object
  properties:
    report_artifact_id: {type: string, minLength: 1}
  required: [report_artifact_id]
  additionalProperties: false
prerequisites: [输入已登记且可读取]
semantic_constraints: [单位缺失时报告未知，不推断为摄氏度]
tools: []
resources:
  network: false
validation: [报告中列出所有变量及缺失单位问题]
```

上例只是包格式示例，没有附带可执行分析实现，不会自动安装到产品。工具依赖格式为 `{name: scientific_inspect, version: 1.0.0}`，version 是工具所属插件的精确 SemVer 版本；必须依据当前实际目录填写。激活时与固定 Cordis 工具目录核对，不满足则拒绝启动该技能。第一版不提供自动安装依赖，也不支持对未版本化 core/MCP 工具声明版本依赖。

主分类为 discovery、acquisition、quality、transformation、analysis、reporting。包 ID 为小写 namespace/name；SemVer 支持预发布与构建标识，拒绝 latest 和版本范围。输入输出限定为 JSON Schema draft-07 的 object，v1 内联定义，不接受 $ref/$id。未知字段、重复 YAML 键和锚点被拒绝。validation 是作者声明的检查要求，不等于检查已经通过；API 一律标记 unverified。

包最多 512 个发布文件、16 MiB，manifest 最大 128 KiB。大科学数据应使用样例引用或独立数据资产。拒绝符号链接；压缩包额外拒绝越界路径、重复条目和总量超限。先验证整个上传批次，再移动到可发现目录，非法批次不会部分导入。基础设施持久化失败不是多包事务，目前仅清理当前失败包，已完成保存的包保留。

选择标识为 `namespace:name@version`，因此多版本可并存于不同目录，旧版技能名称不变。现有安装 API 仍安装一个具体 skill_id。一个已登记版本禁止原地编辑，同版本重复上传被拒绝，修改应上传新版本；尚未提供“复制为草稿”按钮。文件与登记摘要不一致的发布包不会从旧数据库内容恢复为可用技能。

聊天 API 在提交已安装技能时绑定内容摘要；Agent 激活时再次验证，然后冻结本轮通过 Skill 工具读取的说明、脚本和参考文件。磁盘修改、目录刷新和会话中创建新技能不会替换已激活内容。下一轮重新加载；显式锁定的旧摘要不匹配时拒绝，不能静默选新内容。

该锁定覆盖 Skill 工具返回内容，不是对任意 shell 直接读取宿主文件的沙箱快照。脚本仍须通过现有工具执行边界运行。声明的输入输出和语义约束会交给 Agent，但自动验证每次技能调用的输入输出要等 Step/Run 调度层接入；资源声明也不自动改变执行权限或超时。

环境快照把发布内容摘要包含在既有私密身份 HMAC 中；API 仅在技能所有者/可访问目录范围内返回发布摘要，不输出技能物理路径。旧会话、旧技能继续可读。

在 backend 目录执行包结构校验：

```bash
uv run --frozen python -m scripts.validate_data_skill /path/to/package
```

校验命令不执行包脚本、不访问数据源。成功只表明包结构有效，不授予科学正确性或运行环境兼容性认证。
