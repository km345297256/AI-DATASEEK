# Cesium 本地底图（2026-09-18）

## 原因与范围

三维 GeoJSON/CZML 预览原本明确设置 `baseLayer:false`，仅画 WGS84 椭球和本地数据，因而只见标记点和单色背景，缺少可识别的大陆、海岸等地理参照。

使用已锁定的 Cesium 1.145.0 随包提供的 Natural Earth II 瓦片，不新增在线地图服务、Key、Cesium ion、外部地形或模型。不改变输入文件校验、数据集只读挂载及预览 iframe 的同源网络限制。

## 资源与许可

- 资源：`cesium/Build/Cesium/Assets/Textures/NaturalEarthII/`，42 张 JPEG 与 XML 共约 465 KiB（磁盘占用约 536 KiB），EPSG:4326，256×256 JPEG，层级 0–2。
- 现有构建流程已将整个 Cesium Assets 及 LICENSE.md 复制至同源 `/visualization-assets/cesium/`，无需另行下载地图或引入依赖。
- Natural Earth 数据为公共领域，允许内置分发；仍保留来源标注。来源：[Natural Earth 使用条款](https://www.naturalearthdata.com/about/terms-of-use/)，包内许可也列明此项。
- 使用当前包对应的 `TileMapServiceImageryProvider.fromUrl`；参见 [Cesium 官方文档](https://cesium.com/learn/cesiumjs/ref-doc/TileMapServiceImageryProvider.html)。
- 开发资源中间件补齐 XML 与 JPEG MIME，生产仍用现有 Nginx 同源静态资源。

这是一套全球概览底图，不是街道路网、高分辨率卫星图、实际三维地形或测绘依据。

## 行为

- 默认显示“内置自然地理”，可切换“参考网格”或“无底图”。
- 仅替换底图层，不重建 Viewer、不重新读取解析用户数据、不重置相机或 CZML 时间。
- 底图元数据加载独立限时 10 秒；底图失败回退到参考网格，保留数据并提示。这个限时不涉及分析任务。
- 通过切换序号及卸载检查丢弃晚返回；销毁时清理计时器、图层和 Viewer。

## 验证

- 底图单元测试 14 项；离线资源测试 3 项，检查真实 42 张瓦片、许可及 MIME。
- 真实 Chrome/Cesium SDK 验证 GeoJSON、CZML 正常与资源 404 回退四个场景：本地 JPEG 确实进入地球纹理；每个场景用户数据只读取和解析一次；三种底图切换保持 Viewer、数据、相机和时间。
- 四个场景均无外部网络请求、非预期请求或运行错误；卸载后无继续请求、活动 worker/Blob 或子框架。
- 近距离查看时内置低分辨率底图会模糊；不改变原有数据定位视角，也不宣称可提供街道级定位。
- 浏览器报告和截图：`/var/folders/n2/wz3bk3pj2ps8y5qk2zx257v40000gn/T/dataseek-visualization-browser-Pnm4zE/`。
- 补充全球跨洲合成点场景通过，并人工查看截图确认大陆、海洋和海岸线可辨；共 5 个真实浏览器场景通过。报告和截图：`/var/folders/n2/wz3bk3pj2ps8y5qk2zx257v40000gn/T/dataseek-visualization-browser-URfDY7/`。
- 前端全量测试 2737/2737，类型检查、生产构建、Compose 配置检查和 `git diff --check` 均通过；构建仍有既有的大 chunk 提示。

## 发布

通过现有 `./run.sh` 构建并更新前端容器，保持 `ai-dataseek` Compose 项目及 `127.0.0.1:7001`；未重启后端、沙箱或旧任务。发布前运行会话和活动分析任务均为 0。

发布后验证首页和 Cesium 编译模块的 HTTP 内容 SHA-256 与本地构建一致；底图 JPEG 和 XML 返回 200，MIME 分别为 `image/jpeg`、`text/xml`。不读取用户数据进行验收。
