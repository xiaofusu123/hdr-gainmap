import io, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
sys.path.insert(0, r"D:\Program\Code\hdr-gainmap")
from hdrgainmap import gainmap as GM, ultrahdr as U
from hdrgainmap.color import srgb_to_linear, linear_to_srgb

SRC = r"C:\Users\xiaof\Desktop\1688558881488.jpg"
OUT = Path(r"C:\Users\xiaof\Desktop\安卓HDR自检\高调图2")
GMAX, EV_DOWN = 2.6, -0.5          # 基图整体压暗 0.5EV，换来 HDR 里的对比

rgb, _ = GM.load_rgb(SRC)
lin = srgb_to_linear(rgb)
Y = 0.2126*lin[...,0] + 0.7152*lin[...,1] + 0.0722*lin[...,2]
Ys = 0.2126*rgb[...,0] + 0.7152*rgb[...,1] + 0.0722*rgb[...,2]   # 用 sRGB 亮度定权重，避免线性域里整幅都算高光
w = GM._smoothstep(0.93, 1.00, Ys) * GM._smoothstep(0.030, 0.14, np.abs(Y - GM._gauss(Y, 6)) / (GM._gauss(Y, 6) + 0.02))
T = lin * np.exp2(GMAX * w)[..., None]                      # 目标 HDR
B = lin * (2.0 ** EV_DOWN)                                  # 基图压暗
B = B * (1 - 0.18 * GM._smoothstep(0.55, 1.0, B)) # 高光收一点，别顶死
B = np.clip(B, 0, 0.985)
gain = np.clip(T / np.maximum(B, 1e-6), 1.0, 2.0**GMAX)
emis = np.clip(np.log2(np.maximum(0.2126*gain[...,0]+0.7152*gain[...,1]+0.0722*gain[...,2], 1.0))/GMAX, 0, 1)
emis = GM._gauss(emis, 2)
base8 = (linear_to_srgb(B)*255).round().astype(np.uint8)

b = io.BytesIO(); Image.fromarray((emis*255).round().astype(np.uint8)).save(b, "JPEG", quality=94, subsampling=0)
bb = io.BytesIO(); Image.fromarray(base8).save(bb, "JPEG", quality=95, subsampling=0, dpi=(240,240))
blob = U.build(bb.getvalue(), b.getvalue(), gmax=GMAX)
(OUT/"对比版.jpeg").write_bytes(blob)
info = U.parse(blob); e0,e1 = info["entries"]
print("合规", info["primary_len"]+info["gain_len"]==len(blob) and e1["abs"]==info["primary_len"] and e0["attr"]&0x0FFFFFFF==0x030000, len(blob), "B")
print("gain mean %.2fx max %.2fx untouched %.1f%%" % (gain.mean(), gain.max(), 100*(gain<1.02).mean()))

hdr = srgb_to_linear(base8.astype(np.float32)/255.0) * gain
disp = np.where(hdr <= 1.0, hdr, 1.0 + 0.25*(1-np.exp(-(hdr-1.0))))
tiles = [("原图 SDR", (rgb*255).round().astype(np.uint8)),
         ("对比版 SDR基图(压暗0.5EV)", base8),
         ("对比版 模拟HDR", (linear_to_srgb(np.clip(disp,0,1))*255).round().astype(np.uint8))]
w0 = 700; h0 = round(rgb.shape[0]*w0/rgb.shape[1])
canvas = Image.new("RGB", (w0*3, h0+26), (16,16,18)); d = ImageDraw.Draw(canvas)
for i,(lab,arr) in enumerate(tiles):
    canvas.paste(Image.fromarray(arr).resize((w0,h0), Image.LANCZOS), (i*w0, 26))
    d.text((i*w0+8, 8), lab, fill=(235,235,235))
canvas.save(OUT/"对比版preview.png"); print("preview", canvas.size)
