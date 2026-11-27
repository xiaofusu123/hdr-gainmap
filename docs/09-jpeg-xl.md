# 09 JPEG XL

JPEG XL 是这份清单里"位深最高、压缩最好、支持最差"的格式。ISO/IEC 18181 定义了它，规范允许每通道最高 32bit 整数或浮点；本项目固定用 24bit，把 PQ 码值原样封进去，无损。

它的意义不是分发，而是**存档与验证**：当你想确认"增益图算出来的 HDR 到底长什么样"，JXL 24bit 是最接近"没有量化损失"的容器。

## 1. 规范与设计

JPEG XL 由 Joint Photographic Experts Group 开发[\[1\]](#r1)[\[2\]](#r2)，目标是"下一代通用图像格式"，主要特性：

* **VarDCT**：类似 JPEG 的 8×8 DCT，但有自适应量化和更好的熵编码，压缩率显著高于 JPEG。
* **Modular**：基于 MA（Meta-Adaptive）树的预测编码，支持无损。
* **无损重压缩 JPEG**：能把现有 JPEG 无损转成 JXL，体积减约 20%，再转回来字节一致。
* **高位深**：整数最高 32bit，浮点也支持。
* **渐进式解码**、alpha、动画、多帧。
* **色彩编码在码流内**（cICP + 强度目标），不依赖外部 ICC。

规范分多部分：18181-1（码流）、-2（文件格式）、-3（一致性）、-4（参考软件）[\[1\]](#r1)。

## 2. 24bit 的由来

### 2.1 为什么不是 16bit

16bit 已经能装下 PQ 的 65536 个码值，203 nits 处约 38055 级（见 [02](02-pq-and-brightness.md)），肉眼看不出 banding。但项目选 24bit 有两个理由：

1. **避免"伪高位深"**：如果先把 16bit 数据左移 8 位标成 24bit，文件大了、信息没多，是欺骗。项目用 PFM 传 float32，让 cjxl 真正按 24bit 量化。
2. **对齐 DICOM**：DICOM Supplement 232 定义了 24bit 色彩（每通道 1 字节 ×3 的扩展）[\[4\]](#r4)，医学影像和高端存档有这个惯例。

24bit 在 203 nits 处约 975 万级，实际已超过显示器的物理精度；它的价值是"存档时不再引入新的量化"。

### 2.2 PFM 中转

cjxl 不读 PNG 的 cICP/PQ 元数据（PNG 只有 16bit 整数），所以项目用 PFM（Portable Float Map）传浮点码值：

```text
PFM 头："PF\n{w} {h}\n-1.0\n"     # -1.0 = 小端
数据：自下而上，每像素 3 个 float32
```

PFM 的行序是**自下而上**，写文件时要翻转；读的时候再翻回来。float32 能无损装下 24bit 整数（2²⁴ < 2²⁴ 的 float 尾数精度），所以 `--override_bitdepth=24` 的量化不丢信息。

## 3. 本项目的实现

`hdrgainmap/jxl.py`：

```text
cjxl photo.pfm out.jxl
     --override_bitdepth=24
     -x color_space=Rec2100PQ        # 或 Rec2100HLG
     -x exif=dpi.tiff                # 可选：Exif box 装 TIFF（不带 Exif\0\0）
     -e 7                            # effort
     -d 0                            # 无损
     --compress_boxes=0              # 别用 Brotli 压 Exif
     --quiet
```

* `-d 0`：距离 0，无损模式（Modular）。
* `-e 7`：effort 7，速度/压缩比折中；更高档位在 70MP 上太慢。
* `color_space=Rec2100PQ`：把色彩编码写进码流，等价于 primaries=BT.2020、transfer=PQ。
* `--override_bitdepth=24`：cjxl 0.13+ 才有；旧版本会拒绝。

工具查找顺序：`HDRGAINMAP_CJXL` 环境变量 → PATH 里的 `cjxl`；校验用 `HDRGAINMAP_JXLINFO` → `jxlinfo`[\[3\]](#r3)。

`--dpi` 的处理：项目先用 `ultrahdr._exif()` 生成 TIFF（IFD0 里 0x011A/0x011B 有理数、0x0128=2 英寸），去掉 `Exif\0\0` 前缀写成临时文件，再 `-x exif=` 交给 cjxl；验校时从容器里找到 `Exif` box、跳过 4 字节偏移、按 IFD0 读出 XResolution。用 ImageMagick 检查：`magick identify -format "%x %U" x.jxl` → `300 PixelsPerInch`。

### 3.1 已知限制

* **不跟随其他格式的传递函数**：全项目统一 PQ，这条限制自然消失；即使将来恢复多传递函数，JXL 也应保持"码流里写什么就是什么"。
* **DPI 只能借 Exif**：JXL 没有原生分辨率字段。本项目用 `-x exif=<tiff>` 把 XResolution/YResolution/ResolutionUnit 写进容器的 `Exif` box——负载是**纯 TIFF 头**（不带 `Exif\0\0` 六字节），前面还要有 4 字节"到 TIFF 头的偏移"（写 0 即紧随其后）。同时加 `--compress_boxes=0`，否则 libjxl 默认用 Brotli 压缩 Exif box，读端要额外解压。
* **体积大**：70.2MP 约 273MB，是 PNG16 的 1.5 倍、AVIF 的 3.8 倍。
* **慢**：70.2MP 约 232 秒，是五个格式里最慢的。

## 4. 兼容性

| 平台                       | 支持              |
| ------------------------ | --------------- |
| Chrome                   | ❌ 默认不支持（需 flag） |
| Firefox                  | ❌ 默认不支持         |
| Safari                   | ⚠️ 部分版本（17+ 有限） |
| Windows 10/11            | ⚠️ 需扩展/第三方查看器   |
| macOS 预览                 | ⚠️ 新版本有限        |
| GIMP / Krita / darktable | ⚠️ 插件或新版本       |
| `djxl` / `jxlinfo`       | ✅ 完整            |

结论：**JXL 24bit 不适合分发，适合存档、比对、给专业工具读**[\[5\]](#r5)。它的正确用法是"生成一份参考，用来验证其它格式的还原程度"。

## 5. 实测数据

70.2MP 样张：

| 指标     | 值                                         |
| ------ | ----------------------------------------- |
| 编码时间   | 232 s                                     |
| 体积     | 273 MB                                    |
| 位深     | 24bit/通道                                  |
| 色彩     | BT.2020 + PQ                              |
| DPI    | 写入容器 `Exif` box（随 `--dpi`；无则源图 DPI，通常 96） |
| 临时 PFM | 约 840 MB                                  |

## 6. 与其它格式的对比

| 维度       | JXL 24 | PNG 16 | AVIF 12 | HEIC 12 |
| -------- | ------ | ------ | ------- | ------- |
| 位深       | 24     | 16     | 12      | 12      |
| 有损       | 无损     | 无损     | 近无损     | 有损      |
| 体积（70MP） | 273MB  | 187MB  | 71MB    | 7MB     |
| 浏览器      | ❌      | ✅      | ✅       | ❌       |
| 适合       | 存档/验证  | 存档/工具  | 网页      | 苹果生态    |

## 7. 常见错误清单

1. **把 16bit 左移 8 位存成 24bit。** 伪高位深，体积白涨。
2. **PFM 行序没翻转。** 图像上下颠倒。
3. **cjxl 版本低于 0.13。** 没有 `--override_bitdepth`，会报错或按默认位深写。
4. **`-d 0` 写成 `-d 1`。** 变成有损，位深再高也没意义。
5. **`color_space` 名字写错。** cjxl 只认 `Rec2100PQ`/`Rec2100HLG` 等枚举。
6. **以为浏览器能直接看。** 多数不能，要转 AVIF/PNG。
7. **临时 PFM 占满磁盘。** 70MP 的 PFM 约 840MB，编码前先确认空间。
8. **Exif 负载带了 `Exif\0\0`。** JXL 的 `Exif` box 只装 TIFF 头（无六字节签名），带上就读不出 DPI。
9. **忘了 `--compress_boxes=0`。** 默认 Brotli 压缩 Exif box，自己写的读端会解析失败；外部读端不受影响。

## 参考资料

<a id="r1"></a>**\[1]** ISO/IEC. *18181-1:2022 — JPEG XL image coding system — Part 1: Core coding system*（文件格式见 18181-2；现行第 2 版为 2024）. <https://www.iso.org/standard/77977.html> （访问日期 2026-11-27）

<a id="r2"></a>**\[2]** JPEG.org. *JPEG XL*. <https://jpeg.org/jpegxl/> （访问日期 2026-11-27）

<a id="r3"></a>**\[3]** libjxl. *cjxl documentation*. <https://github.com/libjxl/libjxl> （访问日期 2026-11-27）

<a id="r4"></a>**\[4]** DICOM. *Supplement 232: 24-bit color*. <https://www.dicomstandard.org/> （访问日期 2026-11-27）

<a id="r5"></a>**\[5]** caniuse. *JPEG XL*. <https://caniuse.com/jpegxl> （访问日期 2026-11-27）
