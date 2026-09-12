# 原创 Access 数据库测试样本

本目录是插件解析验收 fixture，不是数据集管理中的真实数据。所有表名、值、日期、二进制内容与失效外链均为 AI-DataSeek 测试专用合成内容，未读取用户文件或下载网上数据库。

`GenerateAccessFixtures.java` 使用 **Jackcess 4.0.11** 的官方写入器生成，再通过其独立只读读取器重开基本文件，验证中文、NULL 与空字符串、精确小数、货币、布尔、日期及空表。它不是待测 MDB Tools 读取器自己编造并验证自己的输出。

| 文件 | 用途 |
| --- | --- |
| `synthetic-v2000.mdb` | Access 2000 / Jet 4，Samples 普通表及 Empty 空表 |
| `synthetic-v2010.accdb` | Access 2010 / ACE，内容与 MDB 基本样本一致 |
| `synthetic-linked-v2000.mdb` | 单个 ExternalTable 链接表元数据，目标为不存在的合成路径；禁止打开、追踪或向浏览器返回该路径 |
| `synthetic-memo-ole-v2010.accdb` | MEMO 与 OLE 列边界；不执行或提取嵌入对象 |

所有期望值见 `expected.json`。`Amount`/`Exact` 的字符串是精确十进制 oracle，不经浮点中转；`Observed` 是没有时区的本地墙上时间，不得标为 UTC。JSON 中 Binary 的十六进制值仅用于测试对照，正式预览应省略内容或拒绝不支持的类型。

本组文件仅证明上述两个版本与列类型，不代表 Access 所有 Jet/ACE 方言兼容。不含加密、密码、VBA、QueryDef、多值列或附件功能测试；这些功能不得因此声称已支持。

## 复现

仓库根目录运行以下一次性测试容器流程。只在临时容器安装 JDK；不会修改生产沙箱镜像、后台服务或用户数据。输出到一个新的临时目录，以免覆盖已签入 fixture：

```bash
./run.sh run --rm --no-deps --entrypoint sh \
  -v "$PWD/sandbox/tests/fixtures/database:/fixtures:ro" \
  sandbox-image -lc 'apt-get update -qq && apt-get install -y --no-install-recommends openjdk-17-jdk-headless >/dev/null && bash /fixtures/generate-access-fixtures.sh /tmp/generated-access'
```

脚本固定 Maven Central 的 3 个依赖和 SHA-256（Jackcess 4.0.11、Commons Lang 3.18.0、Commons Logging 1.2），不执行下载的安装脚本。JAR、编译产物和复现文件只在 `--rm` 临时容器内；仓库不携带 JAR。需要导出新 fixture 时，可另挂一个明确的空输出目录，并把最后参数改为该目录。Java 写入器拒绝覆盖已有文件。

**内容可复现，不承诺字节逐位相同**：Access 模板／数据库属性可包含生成元信息，因此测试依赖解析值及结构 oracle，不依赖重新生成文件与旧文件 SHA 相同。

## 上游与许可

- [Jackcess 官方项目](https://jackcess.sourceforge.io/)及[创建数据库示例](https://jackcess.sourceforge.io/cookbook.html)，[Apache-2.0 许可](https://jackcess.sourceforge.io/license.html)。测试数据在 Jackcess 空数据库模板基础上创建，保留其来源说明；原创测试代码／值遵循本仓库许可。
- [Jackcess 依赖清单](https://jackcess.sourceforge.io/dependencies.html)：上述三项均为 Apache-2.0。
- 未引入 [mdbtools/mdbtestdata](https://github.com/mdbtools/mdbtestdata) 的第三方数据库；该库说明这些文件搜集自互联网并因 FLOSS 原因单独存放，不能据此认为数据已获得明确的再分发许可。
