"""WebGPU 后端：PQ 编码与增益系数的逐像素数学放到显卡上；不可用时调用方回退 NumPy。

着色器只做查表与乘加：sRGB 解码、tint、exp2、PQ 都采样 ``lut`` 里的同一张表，
因此与 CPU 路径对齐到码值。强依赖只有 ``wgpu``（pip 包，自带原生库）。
"""
from __future__ import annotations

import os
import threading

import numpy as np

from . import lut

_CHUNK_PX = 4_000_000          # 每块像素上限：rgb 48MB + out 48MB，留足显存
_BACKEND = None                # 缓存 (device, pipelines, info) 或 False
_PROBE: dict | None = None

_COMMON = f"""
// N_* 是步长，L_* 是结点数；索引上限用 L-2，与 CPU 的 _sample 一致
const N_SRGB: f32 = {float(lut.N_SRGB)};
const N_TINT: f32 = {float(lut.N_TINT / lut.TINT_MAX)};
const N_EXP2: f32 = {float(lut.N_EXP2 / lut.EXP2_MAX)};
const N_PQ: f32 = {float(lut.N_PQ)};
const L_SRGB: f32 = {float(lut.N_SRGB + 1)};
const L_TINT: f32 = {float(lut.N_TINT + 1)};
const L_EXP2: f32 = {float(lut.N_EXP2 + 1)};
const L_PQ: f32 = {float(lut.N_PQ + 1)};

fn srgb_at(v: f32) -> f32 {{
    let t = clamp(v, 0.0, 1.0) * N_SRGB;
    let i = u32(min(floor(t), L_SRGB - 2.0));
    let f = t - f32(i);
    return srgb[i] + f * (srgb[i + 1u] - srgb[i]);
}}
fn tint_at(r: f32) -> f32 {{
    let t = clamp(r, 0.0, {float(lut.TINT_MAX)}) * N_TINT;
    let i = u32(min(floor(t), L_TINT - 2.0));
    let f = t - f32(i);
    return tint[i] + f * (tint[i + 1u] - tint[i]);
}}
fn exp2_at(e: f32) -> f32 {{
    let t = clamp(e, 0.0, {float(lut.EXP2_MAX)}) * N_EXP2;
    let i = u32(min(floor(t), L_EXP2 - 2.0));
    let f = t - f32(i);
    return ex2[i] + f * (ex2[i + 1u] - ex2[i]);
}}
fn pq_at(nits: f32) -> f32 {{
    let u = sqrt(sqrt(clamp(nits, 0.0, 10000.0) / 10000.0));
    let t = u * N_PQ;
    let i = u32(min(floor(t), L_PQ - 2.0));
    let f = t - f32(i);
    return pq[i] + f * (pq[i + 1u] - pq[i]);
}}
fn tinted(lin: vec3<f32>, lum: f32, w: vec3<f32>) -> vec3<f32> {{
    let t = lin / max(lum, 1e-4);
    var c = vec3<f32>(tint_at(t.x), tint_at(t.y), tint_at(t.z)) * w;
    c = c / (0.2126 * c.x + 0.7152 * c.y + 0.0722 * c.z);
    return c;
}}
fn bt2020(x: vec3<f32>) -> vec3<f32> {{
    return vec3<f32>(
        0.627404 * x.x + 0.329283 * x.y + 0.043313 * x.z,
        0.069097 * x.x + 0.919540 * x.y + 0.011362 * x.z,
        0.016391 * x.x + 0.088013 * x.y + 0.895595 * x.z);
}}
"""

_BINDINGS = """
@group(0) @binding(0) var<storage, read> rgb: array<f32>;
@group(0) @binding(1) var<storage, read> emis: array<f32>;
@group(0) @binding(2) var<uniform> P: Params;
@group(0) @binding(3) var<storage, read_write> out: array<OUT>;
@group(0) @binding(4) var<storage, read_write> stats: array<atomic<u32>>;
@group(0) @binding(5) var<storage, read> srgb: array<f32>;
@group(0) @binding(6) var<storage, read> tint: array<f32>;
@group(0) @binding(7) var<storage, read> ex2: array<f32>;
@group(0) @binding(8) var<storage, read> pq: array<f32>;
"""

_SHADER_PQ = (_COMMON + """
struct Params { gmax: f32, w0: f32, w1: f32, w2: f32 };
""" + _BINDINGS.replace("OUT", "u32") + """
@compute @workgroup_size(64)
fn main(@builtin(global_invocation_id) gid: vec3<u32>) {
    let i = gid.x;
    if (i >= arrayLength(&emis)) { return; }
    let lin = vec3<f32>(srgb_at(rgb[i * 3u]), srgb_at(rgb[i * 3u + 1u]),
                        srgb_at(rgb[i * 3u + 2u]));
    let lum = 0.2126 * lin.x + 0.7152 * lin.y + 0.0722 * lin.z;
    let e = P.gmax * emis[i];
    let c = tinted(lin, lum, vec3<f32>(P.w0, P.w1, P.w2));
    let hdr = bt2020(lin * vec3<f32>(exp2_at(e * c.x), exp2_at(e * c.y),
                                     exp2_at(e * c.z))) * 203.0;
    let q = vec3<f32>(pq_at(hdr.x), pq_at(hdr.y), pq_at(hdr.z));
    out[i * 3u]      = u32(round(q.x * 65535.0));
    out[i * 3u + 1u] = u32(round(q.y * 65535.0));
    out[i * 3u + 2u] = u32(round(q.z * 65535.0));
    atomicMax(&stats[0], bitcast<u32>(max(hdr.x, max(hdr.y, hdr.z))));
}
""")

_SHADER_EXPO = (_COMMON + """
struct Params { gmax: f32, w0: f32, w1: f32, w2: f32 };
""" + _BINDINGS.replace("OUT", "f32") + """
@compute @workgroup_size(64)
fn main(@builtin(global_invocation_id) gid: vec3<u32>) {
    let i = gid.x;
    if (i >= arrayLength(&emis)) { return; }
    let lin = vec3<f32>(srgb_at(rgb[i * 3u]), srgb_at(rgb[i * 3u + 1u]),
                        srgb_at(rgb[i * 3u + 2u]));
    let lum = 0.2126 * lin.x + 0.7152 * lin.y + 0.0722 * lin.z;
    let e = P.gmax * emis[i];
    let c = tinted(lin, lum, vec3<f32>(P.w0, P.w1, P.w2));
    let expo = e * c;
    out[i * 3u]      = expo.x;
    out[i * 3u + 1u] = expo.y;
    out[i * 3u + 2u] = expo.z;
    atomicMax(&stats[0], bitcast<u32>(expo.x));
    atomicMax(&stats[1], bitcast<u32>(expo.y));
    atomicMax(&stats[2], bitcast<u32>(expo.z));
}
""")


_LOCK = threading.Lock()


class GpuError(RuntimeError):
    """GPU 路径不可用；调用方应回退 NumPy。"""


def probe() -> dict:
    """探测适配器，返回 {ok, adapter, backend, reason}；结果缓存。"""
    global _PROBE
    if _PROBE is not None:
        return _PROBE
    try:
        import wgpu
    except Exception as exc:                       # noqa: BLE001
        _PROBE = {"ok": False, "reason": f"缺少 wgpu：{exc}"}
        return _PROBE
    try:
        adapter = wgpu.gpu.request_adapter_sync(power_preference="high-performance")
        if adapter is None:
            raise GpuError("没有可用的 GPU 适配器")
        info = adapter.info
        _PROBE = {"ok": True, "adapter": getattr(info, "device", "?"),
                  "backend": str(getattr(info, "backend_type", "?"))}
    except Exception as exc:                       # noqa: BLE001
        _PROBE = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    return _PROBE


def _lut_buffers(device, wgpu) -> list:
    """四张表一次上传，之后每块复用。"""
    out = []
    for tab in (lut.SRGB, lut.TINT, lut.EXP2, lut.PQ):
        arr = np.ascontiguousarray(tab, dtype=np.float32)
        buf = device.create_buffer(size=arr.nbytes,
                                   usage=wgpu.BufferUsage.STORAGE | wgpu.BufferUsage.COPY_DST)
        device.queue.write_buffer(buf, 0, arr.tobytes())
        out.append(buf)
    return out


def _init(force: bool = False):
    """建一次设备与两条管线，之后按块复用；多线程共用同一把锁。"""
    global _BACKEND
    with _LOCK:
        if _BACKEND is not None and not force:
            if _BACKEND is False:
                raise GpuError("GPU 不可用")
            return _BACKEND
        if not probe()["ok"]:
            _BACKEND = False
            raise GpuError(probe().get("reason", "GPU 不可用"))
        try:
            import wgpu
            adapter = wgpu.gpu.request_adapter_sync(power_preference="high-performance")
            device = adapter.request_device_sync()
            entries = [
                {"binding": 0, "visibility": wgpu.ShaderStage.COMPUTE,
                 "buffer": {"type": wgpu.BufferBindingType.read_only_storage}},
                {"binding": 1, "visibility": wgpu.ShaderStage.COMPUTE,
                 "buffer": {"type": wgpu.BufferBindingType.read_only_storage}},
                {"binding": 2, "visibility": wgpu.ShaderStage.COMPUTE,
                 "buffer": {"type": wgpu.BufferBindingType.uniform}},
                {"binding": 3, "visibility": wgpu.ShaderStage.COMPUTE,
                 "buffer": {"type": wgpu.BufferBindingType.storage}},
                {"binding": 4, "visibility": wgpu.ShaderStage.COMPUTE,
                 "buffer": {"type": wgpu.BufferBindingType.storage}},
            ]
            for b in (5, 6, 7, 8):
                entries.append({"binding": b, "visibility": wgpu.ShaderStage.COMPUTE,
                                "buffer": {"type": wgpu.BufferBindingType.read_only_storage}})
            bgl = device.create_bind_group_layout(entries=entries)
            layout = device.create_pipeline_layout(bind_group_layouts=[bgl])
            pipes = {}
            for name, code in (("pq", _SHADER_PQ), ("expo", _SHADER_EXPO)):
                module = device.create_shader_module(code=code)
                pipes[name] = device.create_compute_pipeline(
                    layout=layout, compute={"module": module, "entry_point": "main"})
            _BACKEND = {"device": device, "bgl": bgl, "layout": layout, "pipes": pipes,
                        "luts": _lut_buffers(device, wgpu), "info": probe()}
        except GpuError:
            _BACKEND = False
            raise
        except Exception as exc:                   # noqa: BLE001
            _BACKEND = False
            raise GpuError(f"{type(exc).__name__}: {exc}") from exc
        return _BACKEND


def warm() -> bool:
    """提前把设备与管线建好，调用方可在后台线程里预热。"""
    try:
        _init()
        return True
    except GpuError:
        return False


def _as_pair(rgb: np.ndarray, emis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    rgb32 = np.ascontiguousarray(rgb, dtype=np.float32).reshape(-1, 3)
    emis32 = np.ascontiguousarray(emis, dtype=np.float32).reshape(-1)
    if rgb32.shape[0] != emis32.size:
        raise ValueError(f"rgb/emis 尺寸不匹配：{rgb32.shape} vs {emis32.shape}")
    return rgb32, emis32


def _dispatch(shader: str, rgb: np.ndarray, emis: np.ndarray, gmax: float,
              weights, out_dtype: np.dtype) -> tuple[np.ndarray, np.ndarray]:
    """跑一块，返回 (out[n,3], stats[3])；n 超过 _CHUNK_PX 时内部再切。"""
    import wgpu
    be = _init()
    device, pipes, luts = be["device"], be["pipes"], be["luts"]
    rgb32, emis32 = _as_pair(rgb, emis)
    n = emis32.size
    out = np.empty((n, 3), dtype=out_dtype)
    stats = np.zeros(3, dtype=np.uint32)
    step = min(_CHUNK_PX, max(n, 1))
    for start in range(0, n, step):
        stop = min(n, start + step)
        cnt = stop - start
        b_rgb = device.create_buffer(size=cnt * 12, usage=wgpu.BufferUsage.STORAGE
                                    | wgpu.BufferUsage.COPY_DST)
        b_emis = device.create_buffer(size=cnt * 4, usage=wgpu.BufferUsage.STORAGE
                                      | wgpu.BufferUsage.COPY_DST)
        b_par = device.create_buffer(size=16, usage=wgpu.BufferUsage.UNIFORM
                                     | wgpu.BufferUsage.COPY_DST)
        b_out = device.create_buffer(size=cnt * 12, usage=wgpu.BufferUsage.STORAGE
                                     | wgpu.BufferUsage.COPY_SRC)
        b_stat = device.create_buffer(size=12, usage=wgpu.BufferUsage.STORAGE
                                      | wgpu.BufferUsage.COPY_SRC | wgpu.BufferUsage.COPY_DST)
        device.queue.write_buffer(b_rgb, 0, rgb32[start:stop].tobytes())
        device.queue.write_buffer(b_emis, 0, emis32[start:stop].tobytes())
        device.queue.write_buffer(b_par, 0, np.array([gmax, *map(float, weights)],
                                                     np.float32).tobytes())
        device.queue.write_buffer(b_stat, 0, bytes(12))
        bg = device.create_bind_group(layout=be["bgl"], entries=[
            {"binding": 0, "resource": {"buffer": b_rgb, "offset": 0, "size": b_rgb.size}},
            {"binding": 1, "resource": {"buffer": b_emis, "offset": 0, "size": b_emis.size}},
            {"binding": 2, "resource": {"buffer": b_par, "offset": 0, "size": b_par.size}},
            {"binding": 3, "resource": {"buffer": b_out, "offset": 0, "size": b_out.size}},
            {"binding": 4, "resource": {"buffer": b_stat, "offset": 0, "size": b_stat.size}},
            *[{"binding": 5 + k,
               "resource": {"buffer": buf, "offset": 0, "size": buf.size}}
              for k, buf in enumerate(luts)],
        ])
        enc = device.create_command_encoder()
        cp = enc.begin_compute_pass()
        cp.set_pipeline(pipes[shader])
        cp.set_bind_group(0, bg)
        cp.dispatch_workgroups((cnt + 63) // 64)
        cp.end()
        device.queue.submit([enc.finish()])
        raw = device.queue.read_buffer(b_out)
        out[start:stop] = np.frombuffer(raw, out_dtype).reshape(cnt, 3)
        st = np.frombuffer(device.queue.read_buffer(b_stat), np.uint32)
        stats = np.maximum(stats, st)
        for buf in (b_rgb, b_emis, b_par, b_out, b_stat):
            buf.destroy()
    return out, stats


def info() -> dict:
    """GPU 名称/后端；不可用时给出原因。"""
    return dict(probe())


def encode_pq(rgb: np.ndarray, emis: np.ndarray, gmax: float,
              weights) -> tuple[np.ndarray, float]:
    """返回 (uint16 码值 (h,w,3), 峰值 nits)。"""
    shape = emis.shape
    out, stats = _dispatch("pq", rgb, emis, gmax, weights, np.uint32)
    peak = float(np.frombuffer(stats[:1].tobytes(), np.float32)[0])
    return out.reshape(shape + (3,)).astype(np.uint16), peak


def expo(rgb: np.ndarray, emis: np.ndarray, gmax: float,
         weights) -> tuple[np.ndarray, np.ndarray]:
    """返回 (每通道 log2 增益 (h,w,3), 每通道最大值 [3])。"""
    shape = emis.shape
    out, stats = _dispatch("expo", rgb, emis, gmax, weights, np.float32)
    return out.reshape(shape + (3,)), np.frombuffer(stats.tobytes(), np.float32).copy()


def available() -> bool:
    """能否真正跑一条管线（结果缓存）。"""
    if os.environ.get("HDRGAINMAP_GPU", "").lower() in {"0", "off", "none", "false"}:
        return False
    try:
        _init()
        return True
    except Exception:                              # noqa: BLE001
        return False
