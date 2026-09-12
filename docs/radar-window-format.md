# 天气雷达受控窗口：ODIM_H5 2.4

插件 `viz-radar-window` 的 reader / adapter 都是 `radar-window`。它提供**射线存储索引 × 斜距门中心**的二维热图，不是地理 PPI 地图，不调用在线地图或其他外部资源，不推算经纬度、地面距离、方位角、地球曲率或波束高度。

## 首版支持范围

- 单个 `.h5` / `.hdf5`，HDF5 签名位于文件起点；`Conventions=ODIM_H5/V2_4`，`what/version=H5rad 2.4`。
- 根 `what/object` 为 `PVOL` 或 `SCAN`；后者只能包含一个扫描。扫描 `what/product` 必须是极坐标 `SCAN`，不是 ODIM 中的笛卡尔 `PPI` 产品。
- 顺序连续的 `dataset1…datasetN`、`data1…dataN`。每个扫描最多 16 个不同物理量，共最多 32 个扫描、128 个物理量字段。
- 数据为二维 `[nrays, nbins]` 无符号 8 位或 16 位整数，16 位支持大小端。连续存储或受限 chunk；仅未压缩及已有安全 HDF5 shuffle/deflate/Fletcher32 管线。
- 每个字段必须显式声明 `quantity / gain / offset / nodata / undetect`，不代入规范默认值。仅支持下表已知 quantity 单位。定长 ASCII 标量字符串；不支持 VLEN 属性、用户定义 filter、外部存储、软链接、读取路径上的外部链接/对象别名或虚拟数据集。
- 扫描必须显式声明 `elangle / nrays / nbins / rstart / rscale / a1gate`，维度匹配数据；拒绝本首版不支持的扇区/RHI几何声明、数据组的 `where` 覆盖及冲突 CF 标定属性。不读取或输出雷达站点、来源标识、时间或任意自由文本属性。未使用的辅助组不被递归读取或宣称完成验证。

| quantity | 单位 |
|---|---|
| DBZH、DBZV、TH、TV | dBZ |
| ZDR | dB |
| RHOHV | 1（无量纲） |
| PHIDP | degrees |
| KDP | degrees/km |
| VRADH、WRADH | m/s |

其他 ODIM 版本、CF/Radial、Cartesian PPI/RHI、浮点/带符号存储码、未知 quantity、非标准单位、过大或缺失 chunk 均明确拒绝；不会悄悄回退为整文件解析或填充回波。

## 数值与坐标语义

ODIM 2.4 的 `rstart` 和 `rscale` 都以米计；旧版 2.2/2.3 的 `rstart` 不同，因此不能只识别 HDF5 后缀就套公式。零基门索引 `g` 的中心为：

```text
slant_range_metres = rstart + (g + 0.5) * rscale
```

`a1gate` 是采集起始射线的存储索引，不是要求将行平移的指令。首版使用原始行序，不按 `a1gate` 旋转，不假装其为已计算方位角。

默认颜色数值是**原始存储码、无物理单位**。`nodata`（无数据）和 `undetect`（低于探测阈值）保留不同的灰色/浅蓝色遮罩，提示中可查看各自存储码；两者都不作为测量零值，不插值、不纳入连续色标。

用户勾选“应用文件声明的 gain/offset”才对缓存窗口中的有效码执行：

```text
physical_value = offset + gain * raw
```

正负 gain 都支持；保留码先分类，永不代入公式。本地切换不发新请求、不下载数据、不改写源文件。无自动降雨反演、衰减订正、速度退模糊、地理配准、扫描拼接或重采样。

## 请求与协议

先请求 `kind=tree, options={}`。只有结构和受控选择项，不访问数值 dataset 的 `__getitem__`。之后用户显式选择扫描、物理量、射线和距离门窗口，携带同一版本：

```json
{
  "operation": "preview",
  "plugin_id": "viz-radar-window",
  "kind": "image",
  "version": "首次目录返回的64位版本",
  "options": {
    "sweep": 1,
    "quantity": "DBZH",
    "ray_start": 1,
    "ray_count": 2,
    "gate_start": 1,
    "gate_count": 4,
    "decode": "raw"
  }
}
```

私有结果 `type=reader=radar-window, contract_version=2, kind=tree|image`。公共结果仍使用已有 `tree|array`，`payload.view_kind` 保留 tree/image。`choices.sweeps` 提供受控扫描、几何及字段标定；`selected` 必须与请求完全一致。image 的 `array` 保存原始整数矩阵，`radar` 保存斜距中心、射线索引与两种保留码数量。元信息绑定源大小、后端实际范围读取字节/次数及选择涉及的 chunk 解码预算；不向浏览器传递真实路径。

源码入口：`radar_window_preview(read_range, size, fmt, kind, options, limits)`。纯 stdlib `validate_radar_window_options` / `validate_radar_window_payload` 在 sandbox 和 backend 保持同源复制，并有一致性测试。

## 预算和生命周期

源文件最大 8 GiB；每次请求累积最多 8 MiB，最多 128 次范围读，单次最多 1 MiB；输出最多 2 MiB。选区最多 128 × 128 = 16,384 个码。单个解码 chunk 最多 4 MiB，选区相关 chunk 累计最多 16 MiB；压缩数据先检验实际展开长度再交给 native 解码。目录属性累计最多 32 KiB。源维度最多 4,096 射线 × 65,536 距离门，声明几何另有有限值约束。

继续使用现有 `dataseek-window-v1`，不改变 AgentLoop、SSE 或只读数据隔离。HDF5 元信息使用有界预读缓存：很小的文件可能整段取回，但不会自动解码整扫描；UI如实显示传输字节，而不是宣称只读取了输出像素数。

文件/权限/插件版本或选区变化同步取消旧请求；目录轮询仅克隆对象不会重新读文件；卸载和停用立即清理捕获的 Plotly 容器与 ResizeObserver，迟到请求和绘制不会恢复旧图。

## 验证依据与边界

- 官方 [EUMETNET ODIM_H5 2.4 规范](https://eumetnet.eu/wp-content/uploads/2021/07/ODIM_H5_v2.4.pdf)：表 4、13、14、16 和第 5.1 节分别约束范围几何、存储标定、SCAN、quantity 单位及射线排序。
- [xradar 官方 GitHub](https://github.com/openradar/xradar)（MIT）和 [0.10.0 ODIM reader 源码](https://docs.openradarscience.org/projects/xradar/en/v0.10.0/_modules/xradar/io/backends/odim.html)。临时容器固定 `xradar==0.10.0`、`lat-lon-parser==1.3.1`，保持生产 NumPy 1.26.4 / xarray 2024.11.0 / h5netcdf 1.8.1；24 组真实 `OdimBackendEntrypoint` 对照覆盖 uint8、LE/BE uint16、压缩/连续、正负 gain、原始/标定值和米制门中心。xradar 的 `_Undetect` 声明不等于自动遮罩，因此只对照有效值的标定，不把它的行为误说成两种标志都会被自动处理。
- 当前官方 [ODIM writer](https://docs.openradarscience.org/projects/xradar/en/v0.10.0/_modules/xradar/io/export/odim.html) 写出 2.2，本次没有声称验证其 2.4 写出功能；我们使用原创合成 2.4 文件和官方 reader，不引入生产参考依赖。
- `sandbox/tests/test_radar_window_reader.py`、`radar_reference_check.py`、`backend/tests/test_radar_window_visualization.py`、`frontend/tests/radarWindow.test.mjs` 与 `frontend/tests/browser/domain-expansion-radar-fixtures.mjs`。浏览器使用真实 Plotly 栅格检查、无生产 API、无外联，覆盖选择绑定、恶意单位拒绝、本地切换零额外请求及卸载释放。
