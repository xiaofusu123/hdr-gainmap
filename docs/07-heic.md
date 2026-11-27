# 07 HEIC

HEIC 是"苹果生态里的 HDR 默认格式"：iPhone 拍的 HDR 照片、iOS 相册、macOS 预览，默认就是 10bit HEVC 的 HEIC。它比 JPEG 小一半，比 PNG 小一个数量级，还能带 HDR 元数据。

但它也是最"平台绑定"的格式：Windows 需要装 HEVC 扩展，Chrome 不认，很多安卓机型的 12bit 支持不完整。

## 1. 名字与家族

* **HEIF**：High Efficiency Image File Format，ISO/IEC 23008-12，是一个**容器**规范，能装 HEVC、AV1、JPEG 等多种编码。
* **HEIC**：HEIF 里装 HEVC 的常见叫法，文件扩展名 `.heic`。
* **HEVC**：H.265，ISO/IEC 23008-2，视频编码标准；单帧图像就是"只有一帧的视频"。

所以：HEIC = HEIF 容器 + HEVC 编码。AVIF = HEIF 容器 + AV1 编码（见 [08](08-avif.md)），两者共享大量 box 结构。

## 2. 容器结构

HEIF 是 ISO BMFF（ISO/IEC 14496-12）的派生[\[1\]](#r1)[\[4\]](#r4)，核心是 box：

```text
ftyp       品牌：heic / mif1 / msf1 / heix ...
meta       元数据容器
  hdlr     处理器（pict）
  pitm     主图 item ID
  iinf     item 信息（infe：hvc1 / Exif / ...）
  iloc     item 数据位置（偏移 + 长度）
  iprp     属性
    ipco   属性容器（hvcC、ispe、colr、pixi、irot、imir ...）
    ipma   属性与 item 的关联
  iref     引用（thmb 缩略图、cdsc 描述、auxl 辅助图）
mdat       实际码流
```

读图流程：`pitm` 找主图 → `iloc` 找码流偏移 → `iprp` 找编码参数 `hvcC`、尺寸 `ispe`、色彩 `colr` → 解码。

### 2.1 colr / nclx

HDR 的关键在 `colr` box 里的 `nclx`[\[3\]](#r3)：

```text
colr
  type = "nclx"
  primaries      = 9   (BT.2020)
  transfer       = 16  (PQ) / 18 (HLG)
  matrix         = 9   (BT.2020 non-constant luminance YCbCr)
  full_range     = 1
```

**matrix 必须写 9**。HEVC 码流里是 YCbCr，不是 RGB；写成 6（BT.601）会让红色偏橙、蓝色偏紫——这是最常见的 HEIC 偏色原因。PNG 相反，PNG 存 RGB，matrix 必须 0。

### 2.2 编码参数

* **Main10**：10bit，4:2:0，主流[\[2\]](#r2)。项目输出 `heic10`。
* **Main12**：12bit，4:2:0，高动态余量。项目输出 `heic12`。
* **hvc1 vs hev1**：`hvc1` 把参数集（VPS/SPS/PPS）放在 `hvcC` 里，`hev1` 放在码流内。HEIC 一般用 `hvc1`，兼容性更好。

本项目用 pillow-heif 的 `SAVE_HDR_TO_12_BIT`[\[5\]](#r5)，由它生成 HEVC 码流；容器层只做两件事：**改 nclx**、**补 DPI**。

## 3. 为什么 HEIC 适合 HDR

* **10/12bit**：暗部码值比 8bit 密 4–16 倍，PQ 编码后 banding 少。
* **4:2:0**：色度减半，体积小；HDR 主要靠亮度，色度损失可接受。
* **HEVC 效率**：同质量下约为 JPEG 的一半。
* **元数据完整**：能带 ICC、Exif、XMP、tmap（本项目的 HEIC 没写 tmap）。

代价：**没有独立的 SDR 基图**。JPEG Ultra HDR 是"8bit 基图 + 增益图"，老设备看基图；HEIC 是"一张 10bit HDR 图"，SDR 回退靠查看器 tone mapping。不同查看器的回退差异很大，这是 HEIC 在混合设备环境里的最大问题。

## 4. 本项目的实现

### 4.1 写

`hdrgainmap/heic10.py`：

```text
RGB float → 线性 × exp2(expo) → ×203 nits → BT.2020 → PQ → 16bit
→ pillow-heif save(..., bits=10 或 12)
→ 读回字节，定位 colr box，把 nclx 改成 (9, 16, 9, 1)
→ 插入 Exif（DPI 282/283/296）
→ 原子写 {stem}_hdr{bits}_pq.heic
```

为什么不用 pillow-heif 自带的色彩参数？因为它写出的 `nclx` 不总是我们想要的组合（有时 matrix 缺失或写成 6），而改 4 个字节比重新编码便宜得多。

### 4.2 DPI

HEIF 没有"物理分辨率"字段。项目借 Exif 的：

| 标签                      | 值                     |
| ----------------------- | --------------------- |
| `0x011A` XResolution    | `dpi × 100 / 100` 有理数 |
| `0x011B` YResolution    | 同上                    |
| `0x0128` ResolutionUnit | 2（英寸）                 |

`0x0128=3` 是厘米，写错会让 DPI 差 2.54 倍。

### 4.3 读取

验证脚本用 pillow-heif 打开，检查：

* 位深（`info["bits_per_channel"]`）；
* `nclx` 四元组；
* 尺寸；
* 是否有 Exif/ICC。

部分解码器会把 12bit 截成 10bit 或 8bit，读回来的位深不代表文件里的位深——要看 `hvcC` 的 `bit_depth_luma_minus8`。

## 5. 兼容性

| 平台                    | 10bit          | 12bit   |
| --------------------- | -------------- | ------- |
| iOS / macOS           | ✅ 原生           | ✅ 原生    |
| Android 10+           | ✅ 多数           | ⚠️ 部分机型 |
| Windows 10/11         | ⚠️ 需 HEVC 视频扩展 | ❌ 常见报错  |
| Chrome                | ❌              | ❌       |
| Photoshop / Lightroom | ✅ 新版本          | ⚠️ 看版本  |

**Windows 的 `0xC00D3E8C`**：系统照片应用打不开 12bit Main12 HEIC，报这个错误码。原因通常是系统 HEVC 解码器只支持 Main10，或扩展版本太旧。这不是文件损坏，换播放器（如 mpv、VLC 新版）或装 HEVC 扩展可解。

**SDR 回退**：在没有 HDR 能力的屏幕上，系统会 tone mapping。有的提亮整体，有的压高光，有的直接按 SDR 解释 PQ 码值（发灰）。同一张 HEIC 在不同设备上观感差异比 JPEG Ultra HDR 大得多。

## 6. 与其它格式的取舍

| 需求         | 建议                               |
| ---------- | -------------------------------- |
| 苹果生态分发     | HEIC 10bit                       |
| 跨平台、要兼容老设备 | JPEG Ultra HDR                   |
| 存档、无损      | PNG 16bit                        |
| 网页、要小      | AVIF（见 [08](08-avif.md)）         |
| 极致位深/无损    | JXL 24bit（见 [09](09-jpeg-xl.md)） |

## 7. 常见错误清单

1. **nclx matrix 写 6 或 0。** HEIC 是 YCbCr，必须 9。
2. **transfer 写 13（sRGB）却塞 PQ 码值。** 查看器按 SDR 解释，发灰。
3. **以为 HEIC 有 SDR 基图。** 没有，回退靠查看器。
4. **12bit 当 10bit 写 hvcC。** 解码器按 10bit 读，码流错位。
5. **DPI 写进 HEIF 的 `ispe`。** `ispe` 是像素尺寸，不是物理尺寸；DPI 只能借 Exif。
6. **用 `hev1` 且参数集缺失。** 部分解码器找不到 SPS/PPS。
7. **在 Windows 上测 12bit 就判定文件坏。** 先确认 HEVC 扩展。

## 参考资料

<a id="r1"></a>**\[1]** ISO/IEC. *23008-12:2017 — High Efficiency Image File Format (HEIF)*. <https://www.iso.org/standard/66067.html> （访问日期 2026-11-27）

<a id="r2"></a>**\[2]** ISO/IEC. *23008-2:2020 — High Efficiency Video Coding (HEVC)*（与 ITU-T H.265 共同发布；后续版本 2023/2025）. <https://www.iso.org/standard/75484.html> （访问日期 2026-11-27）

<a id="r3"></a>**\[3]** ITU-T. *H.273: Coding-independent code points*（nclx 枚举）. <https://www.itu.int/rec/T-REC-H.273> （访问日期 2026-11-27）

<a id="r4"></a>**\[4]** Nokia. *HEIF specification site*. <https://nokiatech.github.io/heif/> （访问日期 2026-11-27）

<a id="r5"></a>**\[5]** pillow-heif. *Documentation*（`SAVE_HDR_TO_12_BIT`）. <https://pillow-heif.readthedocs.io/> （访问日期 2026-11-27）
