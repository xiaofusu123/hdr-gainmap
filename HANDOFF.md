# 交接文档 —— SDR 插画 → HDR（v2.1.0）

> 覆盖 2026-11-23 ~ 11-25 全部会话（v1.0 → v2.1.0）。代码在 `D:\Program\Code\hdr-gainmap`，
> 实验资产在 `D:\Program\App\DSH\work\15-hdr-202611\`。
>
> 当前状态：`__version__ = "2.1.0"`，git HEAD `cfcdf8d`（前置 `c1da4e8`）。
> 交付物在 `C:\Users\xiaof\Desktop\示例\HDR输出`（6 源图 × 5 格式 + 对比图 + `_batch_report.json`）。

---

## 0. 一句话

把 8bit SDR 插画做成**在真 HDR 屏上确实会亮**的图，不靠 AI 反色调映射，而是估计"这张图里
光从哪来、照到哪"，只给**被光照亮的部分**加增益；同一份 HDR 线性光输出成 5 种容器：

| 产物 | 后缀 | 位深 / 传递函数 | 主要用途 |
|---|---|---|---|
| Ultra HDR JPEG | `_ultrahdr.jpeg` | 8bit 基图 + 8bit 增益图（XMP hdrgm） | 安卓相册、Chrome |
| 16bit PNG | `_hdr16_pq.png` | 16/通道 + cICP(9,16,0,1) | 无损母版、专业工具 |
| HEIC 10bit | `_hdr10_pq.heic` | 10bit + nclx(9,16,9) | 苹果/安卓、体积小 |
| HEIC 12bit | `_hdr12_pq.heic` | 12bit + nclx(9,16,9) | 最高色深；WIC 打不开 |
| AVIF 12bit | `_hdr12_pq.avif` | 12bit 4:4:4 + nclx(9,16,9) + Exif DPI | 未来格式、体积极小 |

**教训一句话**：容器错一个字节 = 无声失败；增益放错地方 = 整幅发灰；显示端有坑 = 文件再对也白搭。

---

## 1. 管线总览

```
源图 (JPEG/PNG, sRGB 8bit)
  │  load_rgb(): RGBA/P 贴黑底 → RGB，float32/255
  ▼
luma() ── 线性亮度 Y（BT.709 权重，先 srgb_to_linear）
  │
  ├─ adapt_params(Y)：按这张图自己的分位定阈值/半径/峰值
  ▼
emissive(Y, cfg)：光照场 + 辐照度扩散 + 环境光/AO + 细碎高光 → emis ∈ [0,1]
  ▼
finalize(emis)：指数饱和（去硬截断光圈）→ gauss(·,2)
  ▼
gain_of(emis, gmax, lift=1.0) → 线性增益 gain ≥ 1
  ├─ Ultra HDR：code = log2(gain)/log2(gain.max())，8bit + 抖动 → 半分辨率灰度 JPEG
  │              gmax_meta = log2(gain.max()) 写进 XMP
  └─ PNG/HEIC/AVIF：hdr_linear = srgb_linear × gain（全分辨率）
                     × 203 nits → BT.2020 → PQ/HLG → 16/12/10bit
```

所有格式共用**同一条 gain 曲线**，没有格式旁路，因此不同容器解出来的画面一致。

---

## 2. 增益图算法（v2.1 光传输模型）

### 2.1 为什么不用 AI 反色调映射

照片训练的 ITM（SingleHDR / LumaFlux / GM-ITM 一类）在二次元平涂上必然翻车：平涂糊出块状增益、
线稿整条发光、纯白死白、cel-shading 出色带。而且 SDR 插画的高光**本来就已削顶**，没有"暗藏细节"
可恢复，硬提只会发灰。

### 2.2 四层判据（全部是低频量，避免"同样亮却一块亮一块不亮"）

```python
illum = gauss(Y, min(h,w)//25)
lo, hi = percentile(illum, 38), percentile(illum, 99)
lit   = smoothstep(lo, hi, illum)                  # ① 被照到的整片区域

spark = smoothstep(p0, p1, Y / (gauss(Y,25)+1e-4)) # ② 细碎高光（星芒、小光斑）

irr   = smoothstep(0.35, 0.85, light.irradiance(Y))# ③ 光传播/衍射/漫反射场

near, far = gauss(Y, min(h,w)//60), gauss(Y, min(h,w)//20)
ao   = clip((near+0.02)/(far+0.02), 0.45, 1.0)**0.9
amb  = 0.10 * smoothstep(0.02, 0.45, far) * ao     # ④ 全局光照 + 环境光遮蔽

emis = base_lit*lit + 0.45*irr + 0.45*spark + amb
emis = halo*gauss(emis, hr) + (1-halo)*emis        # 薄泛光（1–3px）
```

- ① 的阈值区间故意拉宽（38–99 分位）：窄区间会在平滑渐变上留下**等值线圈**，白布上就是那圈"光环"。
- ③ 见 `light.py`：光源取亮度 96 分位以上的部分，4 个尺度（σ 逐级×2、权重 σ^-1.75）扩散，
  核心近似高斯、尾部近似 1/r^p；再乘表面反射率 `a + (1-a)·Y`（浅色表面反射多）。
  降采样到长边约 900px 计算，大图也快。
- ④ 让离光源远的暗部有一点环境光，凹缝（局部比大范围暗）拿到更少。

### 2.3 参数（`gainmap.adapt_params`）

```python
t = clip(p50 / 0.60, 0, 1)                         # 0=夜景，1=高调
b0 = clip(p99   - 0.05, 0.55, 0.95)                # 只有最亮 ~1% 开始抬
b1 = clip(p99.9 + 0.004, b0 + 0.02, 1.0)           # 99.9 分位饱和
halo = 0.16 - 0.06*t        # 暗 0.16 → 亮 0.10（只柔化，不铺底）
hr   = max(1, round(3-2*t)) # 暗 3px → 亮 1px（用户要求 1–3px）
gmax = min((2.2 - 0.6*t) * PEAK_SCALE, GMAX_CAP)   # PEAK_SCALE=0.70, GMAX_CAP=1.45
                            # 上限 1.45 EV 是实机试出来的：>~1.6 EV 时高光会出现黑斑
base_lit = 0.78 - 0.16*t    # 高调图少抬，低调图多抬
lift = 1.0                  # 不做整体抬升
```

残留的 `pr2/pr3/q0/q3` 是中尺度"打光方向"判据的实验参数，目前不参与 `emissive` 主体，
只留作后续可选项。

### 2.4 饱和与抖动

- **指数饱和**：`em = 1 - exp(-emis/p99.9 * 1.6)`，再 `gauss(em, 2)`。
  硬截断会在亮部内部造出"顶到 1.0 的平台"，平台边界就是可见的等值线光圈。
- **抖动**：8bit 增益图量化前加三角分布噪声（±0.5 LSB），消除平滑渐变上的马赫带。
- `render_preview()` 用于 SDR 预览：`x≤1` 原样，`x>1` 走 `1 - knee*exp(-(x-1)*k)`。

### 2.5 历史遗留

v1.0 的 `PROFILES`（strong/detail/lights/soft/night）与 `fit_width()` 已在 v2.0 删除：
固定档位在多分辨率下表现不一致（12960 宽时 k=4 把半径放大 4 倍，抬起面积 17%→29%），
现由分位参数替代。

---

## 3. 色彩链路与元数据

### 3.1 链路

```
sRGB 8bit ─srgb_to_linear→ 线性 sRGB ─×gain→ 线性 HDR
  ─×203nits→ BT.709 线性 ─BT709_TO_2020→ BT.2020 线性
  ─pq_oetf→ 码值 ─×65535 / ×4095 / ×1023→ uint16/12/10bit
```

- `SDR_WHITE_NITS = 203`（`heic10.py`）。
- **HLG**：`hlg_oetf(hdr2020, HLG_PEAK_NITS / 203)`，`HLG_PEAK_NITS = 1000.0`。
  早期误传 `SDR_WHITE_NITS` 当参考峰值，画面暗约 41×。
- 预览/对比图的正确做法：PQ 解码后先 `lin2020 @ inv(BT709_TO_2020).T` 转回 sRGB，
  再按 203 nits 裁切或软肩；否则偏紫偏暗。

### 3.2 各容器标签

| 容器 | 标签 | 说明 |
|---|---|---|
| PNG16 | `cICP = (9,16,0,1)`（HLG 时 18） | RGB 图 matrix 用 0 |
| HEIC | `nclx = (9,16,9,1)` | **matrix 必须 9**，曾写成 6（BT.601）导致偏色 |
| AVIF | `nclx = (9,16,9,0)` | ffmpeg muxer 恒写 2/2，必须编码后改 |

HEIC 的 matrix 走 `pillow_heif` 参数：
`matrix_coefficients=ph.HeifMatrixCoefficients.ITU_R_BT_2020_2_NON_CONSTANT_LUMINANCE`。
AVIF 的 nclx 由 `avif.patch_nclx()` 在落盘后改写 7 字节：
`struct.pack(">HHHB", 9, 16|18, 9, 0)`。

---

## 4. Ultra HDR 容器（仍然是最脆弱的一环）

### 4.1 布局与不动点

```
SOI | APP0 JFIF | APP2 ICC | APP1 Exif | APP2 MPF | APP1 XMP(主图)
    | APP2 ISO 存根 | <主图……EOI> | SOI + APP1 XMP(增益图) + APP2 ISO 61B + <增益图……EOI>
```

`MPF.imageOffset` 是相对 MP 头 TIFF 起点的偏移，而 MPF 段长度又取决于主图长度 ⇒
**迭代 8 次求不动点**；`tiff_abs = len(jfif) + len(icc) + len(exif) + 8`。

### 4.2 MPF（APP2）

- 段长 **90 字节**（marker 2 + length 2 + TIFF 86），TIFF 头是标准 **4 字节** `II*\0` + u32 IFD 偏移；
- 3 个标签：`0xB000`（version，type 7，count 4，值 `"0100"`）、`0xB001`（NumberOfImages=2）、
  `0xB002`（MPEntry，type 7，count 32，值 = 50）；
- 条目 16 字节：`attr | size | offset | dep1 | dep2`（小端）；
- 主图 `attr & 0x0FFFFFFF == 0x00030000`、`offset == 0`；增益图 `attr == 0`、offset 相对 TIFF；
- 段内 TIFF 总长 82 字节，两条条目固定从 TIFF+50 开始。

### 4.3 XMP

主图与增益图**各写一份**完整 hdrgm（数值必须一致）：主图同时带 `Container:Directory`
（Primary 无 Item:Length、GainMap 有 Item:Length=增益图长度）；增益图只带参数。
`GainMapMax = HDRCapacityMax = gmax_meta`，`GainMapMin = 0`，`Gamma = 1`，
`OffsetSDR = OffsetHDR = 0.015625`，`BaseRenditionIsHDR=False`，数值用 `.8g` 格式化（曾用 `%.1f` 丢精度）。

### 4.4 ISO 21496-1：**默认不写**

61 字节单通道元数据（`flags` 置 bit6 `useBaseColorSpace`，bit7 多通道）：

```
u16 min_version=0, u16 writer_version=0, u8 flags=0x40,
u32 base_headroom N/D=0/1, u32 alt_headroom N/D=gmax/1,
每通道 5 组 (N,D): GainMapMin 0/1, GainMapMax gmax/1, Gamma 1/1,
                   OffsetSDR 1/64, OffsetHDR 1/64
```

**安卓 16 的 HDR 路径优先读 ISO 段**，且它对这段元数据的解释与本工具不一致——
实测表现为**白色区域变黑**（桌面 Chromium 正常、手机截图也拍不到）。逐项排除后确认：
只留 XMP 时完全正常，增益图取反时手机确实反向（解码端极性没问题）。

⇒ `pipeline.build(write_iso=False)` 为默认；`ultrahdr.build()` 仍保留 `iso_flags` / `write_iso` 参数，
将来若确认某机型的语义可以再开。

---

## 5. 踩坑清单

### 5.1 v1.0（容器与编码）

| # | 症状 | 根因 | 修复 |
|---|---|---|---|
| 1 | 手机不亮，自检全过 | MPF 主图 attr 写成 `0x03000000` | 按 CIPA DC-007 改 `0x00030000` |
| 2 | 同上 | 增益图 offset 写成绝对偏移 | 改相对 MP 头 |
| 3 | 同上 | 增益图 attr 写成 `0x02000000` | 改 `0x00000000` |
| 4 | 同上 | ISO 段只有命名空间、缺 4 字节版本 | 补 min/writer version |
| 5 | 同上 | 增益图没有任何 ISO 元数据 | 补 61 字节 |
| 6 | PNG 彩虹竖条纹 | 滤波左邻偏移按 1 字节算，规范要 `bpp=6` | `shift(cur, 6)` + 用 Pillow 独立解码验证 |
| 7 | 面板显示 72 DPI | JPEG 没传 dpi、PNG 没写 pHYs、HEIC 无 DPI 字段 | 各自补齐（HEIC 走 EXIF 282/283/296） |
| 8 | HEIC 显示 72 DPI | WIC 缺 DPI 时回退 72 | 写 EXIF 分辨率 |
| 9 | 12bit HEIC 打不开 | Windows HEVC 只有 Main10 | `0xC00D3E8C`；用 pillow-heif/ImageMagick 读 |
| 10 | 预览出现条纹 | 色调映射在 x=1 处不连续 | 改成连续式 |
| 11 | 预览整幅偏亮 | sRGB 当线性又套一次 gamma | 先 `srgb_to_linear` |
| 12 | 增益全 0 | `load_rgb()` 已归一化又除 255 | 直接用返回值 |
| 13 | 小图档位套大图糊 | 半径是绝对值 | （已由 v2.0 分位参数取代） |
| 14 | DecompressionBomb | PIL 像素上限 | `Image.MAX_IMAGE_PIXELS=None` |
| 15 | `KeyError: 'HEIF'` | 没注册 opener | `register_heif_opener()` |
| 16 | `unrecognized image mode` | 对 `RGB;16` 调 `to_pillow()` | 改 `np.asarray(f)` |

### 5.2 v1.1 以后新增（按发现顺序）

| # | 症状 | 根因 | 修复 |
|---|---|---|---|
| 17 | 结构自检过但手机不认 | MPF TIFF 头按 2 字节写 | 必须 `II*\0` + u32 IFD（4 字节） |
| 18 | 同上 | APP2 段漏掉 2 字节长度字段（88≠90） | `_MPF_SEG_LEN=90` 并断言 |
| 19 | 大图元数据跑到扫描数据后面 | `_split_app0` 读 `jpeg[2:4]`（那是 `FF E0`） | 长度在 `jpeg[4:6]`，`end=4+length`；APP0 内出现 SOS 直接报错 |
| 20 | Pillow 警告 tag 296 too many entries | Exif SHORT 的 count 按字节数算 | `count = len(raw)//unit` |
| 21 | 部分解码器偏色 | HEIC `matrix=6`（BT.601） | 改 `matrix=9` |
| 22 | HLG 画面暗 41× | `hlg_oetf` 参考峰值传了 203 | 传 `1000/203` |
| 23 | 反 PQ 恒为 0 | 漏了 `p**(1/M2)` | 修正并加回归 |
| 24 | 复用源 JPEG 出现重复元数据 | 直接拼 `primary[2:]` | `strip_metadata()` 先剥旧 APP1/APP2 |
| 25 | 白布上一圈"光环" | 亮部增益硬截断到 1.0 形成平台 | 指数饱和（处处连续） |
| 26 | 渐变区马赫带 | 8bit 量化 | ±0.5 LSB 抖动（固定种子） |
| 27 | AVIF 色彩标签恒为 unspecified | ffmpeg avif muxer 不写 | 落盘后 `patch_nclx()` 改 7 字节 |
| 28 | AVIF 管道解码报 partial file | ISOBMFF 需要 seek | 写临时文件再解 |
| 29 | AVIF 没有 DPI | muxer 不写 Exif | 手写 item：`iloc` + `iinf`(`infe` **version 2**) + `iref`(HEIF box `cdsc`)，数据追加进 `mdat` |
| 30 | AVIF 显示 24000 dpi | RATIONAL 写成 `(dpi*100,1)` | 写 `(dpi*100,100)` |
| 31 | 抖动导致 `UnboundLocalError` | 用了尚未定义的 `gm_px.shape` | 用 `code.shape` |
| 32 | 手机白区变黑 | 安卓 16 优先读 ISO 21496-1 且解释错误 | 默认只写 XMP（§4.4） |
| 33 | SDR 预览面板空白 | 预览 JPEG 没写 DPI | `save(..., dpi=(dpi,dpi))` |

---

## 6. 实测数据

### 6.1 v1.0（strong 档，五图，主图字节不动）

| 图 | 尺寸 | 峰值 | 不动像素 | Ultra HDR |
|---|---|---|---|---|
| 鸣潮（深夜光柱） | 3200×1367 | 1217 nits | 70.9% | 1.88 MB |
| 泡泡（蓝夜星芒） | 3240×1355 | 1231 nits | 77.7% | 1.43 MB |
| 搜图神器（夜景光球） | 5800×3066 | 1213 nits | 75.4% | 10.19 MB |
| 白猫（高调 detail） | 1433×2000 | 611 nits | 55.7% | 2.49 MB |
| 高调2（detail） | 1414×2000 | 604 nits | 34.8% | 3.37 MB |

- Ultra HDR vs PQ HEIC 同像素均值差 0.033 EV / p95 0.107 / p99 0.239（仅编码与色度取样差异）。
- 16bit PNG 往返 max diff 0；12bit HEIC 1,031,312 B，pillow-heif 与 ImageMagick 都读回 12bit。

### 6.2 v1.1（8192×12288，100.7 Mpx，JPEG 源复用字节）

峰值 1247 nits、gain_mean 1.409、不动 22.2%；Ultra HDR 10.59 MB、PNG16 121.54 MB、HEIC 5.81 MB；
端到端 **5m39s**（jpg 2m42s → png +2m43s → heic 14s）。

### 6.3 v2.0（四格式，分位阈值）

- 3240×1355：gmax 2.16、峰值 896 nits、不动 98.0%、>2× 0.1%。
- 12960×5420：gmax 2.16、峰值 905 nits、不动 98.4%；png 172.3 / heic10 7.31 / heic12 6.96 / avif 70.8 MB（q100=crf0）。

### 6.4 v2.1（Z1：指数饱和 + 抖动，批量 6 图 × 5 格式）

| 源 | gmax | 峰值 | jpeg / png / heic10 / heic12 / avif (MB) |
|---|---|---|---|
| 1687539284005.jpg（1433×2000） | 1.12 | 377 | 2.32 / 8.00 / 0.47 / 0.45 / 3.12 |
| 1688558881488.jpg（1414×2000） | 1.12 | 377 | 3.12 / 11.07 / 0.71 / 0.70 / 4.19 |
| 搜图神器（5800×3066） | 1.45 | 453 | 10.39 / 75.87 / 6.62 / 6.51 / 28.11 |
| upscaled-4x_00004_（12800×5468） | 1.45 | 453 | 34.65 / 186.90 / 6.52 / 6.05 / 74.33 |
| upscaled-4x_00008_（4000×6800） | 1.36 | 432 | 15.43 / 82.35 / 3.16 / 2.93 / 31.33 |
| 1777789210281_x4（12960×5420, 240dpi） | 1.45 | 453 | 36.73 / 195.54 / 7.75 / 7.37 / 74.88 |

全部 `validate` 通过；DPI 与源一致（AVIF 的 Exif item 生效）。

### 6.5 AVIF 质量/体积（3240×1355，参照 PNG16 码值）

| 配置 | 体积 | mean abs | p99 |
|---|---|---|---|
| 12bit 4:2:0 crf20 | 73.8 KB | 282.2 | 1554 |
| 12bit 4:4:4 crf8 | 244.4 KB | 138.3 | 619 |
| 12bit 4:4:4 crf4 | 490.0 KB | 106.8 | 604 |
| 10bit 4:4:4 crf4 | 514.7 KB | 94.4 | 373 |
| 10bit HEIC（对照） | 1013.4 KB | 91.1 | 578 |

默认 4:4:4：插画色边是硬边，4:2:0 会把彩色高光糊掉。

---

## 7. 设备侧结论

| 环境 | 结果 |
|---|---|
| 荣耀 GT / MagicOS 10 / Android 16 | Ultra HDR 正常显示（修完 MPF 后）；**优先读 ISO 21496-1 且会误读 ⇒ 白区变黑**；SDR 模式下把 PQ 码值整体抬高（连 gain=1 的纯 SDR 套 PQ 也偏亮） |
| Windows 11 + WIC | HEIC 10/8bit ✓，12bit ✗（`0xC00D3E8C`）；AVIF ✓；照片 App 不认 Ultra HDR |
| Edge / Chromium（无头） | 可渲染 Ultra HDR 主图、PQ PNG/HEIC、12bit AVIF，适合做兼容性验证 |

"看不出效果"的排查顺序：① 设备真在 HDR 模式？→ ② 软件认这个容器？→ ③ 增益是不是只落在本来就
255 的像素上（内容问题）？

判别容器是否被解码，最省事的是**阶梯测试图**：基图纯灰，增益图分四段 0/85/170/255；
SDR 下四格同灰，HDR 下四级阶梯。

---

## 8. 代码结构与用法

```
hdrgainmap/
├── color.py      sRGB↔线性、PQ/HLG OETF、BT.709→BT.2020
├── gainmap.py    luma / adapt_params / emissive / finalize / gain_of / render_preview
├── light.py      辐照度场：光源提取 + 多尺度扩散 + 漫反射
├── ultrahdr.py   Ultra HDR 容器：build / parse / read_gainmap / reconstruct
├── png16.py      16bit PNG 编解码（手写滤波 bpp=6；解码优先 ffmpeg 管道）
├── heic10.py     10/12bit HEIC 编码、解码与 nclx 检查
├── avif.py       AVIF 12bit：ffmpeg 编码、box 解析、nclx 补丁、Exif DPI 注入
├── validate.py   合规校验（Ultra HDR / PNG16 / HEIC / AVIF）
├── pipeline.py   一次读取 → 增益图 → 各容器编码
└── cli.py        命令行
```

```bash
python -m hdrgainmap 图.png -o out                       # 默认全格式（jpeg,png,heic10,heic12,avif）
python -m hdrgainmap 图.jpg -o out -q 90                 # 质量 50–100，默认 100
python -m hdrgainmap 图.png -o out -f png,heic12         # 只出指定格式
python -m hdrgainmap 图.png -o out --validate --json     # 边出边校验
python -m hdrgainmap.validate 产物.jpeg                  # 事后校验
python tests/test_roundtrip.py                           # 自检
```

```python
from hdrgainmap import pipeline
res = pipeline.build("图.png", "out/", formats=("jpeg","png","heic10","heic12","avif"),
                     quality=100, transfer="PQ")
res["stats"]   # peak_nits / gain_mean / untouched_pct / base_reused
res["gmax"], res["gmax_meta"]
```

`pipeline.build` 的调试开关（默认都不该打开）：`gain_rgb`、`gain_scale`、`iso_flags`、`write_iso`。
`tests/test_roundtrip.py` 覆盖：五格式校验、PNG 双解码路径一致、HEIC 10/12 位深与 nclx(matrix=9)、
AVIF 12bit、Ultra HDR 结构与 `hdrgm:Version`、分位阈值与质量对体积的影响。

---

## 9. 产物位置

| 内容 | 路径 |
|---|---|
| 代码 / 文档 | `D:\Program\Code\hdr-gainmap`（git 仓库） |
| 实验资产 | `D:\Program\App\DSH\work\15-hdr-202611\{scripts,assets,output,research}` |
| 交付示例（6 图 × 5 格式） | `C:\Users\xiaof\Desktop\示例\HDR输出` |
| 对比图 | 同上 `_预览_五格式对比_00008.png` |
| 逐图统计 | 同上 `_batch_report.json` |

`out/` 是临时目录，已在 `.gitignore` 里；提交前清空。

---

## 10. 未解决 / 后续可做

1. **AVIF 目前只是"纯 HDR AVIF"**，不是 gain map AVIF（AOM `tmap` derived item 尚未实现）。
   要做的话需要 `tmap` 引用 SDR 基图与增益图两个 item，工作量和 Ultra HDR 封装的坑类似。
2. **SDR 回退由显示端决定**：HEIC/AVIF 只有一张 HDR 图，SDR 模式下由查看器自行映射；
   安卓 16 会把 PQ 码值整体抬高，导致 SDR 过曝。实测中间调压 0.70 可让 SDR 接近正确，
   但 HDR 中间调会暗 0.5 EV，因此**没有默认开启**。需要时可在编码前对 PQ 码值做
   `k = comp + (1-comp)*(code/cmax)**2.5`（峰值不变），并同时提供两版文件。
3. **iOS/Safari 未验证**（手上没有设备）；HEIC 10bit + PQ 理论上是苹果原生路径。
4. **12bit HEIC 的实际显示**：Windows 端注定打不开；别把它当交付主格式。
5. **光源检测仍是启发式**（96 分位 + 多尺度扩散）。极端构图（大面积白墙、全局泛光）会误判，
   目前靠 `b0/p99` 的分位阈值兜底，没有语义分割。
6. **性能**：12960×5420 单图约 6 分钟，主要花在 PN16 编码与 AVIF q100；100MP 以上内存峰值未测。
7. `.avif/.heic` 文件关联：Win11 UserChoice 会挡住程序化设置，只能用户手点一次。

---

## 11. 版本时间线

| 版本 | 主要变化 |
|---|---|
| v1.0 | 三格式（Ultra HDR / PNG16 / HEIC10）+ 五档位算法；MPF/ISO 首次跑通 |
| v1.1.0 | `validate.py`；Ultra HDR 封装重写（MPF 4 字节 TIFF 头、XMP 拆分、ICC 自动补）；主图零重编码；PNG16 ffmpeg 快解码；`auto` 档 |
| v1.2.0 | AVIF 12bit（ffmpeg + nclx 补丁）；SDR HEIC 校验；`pq_eotf` 修复 |
| v1.3.0 | 去掉 `fit_width` 半径放大；按 `p50` 自适应的 strong；AVIF Exif DPI；预览补 DPI |
| v2.0.0 | 删档位，改分位阈值；格式改为 png/heic10/heic12/avif；HEIC matrix 9；质量默认 100 |
| v2.1.0 | 光传输模型（光照场 + 辐照度扩散 + 环境光/AO + 细碎高光）；指数饱和去光圈；8bit 抖动；默认只写 XMP（安卓 16 白区变黑）；恢复 `.jpeg` Ultra HDR |

---

## 12. 资料与出处

| 资料 | 用途 |
|---|---|
| [Android Ultra HDR v1.1](https://developer.android.com/media/platform/hdr-image-format) | 增益公式、XMP 属性表、GContainer、ISO 兼容与容错 |
| [google/libultrahdr](https://github.com/google/libultrahdr)（`jpegr.cpp`、`gainmapmetadata.cpp`、`multipictureformat.cpp`） | MPF 字节布局、XMP 拆分、61 字节编码、`hasValidMpfAssociation()` |
| [ISO/IEC 21496-1:2025](https://www.iso.org/standard/86775.html) | 增益图标准目录（正文付费；预览只有前置页） |
| [CIPA DC-007](https://www.cipa.jp/std/documents/e/DC-007_E.pdf) | MPF 条目属性与偏移语义 |
| [AOM AVIF 规范](https://aomediacodec.github.io/av1-avif/) | 后续做 AVIF gain map 的 `tmap` 依据 |
| Chapiro 2023, *Perceptually Adaptive Real-Time Tone Mapping*（`research/tar23.pdf`） | 阈值随适应亮度缩放——分位阈值的思想来源 |
| [gainmaps.com](https://gainmaps.com/) | Ultra HDR / Adaptive HDR / ISO 增益图术语对照 |

注：context7 文档服务在本机网络下不可达（连无关库都 `fetch failed`），全部以官方规范页与参考实现源码为准。

---

## 13. 最该记住的五条

1. **容器错一个字节 = 无声失败**。MPF 的属性/偏移基准、TIFF 头宽度、XMP 参数，任何一处错都不报错。
2. **安卓 16 会优先读 ISO 21496-1**，并且会把本工具写的 61 字节解释错（白区变黑）——默认只写 XMP。
3. **增益要落在"被光照到的地方"**，不是"亮的地方"。判据必须是低频量，否则同样亮度会一块亮一块不亮。
4. **不要硬截断**。饱和处用指数逼近、量化前加抖动，否则白布上会出现等值线光圈。
5. **合规 ≠ 好看**。高光已削顶的素材做不出明显的 HDR，这是内容的限制；整幅抬升只会得到灰雾。
