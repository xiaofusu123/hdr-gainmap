# 05 JPEG 与 Ultra HDR

五种产物里，只有 JPEG 是"老设备也能看"的。原因不在 JPEG 本身——它 1992 年就定型了，只有 8bit、不支持 HDR——而在它**可扩展的段结构**：Ultra HDR 把一张增益图"挂"在主图后面，用 MPF 指向它，用 XMP/ISO 描述它。老解码器忽略这些段，看到的就是一张完整的 SDR JPEG。

这一篇讲 JPEG 的容器结构，以及 Ultra HDR 是怎么在不破坏兼容性的前提下塞进 HDR 的。

## 1. JPEG 本身

### 1.1 定位

JPEG（ISO/IEC 10918，1992）[\[4\]](#r4) 是**有损**、8bit、基于 DCT 的格式。它的设计目标是"在可接受的质量下大幅压缩照片"，不是无损，也不是 HDR。今天它仍然是分发最广的图片格式，因为：

* 解码器无处不在（浏览器、系统、相机、打印机）；
* 文件小；
* 渐进式/基线式都有广泛支持。

它的短板也明确：8bit、4:2:0 常见、块效应、不支持透明（需要 PNG/WebP）、不支持 HDR。

### 1.2 编码管线

```text
RGB → YCbCr（可选降采样）→ 8×8 分块 → DCT → 量化 → zigzag → 熵编码（Huffman/算术）
```

* **DCT**：把 8×8 块从空间域变到频率域，能量集中到低频。
* **量化**：按质量参数缩放量化表，高频系数大量归零——这是有损的来源。
* **熵编码**：对量化系数做 Huffman（基线）或算术编码（少见）。

质量参数 `q` 不是线性映射：`q=100` 时量化表几乎全 1，仍有 DCT 舍入；`q=90` 已经很难看出差别；`q<50` 块效应明显。

### 1.3 文件结构

JPEG 是一串 marker：

| Marker        | 段         | 内容               |
| ------------- | --------- | ---------------- |
| `FFD8`        | SOI       | 图像开始             |
| `FFE0`        | APP0      | JFIF：版本、密度单位、DPI |
| `FFE1`        | APP1      | Exif：拍摄参数、朝向、分辨率 |
| `FFE2`        | APP2      | ICC profile（可多段） |
| `FFED`        | APP13     | Photoshop IRB 等  |
| `FFDB`        | DQT       | 量化表              |
| `FFC0`/`FFC2` | SOF0/SOF2 | 帧头：宽高、分量、采样因子    |
| `FFC4`        | DHT       | Huffman 表        |
| `FFDA`        | SOS       | 扫描开始 + 熵编码数据     |
| `FFD9`        | EOI       | 图像结束             |

关键点：**APPn 段对解码器是可选的**。不认识 `APP2` 的解码器会跳过它，继续解主图。Ultra HDR 正是利用这一点：所有 HDR 信息都在 APPn 和第二张图里，主图仍是标准 JPEG。

## 2. 为什么 JPEG 不能直接存 HDR

* **8bit**：每通道 256 级，线性光下暗部只有几个级值，banding 严重。
* **没有绝对亮度**：JPEG 只存 sRGB/YCbCr 码值，不告诉查看器"白是多少 nits"。
* **没有 HDR 元数据**：没有 PQ/HLG 标记，没有峰值信息。
* **广泛部署的解码器只认 SDR**：即使塞了 HDR 数据，它们也不会用。

所以路线只有两条：换格式（PNG/HEIC/AVIF/JXL），或者在 JPEG 里"旁路"存 HDR。Ultra HDR 选后者。

## 3. Ultra HDR 的结构

### 3.1 两张图

```text
主图：8bit SDR JPEG（完整、可独立解码）
增益图：8bit RGB JPEG（本项目全分辨率；规范允许 1/2 或 1/4，也允许单通道）
元数据：MPF（指向增益图）+ XMP（hdrgm 字段）+ ISO 21496-1（二进制字段）
```

主图必须带 ICC；增益图自带的 ICC/EXIF 一律忽略。

### 3.2 组装顺序

`hdrgainmap/ultrahdr.py` 的 `build()` 输出：

```text
FFD8                          SOI
 FFE0 APP0 JFIF
 FFE1 APP1 Exif               DPI、Software、Description
 FFE2 APP2 ISO                主图 4 字节 stub
 FFE2 APP2 MPF                两条 MPEntry
 FFE1 APP1 XMP                GContainer + hdrgm:Version
 <主图熵数据>
 FFD8                         第二张图的 SOI
 FFE1 APP1 XMP               完整 hdrgm（单通道回退）+ ISO 三通道
  FFE2 APP2 ISO               141 字节（三通道）
  <增益图熵数据>
```

两个 `SOI` 是合法的：MPF 把第二个 `SOI` 之后的内容当作"第二张图"。没有 MPF 的解码器看到第二个 `SOI` 会停止，所以主图必须在前、必须完整。

### 3.3 MPF (CIPA DC-007)

APP2 段以 `MPF\0` 开头，后面是一个**小端 TIFF**[\[1\]](#r1)：

```text
"MPF\0"
TIFF header: "II" 2A 00, offset to IFD = 8
IFD:
  0xB000 MPFVersion   type=7 (UNDEFINED) count=4  "0100"
  0xB001 NumberOfImages type=4 (LONG)    count=1  2
  0xB002 MPEntry      type=7             count=32  → 偏移 50
  next IFD = 0
MPEntry[0]: 16 bytes
MPEntry[1]: 16 bytes
```

每条 MPEntry：

| 字段 | 字节 | 主图           | 增益图               |
| -- | -- | ------------ | ----------------- |
| 属性 | 4  | `0x00030000` | `0x00000000`      |
| 大小 | 4  | 主图长度         | 增益图总长             |
| 偏移 | 4  | 0（相对文件头）     | 相对 **MPF TIFF 头** |
| 依赖 | 2  | 0            | 0                 |
| 保留 | 2  | 0            | 0                 |

**两个偏移基准**是最大的坑：

* 主图偏移相对**文件头**（所以是 0，因为主图就在文件开头）。
* 增益图偏移相对**MPF TIFF 头**（即 `MPF\0` 之后的 `II*\0` 位置）。

写错基准，解码器会读到错误位置，表现为"增益图缺失"或"花屏"。

### 3.4 长度与偏移的循环依赖

MPF 的 APP2 段固定 90 字节（4 字节段头 + 4 字节 `MPF\0` 签名 + 82 字节 TIFF 结构），位置取决于主图长度；增益图偏移又等于"主图长度 + 增益图头部长度"。所以：

```text
primary_len ← 初始估计
repeat:
    gain_rel = primary_len + len(gain_head)
    mpf      = build_mpf(primary_len, gain_len, gain_rel)
    primary_len' = len(jfif + icc + exif + iso_stub + mpf + xmp + primary_data)
until primary_len' == primary_len  (最多 8 次)
```

通常 2–3 次就收敛。项目里把上限设成 8，防止异常输入死循环。

这个不动点循环和每个字段的字节序列，可以拿 libultrahdr 的实现逐行对照[\[5\]](#r5)：它同样先估长度、再回填 `MPEntry`，直到两遍算出的 `primary_len` 一致；增益图偏移也以 MPF TIFF 头为基准。

### 3.5 XMP 字段

主图 XMP 里的 `Container:Directory` 告诉查看器"这个文件有两项"[\[2\]](#r2)：

```xml
<Container:Directory>
  <rdf:Seq>
    <rdf:li rdf:parseType="Resource">
      <Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary"/>
    </rdf:li>
    <rdf:li rdf:parseType="Resource">
      <Container:Item Item:Mime="image/jpeg" Item:Semantic="GainMap"
                     Item:Length="123456"/>
    </rdf:li>
  </rdf:Seq>
</Container:Directory>
```

增益图 XMP 里是完整字段：

| 字段                         | 本项目值             | 含义                |
| -------------------------- | ---------------- | ----------------- |
| `hdrgm:Version`            | "1.0"            | 版本                |
| `hdrgm:BaseRenditionIsHDR` | False            | 基图是 SDR           |
| `hdrgm:GainMapMin`         | 0                | log2 最小值          |
| `hdrgm:GainMapMax`         | `log2(max gain)` | log2 最大值          |
| `hdrgm:Gamma`              | 1.0              | 归一化幂              |
| `hdrgm:OffsetSDR`          | 1/64             | SDR 偏移            |
| `hdrgm:OffsetHDR`          | 1/64             | HDR 偏移            |
| `hdrgm:HDRCapacityMin`     | 0                | 最小 headroom（log2） |
| `hdrgm:HDRCapacityMax`     | `GainMapMax`     | 最大 headroom（log2） |

**必须用元素形式**：

```xml
<hdrgm:GainMapMax>1.45</hdrgm:GainMapMax>          ✅
<hdrgm:Version>1.0</hdrgm:Version>
```

而不是属性形式 `hdrgm:GainMapMax="1.45"`。部分 Android 16 / 荣耀设备只解析元素形式，属性形式会让高光变黑（增益被当成 0）。

## 3.6 ISO 21496-1 段

APP2，命名空间 + 二进制字段[\[3\]](#r3)。单通道是 61 字节；本项目写三通道，141 字节：

```text
"urn:iso:std:iso:ts:21496:-1\0"   26 字节（含结尾 0）
min_version   u16 = 0
writer_version u16 = 0
flags         u8 = 0xC0        # bit7=三通道，bit6=useBaseColorSpace
base_headroom       0/1
alternate_headroom  gmax/1
通道 0: min 0/1, max R/1, gamma 1/1, base_offset 1/64, alternate_offset 1/64
通道 1: min 0/1, max G/1, gamma 1/1, base_offset 1/64, alternate_offset 1/64
通道 2: min 0/1, max B/1, gamma 1/1, base_offset 1/64, alternate_offset 1/64
```

每个字段是一个 32bit 分子 + 32bit 分母，大端、各自独立。三通道合计 `5 + 16 + 3×40 = 141` 字节。

`flags` 的 bit6（0x40）表示"使用基图色彩空间"；bit7（0x80）表示三通道。**XMP 只支持单通道**——所以本项目在增益图 XMP 里写 `GainMapMax = max(R,G,B)` 作为旧解码器的回退，真正的逐通道数据只在 ISO 段里[\[5\]](#r5)。

主图里放 4 字节 stub（只有命名空间 + 两个 u16 0），表示"完整元数据在增益图那边"。这是 Ultra HDR 1.1 的约定：读取端先看主图有没有完整 ISO，没有就去增益图找。

## 4. 兼容性与已知问题

| 平台                    | 行为                               |
| --------------------- | -------------------------------- |
| Android 14+           | 系统相册/Google Photos 还原 HDR        |
| Android 16            | 优先 ISO 段；部分设备抬高 PQ 码值，SDR 基图可能过曝 |
| iOS                   | 不识别，显示基图（未实测）                    |
| Chrome                | 不解析 MPF，显示基图                     |
| Windows 照片            | 显示基图                             |
| Photoshop / Lightroom | 部分版本能识别增益图，部分只显示基图               |

已知问题（本项目实测/观察）：

1. **小端 bug**：早期把 0.89387586 写成小端，被读成 75，白区变黑；现已改为大端、分母 1e8。
2. **荣耀/Android 16 属性形式**：XMP 用属性写法时高光变黑；改元素写法解决。
3. **SDR 过曝**：部分 Android 16 设备把 PQ 码值抬高，导致 SDR 基图在 HDR 屏上过曝——这是设备行为，不是文件错误。
4. **只认 XMP 的解码器**：XMP 只有单通道 `GainMapMax`（取三通道最大），逐通道差异丢失；这是回退路径，不是目标效果。

## 5. 本项目的 JPEG 参数

| 参数    | 值                                | 理由           |
| ----- | -------------------------------- | ------------ |
| 基图质量  | `q=100`，`subsampling=0`（4:4:4）   | 质量优先，避免二次损失  |
| 基图复用  | 源是 JPEG 且无 alpha/ICC/朝向变换时直接复用字节 | 避免重编码        |
| 增益图   | 8bit RGB JPEG，`q=100`，`optimize` | 逐通道增益，DCT 友好 |
| 增益图尺寸 | 全分辨率（`w × h`）                    | 质量优先，边缘不糊    |
| 抖动    | ±1 码值三角分布，量化前                    | 抑制 banding   |
| 元数据   | XMP（单通道回退）+ ISO（三通道 141B）双写      | 兼容新旧读取端      |
| DPI   | 写入 Exif 282/283/296              | 打印/排版        |

## 6. 常见错误清单

1. **主图偏移按 TIFF 头算。** 主图相对文件头，增益图相对 TIFF 头。
2. **MPF 段长度算错。** 它会影响主图长度，进而影响增益图偏移；必须迭代。
3. **XMP 用属性形式。** 部分设备不认。
4. **增益图不是 8bit JPEG。** 规范要求 8bit 无符号 JPEG；要 RGB 三通道就得同时写三通道 ISO，只写 XMP 会退化成单通道。
5. **增益图与主图分辨率/朝向不一致。** 解码错位。
6. **只写 XMP 不写 ISO。** 新系统优先 ISO，缺失时行为不一致。
7. **以为老设备会"看到 HDR"。** 老设备看到的是基图，这才是兼容性的意义。

## 参考资料

<a id="r1"></a>**\[1]** CIPA. *DC-007: Multi-Picture Format*. <https://www.cipa.jp/std/documents/e/DC-X007-KEY_E.pdf> （访问日期 2026-11-27）

<a id="r2"></a>**\[2]** Android Developers. *Ultra HDR Image Format v1.1*. <https://developer.android.com/media/platform/hdr-image-format> （访问日期 2026-11-27）

<a id="r3"></a>**\[3]** ISO. *ISO/IEC 21496-1:2025 — Gain map metadata for image conversion*. <https://www.iso.org/standard/86775.html> （访问日期 2026-11-27）

<a id="r4"></a>**\[4]** ITU-T. *T.81: Information technology — Digital compression and coding of continuous-tone still images (JPEG)*. <https://www.itu.int/rec/T-REC-T.81> （访问日期 2026-11-27）

<a id="r5"></a>**\[5]** DeepWiki. *google/libultrahdr*（ISO 段结构、XMP 字段、增益图应用顺序）. <https://deepwiki.com/google/libultrahdr> （访问日期 2026-11-27）
