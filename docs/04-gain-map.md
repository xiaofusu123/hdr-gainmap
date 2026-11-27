# 04 增益图（Gain Map）原理

前三篇讲的是"HDR 是什么"。从这一篇开始讲"怎么把一张 SDR 图变成 HDR，同时不破坏它"。

增益图（gain map）的思路只有一句话：**不改基图，另存一张"每个像素还能亮多少倍"的图。** 支持 HDR 的查看器把两者相乘，不支持的查看器只显示基图。

这比"用 AI 把 SDR 重建成 HDR"保守得多，也稳得多：基图是原图，不存在"猜错纹理"；所有不确定性都被压缩到一张低分辨率的灰度图里。

## 1. 为什么是"比值"而不是"重建"

一张 8bit SDR 图在保存时已经削顶：亮度超过 1.0 的部分被压成 1.0（或 255），信息不可逆地丢了。所以"从 SDR 恢复 HDR"严格说是不可能的——你只能**猜**。

猜法有两类：

| 路线         | 做法                     | 风险                 |
| ---------- | ---------------------- | ------------------ |
| 逆色调映射（ITM） | 用模型/曲线把 SDR 像素映射到更大的范围 | 猜错材质、高光膨胀、暗部过提、塑料感 |
| 增益图        | 不改基图，只存"还能亮几倍"的乘数      | 乘数估得不准；但基图永远正确     |

关键区别在于**信息放在哪**。ITM 把"HDR 长什么样"编码进生成模型，生成结果和原图可能对不上；增益图把"每个像素的余量"编码成一张图，显示端做一次乘法，结果永远以原图为底。

这也是为什么 Android 的 Ultra HDR、ISO 21496-1、以及苹果/谷歌的 HDR 图片都用增益图：它是**向后兼容**的。老设备看到的是完整的 SDR 图，不是"HDR 图的降级版"。

## 2. 三个角色

一个增益图文件由三部分组成：

| 部分                 | 内容                       | 作用                |
| ------------------ | ------------------------ | ----------------- |
| 基图（base / primary） | 完整的 8bit SDR 图像          | 老设备直接显示；新设备作为乘法的底 |
| 增益图（gain map）      | 单通道灰度图，通常是 8bit JPEG     | 每个像素的 log2 增益     |
| 元数据                | 增益范围、gamma、offset、HDR 容量 | 告诉解码器怎么解释灰度值      |

三者必须**同分辨率、同朝向、同一坐标系**（增益图可以降采样，但解码时要按同一网格对齐）。基图通常还带 ICC，保证 SDR 显示时的颜色正确。

## 3. 数学：从像素到增益

### 3.1 定义

设 SDR 基图某像素的线性光为 `Y_sdr`，目标 HDR 的线性光为 `Y_hdr`，则逐像素增益：

```text
pixel_gain = (Y_hdr + offset_hdr) / (Y_sdr + offset_sdr)
```

`offset` 是防止除零、并抑制暗部噪声被放大的小常数，默认 `1/64 = 0.015625`。注意它是加在**线性化、归一化后的值**上的，不是加在 8bit 码值上（DeepWiki 对 libultrahdr 的说明也确认：先做 sRGB 逆 OETF，再乘增益）[\[1\]](#r1)。

上面用 `Y` 写是为了简化。规范里 `pixel_gain` 是逐通道的：ISO 21496-1 支持 1 或 3 个通道，每个通道有自己的 `min/max/gamma/offset`[\[2\]](#r2)。XMP 的 `hdrgm:` 只支持单通道，所以"逐通道"必须走 ISO。

### 3.2 存成 log2

增益的范围可能很大（1 到几十），直接线性存会浪费码值。取 log2 后[\[3\]](#r3)：

```text
map_min_log2 = log2(min_content_boost)
map_max_log2 = log2(max_content_boost)
log_recovery = (log2(pixel_gain) − map_min_log2) / (map_max_log2 − map_min_log2)
recovery     = clamp(log_recovery, 0, 1) ^ gamma
encoded      = floor(recovery × 255 + 0.5)
```

* `map_min_log2` / `map_max_log2` 是"这张图的增益范围"，写进元数据。
* `gamma` 默认 1.0，一般不用改；它是对归一化值的整体幂变换。
* `encoded` 是 0–255 的灰度码值，存进 8bit JPEG。

解码是逆过程：

```text
recovery     = code / 255
log_recovery = recovery ^ (1/gamma)
log_boost    = map_min_log2 × (1 − r) + map_max_log2 × r        # r = log_recovery 归一化后
weight       = clamp((log2(max_display_boost) − capacity_min) / (capacity_max − capacity_min), 0, 1)
gain         = 2 ^ (log_boost × weight)
HDR          = (SDR + offset_sdr) × gain − offset_hdr
```

### 3.3 weight 是"按显示器能力打折"

`weight` 是增益图最巧妙的地方：它不是"有 HDR 就全量应用"，而是按**当前显示器能亮多少**在 log 域插值。

* SDR 显示器：`max_display_boost = 1`，`log2 = 0`，`weight = 0` → 增益 1 → 原图。
* 显示器峰值刚好等于 `capacity_max`：`weight = 1` → 全量增益。
* 介于两者之间：按比例给一部分增益。

这样同一张图在 400 nit 手机和 1600 nit 电视上都能"尽量用满自己的余量"，而不是一刀切。

### 3.4 一个数值例子

设 `Y_sdr = 0.5`（线性），`pixel_gain = 2`，offset 取 1/64：

```text
编码：log2(2) = 1.0；若 map_min=0、map_max=1.45，则 recovery = 1/1.45 = 0.6897
      encoded = floor(0.6897 × 255 + 0.5) = 176
解码：recovery = 176/255 = 0.6902；log_boost = 0.6902 × 1.45 = 1.0008
      gain = 2^1.0008 = 2.0011
      HDR = (0.5 + 0.015625) × 2.0011 − 0.015625 = 1.0162
```

和直接 `0.5 × 2 = 1.0` 差 1.6%，这是 offset 和 8bit 量化共同造成的，肉眼不可见。offset 的影响在亮部可以忽略，在暗部才显著——这正是它想要的效果。

## 4. 为什么 log2 + 8bit 够用

两个理由。

**第一，log2 让误差变成"比例误差"。** 8bit 把 `[map_min, map_max]` 均分 255 份，若 `gmax = 1.45`（即最大增益 2^1.45 ≈ 2.73），每份是：

```text
1.45 / 255 = 0.005686 (log2)
2^0.005686 = 1.00395  →  0.4% 的增益误差
```

0.4% 的亮度差远低于人眼阈值（约 1–2%），所以 8bit 足够。若用线性存 1–2.73，每份是 0.0068，看着也差不多；但增益范围到 10 以上时，线性存的暗部精度会迅速崩掉，log2 不会。

**第二，显示端的 gain 是乘在基图上的，误差不累积。** 增益图只影响"乘数"，基图的 8bit 量化误差仍然存在，但那是 SDR 图本来就有的，不是增益图引入的。

> 注意：8bit 的增益图在"增益接近 1"的区域台阶最粗（相对误差固定为 0.4%，绝对增益差 0.004），但这块区域本来就不需要精确——乘 1.004 和乘 1.000 看不出区别。

## 5. 为什么用全分辨率

规范允许增益图是基图的 1/2 或 1/4（Android 参考实现默认缩放 4 倍）[\[3\]](#r3)，因为增益场本身是低频的：光照、环境光遮蔽、高光扩散都缓变。降采样的收益是体积（1/4 像素数、单通道再乘 1/3）。

本项目用**全分辨率 RGB 增益图**，理由是质量优先：

* 高频边缘（发丝、栏杆、玻璃描边）一侧亮一侧暗时，降采样会把亮侧增益糊到暗侧，产生"漏光"；全分辨率没有这个问题。
* 8bit 的量化台阶本来就只有 0.4%，再叠加 LANCZOS 降采样的插值误差，强边缘会出现可察觉的光晕。
* 增益图是 JPEG，DCT 本身已经吃掉了大部分冗余；全分辨率 RGB 的代价是文件变大，而不是画质变差。

代价也明确：Ultra HDR 文件比半分辨率单通道大数倍（1600×669 的样例从约 0.3 MB 涨到 0.87 MB，见 7.3）。这是有意的取舍——要小体积可以自己把增益图缩到 1/2 再写，规范允许。

仍然保留的细节是**抖动放在量化前**：`code × 255` 之后加三角分布 `U(0,1) − U(0,1)`，把平滑渐变的等值线圈打散。它必须在归一化之后加，否则会被后续的缩放平均掉。

## 6. 容器：增益图存在哪

增益图不是"一种文件格式"，而是**元数据 + 第二张图**的打包方式。目前有三套主流承载：

### 6.1 JPEG + Ultra HDR (Android)

一个 JPEG 文件里塞两张 JPEG[\[3\]](#r3)[\[4\]](#r4)：

```text
SOI
├─ APP0 JFIF
├─ APP1 Exif（DPI、Software 等）
├─ APP2 ISO 21496-1（主图：4 字节 stub）
├─ APP2 MPF（Multi-Picture Format，指向第二张图）
├─ APP1 XMP（GContainer 目录 + hdrgm 字段）
├─ 主图压缩数据（SDR 基图）
└─ SOI + APP1 XMP（完整 hdrgm）+ APP2 ISO（61 字节）+ 增益图压缩数据
```

三个硬约束（`hdrgainmap/ultrahdr.py` 里的注释写得很清楚）：

1. 主图 MPF 条目的属性必须是 `0x00030000`，偏移必须是 0（相对文件头）。
2. 增益图条目的属性必须是 0，长度必须等于增益图总长，偏移相对 **MPF TIFF 头**。
3. 主图长度和增益图偏移互相依赖（MPF 段长度取决于主图长度），要迭代到不动点；本项目最多 8 次。

这也是为什么"手写 Ultra HDR"容易出错：偏移基准有两个（文件头 vs TIFF 头），长度又互相咬合。

### 6.2 ISO 21496-1

ISO/IEC 21496-1:2025 是跨容器的增益图元数据标准[\[2\]](#r2)，JPEG、HEIF、AVIF 都能用。它把字段放在一个 APP2 段（JPEG）或 box（HEIF/AVIF）里，字段是 32bit 有理数，大端：

```text
命名空间 "urn:iso:std:iso:ts:21496:-1\0"
min_version(2) writer_version(2) flags(1)
base_headroom, alternate_headroom,
per-channel: min, max, gamma, base_offset, alternate_offset
```

`flags` 的 bit6（0x40）表示"使用基图色彩空间"，bit7（0x80）表示三通道。XMP 那套（`hdrgm:`）只支持单通道；要逐通道增益就得走 ISO。

三通道载荷的布局是"先两组头，再逐通道重复"：

```text
min_version(2)=0  writer_version(2)=0  flags(1)=0xC0
base_headroom  N/D       alternate_headroom  N/D
通道 0: min N/D, max N/D, gamma N/D, base_offset N/D, alternate_offset N/D
通道 1: 同上
通道 2: 同上
```

每组有理数 8 字节，三通道合计 `5 + 16 + 3×40 = 141` 字节，是单通道 61 字节的 2.3 倍。`base/alternate_headroom` 写的是 `log2(hdr_capacity_min/max)`；本项目 `base_headroom=0`、`alternate_headroom=gmax`，表示"这副 SDR 基图本身没有余量，最亮可到 2^gmax"。

Android Ultra HDR 1.1 同时写 XMP 和 ISO，读取端优先 ISO、缺失时回退 XMP[\[3\]](#r3)。本项目两者都写：主图放 4 字节 ISO stub（表示"元数据在增益图那边"），增益图放完整的 141 字节 ISO 段；XMP 仍写单通道 `GainMapMax`（取三通道最大值），让只认 XMP 的旧解码器也能拿到一个合理的整体增益。代价是旧解码器会忽略通道差异——这是兼容性回退，不是目标行为。

### 6.3 HEIF / AVIF

HEIF 系容器用 `tmap`（tone map）derived item 或厂商私有 box 承载增益图。AVIF 规范定义了 `tmap` 的用法，但**本项目没有实现 tmap**：HEIC/AVIF 输出的是"直接编码成 PQ 的 HDR 图"，不带 SDR 基图。所以这两种格式没有"老设备自动回退"的能力，SDR 回退由查看器自己做 tone mapping。

这是有意的取舍：HEIC/AVIF 的 HDR 图更简单、体积更小，但兼容性不如 Ultra HDR。要"一份文件同时喂饱新老设备"，JPEG + Ultra HDR 是唯一稳妥的选择。

补一句横向对照：PNG 第三版也定义了 `cICP`、`mDCV`、`cLLI` 这些 HDR 相关 chunk，但同样没有增益图机制[\[5\]](#r5)——它靠的是"整张图直接写 PQ"，而不是"基图 + 增益"。所以本项目对 PNG 的处理和 HEIC/AVIF 一样：直接编码 HDR。

## 7. 本项目的增益图怎么算

算法拆成两段：`light.py` 先从亮度场估计光照结构（光源、光束、辐照度、环境光遮蔽、空气透视），`gainmap.py` 再把光照折成每个像素的"发光程度"，最后 `pipeline.py` 决定每条通道抬多少。`gmax` 的自适应也在 `light.adapt` 里，与光场共用一份统计。

### 7.1 先算光场，再算增益

```text
scene = light.analyze(rgb)          # 光源图、光线步进、辐照度、AO、深度代理
cfg   = light.adapt(y, scene)       # gmax 与各阈值随画面自适应
emis  = gainmap.emissive(rgb, cfg, scene)
em    = finalize(emis)              # 指数饱和 + 小高斯
```

`emissive` 是五个判据的加权和：

```text
emis = base_lit·lit + base_irr·irr + base_spark·spark
     + base_white·white + base_amb·amb
```

* `lit`：大尺度"被照亮"判据。用导向滤波（guided filter）代替纯高斯，边缘不再糊到暗侧。
* `irr`：`light.analyze` 的辐照度场——光源像素沿 8 个方向步进、按指数衰减扩散，再乘反射率与深度衰减。
* `spark`：局部峰值 / 局部均值的比值，抓点状高光。
* `white`：接近 1.0 的白，抓已经削顶的高光。
* `amb`：环境光 × 环境光遮蔽，只在大片暗区补一点底，不参与"发光"判断。

`finalize` 做 `1 − exp(−emis / p99.9 × 1.6)` 再高斯模糊，把极值压回 1 附近。`p99.9` 而不是 `max`，是为了对噪点稳健。

`gmax` 的自适应多了一项"光源覆盖率"惩罚：

```text
t     = clip(p50 / 0.60, 0, 1)               # p50 = 亮度中位数
cover = source_pct / 12                      # 判成光源的像素占比
gmax  = min((2.2 − 0.6t) × 0.70 × (1 − 0.10·cover), 1.45)
```

暗图 `t` 小 → `gmax` 接近 1.45；亮图 `t` 大 → 降到约 1.12。`cover` 越大（整张图都在发光），越要把上限压一点，避免"处处都亮"。这种"按画面自身统计调整映射"的做法，感知依据可参考 Chapiro 的自适应渲染模型[\[6\]](#r6)。

### 7.2 分通道：每条通道抬不同的量

整体亮度由 `emis` 决定，颜色则来自光源颜色 `scene.light_color`：

```text
w_c   = clip(clip(light_color, 0.25, 4.0)^0.6, 0.80, 1.25)   # 通道权重
w_c  /= (0.2126·w_R + 0.7152·w_G + 0.0722·w_B)                # 按亮度归一
tint  = clip((lin / lum)^0.35, 0.85, 1.15)                    # 像素自身色相微调
expo_c = gmax · emis · w_c · tint_c                           # 每通道 log2 增益
```

`w_c` 只由光源颜色决定，`tint` 再按像素自身的饱和度微调：暖光下红通道多抬一点，冷光下蓝通道多抬一点。两者都按 BT.709 亮度归一，保证加权亮度仍然等于 `2^(gmax·emis)`——**分通道只改色相，不偷偷改整体亮度**。

### 7.3 存进 JPEG：两遍求每通道上限

```text
pass 1: expo_max_c = max over pixels( expo_c )        # 每通道各自的最大值
pass 2: code_c = clamp(expo_c / expo_max_c, 0, 1) × 255 + dither
        gm_px  = clamp(code_c, 0, 254)
```

`expo_max` 三元组就是 ISO 元数据里的 `max0/max1/max2`，`gmax_meta = max(expo_max)` 写进 XMP。解码端拿 `recovery_c = code_c/255`，按 `2^(max_c·recovery_c)` 还原每通道增益——和编码端的 `expo_c` 逐通道对齐。

两遍扫描是为了避免把全分辨率 `expo` 三通道同时留在内存里（12.9MP 图约 150 MB/份）。每遍都按 `tile_rows=2048` 分块，PNG/HEIC/AVIF/JXL 走同一条 `_channel_terms`，只是不量化成 8bit。

### 7.4 实测数字

以 1600×669 的样例（`docs/images/sample-source.jpg`）为例：

| 指标                  | 值                        |
| ------------------- | ------------------------ |
| `gmax`              | 1.45                     |
| 三通道 `expo_max`      | R 1.20 / G 1.21 / B 1.37 |
| 峰值亮度                | 486 nits                 |
| 增益 < 1.02 的像素（"没动"） | 48.4%                    |
| 增益 > 2.0 的像素        | 1.7%                     |
| JPEG 重建 vs PNG 母版   | 中位误差 1.3%，p95 4.2%       |
| 六格式总耗时              | 4.4 s（4 路并行 + GPU）       |

体积：Ultra HDR 0.87 MB、PNG16 3.94 MB、HEIC10/12 0.36 MB、AVIF 1.73 MB、JXL24 5.73 MB。

"没动"只占一半，是因为这张样图本身是发光场景（水光、气泡、角色轮廓都在发光）；普通日景插画通常在 70% 以上。

## 8. 增益图 vs 色调映射

|       | 增益图                   | 色调映射（tone mapping）          |
| ----- | --------------------- | --------------------------- |
| 作用对象  | 已有 SDR 图 + 元数据        | 已有 HDR 图 → 适配显示器            |
| 方向    | SDR → HDR（扩展）         | HDR → SDR（压缩）               |
| 是否改基图 | 否                     | 是（输出新像素）                    |
| 兼容性   | 老设备看基图                | 输出固定，无法回退                   |
| 信息损失  | 只有增益估计误差              | 压缩掉的高光/暗部不可逆                |
| 典型标准  | Ultra HDR、ISO 21496-1 | ACES、Reinhard、Hable、BT.2390 |

两者经常被混为一谈，因为都叫"映射"。记住方向：**增益图是往上加余量，tone mapping 是往下压范围。**

### 7.5 加速：GPU 与并行

`expo_c` 与 PQ 编码是纯逐像素数学，适合搬到显卡：`hdrgainmap/gpu.py` 用 WebGPU 计算着色器实现 `srgb_to_linear → tint → 加权 → exp2 → BT.2020 → PQ`（幂运算全部查表），再按块回读 `uint16`；峰值用 `atomicMax` 在核内归约，省一次全图扫描。增益图的两遍扫描也走同一条着色器（输出 `f32` 与每通道最大值）。

GPU 只改变"在哪儿算"，不改变公式。为了把两边对齐到码值，幂运算不再各算各的：`hdrgainmap/lut.py` 用 float64 建四张表（sRGB 解码 4096 段、tint 2048 段、exp2 4096 段、PQ 4096 段按 `(nits/10000)^(1/4)` 非均匀取点），CPU 与着色器都用同一段线性插值采样。CPU 侧顺带修正了一个旧误差：直接在 float32 里算 `x^m1` 再 `**78.84`，指数会放大舍入，PQ 端约 0.9 个 16bit 码值；查表反而更接近双精度真值。实测同图 CPU 与 GPU 的 16bit 码值最多差 1 个 LSB，且只有不到 1% 的像素差这 1 个。`--gpu off` 强制走 NumPy，缺 `wgpu` 时自动回退。

格式编码按 `-j` 并行：`avif`/`jxl` 是子进程，`png` 的滤波按块并行，`heic`/`jpeg` 在各自线程。光场分析（`light.analyze` 的多尺度模糊与光线步进）仍在 CPU，是 6MP 以上分辨率的主要成本。

## 9. 误差来源

1. **启发式估计**：`emissive` 是规则 + 滤波，不是物理反演。大面积平亮（白墙、天空）可能被判成"发光"而偏亮；暗图噪点可能被当成高光。
2. **增益图 JPEG 有损**：8bit 码值再经 DCT 压缩，强边缘可能出现 1–2 个码值的振铃；用高质量参数可压住。
3. **8bit 量化**：0.4% 的增益台阶，可忽略。
4. **分通道是启发式**：`w_c` 来自光源颜色、`tint` 来自像素色相，都不是光谱反演。极端饱和的纯色（纯红发光体）可能抬得偏多或偏少。
5. **offset 与暗部**：`1/64` 的 offset 让暗部增益有上限，深暗区不会被放大成噪点，但也限制了极暗区的提升。
6. **显示器差异**：`weight` 只是按峰值插值，没有考虑 ABL、环境光、面板 gamma。

## 10. 常见错误清单

1. **在 gamma 码值上乘增益。** 必须先线性化（见 [02](02-pq-and-brightness.md)）。
2. **增益图与基图分辨率/朝向上不一致。** 解码会错位；EXIF 转正必须在生成增益图之前做。
3. **把 `gmax` 当 `gmax_meta` 用。** 实际最大增益通常小于理论 `gmax`，用错会让画面整体偏亮。
4. **抖动加在量化前，不是缩放前。** 顺序错了噪声会被平均掉。
5. **主图 MPF 偏移按 TIFF 头算。** 主图偏移是相对文件头的，增益图才是相对 TIFF 头。
6. **只写 XMP 不写 ISO。** Android 16 等新系统优先读 ISO，缺失时行为不一致。
7. **以为增益图能"恢复"已削顶的细节。** 它只能给一个乘数；削顶区域乘完还是平的，只是更亮。

## 参考资料

<a id="r1"></a>**\[1]** DeepWiki. *google/libultrahdr — gain map application in linear light*. <https://deepwiki.com/search/02b63462-b5be-446c-9fbb-72afe2b9ee1d> （访问日期 2026-11-27）

<a id="r2"></a>**\[2]** ISO. *ISO/IEC 21496-1:2025 — Digital photography — Gain map metadata for image conversion — Part 1: Dynamic range conversion*. <https://www.iso.org/standard/86775.html> （访问日期 2026-11-27）

<a id="r3"></a>**\[3]** Android Developers. *Ultra HDR Image Format v1.1*（增益图公式、XMP 字段、GContainer、ISO 兼容段）. <https://developer.android.com/media/platform/hdr-image-format> （访问日期 2026-11-27）

<a id="r4"></a>**\[4]** CIPA. *DC-007: Multi-Picture Format*. <https://www.cipa.jp/std/documents/e/DC-X007-KEY_E.pdf> （访问日期 2026-11-27）

<a id="r5"></a>**\[5]** W3C. *PNG Specification (Third Edition)*（HDR 相关 chunk 与术语）. <https://www.w3.org/TR/png-3/> （访问日期 2026-11-27）

<a id="r6"></a>**\[6]** A. Chapiro. *A Perceptual Model of Adaptive HDR Image Rendering* (2023)（自适应 HDR 渲染的感知依据）. <https://achapiro.github.io/Tar23/Tar23.pdf> （访问日期 2026-11-27）
