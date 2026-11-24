"""第三张测试图：基图保持原样（SDR 看跟原图完全一致），只加增益。

之前 remaster 版把高光压进基图，手机上 HDR ≈ 原图 → 看起来"没效果"。
这次反过来：基图不动，增益只往上加，HDR 一定比 SDR 更亮。
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

SRC = r"C:\Users\xiaof\Desktop\搜图神器_1762859644463.jpg"
OUT = Path(r"C:\Users\xiaof\Desktop\安卓HDR自检\第三张")

# 基图不动，只调增益强度
VAR = {
    "1_标准": dict(b0=0.45, b1=0.90, pr=25, p0=1.60, p1=3.20, flat=0.00, halo=0.40, hr=9, gmax=2.0),
    "2_强":   dict(b0=0.30, b1=0.80, pr=25, p0=1.45, p1=2.90, flat=0.15, halo=0.50, hr=11, gmax=2.6),
    "3_极强": dict(b0=0.20, b1=0.70, pr=25, p0=1.30, p1=2.60, flat=0.30, halo=0.60, hr=13, gmax=3.0),
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    g = GM.load_rgb(SRC)
    rgb = g[0] if isinstance(g, tuple) else g      # load_rgb 已归一化到 0..1，不要再除 255
    src_bytes = Path(SRC).read_bytes()
    dpi = Image.open(SRC).info.get("dpi") or (72, 72)

    tiles = [("原图 SDR", (rgb * 255).astype(np.uint8))]
    for name, cfg in VAR.items():
        Y, emis = GM.emissive(rgb, GM.fit_width(cfg, rgb.shape[1]))
        emis = GM.finalize(emis)
        gain = GM.gain_of(emis, cfg["gmax"])

        gm8 = (np.clip(emis, 0, 1) * 255).round().astype(np.uint8)
        b = io.BytesIO()
        Image.fromarray(gm8).save(b, "JPEG", quality=94, subsampling=0)

        blob = U.build(src_bytes, b.getvalue(), gmax=cfg["gmax"])
        p = OUT / ("%s.jpeg" % name)
        p.write_bytes(blob)
        info = U.parse(blob)
        e0, e1 = info["entries"]
        ok = (info["primary_len"] + info["gain_len"] == len(blob)
              and e1["abs"] == info["primary_len"] and e1["size"] == info["gain_len"]
              and e0["attr"] & 0x0FFFFFFF == 0x030000 and e0["offset"] == 0)
        print("%-6s %9d B  合规=%s  gain 均值 %.2fx 最高 %.2fx 不动 %.1f%% 峰值 %d nits" % (
            name, len(blob), ok, gain.mean(), gain.max(),
            100 * (gain < 1.02).mean(), round(203 * gain.max())))

        # 模拟 HDR 观感：亮部抬高后压回 0..1，中低不动
        hdr = srgb_to_linear(rgb) * gain[..., None]
        disp = np.where(hdr <= 1.0, hdr, 1.0 - 0.28 * np.exp(-(hdr - 1.0) * 1.6))
        tiles.append((name + "  模拟HDR", (linear_to_srgb(np.clip(disp, 0, 1)) * 255).astype(np.uint8)))

    w, h = 900, round(rgb.shape[0] * 900 / rgb.shape[1])
    cols = 2
    rows = (len(tiles) + 1) // 2
    canvas = Image.new("RGB", (w * cols, (h + 26) * rows), (16, 16, 18))
    d = ImageDraw.Draw(canvas)
    for i, (lab, arr) in enumerate(tiles):
        x, y = (i % cols) * w, (i // cols) * (h + 26)
        canvas.paste(Image.fromarray(arr).resize((w, h), Image.LANCZOS), (x, y + 26))
        d.text((x + 8, y + 8), lab, fill=(235, 235, 235))
    canvas.save(OUT / "preview.png")
    print("preview", canvas.size)


if __name__ == "__main__":
    main()
