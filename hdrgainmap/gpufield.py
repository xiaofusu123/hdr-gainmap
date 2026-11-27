"""GPU 光场：可分离均值模糊、导向滤波与逐元素算子。

与 ``gainmap._box`` 同定义（边界复制、f32）。模糊走扫描线前缀和：行内
Hillis–Steele + 段偏移，再按窗口差分；垂直方向先转置，复用同一条路径。
"""
from __future__ import annotations

import contextlib
import threading

import numpy as np

from . import gpu

_WG = 256                        # 扫描线工作块
_LOCK = threading.Lock()
_STATE = None                    # {device, pipes, pool} 或 False
_ENC = []                        # 批量模式下的命令编码器栈
_DEFER = []                      # 批次内暂不回收的缓冲

_COMMON = f"""
const WG: u32 = {_WG}u;

struct Scan {{ w: u32, h: u32, blocks: u32, r: u32 }};
struct Elem {{
    mode: f32, e0: f32, e1: f32, w0: f32, w1: f32,
    w2: f32, w3: f32, w4: f32, lo: f32, hi: f32,
    pad0: f32, pad1: f32,
}};
"""

_SH_SCAN = _COMMON + """
@group(0) @binding(0) var<storage, read> src: array<f32>;
@group(0) @binding(1) var<storage, read_write> pfx: array<f32>;
@group(0) @binding(2) var<storage, read_write> tot: array<f32>;
@group(0) @binding(3) var<storage, read_write> off: array<f32>;
@group(0) @binding(4) var<uniform> U: Scan;
@group(0) @binding(5) var<storage, read_write> dst: array<f32>;

var<workgroup> sh: array<f32, 256>;

@compute @workgroup_size(256)
fn scan(@builtin(workgroup_id) wid: vec3<u32>,
        @builtin(local_invocation_id) lid: vec3<u32>) {
    let seg = wid.x;
    let row = wid.y;
    let t = lid.x;
    let idx = seg * WG + t;
    var v = 0.0;
    if (idx < U.w) {
        v = src[row * U.w + idx];
    }
    sh[t] = v;
    workgroupBarrier();
    var st = 1u;
    loop {
        if (st >= WG) { break; }
        var prev = 0.0;
        if (t >= st) { prev = sh[t - st]; }
        workgroupBarrier();
        sh[t] = sh[t] + prev;
        workgroupBarrier();
        st = st * 2u;
    }
    if (idx < U.w) {
        pfx[row * U.w + idx] = sh[t];
    }
    if (t == WG - 1u) {
        tot[row * U.blocks + seg] = sh[WG - 1u];
    }
}

@compute @workgroup_size(64)
fn offsets(@builtin(global_invocation_id) gid: vec3<u32>) {
    let row = gid.x;
    if (row >= U.h) { return; }
    var acc = 0.0;
    for (var b = 0u; b < U.blocks; b = b + 1u) {
        off[row * U.blocks + b] = acc;
        acc = acc + tot[row * U.blocks + b];
    }
}

@compute @workgroup_size(256)
fn apply(@builtin(workgroup_id) wid: vec3<u32>,
         @builtin(local_invocation_id) lid: vec3<u32>) {
    let seg = wid.x;
    let row = wid.y;
    let i = seg * WG + lid.x;
    if (i >= U.w) { return; }
    let base = row * U.w;
    let w = i32(U.w);
    let lo = i32(i) - i32(U.r);
    let hi = i32(i) + i32(U.r);
    let hc = u32(clamp(hi, 0, w - 1));
    var s = pfx[base + hc] + off[row * U.blocks + hc / WG];
    if (lo > 0) {
        let j = u32(lo - 1);
        s = s - (pfx[base + j] + off[row * U.blocks + j / WG]);
    } else if (lo < 0) {
        s = s + f32(-lo) * src[base];
    }
    if (hi > w - 1) {
        s = s + f32(hi - (w - 1)) * src[base + u32(w - 1)];
    }
    dst[base + i] = s / f32(2u * U.r + 1u);
}
"""

_SH_TRANSPOSE = _COMMON + """
@group(0) @binding(0) var<storage, read> src: array<f32>;
@group(0) @binding(1) var<storage, read_write> dst: array<f32>;
@group(0) @binding(2) var<uniform> U: Scan;

var<workgroup> tile: array<f32, 1024>;

@compute @workgroup_size(32, 32)
fn main(@builtin(workgroup_id) wid: vec3<u32>,
        @builtin(local_invocation_id) lid: vec3<u32>) {
    let bx = wid.x * 32u;
    let by = wid.y * 32u;
    let x = bx + lid.x;
    let y = by + lid.y;
    var v = 0.0;
    if (x < U.w && y < U.h) {
        v = src[y * U.w + x];
    }
    tile[lid.y * 32u + lid.x] = v;
    workgroupBarrier();
    let r = bx + lid.y;
    let c = by + lid.x;
    if (r < U.w && c < U.h) {
        dst[r * U.h + c] = tile[lid.x * 32u + lid.y];
    }
}
"""

_SH_ELEM = _COMMON + """
@group(0) @binding(0) var<storage, read> a: array<f32>;
@group(0) @binding(1) var<storage, read> b: array<f32>;
@group(0) @binding(2) var<storage, read> c: array<f32>;
@group(0) @binding(3) var<storage, read> d: array<f32>;
@group(0) @binding(4) var<storage, read> e: array<f32>;
@group(0) @binding(5) var<storage, read_write> out: array<f32>;
@group(0) @binding(6) var<uniform> U: Elem;

fn sstep(lo: f32, hi: f32, x: f32) -> f32 {
    let t = clamp((x - lo) / max(hi - lo, 1e-6), 0.0, 1.0);
    return t * t * (3.0 - 2.0 * t);
}

@compute @workgroup_size(256)
fn main(@builtin(global_invocation_id) gid: vec3<u32>) {
    let i = gid.x;
    if (i >= arrayLength(&out)) { return; }
    let m = i32(U.mode);
    var v = a[i] * b[i];
    if (m == 0) {
        v = U.w0 * a[i] + U.w1 * b[i] + U.w2 * c[i] + U.w3 * d[i] + U.w4 * e[i];
    } else if (m == 1) {
        v = sstep(U.e0, U.e1, a[i]);
    } else if (m == 2) {
        v = a[i] * (1.0 - U.w0 * b[i]);
    } else if (m == 3) {
        v = 1.0 - exp(-a[i] * U.w0);
    } else if (m == 4) {
        v = a[i] / (b[i] + U.w0);
    } else if (m == 5) {
        v = U.w0 * a[i] * (1.0 - U.w1 * b[i]);
    } else if (m == 7) {
        v = a[i] - b[i] * b[i];
    } else if (m == 8) {
        v = a[i] - b[i] * c[i];
    }
    out[i] = clamp(v, U.lo, U.hi);
}
"""


def _init():
    """三条管线（扫描 / 转置 / 逐元素），与 ``gpu`` 共用同一设备。"""
    global _STATE
    with _LOCK:
        if _STATE is not None:
            if _STATE is False:
                raise gpu.GpuError("GPU 光场不可用")
            return _STATE
        try:
            import wgpu
            device = gpu._init()["device"]
            ro = wgpu.BufferBindingType.read_only_storage
            rw = wgpu.BufferBindingType.storage
            ub = wgpu.BufferBindingType.uniform

            def layout(types):
                return device.create_bind_group_layout(entries=[
                    {"binding": b, "visibility": wgpu.ShaderStage.COMPUTE,
                     "buffer": {"type": t}} for b, t in enumerate(types)])

            lay_scan = layout([ro, rw, rw, rw, ub, rw])
            lay_tr = layout([ro, rw, ub])
            lay_ew = layout([ro, ro, ro, ro, ro, rw, ub])

            def pipe(code, lay, entry):
                mod = device.create_shader_module(code=code)
                pl = device.create_pipeline_layout(bind_group_layouts=[lay])
                return device.create_compute_pipeline(
                    layout=pl, compute={"module": mod, "entry_point": entry})

            _STATE = {"device": device,
                      "pipes": {"scan": pipe(_SH_SCAN, lay_scan, "scan"),
                                "apply": pipe(_SH_SCAN, lay_scan, "apply"),
                                "offsets": pipe(_SH_SCAN, lay_scan, "offsets"),
                                "transpose": pipe(_SH_TRANSPOSE, lay_tr, "main"),
                                "elem": pipe(_SH_ELEM, lay_ew, "main")},
                      "layouts": {"scan": lay_scan, "tr": lay_tr, "ew": lay_ew},
                      "pool": {}, "wgpu": wgpu}
        except Exception as exc:                   # noqa: BLE001
            _STATE = False
            raise gpu.GpuError(f"{type(exc).__name__}: {exc}") from exc
        return _STATE


def available() -> bool:
    if not gpu.available():
        return False
    try:
        _init()
        return True
    except Exception:                              # noqa: BLE001
        return False


def _enc():
    """取当前命令编码器；批量模式下只返回、不提交。"""
    st = _init()
    if _ENC:
        return _ENC[-1], False
    return st["device"].create_command_encoder(), True


def _flush(enc, owns):
    if owns:
        _init()["device"].queue.submit([enc.finish()])


@contextlib.contextmanager
def batch():
    """把中间各次 dispatch 并进一个提交，减少驱动往返。"""
    st = _init()
    _ENC.append(st["device"].create_command_encoder())
    try:
        yield
    finally:
        enc = _ENC.pop()           # _sync 可能换过编码器，取最新的
        st["device"].queue.submit([enc.finish()])
        while _DEFER:
            buf = _DEFER.pop()
            st["pool"].setdefault(buf.size, []).append(buf)


def _sync():
    """回读之前先把在途命令提交掉，再开一个新的批。"""
    if not _ENC:
        return
    st = _init()
    enc = _ENC.pop()
    st["device"].queue.submit([enc.finish()])
    _ENC.append(st["device"].create_command_encoder())


def _buf(nbytes):
    st = _init()
    free = st["pool"].setdefault(nbytes, [])
    if free:
        return free.pop()
    wgpu = st["wgpu"]
    usage = (wgpu.BufferUsage.STORAGE | wgpu.BufferUsage.COPY_DST
             | wgpu.BufferUsage.COPY_SRC)
    return st["device"].create_buffer(size=nbytes, usage=usage)


def _put(buf):
    st = _init()
    if _ENC:                     # 批次还没提交，先不回收，避免被后续写入覆盖
        _DEFER.append(buf)
    else:
        st["pool"].setdefault(buf.size, []).append(buf)


def _up(arr):
    st = _init()
    a = np.ascontiguousarray(arr, dtype=np.float32).reshape(-1)
    buf = _buf(a.nbytes)
    st["device"].queue.write_buffer(buf, 0, a.tobytes())
    return buf


def _down(buf, shape):
    raw = _init()["device"].queue.read_buffer(buf)
    return np.frombuffer(raw, np.float32).reshape(shape).copy()


def _uniform(data: bytes):
    st = _init()
    wgpu = st["wgpu"]
    buf = st["device"].create_buffer(
        size=64, usage=wgpu.BufferUsage.UNIFORM | wgpu.BufferUsage.COPY_DST)
    st["device"].queue.write_buffer(buf, 0, data.ljust(64, b"\0"))
    keep = st.setdefault("keep", [])
    keep.append(buf)
    if len(keep) > 256:
        del keep[:128]
    return buf


def _scan_uni(w, h, r):
    return np.array([w, h, (w + _WG - 1) // _WG, r], np.uint32).tobytes()


def _box_h(src, w, h, r):
    """水平方向一次盒滤波，返回新缓冲。"""
    st = _init()
    dev, pipes = st["device"], st["pipes"]
    n = w * h
    blocks = (w + _WG - 1) // _WG
    uni = _uniform(_scan_uni(w, h, r))
    pfx, tot, off, dst = _buf(n * 4), _buf(h * blocks * 4), _buf(h * blocks * 4), _buf(n * 4)
    bg = dev.create_bind_group(layout=st["layouts"]["scan"], entries=[
        {"binding": 0, "resource": {"buffer": src, "offset": 0, "size": src.size}},
        {"binding": 1, "resource": {"buffer": pfx, "offset": 0, "size": pfx.size}},
        {"binding": 2, "resource": {"buffer": tot, "offset": 0, "size": tot.size}},
        {"binding": 3, "resource": {"buffer": off, "offset": 0, "size": off.size}},
        {"binding": 4, "resource": {"buffer": uni, "offset": 0, "size": 64}},
        {"binding": 5, "resource": {"buffer": dst, "offset": 0, "size": dst.size}},
    ])
    enc, own = _enc()
    cp = enc.begin_compute_pass()
    cp.set_bind_group(0, bg)
    cp.set_pipeline(pipes["scan"])
    cp.dispatch_workgroups(blocks, h)
    cp.set_pipeline(pipes["offsets"])
    cp.dispatch_workgroups((h + 63) // 64)
    cp.set_pipeline(pipes["apply"])
    cp.dispatch_workgroups(blocks, h)
    cp.end()
    _flush(enc, own)
    _put(pfx)
    _put(tot)
    _put(off)

    return dst


def _transpose(src, w, h):
    st = _init()
    dev, pipes = st["device"], st["pipes"]
    uni = _uniform(np.array([w, h, 1, 0], np.uint32).tobytes())
    dst = _buf(w * h * 4)
    bg = dev.create_bind_group(layout=st["layouts"]["tr"], entries=[
        {"binding": 0, "resource": {"buffer": src, "offset": 0, "size": src.size}},
        {"binding": 1, "resource": {"buffer": dst, "offset": 0, "size": dst.size}},
        {"binding": 2, "resource": {"buffer": uni, "offset": 0, "size": 64}},
    ])
    enc, own = _enc()
    cp = enc.begin_compute_pass()
    cp.set_bind_group(0, bg)
    cp.set_pipeline(pipes["transpose"])
    cp.dispatch_workgroups((w + 31) // 32, (h + 31) // 32)
    cp.end()
    _flush(enc, own)

    return dst


def _elem(mode, a, b=None, c=None, d=None, e=None, *, e0=0.0, e1=1.0,
          w0=0.0, w1=0.0, w2=0.0, w3=0.0, w4=0.0, lo=-3.0e38, hi=3.0e38):
    """逐元素算子；未用输入槽绑定 a。通道顺序：out = f(a,b,c,d,e)。"""
    st = _init()
    dev, pipes = st["device"], st["pipes"]
    n = a.size // 4
    uni = _uniform(np.array([mode, e0, e1, w0, w1, w2, w3, w4, lo, hi],
                            np.float32).tobytes())
    dst = _buf(n * 4)
    ins = [b or a, c or a, d or a, e or a]
    bg = dev.create_bind_group(layout=st["layouts"]["ew"], entries=[
        {"binding": 0, "resource": {"buffer": a, "offset": 0, "size": a.size}},
        *[{"binding": 1 + k, "resource": {"buffer": buf, "offset": 0, "size": buf.size}}
          for k, buf in enumerate(ins)],
        {"binding": 5, "resource": {"buffer": dst, "offset": 0, "size": dst.size}},
        {"binding": 6, "resource": {"buffer": uni, "offset": 0, "size": 64}},
    ])
    enc, own = _enc()
    cp = enc.begin_compute_pass()
    cp.set_bind_group(0, bg)
    cp.set_pipeline(pipes["elem"])
    cp.dispatch_workgroups((n + 255) // 256)
    cp.end()
    _flush(enc, own)

    return dst


def _box_buf(src, w, h, r):
    """显存内的可分离均值模糊：水平 → 转置 → 水平 → 转置。"""
    if r < 1:
        return src
    hz = _box_h(src, w, h, r)
    t = _transpose(hz, w, h)              # (w, h)：原 y 方向变成行方向
    vz = _box_h(t, h, w, r)
    back = _transpose(vz, h, w)           # (h, w)
    _put(hz)
    _put(t)
    _put(vz)
    return back


def _gauss_buf(src, w, h, r):
    """三次 ``_box_buf``，中间结果留在显存。"""
    cur = src
    for _ in range(3):
        nxt = _box_buf(cur, w, h, r)
        if cur is not src:
            _put(cur)
        cur = nxt
    return cur


def _guided_buf(guide, src, w, h, r, eps=1e-3):
    """显存内的导向滤波，系数与 ``light.guided_filter`` 相同（居中方差）。"""
    if r < 1:
        return src
    mean_i = _box_buf(guide, w, h, r)
    mean_p = _box_buf(src, w, h, r)
    di = _elem(0, guide, mean_i, w0=1.0, w1=-1.0)
    dp = _elem(0, src, mean_p, w0=1.0, w1=-1.0)
    var = _box_buf(_elem(6, di, di), w, h, r)
    cov = _box_buf(_elem(6, di, dp), w, h, r)
    ca = _elem(4, cov, var, w0=eps)
    cb = _elem(8, mean_p, ca, mean_i)
    bb = _box_buf(cb, w, h, r)
    ba = _box_buf(ca, w, h, r)
    t = _elem(6, ba, guide)
    out = _elem(0, t, bb, w0=1.0, w1=1.0)
    for buf in (mean_i, mean_p, di, dp, var, cov, ca, cb, ba, bb, t):
        _put(buf)
    return out


def box(arr: np.ndarray, r: int) -> np.ndarray:
    """与 ``gainmap._box`` 同定义：可分离均值、边界复制。"""
    arr = np.asarray(arr, dtype=np.float32)
    if r < 1:
        return arr
    h, w = arr.shape
    src = _up(arr)
    out = _down(_box_buf(src, w, h, r), (h, w))
    _put(src)
    return out


def gauss(arr: np.ndarray, r: int) -> np.ndarray:
    """三次盒滤波 ≈ 高斯（与 ``gainmap._gauss`` 一致）。"""
    out = np.asarray(arr, dtype=np.float32)
    for _ in range(3):
        out = box(out, r)
    return out


def guided(guide: np.ndarray, src: np.ndarray, r: int, eps: float = 1e-3) -> np.ndarray:
    """自引导滤波（He 2010），与 ``light.guided_filter`` 同定义。"""
    guide = np.asarray(guide, dtype=np.float32)
    if r < 1:
        return np.asarray(src, dtype=np.float32)
    h, w = guide.shape
    g = _up(guide)
    p = _up(np.asarray(src, dtype=np.float32))
    out = _down(_guided_buf(g, p, w, h, r, eps), (h, w))
    _put(g)
    _put(p)
    return out


def emissive(y: np.ndarray, cfg: dict, irr: np.ndarray, ao: np.ndarray):
    """在显卡上重算 ``gainmap.emissive`` 的五层判据，返回 (y, emis)。"""
    y = np.asarray(y, dtype=np.float32)
    h, w = y.shape
    tmp = []
    with batch():
        yb = _up(y)
        tmp.append(yb)
        r_illum = max(8, min(h, w) // 25)
        gi = _gauss_buf(yb, w, h, r_illum)
        illum = _guided_buf(yb, gi, w, h, max(1, r_illum // 2), 1e-3)
        tmp += [gi, illum]
        _sync()
        lo, hi = np.percentile(_down(illum, (h, w)), [38, 99])

        lit = _elem(1, illum, e0=lo, e1=max(hi, lo + 1e-3), lo=0.0, hi=1.0)
        gp = _gauss_buf(yb, w, h, int(cfg["pr"]))
        peak = _elem(4, yb, gp, w0=1e-3, lo=0.0, hi=1e6)
        spark = _elem(1, peak, e0=cfg["p0"], e1=cfg["p1"], lo=0.0, hi=1.0)
        tmp += [lit, gp, peak, spark]

        area = _gauss_buf(lit, w, h, max(8, min(h, w) // 8))
        soft = _elem(1, area, e0=cfg["area_lo"], e1=cfg["area_hi"], lo=0.0, hi=1.0)
        pen = _elem(5, soft, spark, w0=cfg["area_pen"], w1=0.7)
        lit2 = _elem(2, lit, pen, w0=1.0, lo=0.0, hi=1.0)
        tmp += [area, soft, pen, lit2]

        ib = _up(np.asarray(irr, dtype=np.float32))
        irr_b = _elem(1, ib, e0=cfg["i0"], e1=cfg["i1"], lo=0.0, hi=1.0)
        fb = _gauss_buf(yb, w, h, max(12, min(h, w) // 20))
        far = _elem(1, fb, e0=0.02, e1=0.45, lo=0.0, hi=1.0)
        ab = _up(np.asarray(ao, dtype=np.float32))
        aob = _elem(0, ab, w0=1.0, lo=0.45, hi=1.0)
        env = _elem(6, far, aob, lo=0.0, hi=1.0)
        tmp += [ib, irr_b, fb, far, ab, aob, env]

        wb = _elem(1, yb, e0=0.92, e1=1.0, lo=0.0, hi=1.0)
        white = _elem(2, wb, pen, w0=1.0, lo=0.0, hi=1.0)
        emis = _elem(0, lit2, irr_b, spark, white, env,
                     w0=cfg["base_lit"], w1=cfg["base_irr"], w2=cfg["base_spark"],
                     w3=cfg["base_white"], w4=cfg["base_amb"], lo=-1e9, hi=1e9)
        tmp += [wb, white]
        hb = _gauss_buf(emis, w, h, int(cfg["hr"]))
        emis2 = _elem(0, hb, emis, w0=cfg["halo"], w1=1.0 - cfg["halo"],
                      lo=0.0, hi=1.0)
        tmp += [hb, emis, emis2]

        _sync()
        out = _down(emis2, (h, w))
        for buf in tmp:
            _put(buf)
    return y, out


def finalize(emis: np.ndarray) -> np.ndarray:
    """指数饱和 + 小高斯，对应 ``gainmap.finalize``。"""
    emis = np.asarray(emis, dtype=np.float32)
    h, w = emis.shape
    p = float(np.percentile(emis, 99.9)) or 1.0
    with batch():
        b = _up(emis)
        sat = _elem(3, b, w0=1.6 / max(p, 1e-6), lo=0.0, hi=1.0)
        g = _gauss_buf(sat, w, h, 2)
        _sync()
        out = _down(g, (h, w))
        for buf in (b, sat, g):
            _put(buf)
    return out
