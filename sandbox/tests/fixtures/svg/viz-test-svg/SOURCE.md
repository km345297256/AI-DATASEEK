# 插件测试 · SVG 静态矢量图

原创合成 SVG 折线图测试夹具，四个虚构值 1、3、2、4，仅验证文字、本地 CSS、路径与本地引用的安全预览；不是科学观测，不包含用户数据。

发布机构：AI-DataSeek 测试夹具
来源：https://github.com/km345297256/AI-DATASEEK
作者：AI-DataSeek 测试夹具
许可：MIT (https://github.com/km345297256/AI-DATASEEK/blob/v2/LICENSE)
原创项目合成测试夹具，按项目 MIT 许可提供。仅用于渲染功能测试，不用于科学结论。
版本：static SVG fixture v1 (2026-09-30)
范围：640 × 360 的静态矢量图，四个合成数据点；测试文字、本地 CSS 与本地 use 引用。没有外部资源、脚本或用户数据。
转换：未修改来源数据。
下载快照日期：2026-09-30

主预览文件：static-chart.svg
建议可视化插件：viz-svg（image）
在探查页面点击文件，必要时在视图选择器中选择上述插件。未更改已有插件启停偏好。

可分析问题：

## 文件完整性
SHA-256 是下载快照的本地完整性基线，不是发布机构签名。

- static-chart.svg：1142 bytes，SHA-256 6b4001a37f49b18690956dc2578c0fe71e0ddb884b2547117fb982c428f8df77

## 样本性质与测试

project_fixture

1. 打开 static-chart.svg，选择 SVG 矢量图（viz-svg）预览。
2. 核对折线、四个圆点与 A–D 标签；预览通过安全 PNG 显示，长边不超过 2048 像素。
3. 下载保留原始 SVG；关闭 viz-svg 后不回退到通用图片字节预览。

这是一套指定功能的测试入口，不代表该插件全部格式、边界条件或性能已验证。
