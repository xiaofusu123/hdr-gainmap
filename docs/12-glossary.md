# 12 术语表

按英文首字母排序。中文译名尽量用行业习惯说法，括号里给首次出现/展开的篇目。定义只求"能用来读文档"，不追求教科书式的完备。

本篇是索引，不单独列参考资料：每条术语末尾的 `见 [NN]` 指向详解篇目，出处与访问日期都在那一篇的"参考资料"里。

## A

**Adaptive HDR（自适应 HDR）** — 按观看环境、显示器能力、内容亮度动态调整 HDR 呈现的做法。增益图的 `weight`（按显示器 headroom 插值）就是一种自适应。见 [04](04-gain-map.md)。

**Aerial perspective（空气透视 / 空间透视）** — 远处物体因大气散射/吸收而对比度下降、颜色变淡偏蓝、细节减少的现象。绘画里用来表现纵深。见 [11](11-perspective.md)。

**Airy disk（艾里斑）** — 圆孔衍射形成的中央亮斑，第一暗环在 `sinθ = 1.22λ/D`。它决定了光学系统的分辨率极限。见 [10](10-light-and-rendering.md)。

**Ambient occlusion, AO（环境光遮蔽）** — 一个点被周围几何遮挡环境光的比例，`A(x) = (1/π)∫V(x,ω)(n·ω)dω`。只与几何有关，与光源无关。见 [10](10-light-and-rendering.md)。

**AVIF（AV1 Image File Format）** — 基于 AV1 的 HEIF 容器格式，支持 8/10/12bit、4:2:0–4:4:4、HDR。本项目输出 12bit 4:4:4。见 [08](08-avif.md)。

## B

**Base image（基图）** — 增益图文件里的 SDR 主图，老设备直接显示，新设备作为乘法的底。见 [04](04-gain-map.md)。

**Beer–Lambert law（比尔–朗伯定律）** — 光穿过吸收介质按 `I = I₀e^(−αL)` 指数衰减。用于雾、水、大气。见 [10](10-light-and-rendering.md)。

**Bit depth（色深 / 位深）** — 每个通道的量化位数。8bit=256 级，10bit=1024，12bit=4096，16bit=65536。位深决定渐变是否 banding，与色域无关。见 [03](03-color-gamut-and-depth.md)。

**Bloom（泛光）** — 高光溢出到周围像素的后期效果，模拟镜头/眼睛的散射。本项目用 `halo` + `finalize` 近似。见 [10](10-light-and-rendering.md)。

**BRDF（双向反射分布函数）** — `f_r = dL_o/dE_i`，描述表面对入射光的反射分布。满足互易性和能量守恒。见 [10](10-light-and-rendering.md)。

**BT.1886** — ITU-R 建议书，定义 SDR 平板显示器的参考 EOTF（gamma 约 2.4，参考白 100 cd/m²）。见 [01](01-hdr-and-sdr.md)。

**BT.2020** — ITU-R 超高清电视参数建议书，定义更宽的原色（覆盖约 75.8% CIE 1931）、PQ/HLG 两种传递函数。HDR 输出的目标色域。见 [03](03-color-gamut-and-depth.md)。

**BT.2100** — ITU-R HDR 电视建议书，把 PQ 和 HLG 收为两种 HDR 传递函数，并规定参考环境。见 [01](01-hdr-and-sdr.md)。

**BT.2408** — ITU-R 报告，HDR 制作操作指南。规定 HDR 参考白 203 cd/m²、信号电平、上下变换等。见 [01](01-hdr-and-sdr.md)。

## C

**cd/m²（坎德拉每平方米）** — 亮度的 SI 单位，等于 nit。屏幕亮度常用它。见 [01](01-hdr-and-sdr.md)。

**Chroma subsampling（色度采样）** — 对 Cb/Cr 降采样以省体积。4:4:4 不降，4:2:2 水平减半，4:2:0 水平垂直都减半。见 [03](03-color-gamut-and-depth.md)。

**CICP（Coding-independent code points）** — 用整数枚举描述色彩空间：primaries、transfer、matrix、range。PNG 的 `cICP` chunk、HEIF/AVIF 的 `nclx` 都用它。见 [03](03-color-gamut-and-depth.md)。

**CIE 1931** — 国际照明委员会的 XYZ 色彩空间与 xy 色度图，色域覆盖率的通用参照。见 [03](03-color-gamut-and-depth.md)。

**cLLI（Content Light Level Information）** — PNG 第三版的 chunk，记录 MaxCLL/MaxFALL。见 [06](06-png.md)。

**Color bleeding（色溢）** — 间接光把邻近物体的颜色带到白墙上。全局光照的典型现象。见 [10](10-light-and-rendering.md)。

**Color constancy（颜色恒常性）** — 光源变化时人眼仍把物体知觉成"同一颜色"的倾向。它解释了为什么"绝对亮度"没有唯一正确答案。见 [01](01-hdr-and-sdr.md)。

**Color gamut（色域）** — 一个色彩空间能表示的颜色范围，由三原色坐标 + 白点决定。见 [03](03-color-gamut-and-depth.md)。

## D

**D65** — 常用日光白点，色度约 (0.3127, 0.3290)。sRGB、BT.709、BT.2020 都用它。见 [03](03-color-gamut-and-depth.md)。

**Depth proxy（深度代理）** — 2D 图里没有 Z，用"画面垂直位置 + 局部对比度"估出的伪深度，供空气透视使用。见 [11](11-perspective.md)。

**Dither（抖动）** — 量化前加噪声，把规则 banding 打散成随机噪点。三角分布（两个均匀分布之差）常用。见 [03](03-color-gamut-and-depth.md)。

**Dolby Vision** — 杜比专有 HDR 格式，母版常 12bit、动态元数据、ICtCp 编码，需要授权。见 [01](01-hdr-and-sdr.md)。

**Dynamic range（动态范围）** — 最亮与最暗之比，常用 stop 或 `:1` 表示。分场景、显示、编码三种所指。见 [01](01-hdr-and-sdr.md)。

## E

**EOTF（电光转换函数）** — 显示端把信号值转成光。与 OETF 互为逆（理想情况下）。见 [02](02-pq-and-brightness.md)。

**EV（曝光值）** — `EV = log2(N²/t)`；也常用来表示亮度差，1 EV = 1 stop = 亮度翻倍。

**EXIF** — 图片元数据标准，含朝向（0x0112）、分辨率（282/283/296）等。本项目用它转正和写 DPI。见 [05](05-jpeg-ultra-hdr.md)。

## F

**FOV（视场角）** — `FOV = 2·arctan(d/2f)`。广角 FOV 大、长焦 FOV 小。见 [11](11-perspective.md)。

**Fresnel equations（菲涅耳方程）** — 描述反射率随入射角变化。垂直入射 `R₀ = ((n₁−n₂)/(n₁+n₂))²`，掠射趋 1。见 [10](10-light-and-rendering.md)。

## G

**Gain map（增益图）** — 与 SDR 基图配套的图，存每个像素的 log2 增益；显示端按能力乘上去还原 HDR。可以是单通道，也可以是 RGB 三通道。见 [04](04-gain-map.md)。

**Gamma（伽马）** — 幂函数形式的传递曲线，`V = L^(1/γ)`。SDR 显示约 2.2–2.4；增益图的 `Gamma` 字段默认 1.0。见 [02](02-pq-and-brightness.md)。

**GContainer** — Ultra HDR 在 XMP 里描述"主图 + 增益图"目录的命名空间（`Container:Directory`）。见 [05](05-jpeg-ultra-hdr.md)。

**GI（全局光照）** — 直接光 + 间接光的统称。见 [10](10-light-and-rendering.md)。

**Guided filter（导向滤波）** — He 等 2010 提出的保边平滑：用引导图约束局部线性模型，`O(N)`。本项目用它算光照扩散和 AO，避免普通高斯把物体边界抹平。见 [10](10-light-and-rendering.md)。

## H

**HDR（高动态范围）** — 能表示/显示更大亮度范围的格式或设备。不是"更亮"，是"余量更大"。见 [01](01-hdr-and-sdr.md)。

**HDR10** — 开放 HDR 格式：BT.2020 + PQ + 10bit + SMPTE ST 2086 静态元数据。见 [01](01-hdr-and-sdr.md)。

**HEIC / HEIF** — HEIF 是 ISO/IEC 23008-12 定义的形象文件容器；HEIC 是 HEIF 里装 HEVC 的常见叫法。见 [07](07-heic.md)。

**HLG（Hybrid Log-Gamma）** — 场景参考的 HDR 传递函数，低段 gamma、高段对数，向后兼容 SDR。见 [01](01-hdr-and-sdr.md)。

## I

**ICC profile** — 描述设备色彩空间的配置文件，可带 LUT。JPEG/PNG/HEIF 都能嵌。见 [03](03-color-gamut-and-depth.md)。

**Illuminance（照度）** — 落在表面上的光通量密度，单位 lx = lm/m²。见 [10](10-light-and-rendering.md)。

**Inverse-square law（平方反比）** — 点光源照度 `E = I/r²`。见 [10](10-light-and-rendering.md)。

**Irradiance（辐照度）** — 单位面积接收的辐射功率，`E = ∫L cosθ dω`，单位 W/m²。见 [10](10-light-and-rendering.md)。

**ISO 21496-1** — 增益图元数据的国际标准（2025），JPEG/HEIF/AVIF 通用。支持 1 或 3 通道；三通道时标志位 `0x80`，总 141 字节。见 [04](04-gain-map.md)。

## J

**JND（最小可辨差异）** — 人眼刚好能看出的亮度/颜色变化。PQ 的码值分配目标就是"一步约一个 JND"。见 [02](02-pq-and-brightness.md)。

**JPEG XL（JXL）** — ISO/IEC 18181 定义的新格式，支持最高 32bit/通道、无损、VarDCT/Modular 两种模式。见 [09](09-jpeg-xl.md)。

## L

**Lambertian（朗伯）** — 辐射亮度与方向无关的漫反射表面，BRDF `ρ/π`，`M = πL`。见 [10](10-light-and-rendering.md)。

**Linear perspective（线性透视）** — 平行线汇聚到消失点、物体按 `1/Z` 缩小的几何投影规律。见 [11](11-perspective.md)。

**Lookup table / LUT（查找表）** — 把非线性函数按段采样成表，运行时只做线性插值。本项目用它统一 CPU 与 GPU 的 sRGB/tint/exp2/PQ 计算，16bit 码值差不超过 1。见 [02](02-pq-and-brightness.md)、[04](04-gain-map.md)。

**Luminance（亮度）** — 视觉加权的辐射亮度，单位 cd/m²（nit）。见 [01](01-hdr-and-sdr.md)。

**Luminosity function（视见函数）** — `V(λ)`，人眼对不同波长的敏感度，峰值 555 nm，`1 W = 683 lm`。见 [10](10-light-and-rendering.md)。

## M

**mDCV（Mastering Display Color Volume）** — PNG 第三版的 chunk，记录母版显示器的原色、白点与最大/最小亮度。见 [06](06-png.md)。

**Metamerism（同色异谱）** — 不同光谱产生相同三刺激值的现象。RGB 显示的基础。见 [03](03-color-gamut-and-depth.md)。

**MPF（Multi-Picture Format）** — CIPA DC-007 定义的多图容器，Ultra HDR 用它把增益图挂到主图后面。见 [05](05-jpeg-ultra-hdr.md)。

## N

**nclx** — HEIF/AVIF `colr` box 里的 CICP 枚举形式（primaries/transfer/matrix/range）。见 [03](03-color-gamut-and-depth.md)。

**Nit（尼特）** — cd/m² 的俗称。见 [01](01-hdr-and-sdr.md)。

## O

**OETF（光电器转换函数）** — 拍摄/编码端把线性光转成信号。与 EOTF 互为逆。见 [02](02-pq-and-brightness.md)。

**Offset（偏移）** — 增益图公式里加在 SDR/HDR 上的小常数（默认 1/64），防止除零、抑制暗部噪声放大。见 [04](04-gain-map.md)。

## P

**Path tracing（路径追踪）** — 用蒙特卡洛采样解渲染方程的方法，无偏，噪声 `O(1/√N)`。见 [10](10-light-and-rendering.md)。

**Photon mapping（光子映射）** — 两遍法全局光照，先从光源发光子再密度估计，有偏但适合焦散。见 [10](10-light-and-rendering.md)。

**Per-channel gain（分通道增益）** — 为 R/G/B 各存一个增益，而不是共用一个亮度增益。修正光源色偏（暖光下蓝通道需要更多 headroom）。见 [04](04-gain-map.md)。

**Pixel gain（像素增益）** — `(Y_hdr+offset_hdr)/(Y_sdr+offset_sdr)`，增益图存的就是它的 log2。见 [04](04-gain-map.md)。

**PQ（Perceptual Quantizer）** — SMPTE ST 2084 定义的绝对亮度传递函数，0–10000 cd/m²，感知均匀。见 [02](02-pq-and-brightness.md)。

**Primary（原色）** — 色域三角形的三个顶点，用 CIE xy 坐标表示。见 [03](03-color-gamut-and-depth.md)。

## R

**Radiance（辐射亮度）** — 单位面积、单位立体角的辐射功率，`W/(m²·sr)`。渲染方程里的 `L`。见 [10](10-light-and-rendering.md)。

**Radiosity（辐射度）** — 把场景离散成面片解能量方程的漫反射全局光照方法。见 [10](10-light-and-rendering.md)。

**Ray marching（光线步进）** — 沿射线以固定步长采样累积（而不是求解析交点），常用于体积雾/云。本项目拿它近似 2D 亮度场里的"光束传播"。见 [10](10-light-and-rendering.md)。

**Rayleigh criterion（瑞利判据）** — 两点刚好可分辨的最小角距离 `θ = 1.22λ/D`。见 [10](10-light-and-rendering.md)。

**Rayleigh scattering（瑞利散射）** — 小颗粒散射，强度 `∝ 1/λ⁴`，解释天空蓝、夕阳红。见 [10](10-light-and-rendering.md)。

**Rendering equation（渲染方程）** — Kajiya 1986 给出的光传输积分方程，路径追踪的理论基础。见 [10](10-light-and-rendering.md)。

## S

**SDR（标准动态范围）** — 参考白约 100 cd/m²、动态范围 5–8 stops 的传统显示/格式约定。见 [01](01-hdr-and-sdr.md)。

**Snell's law（斯涅尔定律）** — `n₁sinθ₁ = n₂sinθ₂`。见 [10](10-light-and-rendering.md)。

**SSAO（屏幕空间环境光遮蔽）** — 用深度缓冲在屏幕空间估计 AO，快但有光晕。见 [10](10-light-and-rendering.md)。

**Stop（档）** — 亮度翻倍为 1 stop。动态范围 N stops 即 2^N:1。见 [01](01-hdr-and-sdr.md)。

## T

**Tint（色偏）** — 分通道增益里按"通道线性值/亮度"算出的相对色偏，`tint = clip((L_c/L)^0.35, 0.85, 1.15)`。见 [04](04-gain-map.md)。

**Tone mapping（色调映射）** — 把 HDR 范围压到显示能力内的过程。与增益图方向相反。见 [04](04-gain-map.md)。

## U

**Ultra HDR** — Android 的 HDR 图片规范：8bit SDR 基图 + 8bit 增益图 + XMP/ISO 元数据，装在 JPEG 里。增益图是全分辨率 RGB，逐通道增益写在 ISO 141 字节段里。见 [05](05-jpeg-ultra-hdr.md)。

## V

**Vanishing point（消失点）** — 一组平行线在画面上的汇聚点；水平方向的消失点都在地平线上。见 [11](11-perspective.md)。

## W

**Weber–Fechner law（韦伯–费希纳定律）** — 感知强度与刺激的对数成正比；亮度上表现为 `ΔL/L ≈ 常数`（约 1–2%）。见 [10](10-light-and-rendering.md)。

## X

**XMP** — Adobe 的可扩展元数据平台，Ultra HDR 用 `hdrgm:` 命名空间存增益图字段。见 [04](04-gain-map.md)。

***

## 符号速查

| 符号          | 含义                | 单位/范围             |
| ----------- | ----------------- | ----------------- |
| `L` / `Lv`  | 辐射亮度 / 亮度         | W/(m²·sr) / cd/m² |
| `E` / `Ev`  | 辐照度 / 照度          | W/m² / lx         |
| `I`         | 发光强度              | cd                |
| `Φ`         | 光通量               | lm                |
| `Y`         | 线性亮度（luma）        | 0–1 或 cd/m²       |
| `ρ`         | 反射率               | 0–1               |
| `θ`         | 角度                | rad/deg           |
| `λ`         | 波长                | nm                |
| `Ω`         | 立体角               | sr                |
| `g`         | 增益                | ≥1                |
| `gmax`      | 最大增益（log2）        | 1.12–1.45         |
| `gmax_c`    | 第 c 通道的最大增益（log2） | 三通道各自 1.0–1.5     |
| `gmax_meta` | 实际写入元数据的最大增益      | `max(gmax_c)`     |
| `em`        | 归一化发光度            | 0–1               |
| `emis`      | 发光度（finalize 前）   | 0–1               |
| `expo_c`    | 第 c 通道的 log2 曝光量  | `gmax·emis·w_c`   |
| `w_c`       | 第 c 通道的光色权重       | 0.8–1.25          |
| `tint_c`    | 第 c 通道的色偏修正       | 0.85–1.15         |
| `cover`     | 光源覆盖率             | 0–1               |
| `depth`     | 深度代理              | 0–1（1 = 最远）       |
| `t`         | 亮度自适应参数           | 0–1               |
| `ao`        | 环境光遮蔽近似           | 0.45–1.0          |
| `env`       | 环境光包络             | 0–1               |
| `irr`       | 光场辐照度             | 0–1               |
| `spark`     | 点状高光项             | 0–1               |
| `halo`      | 高光混合系数            | 0.10–0.16         |
| `hr`        | 高光模糊半径            | 1–3 px（按图缩放）      |

## 单位速查

| 单位        | 含义  | 换算                    |
| --------- | --- | --------------------- |
| W         | 瓦特  | 辐射通量                  |
| lm        | 流明  | 555 nm 处 1 W = 683 lm |
| lx        | 勒克斯 | 1 lx = 1 lm/m²        |
| cd        | 坎德拉 | 1 cd = 1 lm/sr        |
| cd/m²     | 尼特  | 屏幕亮度                  |
| sr        | 球面度 | 半球 = 2π sr            |
| stop / EV | 档   | 1 stop = 亮度 ×2        |

## 标准代号速查

| 代号               | 内容                      | 篇目                                                        |
| ---------------- | ----------------------- | --------------------------------------------------------- |
| BT.709           | HDTV 原色/OETF            | [01](01-hdr-and-sdr.md)、[03](03-color-gamut-and-depth.md) |
| BT.1886          | SDR 参考 EOTF             | [01](01-hdr-and-sdr.md)                                   |
| BT.2020          | UHDTV 原色/传递函数           | [01](01-hdr-and-sdr.md)、[03](03-color-gamut-and-depth.md) |
| BT.2100          | HDR 电视（PQ/HLG）          | [01](01-hdr-and-sdr.md)、[02](02-pq-and-brightness.md)     |
| BT.2408          | HDR 制作实践（203 nits）      | [01](01-hdr-and-sdr.md)                                   |
| ST 2084          | PQ 数学定义                 | [02](02-pq-and-brightness.md)                             |
| ST 2086          | 母版显示元数据                 | [02](02-pq-and-brightness.md)                             |
| CTA-861.3        | MaxCLL/MaxFALL          | [02](02-pq-and-brightness.md)                             |
| H.273            | CICP 枚举                 | [03](03-color-gamut-and-depth.md)                         |
| ISO/IEC 21496-1  | 增益图元数据                  | [04](04-gain-map.md)                                      |
| CIPA DC-007      | MPF                     | [04](04-gain-map.md)                                      |
| ISO/IEC 23008-12 | HEIF                    | [07](07-heic.md)                                          |
| ISO/IEC 18181    | JPEG XL                 | [09](09-jpeg-xl.md)                                       |
| PNG 3rd          | PNG 第三版（cICP/mDCV/cLLI） | [06](06-png.md)                                           |
| AVIF 1.2         | AVIF 规范                 | [08](08-avif.md)                                          |
| DICOM Sup. 232   | JPEG XL 24bit 依据        | [09](09-jpeg-xl.md)                                       |
