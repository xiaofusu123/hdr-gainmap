import sys, io
from pathlib import Path
import numpy as np
from PIL import Image
sys.path.insert(0, r"D:\Program\Code\hdr-gainmap")
from hdrgainmap import pipeline, heic10, color, gainmap

OUT = Path(r"C:\Users\xiaof\Desktop\安卓HDR自检\其他格式")
JOBS = [(r"C:\Users\xiaof\Desktop\1687539284005.jpg", "白猫"), (r"C:\Users\xiaof\Desktop\1688558881488.jpg", "高调2")]

for src, name in JOBS:
    d = OUT / name
    # A: 降低峰值，PQ 映射后白场不会越界
    r = pipeline.build(src, d / "低峰值", profile="detail", formats=("heic",), gmax=1.0, preview=False)
    f = Path(r["files"]["heic"])
    print("%-5s 低峰值PQ  %8d B  峰值 %3d nits" % (name, f.stat().st_size, r["stats"]["peak_nits"]))
    # B: 纯 SDR 8bit HEIC（不做 HDR，只是换容器）
    im = Image.open(src).convert("RGB")
    buf = io.BytesIO()
    import pillow_heif
    pillow_heif.register_heif_opener()
    im.save(buf, "HEIF", quality=92, dpi=(im.info.get("dpi", (96, 96))[0],)*2)
    p = d / f"{Path(src).stem}_sdr8.heic"
    p.write_bytes(buf.getvalue())
    print("%-5s SDR8bit HEIC %8d B" % (name, p.stat().st_size))
