import sys, time
from pathlib import Path
sys.path.insert(0, r"D:\Program\Code\hdr-gainmap")
from hdrgainmap import pipeline

OUT = r"C:\Users\xiaof\Desktop\安卓HDR自检\其他格式"
JOBS = [
    (r"C:\Users\xiaof\Desktop\1794820829628.png",        "strong", "鸣潮"),
    (r"C:\Users\xiaof\Desktop\1777789210281.png",        "strong", "泡泡"),
    (r"C:\Users\xiaof\Desktop\搜图神器_1762859644463.jpg", "strong", "搜图神器"),
    (r"C:\Users\xiaof\Desktop\1687539284005.jpg",        "detail", "白猫"),
    (r"C:\Users\xiaof\Desktop\1688558881488.jpg",        "detail", "高调2"),
]

for src, prof, name in JOBS:
    t = time.time()
    d = Path(OUT) / name
    r = pipeline.build(src, d, profile=prof, formats=("png", "heic"), preview=False)
    s = r["stats"]
    print("%-6s %-7s 增益 %.2f× 峰值 %4d nits  用时 %4.1fs" % (name, prof, s["gain_mean"], s["peak_nits"], time.time()-t))
    for k, v in r["files"].items():
        p = Path(v)
        if p.suffix in (".png", ".heic"):
            print("        %-28s %10d B" % (p.name, p.stat().st_size))
