"""高调素材专用：只在"细节密集的亮部"加增益。

白猫那张的问题是整件白衣服都是平的亮面 —— 按亮度加权必然整片发白。
这里再乘一个"局部对比"掩膜：只有蕾丝、褶子、发丝、窗框这些有结构的地方才亮，
平坦的白布和皮肤保持原样。
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

SRC = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\xiaof\Desktop\1687539284005.jpg"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(r"C:\Users\xiaof\Desktop\安卓HDR自检\高调图\细节档")


def detail_emissive(rgb, cfg):
    lin = srgb_to_linear(rgb)
    Y = 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]
    base = GM._gauss(Y, cfg["dr"])
    detail = np.abs(Y - base) / (base + 0.02)          # 相对局部对比
    dw = GM._smoothstep(cfg["d0"], cfg["d1"], detail)   # 只保留有结构的像素
    bw = GM._smoothstep(cfg["b0"], cfg["b1"], Y)        # 只保留亮的像素
    emis = bw * dw
    emis = cfg["halo"] * GM._gauss(emis, cfg["hr"]) + (1 - cfg["halo"]) * emis
    return Y, np.clip(emis, 0, 1).astype(np.float32)


VAR = {
    "1_只褶子": dict(b0=0.30, b1=0.85, dr=6, d0=0.035, d1=0.16, halo=0.35, hr=8, gmax=2.6),
    "2_褶子+发丝": dict(b0=0.25, b1=0.80, dr=6, d0=0.020, d1=0.11, halo=0.50, hr=11, gmax=2.6),
    "3_更宽": dict(b0=0.20, b1=0.75, dr=8, d0=0.012, d1=0.08, halo=0.55, hr=13, gmax=2.6),
    "4_轻褶子": dict(b0=0.35, b1=0.90, dr=6, d0=0.045, d1=0.20, halo=0.30, hr=7, gmax=1.6),
    "5_轻+":   dict(b0=0.32, b1=0.88, dr=6, d0=0.038, d1=0.17, halo=0.34, hr=8, gmax=1.9),
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rgb, _ = GM.load_rgb(SRC)
    src_bytes = Path(SRC).read_bytes()

    tiles = [("原图 SDR", (rgb * 255).round().astype(np.uint8))]
    for name, cfg in VAR.items():
        Y, emis = detail_emissive(rgb, cfg)
        emis = GM.finalize(emis)
        gain = GM.gain_of(emis, cfg["gmax"])

        b = io.BytesIO()
        Image.fromarray((np.clip(emis, 0, 1) * 255).round().astype(np.uint8)).save(
            b, "JPEG", quality=94, subsampling=0)
        blob = U.build(src_bytes, b.getvalue(), gmax=cfg["gmax"])
        (OUT / ("%s.jpeg" % name)).write_bytes(blob)

        info = U.parse(blob)
        e0, e1 = info["entries"]
        ok = (info["primary_len"] + info["gain_len"] == len(blob)
              and e1["abs"] == info["primary_len"] and e1["size"] == info["gain_len"]
              and e0["attr"] & 0x0FFFFFFF == 0x030000 and e0["offset"] == 0)
        print("%-10s %8d B  合规=%s  mean %.2f×  max %.2f×  不动 %.1f%%  %d nits" % (
            name, len(blob), ok, gain.mean(), gain.max(),
            100 * (gain < 1.02).mean(), round(203 * gain.max())))

        hdr = srgb_to_linear(rgb) * gain[..., None]
        disp = np.where(hdr <= 1.0, hdr, 1.0 + 0.25 * (1 - np.exp(-(hdr - 1.0))))
        tiles.append((name + "  模拟HDR", (linear_to_srgb(np.clip(disp, 0, 1)) * 255).round().astype(np.uint8)))

    w, h, cols = 760, round(rgb.shape[0] * 760 / rgb.shape[1]), 2
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
