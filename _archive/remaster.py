"""高调图专用的"高光余量"路线：先把高光压出余量存进 SDR 基图，再用增益图补回。

和之前"只抬发光点"不同，这里改的是基图本身：
  T = lin * 2^(gmax*w)     ← 目标 HDR 亮度
  B = shoulder(T)          ← 压回 SDR（0.8 以下不动，以上平滑压到 1.0）
  gain = T / B             ← 解码器按这个还原，结果就是 T
SDR 看＝基图（高光更柔和、细节更多），HDR 看＝高光真正跃出来。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, r"D:\Program\Code\hdr-gainmap")
from hdrgainmap import ultrahdr as U
from hdrgainmap.color import srgb_to_linear, linear_to_srgb
from hdrgainmap.gainmap import _box, _smoothstep as smoothstep


def luma(lin):
    return 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]


def remaster(rgb, gmax=2.0, b0=0.55, b1=0.95, halo=0.35, hr=12, knee=0.80, soft=0.9):
    lin = srgb_to_linear(rgb)
    Y = luma(lin)
    w = smoothstep(b0, b1, Y)
    if halo > 0:
        w = halo * _box(w, hr) + (1 - halo) * w
    w = np.clip(w, 0, 1)

    T = lin * np.exp2(gmax * w)[..., None]
    top = 2.0 ** gmax
    hi = np.clip(T - knee, 0, None)
    shoulder = 1 - np.exp(-hi / soft)
    norm = 1 - np.exp(-(top - knee) / soft)
    B = np.where(T <= knee, T, knee + (1 - knee) * shoulder / norm)
    B = np.clip(B, 0, 1)

    gain = np.clip(T / np.maximum(B, 1e-6), 1.0, top)
    # 增益图必须是单通道：取亮度比值，避免解码端按通道解释
    gy = luma(gain)
    emis = np.clip(np.log2(np.maximum(gy, 1.0)) / gmax, 0, 1)
    return (linear_to_srgb(B) * 255).astype(np.uint8), (emis * 255).astype(np.uint8), emis, gy


def jpg_bytes(arr, q=96, dpi=(240, 240)):
    b = io.BytesIO()
    Image.fromarray(arr).save(b, "JPEG", quality=q, subsampling=0, dpi=dpi)
    return b.getvalue()


def tonemap_sdr(lin, W=2.0):
    c = lin * (1 + lin / (W * W)) / (1 + lin)
    return (np.clip(c, 0, 1) * 255).astype(np.uint8)


def main():
    src = r"C:\Users\xiaof\Desktop\1687539284005.jpg"
    out = Path(r"C:\Users\xiaof\Desktop\安卓HDR自检\新图")
    out.mkdir(parents=True, exist_ok=True)

    rgb = np.asarray(Image.open(src).convert("RGB"), dtype=np.float32) / 255.0
    var = {"1_温和": dict(gmax=2.0, b0=0.55, b1=0.95),
           "2_明显": dict(gmax=2.6, b0=0.45, b1=0.90)}

    tiles = [("原图 SDR", np.asarray(Image.open(src).convert("RGB")))]
    for name, cfg in var.items():
        base, gm, emis, gain = remaster(rgb, **cfg)
        blob = U.build(jpg_bytes(base), jpg_bytes(gm, q=94), gmax=cfg["gmax"])
        p = out / ("%s.jpg" % name)
        p.write_bytes(blob)
        info = U.parse(blob)
        e0, e1 = info["entries"]
        ok = (info["primary_len"] + info["gain_len"] == len(blob) and e1["abs"] == info["primary_len"]
              and e1["size"] == info["gain_len"] and e0["attr"] & 0x0FFFFFFF == 0x030000 and e0["offset"] == 0)
        print("%-6s %8d B  合规=%s  增益 均值 %.2fx 最大 %.2fx  不动 %.1f%%" % (
            name, len(blob), ok, gain.mean(), gain.max(), 100 * (gain < 1.02).mean()))
        # 预览：用基图 + 增益重建 HDR，再压回 SDR 看效果
        tiles.append((name + "  SDR 基图", base))
        gc = np.clip(emis / max(float(emis.max()), 1e-6), 0, 1)
        rgb_g = np.stack([gc, gc * 0.55, gc * 0.15], -1)
        tiles.append((name + "  增益", (rgb_g * 255).astype(np.uint8)))

    w = 760
    h = round(tiles[0][1].shape[0] * w / tiles[0][1].shape[1])
    canvas = Image.new("RGB", (w * len(tiles), h + 26), (16, 16, 18))
    d = ImageDraw.Draw(canvas)
    for i, (lab, arr) in enumerate(tiles):
        canvas.paste(Image.fromarray(arr).resize((w, h), Image.LANCZOS), (i * w, 26))
        d.text((i * w + 8, 8), lab, fill=(235, 235, 235))
    canvas.save(out / "preview.png")
    print("preview", canvas.size)


if __name__ == "__main__":
    main()
