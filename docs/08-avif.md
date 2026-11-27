# 08 AVIF

AVIF = HEIF 容器 + AV1 编码。它和 HEIC 共享同一套 box 结构，区别在编码器：AV1 是开放、免专利的，压缩率比 HEVC 略好，浏览器支持也更广（Chrome/Firefox/Safari 16.4+）。

在本项目里，AVIF 是"体积与质量平衡"的选项：12bit 4:4:4、crf 0，70MP 约 71MB，比 PNG 小 60%，比 HEIC 大 10 倍但色度不降采样。

## 1. 名字与规范

* **AV1**：AOMedia Video 1，开放、免专利的视频编码，2018 年发布。
* **AVIF**：AV1 Image File Format，基于 HEIF（ISO/IEC 23008-12）的图像封装规范，由 AOMedia 维护。
* **规范版本**：v1.2.0（2025-10-16）[\[1\]](#r1)，明确支持 12bit、4:4:4、alpha、HDR。

AVIF 不是 ISO 标准，是行业规范；但它的容器部分完全复用 HEIF[\[3\]](#r3)，所以工具链和 HEIC 高度重合。

## 2. 容器结构

```text
ftyp       品牌：avif / avis（序列）
meta
  hdlr
  pitm     主图 item
  iinf     infe：av01、Exif、alpha（auxl）
  iloc     偏移/长度
  iprp
    ipco   av1C、ispe、colr、pixi、a1lx ...
    ipma
  iref     auxl（alpha 辅助图）、cdsc
mdat       AV1 OBU 码流
```

和 HEIC 的差别：

* `infe` 的 item 类型是 `av01`，不是 `hvc1`；
* 编码配置在 `av1C`，不是 `hvcC`；
* AV1 的序列头（sequence header）里也有 `color_config`[\[2\]](#r2)，**必须与 `colr` box 一致**，否则解码器可能按码流内的值覆盖容器声明。

### 2.1 colr / nclx

和 HEIC 一样[\[1\]](#r1)：

```text
primaries = 9   (BT.2020)
transfer  = 16  (PQ；HLG 为 18，本项目不用)
matrix    = 9   (BT.2020 NCL)
full_range = 0  (limited / tv range，与 ffmpeg 的 YUV 编码一致)
```

AVIF 码流是 YCbCr，matrix 必须是 9（同 HEIC，不同于 PNG 的 0）。

`full_range = 0` 是 AV1 的常规选择：ffmpeg 输出的 `yuv444p12le` 本身就是 studio range，读端会按 limited range 反量化（12bit 上限约 44148 而非 65535）。编码器和 `nclx` 必须说同一件事，否则整体偏移、高光发灰。

### 2.2 位深与色度

| 组合          | 说明           | 本项目  |
| ----------- | ------------ | ---- |
| 8bit 4:2:0  | 兼容性最好，SDR 常见 | ❌    |
| 10bit 4:2:0 | HDR 主流，体积小   | 降级选项 |
| 10bit 4:4:4 | 色度不降采样       | 降级选项 |
| 12bit 4:4:4 | 最高质量         | ✅ 默认 |

4:4:4 的体积约是 4:2:0 的三倍。项目默认 12bit 4:4:4，编码失败时按 `12/444 → 12/420 → 10/444 → 10/420` 降级，最后一级用 SVT-AV1（如果系统装了）。

## 3. 本项目的实现

### 3.1 编码路径

`ffmpeg` 是唯一可靠的 12bit AV1 编码入口[\[4\]](#r4)：

```text
ffmpeg -f rawvideo -pix_fmt rgb48le -s WxH -r 1 -i pipe:0
       -c:v libaom-av1 -still-picture 1 -crf 0 -cpu-used 6
       -pix_fmt yuv444p12le -f avif pipe:1
```

* `-still-picture 1`：告诉 libaom 这是单帧图像，优化编码。
* `-crf 0`：无损模式（实际是"视觉无损"，AV1 的量化仍可能丢一点）。
* `-cpu-used 6`：速度/质量折中；更慢的档位（0–3）在 70MP 上不可接受。

### 3.2 编码后的字节修补

ffmpeg 的 AVIF muxer 有两个已知缺陷：

1. **不写 `colr`/`nclx`**：码流里有 `color_config`，但容器层缺 `colr`，部分查看器按 sRGB 解释。
2. **不写 DPI**：HEIF 本来就没有物理尺寸字段，ffmpeg 也不补 Exif。

项目在编码后直接改字节：

```text
定位 colr box → 改 4 字节 nclx（primaries/transfer/matrix/range）
若没有 colr → 在 ipco 里插入一个
补 Exif item（282/283/296）→ 更新 iloc 偏移
```

**为什么改字节而不是重新封装**：重新封装要解析重建整个 box 树，容易破坏 `iloc` 偏移；只改固定位置的几个字节，风险小得多。

### 3.3 增益图

AVIF 规范支持 `tmap`（tone map derived image item），可以像 JPEG Ultra HDR 一样"基图 + 增益图"。项目**没有实现**：AVIF 输出是一张完整的 12bit HDR 图，SDR 回退靠查看器。

原因：`tmap` 的写入需要重建 item 引用、derived item 的 `iloc` 语义，且当时没有成熟的 Python 写入库；直接用 ffmpeg 出 HDR 图更简单。

## 4. 兼容性

| 平台            | 8/10bit          | 12bit   |
| ------------- | ---------------- | ------- |
| Chrome        | ✅                | ✅（多数版本） |
| Firefox       | ✅                | ⚠️ 看版本  |
| Safari 16.4+  | ✅                | ⚠️      |
| iOS 16+       | ✅                | ⚠️      |
| Windows 10/11 | ✅ 需 AV1 扩展（商店免费） | ⚠️      |
| Photoshop     | ✅ 新版本            | ⚠️      |

浏览器对 12bit AVIF 的支持比 HEIC 好[\[5\]](#r5)，但仍然不是"到处都能开"。AVIF 的优势在网页：同样质量下比 JPEG 小 50%，Chrome/Firefox 直接支持。

## 5. 实测数据

70.2MP 样张，12bit 4:4:4，crf 0：

| 指标        | 值                     |
| --------- | --------------------- |
| 编码时间      | 89.0 s                |
| 体积        | 71.40 MB              |
| 对比 PNG16  | 186.68 MB（AVIF 约 38%） |
| 对比 HEIC10 | 7.40 MB（AVIF 约 9.6 倍） |
| 对比 JXL24  | 273 MB（AVIF 约 26%）    |

AVIF 的定位由此很清楚：**比 PNG/JXL 小得多，比 HEIC 大但色度更好、跨平台更好。**

## 6. 常见错误清单

1. **nclx matrix 写 0 或 6。** AVIF 是 YCbCr，必须 9。
2. **只改容器 colr，不改 AV1 序列头。** 两者不一致时，解码器可能以码流为准。
3. **ffmpeg 版本太旧。** 老版本 muxer 不写 `colr`，也不支持 `-still-picture`。
4. **12bit 用 `yuv420p12le` 却声明 4:4:4。** 色度错位。
5. **改字节后没更新 `iloc`。** 补 Exif 会移动 `mdat`，偏移必须同步。
6. **以为 AVIF 一定有增益图。** 本项目的 AVIF 没有 `tmap`。
7. **在没装 AV1 扩展的 Windows 上测。** 打不开不等于文件坏。

## 参考资料

<a id="r1"></a>**\[1]** AOMedia. *AV1 Image File Format (AVIF) v1.2.0*, 2025-10-16. <https://aomediacodec.github.io/av1-avif/v1.2.0.html> （访问日期 2026-11-27）

<a id="r2"></a>**\[2]** AOMedia. *AV1 Bitstream & Decoding Process Specification*. <https://aomediacodec.github.io/av1-spec/> （访问日期 2026-11-27）

<a id="r3"></a>**\[3]** ISO/IEC. *23008-12:2017 — HEIF*（容器基础）. <https://www.iso.org/standard/66067.html> （访问日期 2026-11-27）

<a id="r4"></a>**\[4]** FFmpeg. *libaom-av1 encoder documentation*. <https://trac.ffmpeg.org/wiki/Encode/AV1> （访问日期 2026-11-27）

<a id="r5"></a>**\[5]** caniuse. *AVIF image format*. <https://caniuse.com/avif> （访问日期 2026-11-27）
