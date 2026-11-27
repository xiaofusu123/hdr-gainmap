---
title: hdr-gainmap 使用说明
slug: hdr-gainmap
summary: 把 8bit SDR 插画转成六种 HDR 封装，用自适应光传输模型只抬“在发光”的区域，默认质量优先并可校验。
description: hdr-gainmap 不靠 AI 提亮，而是用光照场、扩散、环境光遮蔽与大面积白墙惩罚判断哪里在发光，再按同一条增益曲线生成 Ultra HDR JPEG、16bit PNG、10/12bit HEIC、12bit AVIF 和 24bit JPEG XL。默认 q100、PNG/JXL 无损、AVIF 优先 4:4:4，并内置容器合规校验。
---

**简体中文** | [English](README.en.md)

**把 8bit SDR 插画转成 HDR 母版**：一条命令输出 Ultra HDR JPEG、16bit PNG、10/12bit HEIC、12bit AVIF 与 24bit JPEG XL，不设档位，增益参数按每张图自己的光影分布计算。

**版本：Beta 0.1.2**。

## 示例

示例源图 `1777789210281_x4_animejanai.png`（12960×5420，8bit SDR）：

![示例源图](docs/images/sample-source.jpg)

左：SDR 源图；右：用本工具生成的 HDR 母版在 SDR 屏幕上的近似预览（`gainmap.render_preview`）。预览只用来显示“哪里被抬亮”，不代表 HDR 屏上的真实亮度。画面里水面涟漪、气泡边缘、发丝高光和星芒被抬起，夜空与暗部保持不动：

![SDR 源图与 HDR 预览对比](docs/images/sample-compare.jpg)

HDR 预览单独放大：

![HDR 预览](docs/images/sample-hdr-preview.jpg)

`--gain-fullres --debug` 输出的全分辨率增益图。越白表示抬得越多：水面涟漪、发丝与气泡高光拿增益，夜空与暗部接近 0：

![全分辨率增益图](docs/images/sample-gainmap.png)

增益图默认半分辨率；解码时按 `gain = 2 ** (pixel / 255 * GainMapMax)` 还原，HDR 线性光 = SDR 线性光 × gain。

## 原理：一条增益曲线，六种封装

SDR 像素按 sRGB EOTF 还原成线性光 `L_sdr`，乘同一个标量增益 `g`，得到 `L_hdr = g · L_sdr`；再转 BT.2020，按 PQ 或 HLG 编码。六种产物共用这条曲线，区别只是容器、色深与色度抽样，所以同一张图在六种格式里“哪里亮”是一致的。`g = 2 ** (gmax · em)`，`em ∈ [0, 1)`，增益范围是 `[1, 2**gmax)`；`gmax` 由图像自身的曝光分位数决定，不写死。

* **PQ 是绝对亮度**：`L_hdr` 以 SDR 白 = 203 nits 为基准换算成 cd/m² 再编码。203 nits 在 PQ 码值上约 0.58，所以把 PQ 文件直接塞进 SDR 查看器会发灰——不是文件坏了，而是缺少 HDR 渲染链路。
* **HLG 是相对信号**：SDR 白落在约 75%，显示端按自己的峰值做 OOTF（本工具按 1000 nits 峰值编码）。兼容 SDR 的 HLG 显示器能直接出图，代价是峰值不如 PQ 可控。
* **Ultra HDR 不存第二张 HDR 图**：它是一张 8bit SDR 主图 + 一张 8bit 增益图。增益图在 log2 域归一化到 `[0, GainMapMax]`（本项目上限 1.45 EV），解码端按显示器 headroom 插值后乘回 SDR。输出遵循 Ultra HDR 1.1：XMP `hdrgm` 与 ISO 21496-1 同时写入；1.1 向下兼容 1.0。

## 六种产物

| 格式       | 色深 / 编码                                                 | 典型用途                              |
| -------- | ------------------------------------------------------- | --------------------------------- |
| `jpeg`   | 8bit sRGB 主图 + 8bit 增益图，MPF + XMP `hdrgm` + ISO 21496-1 | Ultra HDR 1.1，后缀 `.jpeg`          |
| `png`    | 16bit RGB + PQ 或 HLG（`cICP 9/16/0/1` 或 `9/18/0/1`）      | 无损母版、后期交换                         |
| `heic10` | 10bit HEVC Main10（`nclx 9/16/9`）                        | 兼容性最好（Windows / macOS / iOS / 安卓） |
| `heic12` | 12bit HEVC Main12（`nclx 9/16/9`）                        | 色深最高，需解码端支持                       |
| `avif`   | 12bit AV1 4:4:4（优先；`nclx 9/16/9`）                       | 体积小，网页 / 分享                       |
| `jxl`    | 24bit JPEG XL，固定 PQ + BT.2020（`--override_bitdepth=24`） | 高位深无损母版，需 cjxl                    |

同一张图的六种产物共用**同一条增益曲线**，区别只在容器、色深与编码。Ultra HDR 的增益图默认半分辨率（可 `--gain-fullres` 改全分辨率），其余格式都是逐像素中间量编码；JXL 固定 PQ + BT.2020，不跟随 `--transfer` 走 HLG。

## 它解决什么问题

照片训练的 ITM（inverse tone mapping）模型在二次元平涂上容易翻车：平涂糊出块状增益、线稿整条发光、纯白死白。而 SDR 插画的高光**本来就已经削顶**，并没有暗藏细节可“恢复”。

所以目标不是恢复高光细节，而是判断“这块像素是不是在发光”，只把发光体在 HDR 屏上抬亮：

* **发光体**（灯、星芒、眼神光、高光发丝）拿到明显增益；
* **被光照到的表面**拿到少量增益；
* **白发、白墙、皮肤**这类“本来就亮”的大面积区域尽量不动。

## 算法：先判断“哪里在发光”

整条链路在 `hdrgainmap/gainmap.py` 与 `hdrgainmap/light.py`，分五步。

### 1. 自适应曝光参数

固定阈值在不同曝光的图上必然出错：夜景里 0.6 已是很亮的高光，白天白墙也是 0.9。这里用图像自身的亮度分位数估计“整体曝光”：

```python
t       = clip(p50 / 0.60, 0, 1)                  # 0=夜景，1=高调
gmax    = min((2.2 - 0.6*t) * 0.70, 1.45)         # 最大提升 EV
halo    = 0.16 - 0.06*t                           # 泛光占比
hr      = max(1, round(3 - 2*t))                  # 泛光半径（像素）
base_lit= 0.78 - 0.16*t                           # 受光区域基础权重
```

`PEAK_SCALE = 0.70` 与 `GMAX_CAP = 1.45` 是实测约束：超过约 1.6 EV 时，部分手机的高光会出现黑斑，所以峰值主动收敛。`lift` 固定为 1.0——不再整体抬升，只抬被照到的地方。

### 2. 四层发光判据

```python
illum = gauss(Y, min(h, w) // 25)                 # 光照场（低频）
lit   = smoothstep(p38(illum), p99(illum), illum) # 被照到的整片区域

peak  = Y / (gauss(Y, 25) + 1e-4)                 # 比邻域亮多少
spark = smoothstep(1.45, 2.90, peak)              # 细碎高光

area  = gauss(lit, min(h, w) // 8)                # 受光区域的面积
pen   = 0.55 * smoothstep(0.35, 0.85, area) * (1 - 0.7 * spark)
lit  *= 1 - pen                                   # 大片白墙/天空降权

irr   = smoothstep(0.35, 0.85, irradiance(Y))     # 光传播/漫反射
ao    = clip((near + 0.02) / (far + 0.02), 0.45, 1.0) ** 0.9
amb   = 0.10 * smoothstep(0.02, 0.45, far) * ao   # 环境光
white = smoothstep(0.92, 1.0, Y) * (1 - pen)      # 纯白核心

emis  = base_lit*lit + 0.45*irr + 0.45*spark + 0.18*white + amb
emis  = halo * gauss(emis, hr) + (1 - halo) * emis
```

四个判据各管一段：

1. **`lit`（光照场）**：判据是低频的，灯光照到的地方整片抬起，暗部不动；同亮度的相邻区域不会一块亮一块不亮。
2. **`spark`（局部突起）**：星芒、亮点、小光斑比周围明显亮，单独加一层；它还能让面积惩罚对点光源只生效 30%。
3. **`pen`（面积惩罚）**：一大片都被判为“受光”，更可能是白墙、天空、平亮表面，而不是发光体。面积越大压得越多，避免整片推白。
4. **`white`（绝对白）**：只靠相对比值时，纯白镜面/灯芯的中心比值接近 1，反而不触发，边缘却触发，会形成一圈“光环”。补一个绝对亮度项填上中心；已经被 `pen` 压过的大面积白墙不会重复加。

`irr` 让“光球照到的地方”也拿到增益，而不只是光球本身；`amb` 给远离光源的暗部一点环境光，凹缝处因遮蔽更少。

### 3. 平滑饱和与抖动

```python
em   = 1 - exp(-emis / p99.9(emis) * 1.6)   # 指数饱和，不硬截断
em   = gauss(em, 2)
gain = 2 ** (gmax * em)
```

硬截断会在亮部内部造出一块“顶到 1.0 的平台”，平台边界就是肉眼可见的等值线光圈。指数饱和处处连续可导，最亮处也只是趋近 1，不会再出现平台边界。

Ultra HDR 的 8bit 增益图在 log2 域按全分辨率取码，缩到半分辨率时用 LANCZOS；量化前加 ±0.5 LSB 三角抖动，打散大面积渐变上的马赫带。抖动在**降采样之后**做，否则噪点会被插值抹平。

## 光传输近似

`light.py` 不是物理渲染器，而是一个低成本的扩散近似，用来回答“光从哪来、照到了什么”：

* 先把亮度图降采样到长边约 900px，高亮像素（高于 96 分位）作为光源；
* 做 4 个尺度的高斯扩散，σ 逐级加倍，权重按 `σ^-1.75` 衰减：核心近似高斯，尾部近似 `1/r^p` 的散射光；
* 再乘表面反射率（暗表面吸收、浅色表面反射得多）；
* 按第 99.5 分位归一化，双线性升回原尺寸。

这一步的直觉来自平方反比、漫反射与大气散射（短波更容易散射），但**没有**追光线、没有衍射积分、没有多弹道全局光照；它是为插画增益设计的启发式场，追求“看起来对”，不追求物理准确。

环境光遮蔽同样用亮度近似：`(小半径均值 + 0.02) / (大半径均值 + 0.02)`，局部比大范围暗就视为凹缝，夹在 0.45–1。

## 安装

```bash
pip install -e .
# 或者
pip install -r requirements.txt
```

依赖 `numpy`、`pillow`、`pillow-heif`；生成 AVIF 需要系统里有 `ffmpeg`（带 libaom-av1）。生成 24bit JXL 需要 `cjxl` 0.13+（libjxl），可放进 `PATH`，或用环境变量 `HDRGAINMAP_CJXL` / `HDRGAINMAP_JXLINFO` 指定路径；缺少 cjxl 时只让 jxl 这一项失败，其它格式照常。Python 3.10+。

## 用法

### 常用命令

```bash
# 默认：六种格式全出，质量 100，DPI 沿用源图
python -m hdrgainmap photo.png -o out

# 只要 24bit JXL（固定 PQ + BT.2020）
python -m hdrgainmap photo.png -o out -f jxl

# 批量处理
python -m hdrgainmap a.png b.jpg c.webp -o out --quiet

# 只要无损母版和兼容性最好的 HEIC
python -m hdrgainmap photo.png -o out -f png,heic10

# 质量 90（有损格式）并顺带做合规校验
python -m hdrgainmap photo.png -o out -q 90 --validate

# HLG 传递函数（默认 PQ）
python -m hdrgainmap photo.png -o out --transfer HLG

# 透明图合成到白底
python -m hdrgainmap logo.png -o out --alpha-bg white

# 机器可读输出
python -m hdrgainmap photo.png -o out --json --validate
```

### 输出命名

设输入文件名为 `{stem}`，则：

| 产物             | 文件名                                                                  |
| -------------- | -------------------------------------------------------------------- |
| Ultra HDR JPEG | `{stem}_ultrahdr.jpeg`                                               |
| 16bit PNG      | `{stem}_hdr16_pq.png`                                                |
| 16bit PNG（HLG） | `{stem}_hdr16_hlg.png`                                               |
| 10bit HEIC     | `{stem}_hdr10_pq.heic`                                               |
| 12bit HEIC     | `{stem}_hdr12_pq.heic`                                               |
| 12bit AVIF     | `{stem}_hdr12_pq.avif`                                               |
| 24bit JXL      | `{stem}_hdr24_pq.jxl`（固定 PQ + BT.2020）                               |
| 调试图（`--debug`） | `{stem}_emis.png`、`{stem}_gainmap.png`、`{stem}_gainmap_embedded.png` |

### 退出码与批处理

* `0`：全部成功；加了 `--validate` 时还要求校验通过；
* `1`：至少一个文件或格式失败，或者校验失败；JSON 里 `ok=false`，`errors` 给出原因；
* `2`：命令行参数错误（argparse）。

批量时按命令行顺序逐张处理，单张失败不影响后续；`--quiet` 只保留错误输出。`--json` 单张时输出结果对象，多张时输出 `{"results": [...], "ok": ...}`。

### 参数

| 参数               | 说明                                                      |
| ---------------- | ------------------------------------------------------- |
| `-o, --outdir`   | 输出目录，默认 `out`                                           |
| `-f, --formats`  | 逗号分隔：`jpeg` `png` `heic10` `heic12` `avif` `jxl`，默认全部   |
| `-q, --quality`  | 有损编码质量 50–100，默认 100                                    |
| `--dpi`          | 输出 DPI，默认沿用源图                                           |
| `--transfer`     | 传递函数 `PQ`（默认）/ `HLG`                                    |
| `--alpha-bg`     | 透明图合成底色 `black`（默认）/ `white`                            |
| `--gain-fullres` | 增益图保持全分辨率（默认半分辨率）                                       |
| `--validate`     | 生成后逐个产物跑合规校验，失败时退出码 1                                   |
| `--debug`        | 额外落盘 `_emis.png`、`_gainmap.png`、`_gainmap_embedded.png` |
| `--quiet`        | 不打印进度                                                   |
| `--json`         | JSON 输出（路径 + 统计 + 校验报告）                                 |
| `--version`      | 打印版本                                                    |

参数作用范围：`-q` 只影响 JPEG/HEIC/AVIF，PNG 与 JXL 始终无损；`--transfer` 影响 PNG/HEIC/AVIF，Ultra HDR 的 SDR 主图不变，JXL 固定 PQ + BT.2020；`--dpi` 写入所有产物的元数据（JXL 暂不写 DPI）；`--gain-fullres` 只影响 Ultra HDR 增益图；`--alpha-bg` 只对带 alpha 的源生效。

单张图失败不会中断批量：每个文件独立处理，最后统一以退出码表示是否有失败。

`--debug` 的三张图用途不同：`_emis.png` 是发光场（算法中间权重，不等于最终增益）；`_gainmap.png` 是全分辨率增益的 log2 归一化 8bit 可视化；`_gainmap_embedded.png` 是 Ultra HDR JPEG 内嵌的那张（半分辨率或 `--gain-fullres`，含抖动）。前两者按 `gain = 2 ** (pixel / 255 * GainMapMax)` 换算即可。

## Python API

```python
from hdrgainmap import build

res = build(
    "photo.png",
    "out",
    formats=("jpeg", "png", "heic12", "jxl"),
    quality=100,
    transfer="PQ",
    alpha_bg=(0, 0, 0),
    log=print,          # 可选：进度回调
)
print(res["files"]["heic12"])
print(res["stats"]["peak_nits"], res["stats"]["untouched_pct"])
```

`build()` 返回（数值随图变化）：

```python
{
  "src": "photo.png", "dpi": 240.0, "transfer": "PQ", "gmax": 1.42,
  "files": {"jpeg": "...", "png": "...", "heic12": "..."},
  "errors": {},
  "stats": {"gain_mean": 1.18, "gain_max": 2.05, "untouched_pct": 42.0,
            "gt2x_pct": 1.8, "peak_nits": 416.0, "base_reused": True},
  "timings": {"load": 0.8, "base": 0.0, "gain": 36.7, "hdr": 6.7,
              "jxl": 0.8, "jpeg": 0.8, "png": 22.8, "heic12": 1.0,
              "avif": 4.2, "total": 75.0},
  "ok": True,
}
```

关键字段：

* `files`：成功产物的路径；`errors`：按格式记录失败的异常，其他格式继续；
* `stats.gain_mean / gain_max`：线性增益倍数；
* `stats.untouched_pct`：`gain < 1.02` 的像素占比（基本没动的区域）；
* `stats.gt2x_pct`：增益超过 2× 的像素占比；
* `stats.peak_nits`：HDR 线性峰值对应的绝对亮度；
* `stats.base_reused`：是否直接复用了源 JPEG 作为 SDR 主图（仅当源是 JPEG、无 alpha、无 ICC 转换、方向为 1 且 DPI 未变）；
* `timings`：各阶段秒数，`total` 为总耗时；
* `ok`：没有任何格式失败。

单模块也可以直接用：

```python
from hdrgainmap import gainmap, color, png16, heic10, avif

rgb, im, meta = gainmap.prepare_image("photo.png")   # 方向/alpha/ICC 已归一
y = gainmap.luma(rgb)
cfg = gainmap.adapt_params(rgb, y=y)
_, emis = gainmap.emissive(rgb, cfg, y=y)
gain = gainmap.gain_of(gainmap.finalize(emis), cfg["gmax"])

hdr_linear = color.srgb_to_linear(rgb) * gain[..., None]
q16 = color.pq_oetf(color.to_bt2020(hdr_linear) * 203.0) * 65535
png16.encode(q16, dpi=240, transfer="PQ")
heic10.encode(q16, transfer="PQ", bits=12, quality=100, dpi=240)
avif.encode(q16, transfer="PQ", bits=12, quality=100, dpi=240)   # → (blob, bits)
```

24bit JXL 不经过 q16：先写 PFM（float32 码值），再交给 cjxl：

```python
from pathlib import Path
from hdrgainmap import jxl, pipeline

pfm = Path("tmp.pfm")
pipeline._write_pfm(rgb, gain, "PQ", pfm)
jxl.encode(pfm, "out.jxl", "PQ")     # --override_bitdepth=24 -d 0
pfm.unlink()
```

## 颜色与容器

* **源像素按 sRGB 解释**；带 ICC 的源会先转到 sRGB（Display-P3、Adobe RGB、CMYK、LAB 都支持），转换失败则保留原样。EXIF 方向会先摆正，带 alpha 的图按 `--alpha-bg` 合成。
* 线性化后乘增益，再转 **BT.2020**，按 **PQ（203 nits 参考白）** 或 **HLG（1000 nits 峰值）** 编码。
* **PNG** 写 `cICP = (9, 16, 0, 1)`（BT.2020 / PQ / RGB 矩阵 / 全范围），HLG 时 transfer 为 18；`matrix=0` 对 RGB 是正确的。
* **HEIC** 写 `nclx = (9, 16, 9)`（HLG 为 18）；**matrix 必须是 9**（BT.2020 NCL）。不写会落成 6（BT.601），部分解码器按 601 反矩阵，颜色直接偏。
* **AVIF** 的 ffmpeg muxer 不写色彩标签（一律 2/2），编码后改写 `nclx` 7 字节；DPI 通过补一个 Exif item（`iloc` / `iinf` / `iref` + `mdat` 尾部 TIFF，`infe` 必须 version 2）写入。
* **JXL** 固定 24bit、PQ、BT.2020（`Rec2100PQ`），不跟随 `--transfer`；cjxl 不读 PNG 的 PQ 标记，所以用 PFM 的 float32 传码值，再 `--override_bitdepth=24 -d 0` 无损编码，避免“16bit 重标 24bit”的伪位深。JXL 暂不写 DPI。
* **DPI**：PNG 写 `pHYs`；HEIC 借 EXIF 282/283/296；AVIF 补 Exif item。Explorer 面板才会显示 240 dpi 而不是 72。

### Ultra HDR：一张 JPEG，两幅画面

Ultra HDR 不是把 HDR 样本直接塞进 JPEG，而是**一张 SDR 主图 + 一张 8bit 增益图**。两张 JPEG 通过 **MPF**（Multi-Picture Format）关联；主图本身是完整 SDR 图像，增益图在 log2 域记录每个像素相对主图的光比，解码端按屏幕 headroom 插值后把主图乘亮。

本项目固定输出 Ultra HDR 1.1：XMP `hdrgm` 与 ISO 21496-1 同时写入。1.1 向下兼容 1.0，因此不再提供 XMP-only 模式。

本项目的增益图约定：

* 8bit 灰度 JPEG，默认半分辨率（`--gain-fullres` 改全分辨率）；
* 像素值 = `log2(gain)` 归一化到 `[0, GainMapMax]` 后乘 255 量化；
* 量化前加 ±0.5 LSB 三角抖动；半分辨率时先在 log2 域按全分辨率取码，再 LANCZOS 缩小，最后抖动——顺序反了会把噪点抹平；
* `GainMapMin = 0`，`GainMapMax = log2(max(gain))`，`Gamma = 1`，`OffsetSDR = OffsetHDR = 1/64`（0.015625），`BaseRenditionIsHDR = False`。

#### 容器与元数据

主图和增益图各自是完整 JPEG。主图按 **ICC → EXIF → ISO 21496-1 → MPF → XMP** 排列，增益图里是 **XMP → ISO 21496-1**；MPF 的 TIFF 目录有两条记录，主图属性 `0x00030000`，增益图属性 `0`，偏移量相对 MPF TIFF 头计算。主图 XMP 同时带 `hdrgm` 参数和 `GContainer` 目录（`Primary`、`GainMap` 以及增益图字节长度）。

`hdrgm` 使用**元素**形式（`<hdrgm:GainMapMax>1.089…</hdrgm:GainMapMax>`），不用属性形式：属性形式在部分 Honor / Android 16 设备上会让高光变黑。

#### 两套元数据：XMP 与 ISO 21496-1

* **XMP `hdrgm`**：Ultra HDR 1.1 的原始载体，字段是十进制定点，8 位小数。
* **ISO 21496-1 APP2**：2025 年标准化的通用增益图元数据。主图只放 4 字节版本存根（`min_version` / `writer_version` 均为 0），增益图放完整 61 字节：`flags`（`0x40` = 单通道 + 使用基础色彩空间）、base/alternate headroom、每通道 5 个有理数（gain min/max、gamma、base offset、alternate offset）。**全部大端**，分母 `1e8`，因此能和 XMP 的 8 位小数逐位对齐。
* 两套元数据始终同时写入，值必须一致；`validate` 会交叉核对 `GainMapMax`、`GainMapMin`、`OffsetSDR` 与 ISO 字段。

> ISO 按规范必须大端：`0.89387586` 的小端写法会被读成约 `75`，高光因此变黑。早期版本有过这个小端 bug，现已修复，并有逐字节对照参考样本的回归测试。

#### 解码

忽略屏幕 headroom 时，最简公式是：

```text
gain = 2 ** (pixel / 255 * GainMapMax)
HDR  = (SDR + OffsetSDR) * gain - OffsetHDR
```

其中 `pixel` 是增益图的 8bit 值，`SDR` 是主图线性化到 `0..1` 后的值。支持 headroom 的解码器会先按 `log2(max_display_boost)` 在 `[HDRCapacityMin, HDRCapacityMax]` 之间插值出实际 `log2_gain` 再乘，所以同一张图在 400 nits 和 1000 nits 屏幕上亮度不同。

## 质量与性能

默认按“质量优先”配置：

* JPEG 质量 100、4:4:4 无色度抽样；
* HEIC / AVIF 质量 100（AVIF 的 AV1 `crf = (100-q)*0.63`，100 → crf 0）；
* AVIF 优先 4:4:4，插画色边锐利，4:2:0 会把彩色高光糊掉；若 12bit 4:4:4 失败会自动降级（4:2:0 或 10bit）；
* PNG 无损，zlib level 9；
* JXL 24bit 无损（cjxl `-d 0 -e 7 --override_bitdepth=24`），固定 PQ + BT.2020；
* 增益图默认半分辨率：在 log2 域按全分辨率取码，再用 LANCZOS 缩小，抖动在缩放后生成；`--gain-fullres` 保持全分辨率（70.2MP 实测内嵌增益图 JPEG 9.91MB）。

性能（6 核 CPU，17.8MP / 5800×3066 参考）：

* 只出 Ultra HDR JPEG 约 45s，其中增益计算约 37s；
* 70.2MP（12960×5420）出五种格式（不含 JXL）约 7.5 分钟：gain 181s、PNG 111s、AVIF 89s、HEIC 18s；全分辨率增益图让 Ultra HDR 体积增加约两成；
* 24bit JXL：1000×1700 约 2.4s（jxl 0.8s），9.3MB；70.2MP 约 232s（gain 153s、hdr 25s、jxl 51s），273MB；PFM 是临时文件，体积约为像素数 × 12 字节，70MP 时约 840MB，编码完即删；
* PNG 是耗时大头，17.8MP 约 23s；
* HDR 中间量按 `tile_rows=2048` 分块编码，避免整幅 `float32` 同时驻留；
* 各产物原子写入（临时文件 + `os.replace`），中途失败不会留下半截文件。

上亿像素时 PNG 会到 200MB 量级，请留足内存；`Image.MAX_IMAGE_PIXELS = None` 已放开 Pillow 的解压炸弹保护。

## 校验

```bash
python -m hdrgainmap.validate out/x_hdr16_pq.png out/x_hdr10_pq.heic out/x_hdr12_pq.heic out/x_hdr12_pq.avif out/x_hdr24_pq.jxl
```

* **PNG**：IHDR 16bit RGB、`cICP` 9/16 或 9/18、pHYs、zlib 长度与滤波字节范围、能否解码；
* **HEIC**：位深 10/12、`nclx` primaries=9、transfer 16/18、**matrix=9**、码值是否触顶；
* **AVIF**：major brand、`nclx`（primaries=9、transfer 16/18、matrix=9）、`pixi` 位深、能否解码；
* **Ultra HDR**：MPF 两条、`hdrgm` 参数、增益图可解码；ISO 21496-1 逐字段核对（主图存根、增益图完整载荷、与 XMP 交叉一致）；
* **JXL**：容器/码流签名、`jxlinfo` 读到的位深必须是 24、传递函数 PQ/HLG、原色 Rec.2100；没有 jxlinfo 时退化为签名检查并给出警告。

## 测试

```bash
python -m pytest              # 19 个测试
python tests/test_roundtrip.py  # 也可以直接跑脚本
```

覆盖：PQ 编解码互逆、自适应参数随图变化、六种格式全量构建、24bit JXL 位深/色彩校验、PNG 两条解码路径逐像素一致、HLG 的 `cICP`、质量参数影响体积、EXIF 方向、alpha 合成、ICC→sRGB、分块 HDR 编码一致性、大面积白墙降权、原子写入、JPEG 主图复用、API 参数校验、调试增益图输出、ISO 21496-1 大端布局与可读性。

## 已知限制

* **HEIC 12bit 解码端窄**：Windows 自带 HEVC 扩展只有 Main10，Main12 报 `0xC00D3E8C`；需要第三方解码器。
* **12bit AVIF**：Chromium 系与 Firefox 软件解码可显示；Windows 照片 App 需 AV1 扩展。
* **PQ 直传在 SDR 环境下必然发灰**：SDR 白（203 nits）只占码值约 0.58，没有 HDR 渲染链路时它就是灰的。
* **HEIF 无 DPI 字段**：只能借 EXIF，WIC 认、第三方 App 未必认。
* **HEIC/AVIF 的 SDR 回退取决于查看器**：文件里只有一张 HDR 图，没有独立 SDR 基图；部分安卓 16 设备会把 PQ 码值整体抬高，此时 SDR 观感会过曝，而 HDR 显示正常。这是设备映射问题，未在编码端做“中间调压暗”补偿。
* **启发式模型有边界**：超大面积的平亮表面可能被压得偏暗；极暗图上的噪点可能被当成细碎高光；它不追求物理正确的光传输。
* **AVIF 是普通 HDR AVIF**，不是带增益图的 tmap 封装；SDR 回退同样交给查看器。
* **24bit JXL 的查看器支持有限**：Chrome/Firefox 新版可解码，Windows 照片与资源管理器未必认 24bit；JXL 暂不写 DPI，也不跟 `--transfer` 走 HLG（固定 PQ + BT.2020）。
* **无 GPU 加速**：增益场与模糊都在 CPU 上用 NumPy 完成。

## 目录

```
hdr-gainmap/
├─ hdrgainmap/
│  ├─ color.py      色彩空间与传递函数（sRGB / PQ / HLG / BT.2020）
│  ├─ gainmap.py    读图归一、自适应参数、发光权重、增益
│  ├─ light.py      多尺度光照扩散（光从哪来、照到了什么）
│  ├─ png16.py      16bit PQ/HLG PNG 编解码
│  ├─ heic10.py     10/12bit HEIC 编解码（nclx 9/16/9）
│  ├─ avif.py       12bit AVIF 编解码（ffmpeg/libaom，补 nclx 与 Exif DPI）
│  ├─ jxl.py        24bit JPEG XL（PFM → cjxl，固定 PQ + BT.2020）
│  ├─ ultrahdr.py   Ultra HDR 1.1 封装/解析（MPF + XMP hdrgm + ISO 21496-1）
│  ├─ validate.py   产物合规校验
│  ├─ pipeline.py   一条龙构建
│  └─ cli.py        命令行
├─ tests/
│  ├─ test_roundtrip.py
│  └─ test_units.py
├─ docs/images/     README 示例图（源图、对比、预览、增益图）
├─ pyproject.toml
├─ LICENSE          MIT
├─ THIRD_PARTY_NOTICES.md
└─ _archive/        历史快照与实验脚本（仅备查）
```

## 参考

* [Android Ultra HDR 图像格式](https://developer.android.com/media/platform/hdr-image-format)
* [ISO/IEC 21496-1](https://www.iso.org/standard/86775.html) 与 [CIPA DC-007](https://www.cipa.jp/std/documents/e/DC-X007-KEY_E.pdf)
* [google/libultrahdr](https://github.com/google/libultrahdr)
* [PNG 第三版（W3C Recommendation）](https://www.w3.org/TR/png-3/)
* [AVIF 规范](https://aomediacodec.github.io/av1-avif/)
* [JPEG XL 项目与规范入口](https://jpeg.org/jpegxl/) 与 [libjxl](https://github.com/libjxl/libjxl)（ISO/IEC 18181-1/2）
* [ITU-R BT.2408-9（HDR 参考白 203 nits）](https://www.itu.int/dms_pub/itu-r/opb/rep/R-REP-BT.2408-9-2026-PDF-E.pdf)
* Tariq & Chapiro, *Perceptually Adaptive Real-Time Tone Mapping*, 2023（[论文](https://achapiro.github.io/Tar23/Tar23.pdf)）
* [gainmaps.com](https://www.gainmaps.com/)

## 许可与合规

本项目以 MIT 发布，见 [LICENSE](LICENSE)。第三方依赖、工具、版本与许可证逐项列在 [THIRD\_PARTY\_NOTICES.md](THIRD_PARTY_NOTICES.md)。

* 运行依赖：NumPy（BSD-3-Clause）、Pillow（MIT-CMU）、pillow-heif（BSD-3-Clause；wheel 内含 libheif LGPL-3.0、x265 GPL-2.0/商业双许可、libde265 LGPL-3.0）。
* 外部工具：FFmpeg（本机 9.1 full build 启用了 GPL 组件，按 GPLv3 使用）、libaom（BSD-style）、SVT-AV1（BSD-3-Clause Clear）、x264/x265（GPL-2.0）、libjxl/cjxl（BSD-3-Clause）。
* 本仓库不打包上述二进制：ffmpeg 通过 `subprocess` 调用，HEIC 通过 pillow-heif 使用 libheif。因此本项目自身仍按 MIT 发布；若二次分发 GPL 版 ffmpeg 或含 x265 的 wheel，需要按 GPLv3/LGPL-3.0 提供许可证文本与对应源码。
* 当前使用方式不需要额外购买许可证；x265 的商业许可只在无法接受 GPL 条款、且需要闭源分发时才有必要。
* 仓库不含第三方图片；示例图由用户提供的源图生成，公开分发前请确认源图使用权。
