"""生成同一张阶梯图的多种 ISO 元数据变体，用来定位荣耀相册到底认哪种写法。"""
import io
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hdrgainmap import ultrahdr as U

OUT = Path(r"C:\Users\xiaof\Desktop\安卓HDR自检\变体")
OUT.mkdir(parents=True, exist_ok=True)


def jpg(im, q=96):
    b = io.BytesIO()
    im.save(b, "JPEG", quality=q, subsampling=0, dpi=(240, 240))
    return b.getvalue()


def ladder(w=1600, h=800, text="STEP %d/4"):
    """四段阶梯：SDR 下四格同灰，HDR 下 1x / 1.59x / 2.52x / 4x。"""
    base = Image.new("RGB", (w, h), (96, 96, 96))
    d = ImageDraw.Draw(base)
    for i in range(4):
        y = i * h // 4
        d.line([(0, y), (w, y)], fill=(40, 40, 40), width=4)
        d.text((24, y + 24), (text % (i + 1)) + "   SDR: same gray   HDR: step %d" % (i + 1),
               fill=(235, 235, 235))
    gm = np.zeros((h // 2, w // 2), np.uint8)
    for i, v in enumerate([0, 85, 170, 255]):
        gm[i * (h // 2) // 4:(i + 1) * (h // 2) // 4, :] = v
    return jpg(base), jpg(Image.fromarray(gm, "L"))


def build(primary, gain_jpeg, gmax=2.0, offset=1 / 64, iso="full_secondary", mpf=True):
    tail = primary[2:]
    exif_seg = U._seg(0xE1, U._exif("Emissive GainMap 0.1", "Ultra HDR test"))
    meta = U._iso_metadata(gmax, offset)

    if iso == "none":
        p_iso = b""
    elif iso == "ns":
        p_iso = U._seg(0xE2, U.ISO_MARKER)
    elif iso in ("ver", "full_secondary", "full_both"):
        p_iso = U._seg(0xE2, U.ISO_MARKER + b"\0\0\0\0")
    else:
        raise ValueError(iso)

    if iso == "full_secondary":
        secondary = gain_jpeg[:2] + U._seg(0xE2, U.ISO_MARKER + meta) + gain_jpeg[2:]
    elif iso == "full_both":
        secondary = gain_jpeg[:2] + U._seg(0xE2, U.ISO_MARKER + meta) + gain_jpeg[2:]
    else:
        secondary = gain_jpeg

    gain_len = len(secondary)
    mpf_seg = lambda pl, gl: (U._seg(0xE2, U._mpf([(0x03000000, pl, 0), (0x02000000, gl, pl)]))
                              if mpf else b"")

    head = (exif_seg + mpf_seg(0, gain_len)
            + U._seg(0xE1, U.XMP_HEADER + U._xmp(0, gain_len, gmax, offset)) + p_iso)
    primary_len = 2 + len(head) + len(tail)
    for _ in range(8):
        trial = (exif_seg + mpf_seg(primary_len, gain_len)
                 + U._seg(0xE1, U.XMP_HEADER + U._xmp(primary_len, gain_len, gmax, offset)) + p_iso)
        if len(trial) == len(head):
            head = trial
            break
        head = trial
        primary_len = 2 + len(head) + len(tail)
    return b"\xff\xd8" + head + tail + secondary


def main():
    p, g = ladder()
    cases = {
        "A_xmp_mpf_only": dict(iso="none"),
        "B_ns_only": dict(iso="ns"),
        "C_ns_ver": dict(iso="ver"),
        "D_full_secondary": dict(iso="full_secondary"),
        "E_full_both": dict(iso="full_both"),
        "F_xmp_only": dict(iso="none", mpf=False),
    }
    for name, kw in cases.items():
        blob = build(p, g, **kw)
        (OUT / f"{name}.jpg").write_bytes(blob)
        info = U.parse(blob)
        ok = info["primary_len"] + info["gain_len"] == len(blob)
        print("%-18s %7d B  primary=%d gain=%d 匹配=%s" % (name, len(blob), info["primary_len"], info["gain_len"], ok))


if __name__ == "__main__":
    main()
