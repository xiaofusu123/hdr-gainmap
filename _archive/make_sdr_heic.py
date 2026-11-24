import sys
from pathlib import Path
sys.path.insert(0, r"D:\Program\Code\hdr-gainmap")
from hdrgainmap import pipeline, heic10

OUT = Path(r"C:\Users\xiaof\Desktop\安卓HDR自检\其他格式")
JOBS = [
    (r"C:\Users\xiaof\Desktop\1687539284005.jpg", "detail", "白猫"),
    (r"C:\Users\xiaof\Desktop\1688558881488.jpg", "detail", "高调2"),
    (r"C:\Users\xiaof\Desktop\1794820829628.png", "strong", "鸣潮"),
    (r"C:\Users\xiaof\Desktop\1777789210281.png", "strong", "泡泡"),
    (r"C:\Users\xiaof\Desktop\搜图神器_1762859644463.jpg", "strong", "搜图神器"),
]
for src, prof, name in JOBS:
    r = pipeline.build(src, OUT/name, profile=prof, formats=("heic_sdr",), preview=False)
    p = Path(r["files"]["heic_sdr"])
    arr, info = heic10.decode(p.read_bytes())
    print("%-6s %-8s %9d B  bit_depth=%s  nclx=%s  size=%dx%d" % (
        name, p.name, p.stat().st_size, info["bit_depth"], info["nclx"], arr.shape[1], arr.shape[0]))
