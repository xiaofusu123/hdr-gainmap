# hdr-gainmap

把 8bit SDR 插画转成 HDR，四种产物：**Ultra HDR JPEG**（安卓原生）、**16bit PQ PNG**（无损母版）、
**10bit PQ HEIC**（苹果/桌面）、**8bit SDR HEIC**（只换容器）。

## 思路：发光体分离，不是 AI 反色调映射

照片训练的 ITM 模型在二次元平涂上会翻车——平涂糊出块状增益、线稿整条发光、纯白死白、色带。
而且 SDR 插画的高光**本来就已经削顶**，没有暗藏细节可"恢复"。

所以这里只判断"这块像素是不是在发光"，把光柱、星芒、水面反光、眼神光抬到 SDR 白以上：

```
bright = smoothstep(b0, b1, Y)                     # 绝对亮度够高
peak   = Y / (gauss(Y, pr) + 1e-4)                 # 比邻域亮多少
emis   = bright * (flat + (1 - flat) * smoothstep(p0, p1, peak))
emis   = halo * gauss(emis, hr) + (1 - halo) * emis   # 向外 bloom 一圈
gain   = 2 ** (gmax * emis)                        # 线性倍数，gmax = 最大档数
```

高调素材换成按**局部结构**选点（`detail` 档）：

```
detail = |Y - gauss(Y, dr)| / (gauss(Y, dr) + 0.02)   # 相对邻域的起伏
emis   = smoothstep(b0, b1, Y) * smoothstep(d0, d1, detail)
```

平坦的白布和皮肤不动，只有蕾丝褶子、发丝、窗框这些细节密集处才亮。

## 安装

```bash
pip install -r requirements.txt
```

依赖：`numpy`、`pillow`、`pillow-heif`。Python 3.10+。

## 用法

### 命令行

```bash
# 默认：strong 档，出 Ultra HDR JPEG + 16bit PNG + 10bit HEIC
python -m hdrgainmap photo.png -o out

# 只要安卓能认的 Ultra HDR（jpg 与 jpeg 等价，产物后缀是 .jpeg）
python -m hdrgainmap photo.png -o out -f jpeg

# 高调素材：细节掩膜档，且 HEIC 只换容器
python -m hdrgainmap white.png -o out -p detail -f jpg,heic_sdr

# 提高峰值（单位 EV，2.0 = 4×），并压掉预览
python -m hdrgainmap photo.png -o out --gmax 2.8 --no-preview

# 机器可读
python -m hdrgainmap photo.png -o out --json
```

| 参数 | 说明 |
|---|---|
| `-o, --outdir` | 输出目录，默认 `out` |
| `-p, --profile` | `strong`(默认) / `detail` / `lights` / `soft` / `night` |
| `-f, --formats` | 逗号分隔：`jpg`(`jpeg`) `png` `heic` `heic_sdr`，默认 `jpg,png,heic` |
| `--dpi` | 输出 DPI，默认沿用源图 |
| `--gmax` | 最大增益档（EV），覆盖档位预设 |
| `--transfer` | PNG/HEIC 的传递函数，`PQ`(默认) / `HLG` |
| `--no-preview` | 不生成 SDR 模拟预览 |
| `--debug` | 额外落盘 `_base.jpg`、`_gainmap.png` |
| `--json` | JSON 输出结果（路径 + 统计） |

### 代码调用

```python
from hdrgainmap import build

res = build("photo.png", "out", profile="strong", formats=("jpg", "heic_sdr"))
print(res["files"]["jpg"])
print(res["stats"]["peak_nits"])        # 峰值亮度，单位 nits
```

`build()` 返回：

```python
{
  "src": "...", "profile": "strong", "dpi": 240.0, "gmax": 2.6,
  "files": {"jpg": "...", "png": "...", "heic": "...", "heic_sdr": "...", "preview": "..."},
  "stats": {"gain_mean": 1.07, "gain_max": 6.06, "untouched_pct": 70.9,
            "gt2x_pct": 24.6, "peak_nits": 1213.0,
            "changed_gt2_pct": 24.6, "changed_gt16_pct": 5.1},
}
```

单模块也可以直接用：

```python
from hdrgainmap import ultrahdr, png16, heic10

blob = ultrahdr.build(primary_jpeg, gainmap_jpeg, gmax=2.6)   # 封装
info = ultrahdr.parse(blob)                                   # 校验主图/增益图边界
png16.encode(q16, dpi=240)                                    # 16bit PQ PNG
heic10.encode(q16, transfer="PQ", bits=10, dpi=240)           # 10bit HEIC
heic10.encode_sdr(pil_image, dpi=240)                         # 只换容器
```

## 档位

| 档位 | 适用 | 峰值 | 典型不动像素 |
|---|---|---|---|
| `strong`（默认） | 深色背景 + 明确光源 | ~1200 nits | 70% |
| `detail` | 高调（整体亮度 >0.8）、白底、逆光 | ~600 nits | 35% |
| `lights` | 只点光源，最克制 | ~800 nits | 96% |
| `soft` | 整体温柔提亮 | ~800 nits | 65% |
| `night` | 暗部多、要更强的夜感 | ~800 nits | 57% |

`soft` / `night` 会把角色和环境一起抬起来，容易发灰，谨慎使用。

**高调素材不要硬上强档**：整幅都在 0.8 以上时，抬任何一片都是"变白"，不是"变亮"，
`detail` 是这类图的可选上限。

## 输出文件

| 文件 | 内容 |
|---|---|
| `*_ultrahdr.jpeg` | 主图字节原样 + 半分辨率灰度增益图 + MPF + XMP(hdrgm 1.0) + ISO 21496-1 |
| `*_hdr16.png` | 16bit RGB + `cICP(9,16,0,1)` 的 PQ 图，无损母版 |
| `*_hdr10_pq.heic` | HEVC Main10、BT.2020 + PQ、4:2:0，带 EXIF DPI |
| `*_sdr8.heic` | 8bit sRGB，只换容器，不编码 HDR |
| `*_hdrsim.jpg` | 把 HDR 压回 SDR 的模拟图，普通屏上看效果 |
| `*_base.jpg`、`*_gainmap.png` | 仅 `--debug` 时产生，供排查 |

Ultra HDR 的主图**不做任何重编码**，SDR 观感与源图逐字节一致；
PNG / HEIC 从源图重建，不经过 q95 JPEG 中转，是独立母版。

## 容器要点（踩过的坑）

- **MPF 是硬约束**。解码器先看 MPF，不符就直接回退 SDR，XMP 写得再对也没用：
  - 主图条目 `attr=0x00030000`、`offset=0`；
  - 增益图条目 `attr=0x00000000`、`size=增益图总长`、`offset` **相对 MP 头**（不是文件绝对偏移）；
  - 每个条目 16 字节，字段序 `attr|size|offset|dep1|dep2`，小端。
- **ISO 21496-1 元数据不能只写命名空间**：主图段写 `命名空间 + u16 min_ver + u16 writer_ver`，
  增益图段写 61 字节完整元数据（headroom / min / max / gamma / offset），Android 15+ 优先读它。
- **PNG 自适应滤波的左邻偏移必须等于 `bpp`**（16bit RGB = 6），写成 1 会得到彩虹竖条，
  而且用同样的错误偏移自检还会"通过"。

## 已知限制

- **JPEG 没有 10bit**：通道固定 8bit，只有 8bit(baseline) 与 12bit(extended) 两档，后者没人支持。
- **PNG 没有 10bit**：规范只有 1/2/4/8/16，10bit 需求用 16bit 承载。
- **HEIC 12bit 解不了**：Windows HEVC 扩展只有 Main10，Main12 报 `0xC00D3E8C`。
- **Windows 照片 App 不认 Ultra HDR**：要看效果用 Chrome / Edge，或安卓 14+ 相册。
- **PQ 直传在 SDR 环境下必然发灰**：SDR 白(203 nits)只占码值 0.58，没有 HDR 渲染链路时它就是灰的。
- **峰值低于 ~650 nits 的素材别走 PQ**：整幅贴着白点，播放器一映射就没层次，改用 `heic_sdr`。
- **HEIF 无 DPI 字段**：只能借 EXIF 的 282/283/296，WIC 认、第三方 App 未必认。
- **上亿像素的 4× 放大图**：Pillow 默认会报 `DecompressionBombWarning`，包内已放开
  （`Image.MAX_IMAGE_PIXELS = None`）；但一亿像素的 16bit PNG 会到 200 MB 量级，注意磁盘。

## 自测

```bash
python tests/test_roundtrip.py
```

合成一张暗底 + 高光点的图，跑通三种格式，校验 MPEntry 顺序与偏移、PNG 往返、
HEIC 位深与 nclx。

## 目录

```
hdr-gainmap/
├─ hdrgainmap/
│  ├─ color.py      色彩空间与传递函数（sRGB / PQ / HLG / BT.2020）
│  ├─ gainmap.py    发光体分离、细节掩膜、档位预设
│  ├─ ultrahdr.py   Ultra HDR 封装与解析（MPF / XMP / ISO 21496-1）
│  ├─ png16.py      16bit PQ PNG 编解码
│  ├─ heic10.py     10bit HEIC 编解码 + SDR 换容器
│  ├─ pipeline.py   一条龙构建
│  └─ cli.py        命令行
├─ tests/test_roundtrip.py
└─ _archive/        历史实验脚本（对比版、A–F 容器变体、批量出图等，仅备查）
```

`out/` 是默认输出目录，已被 `.gitignore` 忽略。
