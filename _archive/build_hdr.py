"""通用出图：基图保持原样（SDR 看等于原图），只叠加增益。

用法：python build_hdr.py <源图> <输出目录> <highkey|dark>
  highkey：整体很亮的图，门槛抬高，只让窗光/纯白发光
  dark   ：整体偏暗的图，门槛放低，光源和逆光发丝都会亮
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, r"D:\Program\Code\hdr-gainmap")
from hdrgainmap import gainmap as GM
from hdrgainmap import ultrahdr as U
from hdrgainmap.color import srgb_to_linear, linear_to_srgb

PRESETS = {
    "dark": {
        "1_标准": dict(b0=0.45, b1=0.90, pr=25, p0=1.60, p1=3.20, flat=0.00, halo=0.40, hr=9, gmax=2.0),
        "2_强":   dict(b0=0.30, b1=0.80, pr=25, p0=1.45, p1=2.90, flat=0.15, halo=0.50, hr=11, gmax=2.6),
        "3_极强": dict(b0=0.20, b1=0.70, pr=25, p0=1.30, p1=2.60, flat=0.30, halo=0.60, hr=13, gmax=3.0),
    },
    "highkey": {
        "1_标准": dict(b0=0.70, b1=0.97, pr=25, p0=1.55, p1=3.10, flat=0.00, halo=0.40, hr=9, gmax=2.0),
        "2_强":   dict(b0=0.58, b1=0.93, pr=25, p0=1.40, p1=2.80, flat=0.10, halo=0.50, hr=11, gmax=2.6),
        "3_极强": dict(b0=0.45, b1=0.88, pr=25, p0=1.25, p1=2.50, flat=0.20, halo=0.60, hr=13, gmax=3.0),
    },
    "highkey_spot": {
        "1_只给窗光":  dict(b0=0.86, b1=1.00, pr=25, p0=1.50, p1=3.00, flat=0.00, halo=0.45, hr=10, gmax=3.0),
        "2_窗光+发丝": dict(b0=0.72, b1=0.98, pr=25, p0=1.40, p1=2.80, flat=0.00, halo=0.55, hr=14, gmax=3.0),
    },
    "strong": {
        "strong": dict(b0=0.30, b1=0.80, pr=25, p0=1.45, p1=2.90, flat=0.15, halo=0.50, hr=11, gmax=2.6),
    },
}


def main(src, outdir, preset):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    rgb, pil = GM.load_rgb(src)
    src_bytes = Path(src).read_bytes()
    if not src_bytes.startswith(b"\xff\xd8"):
        # 主图必须是 JPEG：PNG 源先转成高质量 JPEG 当基图
        b = io.BytesIO()
        pil.save(b, "JPEG", quality=95, subsampling=0, dpi=pil.info.get("dpi") or (72, 72))
        src_bytes = b.getvalue()

    tiles = [("原图 SDR", (rgb * 255).round().astype(np.uint8))]
    for name, cfg in PRESETS[preset].items():
        Y, emis = GM.emissive(rgb, GM.fit_width(cfg, rgb.shape[1]))
        emis = GM.finalize(emis)
        gain = GM.gain_of(emis, cfg["gmax"])

        b = io.BytesIO()
        Image.fromarray((np.clip(emis, 0, 1) * 255).round().astype(np.uint8)).save(
            b, "JPEG", quality=94, subsampling=0)

        blob = U.build(src_bytes, b.getvalue(), gmax=cfg["gmax"])
        (out / ("%s.jpeg" % name)).write_bytes(blob)

        info = U.parse(blob)
        e0, e1 = info["entries"]
        ok = (info["primary_len"] + info["gain_len"] == len(blob)
              and e1["abs"] == info["primary_len"] and e1["size"] == info["gain_len"]
              and e0["attr"] & 0x0FFFFFFF == 0x030000 and e0["offset"] == 0)
        print("%-6s %9d B  合规=%s  gain 均值 %.2fx 最高 %.2fx 不动 %.1f%% 峰值 %d nits" % (
            name, len(blob), ok, gain.mean(), gain.max(),
            100 * (gain < 1.02).mean(), round(203 * gain.max())))

        hdr = srgb_to_linear(rgb) * gain[..., None]
        disp = np.where(hdr <= 1.0, hdr, 1.0 + 0.25 * (1 - np.exp(-(hdr - 1.0))))
        tiles.append((name + "  模拟HDR", (linear_to_srgb(np.clip(disp, 0, 1)) * 255).round().astype(np.uint8)))

    w = 900
    h = round(rgb.shape[0] * w / rgb.shape[1])
    cols, rows = 2, (len(tiles) + 1) // 2
    canvas = Image.new("RGB", (w * cols, (h + 26) * rows), (16, 16, 18))
    d = ImageDraw.Draw(canvas)
    for i, (lab, arr) in enumerate(tiles):
        x, y = (i % cols) * w, (i // cols) * (h + 26)
        canvas.paste(Image.fromarray(arr).resize((w, h), Image.LANCZOS), (x, y + 26))
        d.text((x + 8, y + 8), lab, fill=(235, 235, 235))
    canvas.save(out / "preview.png")
    print("preview", canvas.size)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "dark")
