# 更新日志

版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。Beta 期间次版本号可能包含不兼容变更，升级前请先看本节。

## 0.2.0 · 2026-11-27

### 新增

* **光场分析**（`hdrgainmap/light.py`）：光源提取 → 8 方向光线步进 → 4 尺度漫反射扩散 → 深度代理 → 大气消光 → 环境光遮蔽 → 光色。自适应参数改由光场驱动，不再只看全局直方图。
* **分通道增益**：增益图从 8bit 灰度改为 8bit RGB、全分辨率；每个通道有自己的 `gmax_c`，由光色与表面色偏共同决定。
* **PNG 规范字段**：写 `mDCV`（BT.2020 / D65 / 1000 nits 上限 / 0.005 nits 下限）与 `cLLI`（MaxCLL = MaxFALL = 实际峰值）。
* **ISO 21496-1 三通道载荷**：Ultra HDR 的增益图侧写 141 字节（`flags = 0xC0`），主图侧保留 4 字节版本存根。
* **JPEG XL 的 DPI**：通过 `-x exif=` 把分辨率写进容器 `Exif` box，并用 `--compress_boxes=0` 保持明文可读。
* 本文件；技术文档集重写为 12 篇。

### 变更

* **统一 PQ + BT.2020**：移除 `--transfer` 与 HLG 输出路径。HLG 只作为标准对照留在文档里。
* **增益图改全分辨率**：不再默认半分辨率，避免量化误差与插值误差叠加成高光光晕；体积代价接受。
* **CLI**：移除 `--transfer`、`--gain-fullres`。
* **文档**：五种格式各成一篇（JPEG/Ultra HDR、PNG、HEIC、AVIF、JPEG XL），光、透视、术语表独立成篇；版本历史从 README 移入本文件。
* 根 README 与 `docs/README.md` 重排，语言切换改为页首/页尾双语入口。

### 修复

* JPEG XL 读端一律显示 72 DPI（容器里没有分辨率字段）。
* AVIF 的 `full_range` 实际为 0（limited range），文档先前写作 1。
* 早先的 ISO 21496-1 有理数曾按小端写入，`0.89387586` 会被读成约 `75`、高光变黑；现固定大端，并有逐字节回归测试。

### 移除

* `THIRD_PARTY_NOTICES.md`：依赖与工具许可证并入 README 的“许可与第三方”一节。
* README 的“参考”一节：标准链接直接标在正文。

## 0.1.2 · 2026-11-27

### 新增

* JPEG XL 输出（`hdrgainmap/jxl.py`）：24bit、PQ + BT.2020，PFM 中转、`--override_bitdepth=24 -d 0` 无损编码；`cjxl` 缺失时该格式单独报错，不影响其余产物。
* 校验器补 JXL 分支；CLI 的 `-f` 接受 `jxl`；测试覆盖 24bit 往返。

### 变更

* 六种产物（JPEG/PNG/HEIC10/HEIC12/AVIF/JXL）共用同一条增益曲线。
* 示例图与 README 同步到 24bit JXL。

## 0.1.1 · 2026-11-27

### 变更

* Ultra HDR 固定 1.1：XMP `hdrgm` 与 ISO 21496-1 始终同写（大端），移除 `--write-iso`。
* 修正 ISO 解析与 XMP/ISO 交叉校验；MPF 长度/偏移迭代更稳。
* 示例图重出，README 更新。

## 0.1.0 · 2026-11-25

首个打包发布的 Beta。

### 新增

* 五种产物：Ultra HDR JPEG、16bit PNG（PQ/HLG）、10/12bit HEIC、12bit AVIF。
* 自适应增益算法：光照图 + 辐照度扩散 + 环境光近似 + 大面积平亮惩罚，指数饱和去光晕，量化前抖动。
* 容器级校验器（`--validate`）、原子写入、分块编码。
* `pyproject.toml` 打包、MIT 许可证、第三方声明、中英双语 README、pytest 用例。
