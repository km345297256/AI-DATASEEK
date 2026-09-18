# 地学插件样例的离线复现

这些脚本只转换已经人工核实来源与许可的科学文件，不联网、不执行数据文件内代码、不注册数据库、不更新服务。它们是 `.cache/plugin-samples-20260918/geo/` 中已执行脚本的持久副本。不会更改现有窗口安全限额；派生数据始终保留原件。

## 运行边界

使用 `./run.sh run --rm --no-deps` 启动一次性 `sandbox-image` 容器，入口为 `/app/.venv/bin/python`。把本仓库以只读方式挂到 `/repo:ro`，把专用暂存目录挂到 `/samples`。原件须预先按下表子目录存放。运行脚本路径为 `/repo/backend/scripts/plugin_sample_reproduction/<脚本名>`。不挂载正式数据目录或数据库，不安装依赖，不启动第二套服务。

| 脚本 | 预置输入目录 | 行为与验证 |
|---|---|---|
| `derive_basic.py` | `viz-usgs-anmo-seismic`、`viz-metpy-may4-sounding`；仓库现有 NOAA 和 USGS 样例 | 复制 MiniSEED 首个完整 512B 记录；选出探空 30 个完整层原数值；12 月气候态转 ENVI 并逐值核对；99 个 USGS 点转换为二维 KML，深度不当高度。 |
| `derive_mesh_grib.py` | `viz-noaa-gfs-grib`、`viz-xugrid-netherlands` | GRIB 标准 WMO 2m 温度仅清除未用本地表声明，要求只变第 26 字节，核对全部值与坐标；UGRID 保留前512原面/627原节点并稳定重编号，保存索引映射，拓扑/高程逐值相等。 |
| `repack_array.py` | `viz-noaa-pressure-air-2022-2025/air.2025.nc` | 完整复制五个数值数组到无压缩连续 HDF5，保留原 dtype 和全部层，分块比较完整数值 SHA256。源4年度原件保留；副本约1.04GB，不抽样、不填零扩容。 |
| `derive_radar_fixture.py` | 本仓库 `sandbox/tests/radar_window_fixtures.py` 与 `LICENSE` | 调用本项目已审阅的合成 ODIM2.4 fixture 生成器，删除安全测试的假私有路径文字；不改栅格/标定。明确不是雷达观测，MIT许可。 |

输出全部限定 `/samples`，脚本拒绝覆盖不一致的已有文件；`repack_array.py` 在目标存在时直接退出，复现须使用新的空目标暂存目录。NetCDF/HDF5 容器元数据可能随库版本变化，因此跨环境复现重点核对完整数组/拓扑一致性，现有入库文件仍以已冻结的文件 SHA256 为准。

## 明确未通过的原格式

- NOAA 原年度 NetCDF4 的 HDF5 chunk 索引遍历超过当前 128 次范围请求预算；没有增加限额。只有全值连续 HDF5 副本作为一键入口，实测 73×144 气温窗读取 46,144B、2 次范围请求。
- 两份公开 wradlib 雷达观测为 ODIM2.2，并且所选原反射率 `nodata=undetect=0`。没有伪改标记、伪造阈下/缺测分类；当前雷达插件采用明确合成的回归样本。
- MiniSEED 原一分钟文件后续记录包含当前 reader 不支持的字段，仅首个完整记录作为入口。不拼接、不重采样、不做仪器响应校正。
- ENVI 中月份不是光谱波长；气候态不是逐年趋势。UGRID 未声明 CRS 时保持未知。NASA FOS 复用文件的 `FILETYPE=ERR`，是误差谱，不能将它描述为通量谱或波长坐标。

浏览器/API 验收与原件哈希分别记录在本轮测试矩阵中。以上转换本身不证明浏览器验收完成。
