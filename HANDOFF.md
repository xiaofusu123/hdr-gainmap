# 交接文档 —— SDR 插画转 HDR（Ultra HDR / PNG16 / HEIC）

> 本文档整合 2026-11-23 ~ 11-24 全部会话内容：结论、规范细节、踩过的坑、实测数据、产物位置。
> 代码在 `D:\Program\Code\hdr-gainmap`，实验脚本与资产在 `D:\Program\App\DSH\work\15-hdr-202611\`。

---

## 0. 一句话概括

把 8bit SDR 插画做成**在真 HDR 屏上确实会亮**的 HDR 图片，不是靠 AI 反色调映射，而是
**只把"发光体"（光柱、星芒、水面反光、眼神光）抬到 SDR 白以上**，其余像素一个不动；
封装成三种格式：Ultra HDR JPEG（安卓原生）、16bit PQ PNG（无损母版）、10/12bit HEIC（苹果/桌面）。

**最容易踩的坑不在算法，在容器**：MPF 的三条字段和 ISO 21496-1 的 61 字节元数据错一个字节，
手机就会静默回退到 SDR，表现就是"怎么看都没效果"。

---

## 1. 起点：什么算"真 HDR"

会话最开始那张 `HDR_20261123_004913.png`（已从桌面删除）是要判定对象。

- 它其实是 **JPEG**（`FF D8 FF E0 4A 46 49 46`），扩展名是错的。
- 内部两张 JPEG：主图 3200×1367 8bit sRGB（1,676,224 B）+ 增益图 1600×683 灰度（433,002 B）。
- 容器结构正确：MPF 双图索引 + XMP GContainer + `APP2 urn:iso:std:iso:ts:21496:-1`，
  XMP `hdrgm 1.0`：`GainMapMax=2`、`Gamma=1`、`OffsetSDR=OffsetHDR=0.015625`、`HDRCapacityMax=2`。
- **但内容是假 HDR**：增益图与主图亮度回归得 `gain = 1.006 × luma − 0.005`，R²=0.9989、
  corr=0.9995——增益图就是亮度图本身，没有任何新的高光信息。35.9% 像素超 SDR 白、峰值 3.94×。
- 写入者是 `Glow HDR 1.2.0`（安卓端把 SDR 机械提升成 Ultra HDR 的 App）。

**结论**：格式合规 ≠ 内容有意义。后面所有工作都是在"合规"和"有意义"之间做取舍。

---

## 2. Ultra HDR 容器的三个硬约束

任何一个错了，解码器都会退回 SDR**且不报错**。这三条是排查了整整一天才定位的。

### 2.1 文件布局

```
SOI | APP1 Exif | APP2 MPF | APP1 XMP | APP2 ISO | <主图，含 EOI> | <增益图 JPEG>
```

### 2.2 MPF (APP2, `MPF\0`) —— 三条校验

参考 `libultrahdr/lib/src/jpegr.cpp` 的 `validate_mpf()`：

| 项 | 主图 | 增益图 |
|---|---|---|
| `attribute & 0x0fffffff` | `0x00030000` | `0x00000000` |
| `imageOffset` | 必须为 `0` | **相对 MP 头的偏移**，不是文件绝对偏移 |
| 交叉校验 | — | `tiff_abs + offset == 主图长度` 且 `size == 增益图长度` |

- MPEntry 固定 **16 字节**：`attribute(4) | imageSize(4) | imageOffset(4) | dep1(2) | dep2(2)`，
  按 TIFF 头字节序（"II" = 小端）读取。
- `tiff_abs` = MP 段内 TIFF 头的绝对位置 = `2 + len(exif_seg) + 8`。
- 因为"元数据长度 ↔ 偏移"互相依赖，构建时必须**迭代到不动点**。

### 2.3 XMP (APP1, `hdrgm 1.0` + GContainer)

```xml
Item:Semantic="Primary" Item:Length="<主图字节数>"
Item:Semantic="GainMap" Item:Length="<增益图字节数>"
hdrgm:GainMapMax=<log2 最大提升>, GainMapMin=0, Gamma=1,
hdrgm:OffsetSDR=0.015625, hdrgm:OffsetHDR=0.015625,
hdrgm:HDRCapacityMin=0, hdrgm:HDRCapacityMax=<同上>,
hdrgm:BaseRenditionIsHDR=False
```

### 2.4 APP2 ISO 21496-1（Android 15+ 优先读这个）

- **主图段**：只写命名空间 + 4 字节版本占位 `urn:iso:std:iso:ts:21496:-1\0` + `u16 min_version` + `u16 writer_version`。
- **增益图段**：`SOI` + `APP2(命名空间 + 完整元数据)` + 其余字节；`Item:Length` 要算上这段。
- 元数据 **单通道共 61 字节**：

```
u16 min_ver, u16 writer_ver, u8 flags,        # flags: bit0=多通道, bit1=基础色彩空间
u32 baseHdrHeadroomN, u32 baseHdrHeadroomD,    # = log2(capacity_min) = 0
u32 altHdrHeadroomN,  u32 altHdrHeadroomD,     # = log2(capacity_max) = gmax
每通道 5 组 (i/I): GainMapMin, GainMapMax, Gamma, OffsetSDR, OffsetHDR
```

- 本项目的取值解出来是 `(0, 1, 2, 1, 1, 1, 1, 64, 1, 64)`。
- 分母取 2 的幂即可精确表示 `1/64`（`_ratio()` 就是干这个的）。

> **只写命名空间、不写 4 字节版本号**，或增益图段完全没有元数据 —— 这是本会话最贵的 bug：
> 文件结构自检全过，手机毫无反应。

---

## 3. 增益图算法（为什么不用 AI 反色调映射）

照片训练的 ITM 模型（SingleHDR / LumaFlux / GM-ITM 一类）在二次元平涂上必然翻车：
平涂糊出块状增益、线稿整条发光、纯白死白、cel-shading 出色带。而且 **SDR 插画的高光本来就已削顶**，
没有"暗藏细节"可以恢复——硬提只会发灰。

所以只判断"这块像素是不是在发光"：

```
bright = smoothstep(b0, b1, Y)                    # 绝对亮度够高（Y = 线性亮度）
peak   = Y / (gauss(Y, pr) + 1e-4)                # 比邻域亮多少
emis   = bright * (flat + (1 - flat) * smoothstep(p0, p1, peak))
emis   = halo * gauss(emis, hr) + (1 - halo) * emis   # 向外 bloom
gain   = 2 ** (gmax * emis)                       # 线性倍数
```

高调素材（整幅都亮）改用**局部结构**选点，平坦的白布皮肤不动，只有褶子/发丝亮：

```
detail = |Y - gauss(Y, dr)| / (gauss(Y, dr) + 0.02)
emis   = smoothstep(b0, b1, Y) * smoothstep(d0, d1, detail)
```

### 三个档位（`gainmap.PROFILES`）

| 档位 | 参数 | 适用 |
|---|---|---|
| `strong`（默认） | b0=.30 b1=.80 pr=25 p0=1.45 p1=2.90 flat=.15 halo=.50 hr=11 gmax=2.6 | 深色插画、有明确光源 |
| `detail` | b0=.35 b1=.90 dr=6 d0=.045 d1=.20 halo=.30 hr=7 gmax=1.6 | 高调/粉白素材 |
| `lights` | b0=.45 b1=.90 pr=25 p0=1.6 p1=3.2 flat=0 halo=.40 hr=9 gmax=2.0 | 只想让星芒跳出来 |
| `soft` / `night` | b1=.52 flat=.35 / b1=.44 flat=.55 | 已废弃：整体发灰 |

- `pr` / `hr` 按宽度自适应：`fit_width(cfg, w)`，`k = max(1, w/3240)`，4× 放大图不会被"按小图调好的半径"糊掉。
- **基图默认保持原始字节不动**：SDR 上看等于原图，HDR 上才是"原图 + 增益"，对比关系最稳。

### 血的教训：加大增益 = 走向发灰

试过 medium（均值 1.45×）和 strong（1.73×）两轮"整体加亮"，结果都是整幅起雾、对比度被压扁。
一张高光已削顶的图，HDR 化在视觉上"几乎无变化"**是正确结果**，不是 bug。
真要看出差别，只能换有高光余量的内容（深色夜景 + 光源），或者接受局部过曝。

---

## 4. 色彩链路与格式能力

```
SDR sRGB(8bit) --srgb_to_linear--> 线性 --×2^(gmax·emis)--> 线性 HDR
   --×203nits--> BT.709 线性 --to_bt2020--> BT.2020 线性
   --pq_oetf--> 码值 --×65535--> uint16
```

| 产物 | 位深 | 传输函数 | 谁最认它 |
|---|---|---|---|
| Ultra HDR JPEG | 8+8 | SDR base + 增益图 | **安卓 14+ 原生**、Chrome |
| 16bit PNG | 16/通道 | PQ（`cICP` 9/16/0/1） | 专业工具；Windows 会灰白（按 sRGB 显示） |
| 10bit HEIC | 10 | PQ（`nclx` 9/16/6） | 苹果生态；安卓仅部分机型 |
| 12bit HEIC | 12 | PQ | 只有 x265 12bit 能编；**WIC 解不了** |
| 8bit SDR HEIC | 8 | sRGB | 只是换容器，给高调素材用 |

**JPEG 没有 10bit**：只有 8bit（baseline SOF0）和 12bit（extended SOF1）。12bit JPEG 得用医学影像那套
特殊编译的 libjpeg，浏览器/相册/手机全不认；Ultra HDR 规范也要求基图 8bit。

**HLG 别用**：HLG 是相对信号，反推亮度要做 OOTF，依赖参考峰值和 system gamma，各播放器解释不同
（实测在荣耀 GT 上**过曝**）。PQ 是绝对 nits 编码，无歧义——**定稿 PQ**。

---

## 5. 踩坑清单（症状 → 根因 → 修复）

| # | 症状 | 根因 | 修复 |
|---|---|---|---|
| 1 | 手机怎么都不亮，文件自检全过 | MPF 属性值写反（`0x03000000` 应为 `0x00030000`） | 按 CIPA DC-007 改字段与掩码 |
| 2 | 同上 | 增益图 offset 写成了文件绝对偏移 | 改成相对 MP 头（`tiff_abs + off`） |
| 3 | 同上 | 增益图 `attribute` 写成 `0x02000000` | 改 `0x00000000` |
| 4 | 同上 | ISO 21496-1 只有命名空间，缺 4 字节版本号 | 补 `min_version/writer_version` |
| 5 | 同上 | 增益图段无完整 61 字节元数据 | 补 `_iso_metadata()` |
| 6 | PNG 是彩虹竖条纹 | 滤波左邻偏移按 1 字节算，规范要求 `bpp=6`；自检用同样错误偏移解码→假通过 | `shift(cur, bpp)`，并用 **Pillow 独立解码**比对（0.00 误差 / 100% 一致） |
| 7 | 属性面板 72 DPI、位深不对 | JPEG 没传 dpi；PNG 没写 `pHYs`；HEIC 容器根本没 DPI 字段 | `dpi=(240,240)`；`pHYs(9449,9449,1)`；HEIC 靠 EXIF 282/283/296 |
| 8 | HEIC 显示 72 DPI | 缺 DPI 时 **WIC 回退 72**（不是 96） | 写 EXIF 分辨率 |
| 9 | 12bit HEIC 打不开 | Windows HEVC 只有 Main10 | 12bit = `0xC00D3E8C`；改用 ImageMagick 读 |
| 10 | 预览图出现彩虹条纹 | 预览色调映射在 x=1 处不连续（1.0→0.72） | 改成连续式 `1 + 0.25*(1-exp(-(x-1)))` |
| 11 | 预览整幅偏亮 | sRGB 值当线性又套一次 gamma | 先 `srgb_to_linear` 再算 |
| 12 | 增益全为 0 | `load_rgb()` 已归一化，又除了一次 255 | 直接用返回值 |
| 13 | 小图档位套到大图上糊 | 半径是绝对值 | `fit_width()` 按宽度缩放 |
| 14 | PIL 打开上亿像素放大图报 DecompressionBomb | 默认像素上限 | `Image.MAX_IMAGE_PIXELS = None` |
| 15 | `KeyError: 'HEIF'` | 没注册 opener | `ph.register_heif_opener()` |
| 16 | `unrecognized image mode` | 对 `RGB;16` 调 `to_pillow()` | 改 `np.asarray(f)` |

---

## 6. 实测数据

五张测试图（`strong` 档，基图字节不动）：

| 图 | 尺寸 | 峰值 | 不动像素 | Ultra HDR 体积 |
|---|---|---|---|---|
| 鸣潮（深夜光柱） | 3200×1367 | 1217 nits | 70.9% | 1.88 MB |
| 泡泡（蓝夜星芒） | 3240×1355 | 1231 nits | 77.7% | 1.43 MB |
| 搜图神器（夜景光球） | 5800×3066 | 1213 nits | 75.4% | 10.19 MB |
| 白猫（高调，`detail`） | 1433×2000 | 611 nits | 55.7% | 2.49 MB |
| 高调2（高调，`detail`） | 1414×2000 | 604 nits | 34.8% | 3.37 MB |

其他验证：

- **交叉验证**：Ultra HDR 与 PQ HEIC 同一像素，均值差 `0.033 EV`、p95 `0.107`、p99 `0.239`
  ——两者的差异只来自 HEVC 量化与 4:2:0。
- **12bit HEIC**：`泡泡_12bit_PQ.heic`，1,031,312 B，3240×1355，`bit_depth=12`、4:2:0、1231 nits。
  pillow-heif 与 ImageMagick 两条路径都读回 12 bit；WIC 仍报 `0xC00D3E8C`。
- **16bit PNG**：13.13 MB（3240×1355），编解码往返用独立解析器比对 **max diff 0、100% 一致**
  （PNG 滤波 `bpp=6` 那个 bug 修好之后的结果）。
- **体积对照**（同图）：原图 1.76 MB → HEIC10 0.89 MB → HEIC8 1.26 MB（8bit PQ 量化浪费）。

---

## 7. 设备侧结论

| 环境 | 能力 | 结论 |
|---|---|---|
| 荣耀 GT（AMG-AN00，MagicOS 10 / Android 16） | 支持 Ultra HDR 显示；但其相机写的是**私有 AMG 段**（APP6–APP9） | 标准 Ultra HDR 可用（修完 MPF 后实测三张都亮）；私有段不必去逆向 |
| Windows 11 26H2 + WIC | HEIC 10/8bit ✓，**12bit ✗**；AVIF/JXL/WebP ✓ | 12bit HEIC 只能靠 ImageMagick / pillow-heif |
| Windows HDR | 需 `DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020` + AdvancedColorEnabled | 10bit 色深 ≠ 开 HDR；SDR 下 Chrome 不应用增益图 |
| Windows 照片 | 不认 Ultra HDR；PQ PNG/HEIC 会按 sRGB 显示（发灰） | 验证 HDR 要看设备是否真在 HDR 模式 |

**"看不出效果"的三层排查顺序**：① 设备是否真开 HDR → ② 看图软件是否认这个格式 →
③ 增益是否只落在本来就 255 的高光上（那就等于没有效果，是内容问题）。

判别容器是否被解码，最省事的工具是**阶梯测试图**：基图纯灰，增益图分四段 0/85/170/255。
SDR 下四格完全同灰，HDR 下呈四级阶梯——一眼就能分清"容器坏了"还是"图不亮"。

---

## 8. 代码结构与用法

```
hdr-gainmap/
├── hdrgainmap/
│   ├── color.py      sRGB↔线性、PQ/HLG OETF、BT.709→BT.2020
│   ├── gainmap.py    档位表 PROFILES、emissive()、fit_width()
│   ├── ultrahdr.py   Ultra HDR 容器：build() / parse() / read_gainmap()
│   ├── png16.py      16bit PQ PNG 编解码（手写滤波，bpp=6）
│   ├── heic10.py     10/12bit PQ HEIC、8bit SDR HEIC
│   ├── pipeline.py   一次读取，三种格式共用同一条 HDR 重建
│   └── cli.py        命令行
├── tests/test_roundtrip.py
├── _archive/         一次性实验脚本（build_hdr / build_white / build3 / remaster / variants …）
└── README.md
```

```bash
python -m hdrgainmap 图.png -o out/                       # 默认 strong，出 jpg+png+heic
python -m hdrgainmap 图.png -p detail -f jpeg --no-preview # 高调素材
python -m hdrgainmap 图.heic -f heic_sdr                  # 只换容器
python tests/test_roundtrip.py                            # 自检
```

API：

```python
from hdrgainmap import pipeline
info = pipeline.build("图.png", "out/", profile="strong",
                      formats=("jpg", "png", "heic"), gmax=2.6, transfer="PQ")
```

---

## 9. 产物位置

| 内容 | 路径 |
|---|---|
| 代码 | `D:\Program\Code\hdr-gainmap` |
| 实验脚本与资产 | `D:\Program\App\DSH\work\15-hdr-202611\{scripts,assets,output}` |
| 最终成品（桌面） | `C:\Users\xiaof\Desktop\安卓HDR自检\{第一张-鸣潮,第一张-泡泡,第三张,高调图,高调图2,其他格式,变调档}\` |
| 12bit 样例 | `C:\Users\xiaof\Desktop\泡泡_12bit_PQ.heic` |
| 阶梯测试图 | `桌面\安卓HDR自检\01_阶梯测试_4档.jpg` |

---

## 10. 仍未解决 / 后续可做

1. **AVIF 编码**：Python 无 wheel（`pillow-avif-plugin` 编译失败），但 ffmpeg 9.1 带 `libsvtav1`/`libaom` + `avif` muxer，可用 `rawvideo` 管道绕过去。
2. **VVC/H.266**：Windows 没有民用解码器，装不上；暂时放弃。
3. **按内容自动选档**：现在靠人眼看图选 `strong` / `detail`，可以按 `p99.9` 与纯白占比自动判定。
4. **`.avif/.heic` 文件关联**：Win11 的 UserChoice 会挡住程序化设置，只能用户在"设置 → 默认应用"里点一次。
5. **12bit HEIC 的实际显示**：只有部分安卓机型和苹果生态认；Windows 端注定打不开，别拿它当交付主格式。

---

## 11. 三条最该记住的

1. **容器错一个字节 = 无声失败**。MPF 的属性值、偏移基准、ISO 21496-1 的 61 字节，任何一处错都不报错，只回退 SDR。
2. **合规 ≠ 好看**。增益必须落在真正发光的地方；把整幅抬高只会得到一张灰雾图。
3. **别跟物理较劲**。高光已削顶的素材做不出"明显的 HDR"，这是内容的限制，不是工具的问题。

---

## 12. v1.1.0 变更（2026-11-24 迭代）

### 新增 `validate.py`（严格合规校验）
`python -m hdrgainmap.validate <文件...>` 或 `validate.validate_file(path)`。逐条镜像 Android
Ultra HDR v1.1 与 libultrahdr `hasValidMpfAssociation()`，把「解码器必然忽略」的 error 与
「能显示但不合规」的 warning 分开。产物生成时用 `--validate` 可直接跑一遍。

### Ultra HDR 封装重写（对标规范与 libultrahdr）
- 布局改为：`SOI | APP0 | APP1 Exif | APP2 MPF | APP1 XMP(主图) | APP2 ISO 存根 | 主图 | SOI + APP1 XMP(增益图) + APP2 ISO 61B + 增益图`。
- XMP 按规范拆两处：主图只写 `hdrgm:Version="1.0"` + `Container:Directory`；完整 hdrgm 参数写进增益图 XMP。
- MPF 的 TIFF 头改成标准 4 字节字节序 `II*\0` + u32 IFD 偏移（此前按 2 字节写），条目表固定从 TIFF+50 开始；APP2 段正好 90 字节。
- ISO 元数据 flags 置 bit6（`useBaseColorSpace=1`）；主图存根仍只有 `命名空间 + 0/0`。
- `strip_metadata()` 复用源 JPEG 字节时先剥掉旧的 Exif/XMP/MPF/ISO（保留 ICC/JFIF/APP13），不会出现重复元数据。
- Exif 现在会写回 DPI（282/283/296），复用源字节时属性面板仍显示原 DPI。

### 管线
- **主图零重编码**：源是 JPEG 且 DPI 不变时直接复用原始字节；`--no-reuse-base` 可强制重编码。
- HDR 还原改用规范公式 `(SDR+offset)*2^(GainMapMax*g)-offset`，PNG/HEIC 都用**落盘的 8bit 增益图**重建，三格式逐像素一致。
- 新增 `-p auto`：近白 >0.5% → `detail`；极暗且 p99.9<0.55 → `lights`；其余 `strong`。
- `soft` / `night` 标记弃用（保留兼容）。
- `--validate` / `--no-reuse-base` 新参数；`--json` 里带 `report` 与 `ok`。
- stats 新增 `base_reused`、`auto`、`gt2x_pct`、`changed_gt2_pct`、`changed_gt16_pct`。

### PNG16 解码
`png16.decode(blob, fast=True)` 优先用 ffmpeg（`rawvideo rgb48le` 管道）解码，失败回退 NumPy；
Sub/Up 滤波已向量化，Average/Paeth 仍逐字节。8192×12288 的 16bit PNG 从「几分钟」降到约 32 秒。

### 实测（3240×1355 高调样本，auto → detail）
`1777789210281_base.jpg`：峰值 622 nits、不动像素 85.7%、主图复用源字节，jpg/png/heic/preview 四件产物 validate 全过，耗时 14.8 s。

### 仍未做
AVIF（ffmpeg libsvtav1 管道）、12bit HEIC 显示验证、大图（8192×12288）端到端跑通与内存峰值测量。

### v1.1.0 补记（同日）
- 修 `_split_app0` 读长度的字节位置：APP0 的 length 字段在 `jpeg[4:6]`，`end = 4 + length`。
  之前误读 `jpeg[2:4]`（那是 `FF E0` 标记，值 65504），大图会命中 `65506 < len` 这条错误分支，
  把 Exif/MPF/XMP 插进扫描数据之后——小图因为长度越界走了 fallback 才侥幸正常。
  现已在 `build()` 里加防御：APP0 段内出现 SOS 直接报错。
- 主图缺 ICC 时自动补 sRGB（Pillow 自带 lcms），产物不再有「主图没有 ICC」警告。
- 大图实测 `out\upscaled-4x_00007__base.jpg`（8192×12288，100.7 Mpx，JPEG 源复用字节）：
  - 耗时：目录创建 10:31:15 → Ultra HDR 10:33:57（2m42s）→ PNG16 10:36:40（+2m43s）→ HEIC 10:36:54，合计 **5m39s**；
  - 体积：Ultra HDR 10.59 MB、PNG16 121.54 MB、HEIC 5.81 MB；
  - 峰值 1247 nits、增益均值 1.409×、不动像素 22.2%、>2× 0.35%；
  - 三件产物 `validate` 全过、零 warning（jpg 被正确识别为 ultrahdr）。
- 小图/真实样本回归：3240×1355 高调样本 auto→detail、峰值 622 nits、四件产物全过、14.8 s；
  合成 256×128 自测 5185 B Ultra HDR、往返一致。

---

## 13. 本轮查到的资料（v1.1 的依据）

| 资料 | 用途 | 链接 |
|---|---|---|
| Android Ultra HDR v1.1 规范页 | 增益公式、XMP 属性表、GContainer 规则、ISO 兼容与解码容错 | https://developer.android.com/media/platform/hdr-image-format |
| libultrahdr 源码（AOSP） | MPF 字节布局、XMP 拆分、ISO 21496-1 61 字节编码、`hasValidMpfAssociation()` 校验 | https://github.com/google/libultrahdr |
| ISO/IEC 21496-1:2025 | 标准目录与增益图元数据结构（正文付费；ithe 预览只有前置页） | https://www.iso.org/standard/86775.html |
| CIPA DC-007 (MPF) | 多图 MPEntry 属性/偏移语义 | https://www.cipa.jp/std/documents/e/DC-007_E.pdf |
| gainmaps.com | Ultra HDR / Adaptive HDR / ISO 增益图术语对照 | https://gainmaps.com/ |
| AOM AVIF 规范（tmap） | 后续做 AVIF 增益图时的 derived item 依据 | https://aomediacodec.github.io/av1-avif/ |

注：context7 文档服务在本机网络下不可达（连无关库都 `fetch failed`），因此全部以官方规范页 + 参考实现源码为准。

---

## 14. v1.2.0：AVIF 产物（12bit 4:4:4）

需求：在 v1.1 基础上增加 AVIF，默认 12bit，不行再降。

### 实现
- 新模块 `hdrgainmap/avif.py`：ffmpeg 管道编码/解码 + 手写 box 解析。
  - `_encode_once()` 用 `libaom-av1`，`yuv444p12le`、`-still-picture 1`、`-cpu-used 6`；
  crf 由 `quality` 映射：`crf = round((100-quality)*0.63)`，默认 `quality=100 → crf 0`（视觉无损）。
    降级序列 `(12,444) → (12,420) → (10,444) → (10,svt 420)`。
  - **ffmpeg 的 avif muxer 不写色彩标签**：`-color_primaries/-color_trc` 用名字或数字、aom 或 svt，
    落盘 `nclx` 恒为 2/2（unspecified）。`patch_nclx()` 编码后直接改写 7 字节为 `9/16/9`（BT.2020/PQ，HLG 为 18）。
  - **解码必须走临时文件**：AVIF 是 ISOBMFF，ffmpeg 从管道读会报 `partial file` / `cannot determine format`。
  - `ispe` 读尺寸、`pixi` 读位深、`nclx` 读写色彩标签，位深 12 可直接从 box 确认而不用解码。
- `validate.py` 增加 `validate_avif()`：brand、nclx（9/16|18/9）、pixi（10/12）、能否解码、码值；
  `validate_file()` 按 `ftyp` major brand 分派 `avif`/`avis` → AVIF，其余仍走 HEIC。
- `pipeline.build(..., avif_bits=12)`；只出 AVIF 时不再白白生成一遍主图 JPEG。
- CLI 增加 `-f avif` 与 `--avif-bits {10,12}`，输出多一行 `AVIF : 12bit`。

### 质量与体积（3240×1355，参照 16bit PNG 码值）
| 配置 | 体积 | mean abs | p99 |
|---|---|---|---|
| 12bit 4:2:0 crf20 | 73.8 KB | 282.2 | 1554 |
| 12bit 4:4:4 crf8（默认） | 244.4 KB | 138.3 | 619 |
| 12bit 4:4:4 crf4 | 490.0 KB | 106.8 | 604 |
| 10bit 4:4:4 crf4 | 514.7 KB | 94.4 | 373 |
| 10bit HEIC（对照） | 1013.4 KB | 91.1 | 578 |

选 4:4:4 的理由：插画色边/彩色高光是硬边，4:2:0 会在色度上直接糊掉（mean 差一倍）。

### 大图与兼容性
- 8192×12288（100.7 Mpx）：12bit 4:4:4 crf8 → **0.72 MB**，`validate` 全过；
  与 PNG16 比对 mean 56.08 / p99 320 / max 3386。
- HLG 变体（`--transfer HLG`）同链路通过，transfer=18。
- 兼容性验证：无头 Edge（Chromium）直接打开产物可正常渲染并截图，
  说明桌面 Chromium 软件解码 12bit AVIF 可用；Windows 照片 App 仍需 AV1 扩展，不保证 12bit。

### 自测
- `tests/test_roundtrip.py` 增加 AVIF 段（`avif.available()` 守卫）：brand/nclx/pixi/往返尺寸/`avif_bits`。
- 全套通过（"AVIF 12bit OK 565 B"）。

### v1.2.0 补记（同日）
- `validate_heic()` 现在区分 SDR 变体：8bit 且传递曲线不是 PQ/HLG 时按 `heic-sdr` 校验（primaries 1/2、
  transfer 13 等），不再把 `_sdr8.heic` 误报为「位深 8 应为 10 / transfer 13 应为 16」。
- 修 `color.pq_eotf()`：漏了先对码值做 `**(1/PQ_M2)` 的幂运算，导致 PQ→nits 恒为 0
  （只影响分析脚本与校验展示，不影响编码产物）。新增 `check_pq_roundtrip()` 回归断言（1/10/100/203/1000/4000 nits）。
- 一次产出全部格式并逐件校验：3240×1355 样本 17.4 s，jpg/png/heic/avif/heic_sdr/preview 六件全部 OK
  （1.26 MB / 13.02 MB / 0.99 MB / 0.24 MB / 1.64 MB / 0.93 MB）。

## 15. v1.3.0：默认档改为「按光影自适应」+ AVIF 写 DPI（2026-11-24 晚）

### 起因
用户反馈：PQ HEIC 观感不如桌面上那份旧文件（`1777789210281_x4_animejanai_hdr10_pq.heic`），
"亮的泛光太亮反而丢失原图细节"；AVIF/JPEG 面板 DPI 与原图不一致（原图 240，AVIF 显示 72）。
进一步确认：参考文件本身就是本工具 strong 档的旧产物，问题出在"同一组常数对所有图"。

### 改了什么
1. **半径不再随分辨率放大**：`gainmap.SCALE_EXP = 0.0`（旧行为 `k = w/3240`）。
   实测 12960 宽：k=4 时抬起面积 29.4%、峰值 1298 nits；k=1 时 16.3%、821 nits，
   与参考文件（17.1%、844 nits）吻合。旧行为让同一档位在高分辨率下"泛光摊得太开"。
2. **strong 档自适应**：`gainmap.adapt_params()` 用亮度中位数 `p50` 算
   `t = clamp(p50/0.6, 0, 1)`，`b0=0.78+0.10t`、`b1=0.97+0.02t`、`halo=0.16-0.06t`、   `hr=max(1, round(3-2t))`（1–3px，暗场景大、亮场景小）、`gmax=2.2-0.6t`。暗场景 gmax≈2.16、高调亮图 gmax≈1.6。
   过渡处只保留很薄的一圈（`halo≤0.16`），不再把亮部周围整片推白；`flat` 从 0.15 降到 0.05。
3. **SDR 预览补 DPI**：`*_hdrsim.jpg` 之前不写 DPI，Explorer 面板空白。
4. **AVIF 写 Exif DPI**：见 §15.1。

### 效果（12960×5420 源）
| 版本 | gmax | 峰值 | 不动像素 | >2× |
|---|---|---|---|---|
| 旧 strong（k=4） | 2.6 | 1298 | 78.3% | 7.6% |
| 新 strong（k=0，自适应） | 2.54 | 1192 | 87.6% | 0.22% |
| 参考文件 | — | 844 | 82.9% | 6.5% |

结合"`hr` 1–3px + `halo` 0.10–0.16"，头发/皮肤附近的过渡区不再被整片推白，发丝层次保留。

### 15.1 AVIF 的 DPI：手写 Exif item
- ffmpeg 的 avif muxer 不写 Exif；libheif 认的布局要自己拼：
  `iloc` 追加 item → `iinf` 追加 `infe`（**version 必须 2**，v0 没有 item_type 字段，会被静默忽略）→
  `iref` 写 HEIF box 形式的 `cdsc`（`u32 size + "cdsc" + from + count + to`）→
  Exif 数据**追加进 mdat 并把 mdat size 加大**；载荷是 `u32 offset=6 + "Exif\0\0" + TIFF`。
- TIFF 里 0x011A/0x011B 必须是 RATIONAL `(dpi*100, 100)`；写 `(dpi*100, 1)` 会显示成 24000 dpi。
- 验证：Explorer 面板 `240 dpi / 240 dpi、3240×1355、位深 64`；`pillow_heif` 读到 72 字节 exif；
  `avif.decode()` 正常；无头 Edge 仍能渲染。

### 15.2 产物整理
- 只保留最新产物：`out/sample/`（3240×1355 六种格式）与 `out/x4/`（12960×5420 的 heic + Ultra HDR jpeg）。
- 中间实验目录（`_probe*.avif`、`_x4_*`、`_hair_*`、`_avif_tmp*.py` 等）已删除。


## §16 v2.0：按格式重构（2026-11-24）

用户要求：不再分档位，只分输出格式与质量；不再输出 jpg；各格式用支持的最高色深
（PNG16、HEIC 10/12、AVIF12）；颜色必须正确映射；注释优化。

### 16.1 接口变化（破坏性）
- `pipeline.build(src, outdir, formats=("png","heic10","heic12","avif"), quality=100, dpi=None, transfer="PQ", debug=False)`
- 取消：`profile` / `gmax` / `preview` / `reuse_base` / `avif_bits` / `heic_sdr` / `choose_profile` / `PROFILES`。
- 产物名：`_hdr16_pq.png`、`_hdr10_pq.heic`、`_hdr12_pq.heic`、`_hdr12_pq.avif`。
- CLI：`-f png,heic10,heic12,avif`、`-q 50..100`、`--transfer PQ|HLG`、`--validate`、`--debug`、`--json`。
- `__version__ = "2.0.0"`；`ultrahdr.py` 保留（历史产物校验用），不再参与构建。

### 16.2 自适应阈值改为亮度分位
- 固定 `b0=0.78+0.10t` 换成按图分位：`b0 = clip(p99-0.05, 0.55, 0.95)`、`b1 = clip(p99.9+0.004, b0+0.02, 1)`。
- 依据：Chapiro 2023 的局部适应亮度思想（阈值随适应亮度缩放）；这里用图像自身分位代替适应亮度。
- 效果（3240 样本）：gmax 2.16、峰值 896 nits、不动 98.0%、>2× 0.1%；头发区域 gain>1.05 从 9.7% 降到 2.2%。
- `halo/hr/gmax` 仍随 `t = clip(p50/0.6)` 走：0.16→0.10、3px→1px、2.2→1.6 EV。

### 16.3 颜色映射修复
- **HEIC 之前 matrix=6（BT.601）**：现在 `matrix_coefficients=ITU_R_BT_2020_2_NON_CONSTANT_LUMINANCE`(9)，
  实测三份 HEIC/AVIF 都是 `nclx 9/16/9`；PNG 保持 `cICP 9/16/0/1`（RGB 用 matrix 0 是对的）。
- `validate_heic` 增加 matrix 检查与 12bit 支持；`validate_png16` 接受 HLG 的 cICP 9/18/0/1。

### 16.4 其他
- HEIC：`heic10.encode(bits=10|12)`，12bit 走 `SAVE_HDR_TO_12_BIT`；删除 `encode_sdr`。
- AVIF：`quality` 映射 crf；12bit 4:4:4 优先，失败逐级降级。
- 测试重写：四格式校验、PNG 双解码路径一致、HEIC 10/12 位深+nclx(matrix=9)、AVIF 12bit、分位阈值、质量影响体积。
- 交付：`C:\Users\xiaof\Desktop\示例\`（3240×1355 四格式 + `_预览_四格式对比.png`；12960×5420 另出）。

### 16.5 交付实测（2026-11-24 21:35）
- 3240×1355：gmax 2.16、峰值 896 nits、不动 98.0%、>2× 0.1%；四件 240 dpi（PNG 位深 48、HEIC/AVIF 64）。
- 12960×5420：gmax 2.16、峰值 905 nits、不动 98.4%、>2× 0.7%；体积 png 172.3 MB / heic10 7.31 MB / heic12 6.96 MB / avif(q100) 70.8 MB。
  ⇒ AVIF 质量 100 等于 crf 0，体积代价大；要小文件用 `-q 80` 左右（q80 ≈ crf 13）。
- 分位规则对比（3240 样本，头发区 gain>1.05）：旧 `b0=0.78+0.10t` 2.2% → 分位 `b0=p99-0.05` 0.7%；
  大亮图整图 9.0% → 6.1%。阈值确实压到了"接近白的高光"。

### 16.6 更正：jpeg（Ultra HDR）保留
- 用户澄清"不再输出 jpg"指的是**不要 .jpg 后缀/普通 jpg**，Ultra HDR 的 **.jpeg 仍然需要**。
- 恢复 `pipeline` 的 `jpeg` 格式：`FORMATS=("jpeg","png","heic10","heic12","avif")`；
  产物 `_ultrahdr.jpeg`；源是 JPEG 且 DPI 不变时主图仍复用原始字节（`base_reused`）。
- 增益图按 `quality` 编码半分辨率灰度 JPEG；封装走 `ultrahdr.build(gmax=cfg["gmax"], dpi, icc)`。
- tests 增加 Ultra HDR 断言（两条 MPEntry、`hdrgm:Version`、增益图 SOI、MPF 在 SOS 之前）。

## §17 安卓 16「白色区域变黑」的定位与处置（2026-11-25）

现象：手写封装的 Ultra HDR `.jpeg` 在安卓 16 相册里，**白色/高光区域**出现黑块；桌面 Chromium
渲染正常，手机截图也拍不到（截图走 SDR 合成路径）。

排除过程（每次只改一个变量，出文件到实机看）：峰值高低、主图 ICC、增益图通道数、增益图分辨率、
增益值封顶、图像尺寸、基图纯白压制、XMP 属性写法 vs 元素写法——全部仍黑。增益图全分辨率时更严重。

决定性对照（同一张图、参数完全相同）：
- M1 `iso_flags=0`（仍写 ISO 21496-1）→ 有黑影
- M2 **完全不写 ISO**，只留 XMP `hdrgm` → 正常
- M3 增益图取反 → 手机显示确实反向（说明解码端极性正常）

结论：安卓 16 的 HDR 路径会**优先读 ISO 21496-1 元数据**，而它对本工具写的 61 字节解释有误，
把白色区域算成黑；只留 XMP 时显示正常。因此：

- `pipeline.build` 默认 `write_iso=False`（`hdrgainmap/ultrahdr.py` 仍保留 `iso_flags`/`write_iso` 参数）。
- `validate.py` 把「没有 ISO 段」降为 info，不再告警。
- 若将来要恢复 ISO 输出，先确认 61 字节里 headroom/flags 的语义与目标机一致，再改默认值。
