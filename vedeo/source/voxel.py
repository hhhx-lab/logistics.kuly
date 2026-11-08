"""原创体素（方块）风格的小型软件渲染器。

- 透视相机 + Z 缓冲三角形光栅化（numpy 向量化）
- 像素贴图最近邻采样（方块游戏质感），界面贴图可双线性采样
- 太阳方向光 + 阴影贴图（窗格光斑、人物投影）
- 室外物体带大气雾
不使用任何第三方游戏的贴图或模型，全部程序化生成。
"""
import math
import numpy as np


def norm(v):
    v = np.asarray(v, np.float64)
    return v / np.linalg.norm(v)


def rot_y(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], np.float64)


def rot_x(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], np.float64)


def rot_z(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], np.float64)


class Xf:
    """刚体变换：p_world = R @ p_local + t，可嵌套。"""

    def __init__(self, R=None, t=(0, 0, 0)):
        self.R = np.eye(3) if R is None else np.asarray(R, np.float64)
        self.t = np.asarray(t, np.float64)

    def __matmul__(self, o):
        return Xf(self.R @ o.R, self.R @ o.t + self.t)

    def pt(self, p):
        return np.asarray(p, np.float64) @ self.R.T + self.t

    def vec(self, v):
        return np.asarray(v, np.float64) @ self.R.T


def solid(rgb, a=1.0):
    t = np.empty((1, 1, 4), np.float32)
    t[0, 0] = (*rgb, a)
    return t


def hex3(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)


def ascii_tex(rows, pal):
    """用字符画定义像素贴图。pal: 字符 -> '#RRGGBB' 或 None(透明)。"""
    h, w = len(rows), len(rows[0])
    t = np.zeros((h, w, 4), np.float32)
    for j, row in enumerate(rows):
        assert len(row) == w, (j, row, len(row), w)
        for i, ch in enumerate(row):
            c = pal[ch]
            if c is None:
                continue
            t[j, i, :3] = hex3(c)
            t[j, i, 3] = 1.0
    return t


def noise_tex(base, w=16, h=16, amp=0.05, seed=0, tint=None):
    """带轻微像素噪点的纯色贴图。"""
    rng = np.random.RandomState(seed)
    t = np.ones((h, w, 4), np.float32)
    n = (rng.rand(h, w, 1) - 0.5) * 2 * amp
    t[..., :3] = np.clip(np.array(hex3(base), np.float32) + n, 0, 1)
    if tint is not None:
        t[..., :3] *= np.array(tint, np.float32)
    return t


class Quad:
    __slots__ = ("v", "tex", "n", "double", "emissive", "outdoor", "smooth", "uv", "noshadow", "bright", "blend")

    def __init__(self, v, tex, n, double=False, emissive=False, outdoor=False, smooth=False,
                 uv=None, noshadow=False, bright=1.0, blend=False):
        self.v = np.asarray(v, np.float64)          # TL, TR, BR, BL
        self.tex = tex
        self.n = np.asarray(n, np.float64)
        self.double = double
        self.emissive = emissive
        self.outdoor = outdoor
        self.smooth = smooth
        self.noshadow = noshadow
        self.bright = bright
        self.blend = blend          # True：按贴图 alpha 半透明混合（透明文件夹等），最后绘制、不写深度
        self.uv = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float64) if uv is None else np.asarray(uv, np.float64)


FACES = ("front", "back", "left", "right", "top", "bottom")


def box(scene, lo, hi, tex, xf=None, skip=(), **kw):
    """加入一个长方体。lo/hi 为局部坐标的最小/最大角；tex 可为单贴图或 {面: 贴图}。
    局部坐标：X 右、Y 上、Z 前（远离观众）；“front” 指 -Z 面（朝向观众的一面）。"""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    xf = xf or Xf()
    faces = {
        "front": ([(x0, y1, z0), (x1, y1, z0), (x1, y0, z0), (x0, y0, z0)], (0, 0, -1)),
        "back": ([(x1, y1, z1), (x0, y1, z1), (x0, y0, z1), (x1, y0, z1)], (0, 0, 1)),
        "left": ([(x0, y1, z1), (x0, y1, z0), (x0, y0, z0), (x0, y0, z1)], (-1, 0, 0)),
        "right": ([(x1, y1, z0), (x1, y1, z1), (x1, y0, z1), (x1, y0, z0)], (1, 0, 0)),
        "top": ([(x0, y1, z1), (x1, y1, z1), (x1, y1, z0), (x0, y1, z0)], (0, 1, 0)),
        "bottom": ([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], (0, -1, 0)),
    }
    for name in FACES:
        if name in skip:
            continue
        t = tex.get(name, tex.get("all")) if isinstance(tex, dict) else tex
        if t is None:
            continue
        v, n = faces[name]
        scene.append(Quad(xf.pt(v), t, xf.vec(n), **kw))


def tiled(tex, nx, ny):
    """把贴图平铺 nx×ny 次（用于大面积墙面、地板）。"""
    return np.tile(tex, (max(1, int(round(ny))), max(1, int(round(nx))), 1))


class Camera:
    def __init__(self, pos, target, fov_deg, w, h):
        self.pos = np.asarray(pos, np.float64)
        f = norm(np.asarray(target, np.float64) - self.pos)
        r = norm(np.cross((0, 1, 0), f))
        u = np.cross(f, r)
        self.M = np.stack([r, u, f])               # world -> view
        self.w, self.h = w, h
        self.fpx = (h / 2) / math.tan(math.radians(fov_deg) / 2)

    def project(self, p):
        pv = (np.asarray(p, np.float64) - self.pos) @ self.M.T
        z = pv[..., 2]
        sx = self.w / 2 + self.fpx * pv[..., 0] / z
        sy = self.h / 2 - self.fpx * pv[..., 1] / z
        return np.stack([sx, sy], -1), z


def _raster_tri(sp, w, h):
    """返回 (y0,y1,x0,x1, l0,l1,l2, mask)；重心坐标在包围盒像素网格上。"""
    x0 = max(0, int(math.floor(sp[:, 0].min())))
    x1 = min(w, int(math.ceil(sp[:, 0].max())) + 1)
    y0 = max(0, int(math.floor(sp[:, 1].min())))
    y1 = min(h, int(math.ceil(sp[:, 1].max())) + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    (ax, ay), (bx, by), (cx, cy) = sp
    den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(den) < 1e-9:
        return None
    xs = (np.arange(x0, x1, dtype=np.float32) + 0.5)[None, :]
    ys = (np.arange(y0, y1, dtype=np.float32) + 0.5)[:, None]
    l0 = ((by - cy) * (xs - cx) + (cx - bx) * (ys - cy)) / den
    l1 = ((cy - ay) * (xs - cx) + (ax - cx) * (ys - cy)) / den
    l2 = 1 - l0 - l1
    e = -1e-5
    mask = (l0 >= e) & (l1 >= e) & (l2 >= e)
    if not mask.any():
        return None
    return y0, y1, x0, x1, l0, l1, l2, mask


class Sun:
    """方向光 + 正交阴影贴图。"""

    def __init__(self, to_sun, center, extent, depth, res=2048):
        self.L = norm(to_sun)
        f = -self.L
        r = norm(np.cross((0, 1, 0), f))
        u = np.cross(f, r)
        self.M = np.stack([r, u, f])
        self.center = np.asarray(center, np.float64)
        self.extent, self.depth, self.res = extent, depth, res
        self.map = np.full((res, res), 1e9, np.float32)

    def to_light(self, p):
        pv = (p - self.center) @ self.M.T
        sx = (pv[..., 0] / self.extent * 0.5 + 0.5) * self.res
        sy = (0.5 - pv[..., 1] / self.extent * 0.5) * self.res
        return sx, sy, pv[..., 2]

    def build(self, scene):
        res = self.res
        for q in scene:
            if q.outdoor or q.noshadow or q.emissive or q.blend:
                continue
            sx, sy, d = self.to_light(q.v)
            for tri in ((0, 1, 2), (0, 2, 3)):
                idx = list(tri)
                sp = np.stack([sx[idx], sy[idx]], -1)
                r = _raster_tri(sp, res, res)
                if r is None:
                    continue
                y0, y1, x0, x1, l0, l1, l2, mask = r
                dd = (l0 * d[idx[0]] + l1 * d[idx[1]] + l2 * d[idx[2]]).astype(np.float32)
                # 贴图透明处不投影
                if q.tex.shape[0] * q.tex.shape[1] > 1 and (q.tex[..., 3] < 0.5).any():
                    uv = q.uv[idx]
                    u = l0 * uv[0, 0] + l1 * uv[1, 0] + l2 * uv[2, 0]
                    v = l0 * uv[0, 1] + l1 * uv[1, 1] + l2 * uv[2, 1]
                    th, tw = q.tex.shape[:2]
                    iu = np.clip((u * tw).astype(np.int32), 0, tw - 1)
                    iv = np.clip((v * th).astype(np.int32), 0, th - 1)
                    mask = mask & (q.tex[iv, iu, 3] > 0.5)
                sub = self.map[y0:y1, x0:x1]
                upd = mask & (dd < sub)
                sub[upd] = dd[upd]

    def lit(self, p, bias=14.0):
        """p: (...,3) 世界坐标 -> 受光比例 0..1（2x2 PCF）。范围外视为受光。"""
        sx, sy, d = self.to_light(p)
        res = self.res
        out = np.zeros(sx.shape, np.float32)
        fx, fy = sx - 0.5, sy - 0.5
        ix, iy = np.floor(fx).astype(np.int32), np.floor(fy).astype(np.int32)
        wx, wy = (fx - ix).astype(np.float32), (fy - iy).astype(np.float32)
        for dy_, wyy in ((0, 1 - wy), (1, wy)):
            for dx_, wxx in ((0, 1 - wx), (1, wx)):
                jx, jy = ix + dx_, iy + dy_
                ok = (jx >= 0) & (jx < res) & (jy >= 0) & (jy < res)
                m = self.map[np.clip(jy, 0, res - 1), np.clip(jx, 0, res - 1)]
                l = np.where(ok, (d - bias <= m), True)
                out += l * wxx * wyy
        return out


def _clip_near(V, UV, z, near):
    """把跨越近裁剪面的四边形裁成若干三角形（Sutherland–Hodgman），保留 z >= near 的部分。"""
    out = []
    n = len(V)
    for i in range(n):
        j = (i + 1) % n
        a_in, b_in = z[i] >= near, z[j] >= near
        if a_in:
            out.append((V[i], UV[i]))
        if a_in != b_in:
            s = (near - z[i]) / (z[j] - z[i])
            out.append((V[i] + (V[j] - V[i]) * s, UV[i] + (UV[j] - UV[i]) * s))
    tris = []
    for k in range(1, len(out) - 1):
        tris.append((np.array([out[0][0], out[k][0], out[k + 1][0]]),
                     np.array([out[0][1], out[k][1], out[k + 1][1]])))
    return tris


def render(scene, cam, sun, sky, ambient=(0.62, 0.64, 0.70), sun_col=(1.0, 0.86, 0.62),
           fog_col=(0.80, 0.88, 0.94), fog_k=38000.0):
    """sky: HxWx3 背景。返回 float32 HxWx3 (线性感不严格，直接按显示色处理)。"""
    w, h = cam.w, cam.h
    color = sky.astype(np.float32).copy()
    zbuf = np.zeros((h, w), np.float32)             # 存 1/z，越大越近
    amb = np.array(ambient, np.float32)
    scol = np.array(sun_col, np.float32)
    fogc = np.array(fog_col, np.float32)
    near = 50.0
    for q in sorted(scene, key=lambda q: q.blend):
        if not q.double and np.dot(q.n, cam.pos - q.v.mean(0)) <= 0:
            continue
        sp, z = cam.project(q.v)
        if (z < near).all():
            continue
        if (z < near).any():
            tris = _clip_near(q.v, q.uv, z, near)
        else:
            tris = [(q.v[[0, 1, 2]], q.uv[[0, 1, 2]]), (q.v[[0, 2, 3]], q.uv[[0, 2, 3]])]
        n = q.n
        if q.double and np.dot(n, cam.pos - q.v.mean(0)) < 0:
            n = -n
        ndl = max(0.0, float(np.dot(n, sun.L)))
        # 方块游戏式的固定面向明暗：顶面最亮，侧面依次变暗
        face = 0.80 + 0.20 * n[1] - 0.07 * abs(n[0])
        th, tw = q.tex.shape[:2]
        for TV, uv in tris:
            tsp, tz = cam.project(TV)
            r = _raster_tri(tsp, w, h)
            if r is None:
                continue
            y0, y1, x0, x1, l0, l1, l2, mask = r
            iz0, iz1, iz2 = 1.0 / tz[0], 1.0 / tz[1], 1.0 / tz[2]
            iz = (l0 * iz0 + l1 * iz1 + l2 * iz2).astype(np.float32)
            sub_z = zbuf[y0:y1, x0:x1]
            mask = mask & (iz > sub_z)
            if not mask.any():
                continue
            a0, a1, a2 = l0 * iz0 / iz, l1 * iz1 / iz, l2 * iz2 / iz    # 透视校正权重
            if th * tw == 1:
                tex = np.broadcast_to(q.tex[0, 0], mask.shape + (4,))
            else:
                u = a0 * uv[0, 0] + a1 * uv[1, 0] + a2 * uv[2, 0]
                v = a0 * uv[0, 1] + a1 * uv[1, 1] + a2 * uv[2, 1]
                if q.smooth:
                    uu = np.clip(u * tw - 0.5, 0, tw - 1.001)
                    vv = np.clip(v * th - 0.5, 0, th - 1.001)
                    iu, iv = uu.astype(np.int32), vv.astype(np.int32)
                    fu, fv = (uu - iu)[..., None], (vv - iv)[..., None]
                    T = q.tex
                    tex = (T[iv, iu] * (1 - fu) * (1 - fv) + T[iv, iu + 1] * fu * (1 - fv)
                           + T[iv + 1, iu] * (1 - fu) * fv + T[iv + 1, iu + 1] * fu * fv)
                else:
                    iu = np.clip((u * tw).astype(np.int32), 0, tw - 1)
                    iv = np.clip((v * th).astype(np.int32), 0, th - 1)
                    tex = q.tex[iv, iu]
                if not q.blend:
                    mask = mask & (tex[..., 3] > 0.5)
                if not mask.any():
                    continue
            alb = tex[..., :3]
            if q.emissive:
                c = alb * q.bright
            else:
                wp = (a0[..., None] * TV[0] + a1[..., None] * TV[1] + a2[..., None] * TV[2])
                if q.outdoor:
                    light = amb * 1.12 * face + scol * ndl * 0.55
                    c = alb * light
                    dist = (1.0 / iz)[..., None]
                    f = 1 - np.exp(-dist / fog_k)
                    c = c * (1 - f) + fogc * f
                else:
                    s = sun.lit(wp + n * 6.0) if ndl > 0 else 0.0
                    light = amb * face + scol * (ndl * np.asarray(s, np.float32))[..., None] * 0.95
                    c = alb * light * q.bright
            sub_c = color[y0:y1, x0:x1]
            if q.blend:
                a = tex[..., 3:4][mask]
                sub_c[mask] = sub_c[mask] * (1 - a) + np.asarray(c, np.float32)[mask] * a
            else:
                sub_c[mask] = np.asarray(c, np.float32)[mask]
                sub_z[mask] = iz[mask]
    return color, zbuf
