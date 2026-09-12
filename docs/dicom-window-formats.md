# DICOM 单文件灰度区域预览

`viz-dicom-window` 使用 `dicom-window` reader/adapter。它沿用 Cordis 严格 v2、现有 `dataseek-window-v1` 私有字节能力和无网络隔离 worker；无 DICOM 服务器、PACS、远程 WADO URL、临床序列管理或新依赖，不改变 AgentLoop/SSE。

## 支持的安全子集

- `.dcm/.dicom` 必须有 Part 10 的 128 字节 preamble、DICM 标记、合法且有界的 File Meta Information。只接受未压缩 Implicit VR Little Endian `1.2.840.10008.1.2` 和 Explicit VR Little Endian `1.2.840.10008.1.2.1`，不靠扩展名猜编码。
- 灰度 `MONOCHROME1/2`，SamplesPerPixel=1，BitsAllocated=8/16，BitsStored=1..BitsAllocated，HighBit=BitsStored−1，PixelRepresentation 为无符号或二进制补码有符号。掩去未使用高位，再按 BitsStored 解释符号；不用整容器位宽误解 12 位负数。
- SOP Class 白名单为传统 CT Image Storage、MR Image Storage、Secondary Capture（均只允许单帧），以及 Multi-frame Grayscale Byte/Word Secondary Capture（最多 4096 帧、无符号、对应 8/16 位）。这不是完整 IOD 合规验证器，不据此证明临床可用。
- 只接受显式确定长度、严格递增且无重复的顶层数据元素；Pixel Data 长度与行列、帧数及偶数字节填充精确一致，而且必须是文件最后一个元素。显式 VR 未知 VR/非零保留字节、Undefined Length/压缩片段、彩色、浮点 Pixel Data、增强功能组、Modality/VOI/Presentation LUT、叠加层、Real World Value Mapping 和显式 SQ 均拒绝。隐式 VR 中未知且有明确长度的字段仅跳过，不解释其序列或文本。
- 已声明的单组 Rescale Slope/Intercept、单对 WindowCenter/WindowWidth 和默认 `LINEAR` 被严格验证。多组窗设置、SIGMOID/LINEAR_EXACT 本期拒绝，不擅自选择第一组。Rescale 单位仅接受标准受控枚举；缺失单位显示 `unspecified`，不因为 CT 或文件名推断 HU。`US` 是该字段的“未指定”单位，不是超声模态。
- Pixel Padding Value/Range Limit 可以使用；仍返回原存储整数，浏览器将相应像素画为透明。没有在原数据里置零、插值、重采样、保存变换或做统计推断。

## 隐私和诊断边界

读取器必须看到 `PatientIdentityRemoved (0012,0062)=YES` 和 `BurnedInAnnotation (0028,0301)=NO`；若 `RecognizableVisualFeatures (0028,0302)` 存在，也必须为 NO。读取像素还需用户勾选已核实脱敏，通过 `confirm_deidentified:true` 显式确认。

**这些只是文件声明和用户确认，不能保证像素真正脱敏。此工具不进行脱敏验证、烧录文本检测、面部识别消除或临床诊断。** 未核实脱敏的数据不应打开。尤其 PS3.15 的普通标签去标识不等于清理像素；不把不返回患者标签描述为像素匿名化。

响应只有固定枚举、尺寸、位深、显示所需受控数值和当前 ROI 原整数；不返回患者、机构、日期、Study/Series/SOP Instance UID、路径、私有属性或原始自由文本。错误为固定提示，不向浏览器带出解析异常或源路径。文件名和既有文件面板行为不属于本插件的自动脱敏能力。

## 交互与显示语义

初次 `kind:tree, options:{}` 只逐字段检查头，不读取 Pixel Data；未知普通标签仅验证长度后跳过。返回尺寸和帧数，不能据此认定这些帧有临床时间／空间关系。

用户显式请求 `kind:image, options:{frame:0,roi:[x,y,width,height],confirm_deidentified:true}`，必须绑定初次外层 `version`。数组按原始行列次序返回，界面不推断患者方向、像素间距或配准。ROI 原值缓存在当前组件中，改变窗宽／窗位只重新绘制 Canvas，不再次读取文件。

灰度显示按 PS3.3 C.11.2 的顺序：原存储值 → 明确声明的 `slope × value + intercept`（如无声明则恒等）→ `LINEAR` 窗变换 → MONOCHROME1 灰度反转；这些都是显示变换，不是原值变化。宽=1 按阈值处理，避免除零；支持小数窗位。初次优先使用合法文件窗设置；缺失时明确标记“当前 ROI 范围（非文件窗设置）”，不冒称恢复了原临床显示。

切换文件、插件、帧或 ROI、撤销确认、取消、停用、卸载均同步取消旧请求并清空画布；迟到响应不能复活。清理捕获到的实际 Canvas，宽高归零释放 backing bitmap，不依赖卸载后 Vue ref 是否仍存在。无 Blob、Worker 或外部 SDK 请求。

## 预算

宿主源硬上限 8 GiB；每次取回≤8 MiB、≤128 范围请求，单次≤1 MiB；头位置≤1 MiB、File Meta≤64 KiB、顶层元素≤4096，ROI≤128²，JSON输出≤2 MiB。原生 Pixel Data 的 32 位 VL 还构成更严格的约 4 GiB 单元素上限，不宣称可以解码 8 GiB 原生像素块。

目录逐头读取优先避免在确认前读取像素，属性很多的文件可能先触及请求次数预算。相邻 ROI 行只在间隔≤4096 字节且单请求范围可控时合并，间隙字节照实计入预算；不默默读取整帧或无限重试。无法满足预算会拒绝，请选择更小 ROI 或使用后续明确实现的格式能力。

## 上游规范及独立验证

- [DICOM PS3.10 文件格式](https://dicom.nema.org/medical/dicom/current/output/chtml/part10/chapter_7.html)：文件头、元信息及传输语法。
- [PS3.5 数据元素](https://dicom.nema.org/medical/dicom/current/output/chtml/part05/chapter_7.html)：VR、长度、顺序与未定义长度。
- [PS3.3 Image Pixel](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/sect_C.7.6.3.html)、[Modality LUT / Rescale](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/sect_C.11.html)、[LINEAR VOI](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/sect_C.11.2.html)：灰度与变换顺序，不从别的库猜规则。
- [PS3.15 隐私配置](https://dicom.nema.org/medical/dicom/current/output/chtml/part15/sect_E.2.html)：普通标签隐私配置不涵盖像素，需另有 Clean Pixel/Recognizable Features 处理。
- [pydicom 官方项目](https://github.com/pydicom/pydicom)、[apply_windowing 文档](https://pydicom.github.io/pydicom/stable/reference/generated/pydicom.pixels.apply_windowing.html)：只在一次性参考容器安装固定 3.0.1；不复制实现或新增生产依赖。执行 `sandbox/tests/dicom_reference_check.py` 不缺依赖跳过，24组真实 `pixel_array` 原值对照，8组真实 modality/windowing 灰度参考；前端再逐 RGBA 与该结果对照。

回归文件：`sandbox/tests/test_dicom_window_reader.py`、`frontend/tests/dicomWindow.test.mjs`、`frontend/tests/browser/domain-expansion-dicom-fixtures.mjs`。数据均为原创合成 Part10，不使用患者文件；浏览器按既有 harness 拦截全部请求，不访问运行业务 API。
