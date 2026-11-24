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
