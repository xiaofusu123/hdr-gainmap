# hdr-gainmap

把 8bit SDR 插画转成 HDR 母版，输出四种格式，**不设档位**——增益参数按每张图自己的光影分布算：

| 格式 | 色深 / 编码 | 用途 |
|---|---|---|
| `jpeg` | 8bit 主图 + 增益图（Ultra HDR，XMP `hdrgm`） | 安卓原生 HDR，后缀 `.jpeg` |
| `png` | 16bit RGB + PQ/HLG（`cICP 9/16/0/1`） | 无损母版、后期交换 |
| `heic10` | 10bit HEVC Main10（`nclx 9/16/9`） | 兼容性最好（Windows/macOS/iOS/安卓） |
| `heic12` | 12bit HEVC Main12（`nclx 9/16/9`） | 色深最高，需解码端支持 |
| `avif` | 12bit AV1 4:4:4（`nclx 9/16/9`） | 体积最小，网页/分享 |

## 思路：只抬发光体，阈值随图自适应

照片训练的 ITM 模型在二次元平涂上会翻车——平涂糊出块状增益、线稿整条发光、纯白死白。
而 SDR 插画的高光**本来就已经削顶**，没有暗藏细节可"恢复"。所以这里只判断"这块像素是不是在发光"：

```
bright = smoothstep(b0, b1, Y)                        # 绝对亮度够高
peak   = Y / (gauss(Y, pr) + 1e-4)                    # 比邻域亮多少
emis   = bright * (flat + (1 - flat) * smoothstep(p0, p1, peak))
emis   = halo * gauss(emis, hr) + (1 - halo) * emis   # 向外 bloom 一圈
gain   = 2 ** (gmax * emis)                           # 线性倍数
```

固定阈值在不同曝光的图上必然出错：夜景里 0.6 已是很亮的高光，白天白墙也是 0.9。参照局部适应
亮度做阈值（Chapiro, *Perceptually Adaptive Real-Time Tone Mapping*, 2023：适应亮度取金字塔更高层，
阈值随适应亮度缩放），这里用**图像自身的亮度分位**代替：

```
t   = clamp(p50 / 0.60, 0, 1)          # 整体曝光：0=夜景，1=高调
b0  = clip(p99 - 0.05, 0.55, 0.95)     # 只有最亮的那 ~1% 开始抬
b1  = clip(p99.9 + 0.004, b0+0.02, 1)  # 99.9 分位饱和
halo= 0.16 - 0.06t                     # 泛光占比：暗 0.16 → 亮 0.10
hr  = max(1, round(3 - 2t))            # 泛光半径：暗 3px → 亮 1px
gmax= (2.2 - 0.6t) * 0.85               # 最大提升：暗 1.87 EV → 高调 1.36 EV
lift= 1.10 - 0.02t                      # 整体微抬，避免皮肤偏黑
gain= lift * 2 ** (gmax * emis)
```

于是白发、白墙、皮肤这些"本来就亮"的大面积区域不会被整片推白，只有星芒、气泡高光、眼神光
这类局部突起才拿到增益；暗图阈值自然低、亮图自然高，不需要按素材类型分档。

## 光传输模型（v2.1）

增益不是逐点看"这个像素亮不亮"，而是先估计**光从哪里来、照到了什么**：

1. **光照场**：对亮度做 ~1/25 图宽的大半径模糊，取该场的 38–99 分位做平滑阈值；
   被照到的区域整片抬起，暗部不动 —— 判据是低频的，同亮度的相邻区域不会一块亮一块不亮。
2. **传播 / 衍射 / 漫反射**（`hdrgainmap/light.py`）：高亮像素作为光源，做多尺度扩散
   （σ 逐级加倍、权重按 σ^(-1.75) 衰减，核心近似高斯、尾部近似 1/r^p 的散射光），
   再乘表面反射率（暗表面吸收、浅色表面反射得多）。
3. **全局光照 + 环境光遮蔽**：`amb = 0.10 × 环境亮度 × 遮蔽`，遮蔽用
   `(小半径均值)/(大半径均值)` 近似，夹在 0.45–1，凹缝处更少。
4. **细碎高光**：比周围明显突起的像素（星芒、亮点）单独加一层。
5. **平滑饱和**：`em = 1 − exp(−emis/p × 1.6)`，不再硬截断 —— 硬截断会在亮部
   形成"顶到满值的平台"，平台边界就是肉眼可见的等值线光圈。
6. **抖动**：增益图量化到 8bit 前加 ±0.5 LSB 三角噪声，打散大面积渐变上的马赫带。

没有"档位"，也没有整体抬升；所有格式（Ultra HDR JPEG / PNG16 / HEIC 10-12bit / AVIF 12bit）
走同一条增益曲线。

## 安装

```bash
pip install -r requirements.txt
```

依赖：`numpy`、`pillow`、`pillow-heif`；AVIF 需要系统里有 `ffmpeg`（带 libaom-av1）。
Python 3.10+。

## 用法

```bash
# 默认：四种格式全出，质量 100，DPI 沿用源图
python -m hdrgainmap photo.png -o out

# 只要无损母版和兼容性最好的 HEIC
python -m hdrgainmap photo.png -o out -f jpeg,png,heic10

# 质量 90（有损格式）并顺带做合规校验
python -m hdrgainmap photo.png -o out -q 90 --validate

# HLG 传递函数（默认 PQ）
python -m hdrgainmap photo.png -o out --transfer HLG

# 机器可读输出
python -m hdrgainmap photo.png -o out --json --validate
```

| 参数 | 说明 |
|---|---|
| `-o, --outdir` | 输出目录，默认 `out` |
| `-f, --formats` | 逗号分隔：`jpeg` `png` `heic10` `heic12` `avif`，默认全部 |
| `-q, --quality` | 有损编码质量 50–100，默认 100（100 = 视觉无损） |
| `--dpi` | 输出 DPI，默认沿用源图 |
| `--transfer` | 传递函数 `PQ`(默认) / `HLG` |
| `--validate` | 生成后逐个产物跑合规校验，失败时退出码 1 |
| `--debug` | 额外落盘 `_gainmap.png` |
| `--json` | JSON 输出（路径 + 统计 + 校验报告） |

### 代码调用

```python
from hdrgainmap import build, validate

res = build("photo.png", "out", formats=("png", "heic12"), quality=100)
print(res["files"]["heic12"])
print(res["stats"]["peak_nits"], res["stats"]["untouched_pct"])
print(validate.validate_file(res["files"]["heic12"]).ok)
```

`build()` 返回：

```python
{
  "src": "...", "dpi": 240.0, "transfer": "PQ", "gmax": 2.16,
  "files": {"png": "...", "heic10": "...", "heic12": "...", "avif": "..."},
  "stats": {"gain_mean": 1.00, "gain_max": 4.41, "untouched_pct": 98.0,
            "gt2x_pct": 0.1, "peak_nits": 896.0, "avif_bits": 12},
}
```

单模块也能直接用：

```python
from hdrgainmap import gainmap, color, png16, heic10, avif

cfg  = gainmap.adapt_params(rgb)                    # 按图算阈值
y, emis = gainmap.emissive(rgb, cfg)
gain = gainmap.gain_of(gainmap.finalize(emis), cfg["gmax"])
q16  = color.pq_oetf(hdr_linear * 203.0) * 65535    # PQ 码值
png16.encode(q16, dpi=240)
heic10.encode(q16, transfer="PQ", bits=12, quality=100, dpi=240)
avif.encode(q16, transfer="PQ", bits=12, quality=100, dpi=240)   # → (blob, bits)
```

## 颜色

- 源像素按 **sRGB** 解释（插画常见），线性化后乘增益，再转 BT.2020 编码成 PQ/HLG。
- PNG 写 `cICP = (9, 16, 0, 1)`（BT.2020 / PQ / RGB 矩阵 / 全范围）；`matrix=0` 对 RGB 是正确的。
- HEIC 写 `nclx = (9, 16, 9, 1)`；**matrix 必须是 9**（BT.2020 NCL）。不写会落成 6（BT.601），
  部分解码器按 601 反矩阵，颜色直接偏——这是 v1.3 的 bug，v2.0 已修。
- AVIF 同样写 `nclx = (9, 16, 9)`：ffmpeg 的 avif muxer 不写色彩标签（一律 2/2），编码后改写 7 字节。
- HLG 是相对信号：1.0 对应 1000 nits 峰值，SDR 白(203 nits)落在约 0.75，所以编码前按峰值归一。

## 编码细节

- **质量默认 100**：HEIC/AVIF 的 `quality` 映射为 AV1 的 `crf = (100-q)*0.63`（100 → crf 0），
  HEVC 走 libx265 的 qp；PNG 无损。体积换画质，需要小文件自己降。
- **AVIF 固定 4:4:4**：插画色边锐利，4:2:0 会把彩色高光糊掉；代价是体积约为 4:2:0 的三倍。
- **AVIF 12bit profile 2**：`high_bitdepth + twelve_bit`，libaom 支持；`libsvtav1` 只到 10bit 4:2:0，
  所以 12bit 失败时的降级顺序是 `12bit 4:4:4 → 12bit 4:2:0 → 10bit 4:4:4 → 10bit 4:2:0(svt)`。
- **DPI**：PNG 写 `pHYs`；HEIC 借 EXIF 282/283/296；AVIF 的 ffmpeg muxer 不写 Exif，本工具在
  `meta` 里补一个 Exif item（`iloc`/`iinf`/`iref` + `mdat` 尾部 TIFF，`infe` 必须 version 2），
  Explorer 面板才会显示 240 dpi 而不是 72。
- **上亿像素**：PNG16 会到 200 MB 量级；`Image.MAX_IMAGE_PIXELS = None` 已放开 Pillow 的解压炸弹保护。

## 已知限制

- **HEIC 12bit 解码端窄**：Windows 自带 HEVC 扩展只有 Main10，Main12 报 `0xC00D3E8C`；需要第三方解码器。
- **12bit AVIF**：Chromium 系（Chrome/Edge）与 Firefox 软件解码可显示；Windows 照片 App 需 AV1 扩展。
- **PQ 直传在 SDR 环境下必然发灰**：SDR 白(203 nits)只占码值 0.58，没有 HDR 渲染链路时它就是灰的。
- **HEIF 无 DPI 字段**：只能借 EXIF，WIC 认、第三方 App 未必认。
- **源图 ICC 被忽略**：整条管线按 sRGB 解释源像素；Display-P3 源需先转 sRGB，否则颜色会偏。
- **Ultra HDR 默认不写 ISO 21496-1 元数据**：安卓 16 的 HDR 解码路径会优先读 ISO，而它把本工具
  写的 61 字节解释错，导致**白色区域变黑**；只留 XMP `hdrgm` 时同一文件显示正常。需要 ISO 元数据
  的场合可传 `write_iso=True`（生成后务必在目标机验证）。

## 校验

```bash
python -m hdrgainmap.validate out/x_hdr16_pq.png out/x_hdr10_pq.heic out/x_hdr12_pq.heic out/x_hdr12_pq.avif
```

- PNG：IHDR 16bit RGB、`cICP` 9/16/0/1、pHYs、zlib 长度与滤波字节范围、能否解码。
- HEIC：位深 10/12、`nclx` primaries=9、transfer 16/18、**matrix=9**、码值是否触顶。
- AVIF：major brand、`nclx`（primaries=9、transfer 16/18、matrix=9）、`pixi` 位深 10/12、能否解码。
- Ultra HDR：MPF 两条、`hdrgm` 参数、增益图可解码；ISO 21496-1 元数据若存在则逐字段核对。

## 自测

```bash
python tests/test_roundtrip.py
```

合成一张暗底 + 高光点的图，跑通四种格式并校验：`validate` 全过、PNG 两条解码路径逐像素一致、
HEIC 10/12 的位深与 nclx、AVIF 12bit 的 nclx/pixi、自适应阈值确实跟着 p99 走、质量参数影响体积。

## 目录

```
hdr-gainmap/
├─ hdrgainmap/
│  ├─ color.py      色彩空间与传递函数（sRGB / PQ / HLG / BT.2020）
│  ├─ gainmap.py    自适应发光体分离（阈值按亮度分位计算）
│  ├─ png16.py      16bit PQ/HLG PNG 编解码
│  ├─ heic10.py     10/12bit HEIC 编解码（nclx 9/16/9）
│  ├─ avif.py       12bit AVIF 编解码（ffmpeg/libaom，补 nclx 与 Exif DPI）
│  ├─ ultrahdr.py   Ultra HDR 封装/解析（旧版产物，保留供校验）
│  ├─ validate.py   产物合规校验
│  ├─ pipeline.py   一条龙构建
│  └─ cli.py        命令行
├─ tests/test_roundtrip.py
└─ _archive/        历史快照与实验脚本（仅备查）
```
