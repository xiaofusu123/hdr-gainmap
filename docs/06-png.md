# 06 PNG

PNG（Portable Network Graphics）是这套产物里"最笨但最可靠"的格式：无损、无专利、结构简单、任何平台都能读。代价是体积——70MP 的 16bit PNG 能到 186MB。

它的 HDR 能力来自 2025 年的第三版标准：新增 `cICP`、`mDCV`、`cLLI`，正式支持 PQ/HLG 和 1–16bit。在此之前，PNG 只能靠 16bit + ICC 勉强表达 HDR，查看器基本不认。第二版（2003）作为 ISO/IEC 15948:2004 发布，色彩描述只有 `sRGB` / `iCCP` / `gAMA`+`cHRM` 这一套，没有 CICP 那种"直接声明传输函数"的字段[\[2\]](#r2)。

## 1. 定位

* 1996 年发布，替代 GIF（当时有专利问题）[\[3\]](#r3)。
* 无损、支持 alpha、支持 1–16bit、支持调色板。
* 不支持动画（APNG 是后来的扩展）、不支持有损压缩。
* 适合：截图、图标、线条图、存档、需要像素级可验证的中间产物。

## 2. 文件结构

```text
89 50 4E 47 0D 0A 1A 0A        8 字节签名
IHDR                            第一个 chunk，图像头
...                             可选 chunk（色彩、文本、物理尺寸）
IDAT                            压缩数据（可多段）
IEND                            结束
```

每个 chunk：

```text
长度 (4, 大端) | 类型 (4, ASCII) | 数据 (长度) | CRC32(4)
```

所有整数都是**大端**（网络字节序）。CRC 覆盖"类型 + 数据"，校验失败的解码器可以跳过该 chunk 或报错。

### 2.1 IHDR

| 字段             | 本项目值 | 说明                                 |
| -------------- | ---- | ---------------------------------- |
| width / height | 图像尺寸 | 1–2³¹−1                            |
| bit depth      | 16   | 1/2/4/8/16，按颜色类型受限                 |
| color type     | 2    | 0=灰度、2=RGB、3=调色板、4=灰度+alpha、6=RGBA |
| compression    | 0    | 只有 deflate                         |
| filter         | 0    | 只有自适应滤波                            |
| interlace      | 0    | 0=不隔行，1=Adam7                      |

颜色类型 2 + 16bit = 每像素 48bit，没有 alpha。本项目在准备阶段已经把 alpha 合成到黑/白底，所以输出不需要 alpha 通道。

### 2.2 关键 chunk

| chunk  | 作用                   | 本项目                   |
| ------ | -------------------- | --------------------- |
| `cICP` | CICP 色彩编码点           | ✅ 9/16/0/1            |
| `iCCP` | ICC profile（压缩）      | ❌ 不写，避免与 cICP 冲突      |
| `sRGB` | 标记 sRGB              | ❌                     |
| `cHRM` | 原色/白点                | ❌                     |
| `gAMA` | gamma                | ❌                     |
| `mDCV` | 母版显示色域/亮度            | ✅ BT.2020 / 1000 nits |
| `cLLI` | 内容亮度（MaxCLL/MaxFALL） | ✅ 按实际峰值               |
| `pHYs` | 像素密度                 | ✅ DPI → 像素/米          |
| `tEXt` | 文本                   | ✅ Software            |
| `IDAT` | 压缩数据                 | ✅                     |
| `tIME` | 修改时间                 | ❌                     |

**色彩块优先级**：`cICP` > `iCCP` > `sRGB` > `cHRM`+`gAMA`（PNG 第三版 §4.3）[\[1\]](#r1)。同时写多套且不一致，是给自己埋雷；本项目只写 cICP。

`cICP` 的四个字节[\[4\]](#r4)：

| 字段         | 本项目值            |
| ---------- | --------------- |
| primaries  | 9（BT.2020）      |
| transfer   | 16（PQ）          |
| matrix     | 0（identity，RGB） |
| full range | 1               |

PNG 存的是 RGB 样本，所以 matrix 必须是 0（恒等），不是 9（YCbCr）。这一点和 HEIC/AVIF 相反：那边码流是 YCbCr，matrix 要写 9。

### 2.3 滤波

每一行扫描线在压缩前要选一种滤波器[\[1\]](#r1)：

| 类型 | 名字      | 预测值               |
| -- | ------- | ----------------- |
| 0  | None    | 0                 |
| 1  | Sub     | 左邻                |
| 2  | Up      | 上邻                |
| 3  | Average | (左 + 上)/2         |
| 4  | Paeth   | 左/上/左上 的 Paeth 预测 |

编码器逐行试 5 种，选"残差绝对值和最小"的那种，把类型字节写在行首。这是 PNG 压缩率的关键；选错滤波，zlib 再努力也压不动。

Paeth 预测：

```text
p = a + b − c            # a=左，b=上，c=左上
选 a/b/c 中与 p 最接近的那个
```

### 2.4 压缩

`IDAT` 里是 zlib 流（deflate + Adler-32）。本项目用 `zlib level 9`，压缩率高但慢；70MP 图约 111 秒，是五个格式里第二慢的（仅次于 JXL）。

## 3. PNG 的 HDR 能力

### 3.1 第三版新增

W3C 在 2025-06-24 发布 PNG 第三版[\[1\]](#r1)，正式加入：

* `cICP`：用整数枚举声明色彩空间，支持 PQ/HLG。
* `mDCV`：母版显示色域与亮度（Mastering Display Color Volume）。
* `cLLI`：内容亮度信息（MaxCLL/MaxFALL）。
* 明确 1–16bit 的样本深度。

这让 PNG 第一次能"自描述"地存 HDR：16bit + cICP(9/16/0/1) 就是一张标准 PQ 图，不需要 ICC 也能被正确解释。

### 3.2 为什么用 16bit

PQ 是绝对亮度编码，暗部码值稀疏。10bit 在 0.01 nit 处一个台阶约 9%（见 [02](02-pq-and-brightness.md)），12bit 降到 2.3%，16bit 约 0.14%。PNG 支持 16bit，所以直接用满：

* 203 nits 处有约 38055 个码值；
* 暗部渐变不会因为编码而 banding；
* 代价是体积：16bit 是 8bit 的两倍，压缩后差距略小。

### 3.3 体积为什么这么大

四个因素叠加：

1. **无损**：不像 JPEG/AVIF 那样丢高频。
2. **16bit**：样本翻倍。
3. **4:4:4**：没有色度降采样。
4. **内容熵高**：增益抬过高光后，原本平滑的区域出现更多层次，压缩率下降。

实测：70MP 的 16bit PNG 约 187MB，同一张图的 8bit SDR 版大约 20–30MB。分通道增益会让高光区的通道差异更细，压缩率略降，体积同量级。

## 4. 本项目的实现

### 4.1 编码

`hdrgainmap/png16.py` 的 `encode()`：

```text
签名
IHDR(w, h, bit_depth=16, color_type=2, 0, 0, 0)
cICP[9, 16, 0, 1]          # BT.2020 / PQ / RGB / 全范围
mDCV(24 字节)               # BT.2020 原色 + D65 + 1000/0.005 nits
tEXt(Software="hdr-gainmap")
pHYs(ppm, ppm, 1)          # ppm = round(dpi / 0.0254)
cLLI(8 字节)               # MaxCLL = MaxFALL = 实际峰值
IDAT(zlib(滤波后的扫描行), level=9)
IEND
```

`mDCV` 是 24 字节：R/G/B 三对原色 + 白点，各是 `uint16`、单位 `0.00002`（BT.2020 定点 35400/14600、8500/39850、6550/2300，D65 15635/16450）；随后母版最大亮度与最小亮度各 `uint32`、单位 `0.0001 cd/m²`（写 1000 nits 与 0.005 nits）[\[1\]](#r1)。

`cLLI` 是 8 字节：MaxCLL 与 MaxFALL 各 `uint32`、单位 `0.0001 cd/m²`。单帧图像没有"帧平均"的概念，所以两者都写实际峰值——这是保守上界，不会低报。

`_filter()` 逐行试 5 种滤波，按残差 int8 绝对值和选最优。选哪一种只取决于当前行与上一行原始数据，行与行之间没有递推，所以可以先整块算完 5 份候选再按行取最小：`-j` 大于 1 时按行切块、多线程并行，边算边喂 `zlib.compressobj(9)`，字节流与逐行串行完全一致。`_chunk()` 负责长度/类型/CRC 的大端打包。

### 4.2 解码

`decode()` 优先调用 ffmpeg：

```text
ffmpeg -f rawvideo -pix_fmt rgb48le -i pipe:0
```

`rgb48le` 是**小端** 16bit RGB——ffmpeg 的输出格式，和 PNG 文件里的大端样本不是一回事。ffmpeg 不可用时回退纯 NumPy 的 unfilter：

* 滤波 0/1/2 向量化；
* 滤波 3/4 逐字节（Paeth 有依赖）。

回退路径慢，但保证没有 ffmpeg 也能跑测试。

### 4.3 DPI

`pHYs` 的单位是"像素/米"，不是 DPI[\[1\]](#r1)：

```text
ppm = round(dpi / 0.0254)      # 1 英寸 = 0.0254 米
```

96 DPI ≈ 3780 ppm；240 DPI ≈ 9449 ppm。读的时候反过来乘 0.0254。

## 5. 兼容性

| 平台                       | 行为                                              |
| ------------------------ | ----------------------------------------------- |
| Chrome / Firefox         | 能显示 16bit PNG，但多数忽略 cICP，按 sRGB 解释 → HDR 图发灰/过曝 |
| Safari                   | 部分版本识别 cICP，行为不稳定                               |
| Windows 照片               | 按 sRGB 显示，16bit 可能被截到 8bit                      |
| Photoshop                | 能读 16bit 和 cICP（新版本），可正确解释 PQ                   |
| 专业工具（ffmpeg、ImageMagick） | 能读像素，色彩解释看版本                                    |

结论：**PNG 16bit 适合存档和工具链，不适合直接分发给普通用户。** 它的价值是"像素可验证、无损、无专利"，不是"打开就能看 HDR"。

## 6. 常见错误清单

1. **不写 cICP，只写 16bit。** 查看器不知道是 PQ 还是线性，只能猜。
2. **cICP 的 matrix 写 9。** PNG 是 RGB 样本，matrix 必须是 0。
3. **同时写 cICP 和 iCCP 且不一致。** 按优先级 cICP 赢，iCCP 是隐患。
4. **滤波类型与数据不匹配。** 解码花屏/偏色。
5. **CRC 算错。** 严格解码器直接拒绝。
6. **整数用小端写。** PNG 全部大端。
7. **pHYs 直接写 DPI。** 单位是像素/米。
8. **16bit 样本按 8bit 读。** 高低字节错位，图像变噪点。
9. **mDCV/cLLI 单位写错。** 前者亮度单位 `0.0001 cd/m²`、色度 `0.00002`；后者亮度也是 `0.0001 cd/m²`，写成 nits 会差 4 个数量级。

## 参考资料

<a id="r1"></a>**\[1]** W3C. *Portable Network Graphics (PNG) Specification (Third Edition)*, W3C Recommendation, 2025-06-24. <https://www.w3.org/TR/png-3/> （访问日期 2026-11-27）

<a id="r2"></a>**\[2]** W3C. *PNG Specification (Second Edition)*, ISO/IEC 15948:2004. <https://www.w3.org/TR/PNG/> （访问日期 2026-11-27）

<a id="r3"></a>**\[3]** Wikipedia. *Portable Network Graphics*（chunk 结构、滤波、历史）. <https://en.wikipedia.org/wiki/Portable_Network_Graphics> （访问日期 2026-11-27）

<a id="r4"></a>**\[4]** ITU-T. *H.273: Coding-independent code points*（cICP 枚举）. <https://www.itu.int/rec/T-REC-H.273> （访问日期 2026-11-27）
