# Data Skills 仓库

此目录是当前项目内的可版本管理技能源码仓库。独立远程仓库和自动发布尚未建立。技能描述方法与约束，确定性执行由脚本及平台准入的工具承担。

## 分类

| category | 用途 |
| --- | --- |
| discovery | 数据检索 |
| acquisition | 数据获取 |
| quality | 理解与质检 |
| transformation | 整理与转换 |
| analysis | 数据分析 |
| reporting | 成果整理 |

领域写在 domains 中，与流程分类分开；跨领域通用技能无需绑定生物或地学。

首个包是 `packages/tabular-quality`：只读 CSV 基础质检。包包含清单、方法、脚本、合成样例和许可证；输入输出契约中的 ID 是平台文件标识，不是宿主路径。脚本输出报告内容，由执行工作流保存为成果后返回 report_file_id，不冒充自动运行适配器。

该首版随后端镜像提供为官方目录候选，不自动添加到个人技能或启用。后续发布新版本须使用独立版本目录并保留旧版文件，不原地覆盖仍可能被运行引用的包。

## 本地验证与打包

从仓库根目录执行：

```bash
cd backend
uv run --frozen python -m scripts.validate_data_skill ../data-skills/packages/tabular-quality
uv run --frozen pytest tests/test_tabular_quality_skill.py
uv run --frozen python ../data-skills/packages/tabular-quality/scripts/profile_csv.py ../data-skills/packages/tabular-quality/references/sample.csv
uv run --frozen python -m scripts.build_data_skill ../data-skills/packages/tabular-quality ../.local/tabular-quality-1.0.0.zip
```

输出目录须提前存在。打包拒绝覆盖现有文件，输出必须在包目录外；同一环境与内容生成相同 ZIP，不执行包内代码。可在插件 → 技能上传 ZIP，之后按固定版本添加；不会自动安装、启用或执行。格式校验和本地样例通过不改变平台“尚未验证”状态。

## 贡献流程

1. 复制首个包作为结构参考，并按 [贡献模板](templates/CONTRIBUTING.md) 修改包标识、方法、契约与样例，删除不适用内容。
2. 使用真实许可证和作者信息；把输入范围、单位/缺失值口径、资源上限及失败行为写入清单。
3. 提交小型合成样例、独立预期值以及异常用例，不提交个人数据、凭据或大型真实数据。
4. 运行包格式校验、专项测试和仓库规定检查；评审后以新版本发布。同一版本内容不可替换。

任何附带脚本都需要审阅后在获准执行环境运行。仓库并不保证技能适用于任意输入；安装包不绕过只读数据边界或工具权限。
