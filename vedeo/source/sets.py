"""镜 03–09 的场景（原创体素语汇，单位 mm；X 右、Y 上、Z 远离观众）。
远景部件一律 outdoor=True（带大气雾、不进阴影贴图），近景部件参与阴影。
"""
import math
import numpy as np

import scene as S
from voxel import Quad, box, tiled, solid, hex3, noise_tex, ascii_tex, Xf, rot_y, norm

SEA = -30000.0
X = {}          # 本模块额外的贴图缓存


def init():
    S.init_tex()
    if X:
        return
    X["facade"] = noise_tex("#E8DAC2", amp=0.022, seed=101)
    X["facade_d"] = noise_tex("#D3C3A8", amp=0.022, seed=102)
    X["glass"] = _glass_tex()
    X["mullion"] = noise_tex("#F7F5EF", amp=0.01, seed=103)
    X["inwall"] = noise_tex("#F3E7D2", amp=0.018, seed=3)
    X["deck"] = noise_tex("#6F8F86", amp=0.035, seed=104)
    X["deck_l"] = noise_tex("#83A39A", amp=0.035, seed=105)
    X["rail"] = noise_tex("#F2F2EC", amp=0.01, seed=106)
    X["hull"] = noise_tex("#1D4867", amp=0.02, seed=14)
    X["hull_red"] = noise_tex("#C9665A", amp=0.02, seed=33)
    X["bridge"] = noise_tex("#F4F1E8", amp=0.02, seed=31)
    X["bridge_win"] = _band_tex("#F4F1E8", "#3D5868", 16, 8, (3, 5))
    X["foam"] = noise_tex("#EAF5F8", amp=0.05, seed=107)
    X["gull"] = noise_tex("#FAFAF7", amp=0.0, seed=1)
    X["gull_d"] = noise_tex("#5F6B72", amp=0.0, seed=1)
    X["grass"] = noise_tex("#8DBF6E", amp=0.07, seed=108)
    X["grass2"] = noise_tex("#A6CC7A", amp=0.07, seed=109)
    X["field"] = _field_tex()
    X["field2"] = _field_tex(base="#C9CF6F", row="#B5BB58", seed=111)
    X["whitewall"] = noise_tex("#F4F2EC", amp=0.02, seed=112)
    X["roof"] = _roof_tex()
    X["water"] = noise_tex("#78AFC4", amp=0.04, seed=113)
    X["stone"] = noise_tex("#B5B8B2", amp=0.05, seed=114)
    X["gravel"] = noise_tex("#9C9A92", amp=0.08, seed=115)
    X["sleeper"] = noise_tex("#6B5640", amp=0.05, seed=116)
    X["steel"] = noise_tex("#A9B1B6", amp=0.02, seed=117)
    X["platform"] = noise_tex("#C9C6BE", amp=0.03, seed=118)
    X["edge"] = noise_tex("#F2C94C", amp=0.02, seed=119)
    X["loco"] = noise_tex("#2F6F8F", amp=0.02, seed=120)
    X["loco_d"] = noise_tex("#24566F", amp=0.02, seed=121)
    X["loco_win"] = _band_tex("#2F6F8F", "#2C3E48", 16, 8, (2, 5))
    X["loco_side"] = _loco_side_tex()
    X["wagon"] = noise_tex("#5A646B", amp=0.02, seed=122)
    X["wheel"] = noise_tex("#33393E", amp=0.02, seed=123)
    X["hill"] = noise_tex("#9BB9A0", amp=0.03, seed=124)
    X["hill2"] = noise_tex("#B3CBB3", amp=0.03, seed=125)
    X["tree"] = noise_tex("#5E9E62", amp=0.08, seed=126)
    X["tree2"] = noise_tex("#78B26B", amp=0.08, seed=127)
    X["trunk"] = noise_tex("#7C5A3C", amp=0.05, seed=128)
    X["wh_wall"] = noise_tex("#DCD8CC", amp=0.02, seed=129)
    X["wh_floor"] = noise_tex("#A8A497", amp=0.035, seed=130)
    X["shelf"] = noise_tex("#E08A3C", amp=0.03, seed=131)
    X["shelf_b"] = noise_tex("#3F6F8E", amp=0.03, seed=132)
    X["bin"] = _bin_tex()
    X["office_wall"] = noise_tex("#F1ECE2", amp=0.016, seed=133)
    X["office_floor"] = S.plank_tex("#CDB48E", seed=134)
    X["desk2"] = S.plank_tex("#E6D7BC", seed=135, planks=4)
    X["counter"] = S.plank_tex("#A9805A", seed=138, planks=4)
    X["cabinet"] = noise_tex("#B9D2D6", amp=0.02, seed=136)
    X["clip"] = noise_tex("#B68B5A", amp=0.03, seed=137)
    X["sheet"] = _sheet_tex()
    X["lid"] = noise_tex("#C9CFD3", amp=0.012, seed=6)
    X["sea_open"] = _open_sea_tex()


def _loco_side_tex():
    """机车侧面：面板接缝、散热百叶、两扇小窗（无字、无标识）。"""
    w, h = 32, 16
    t = np.ones((h, w, 4), np.float32)
    rng = np.random.RandomState(141)
    b = np.array(hex3("#2F6F8F"), np.float32)
    t[..., :3] = np.clip(b + (rng.rand(h, w, 1) - 0.5) * 0.03, 0, 1)
    t[0:2, :, :3] = hex3("#24566F")
    for x in (7, 15, 23):
        t[2:h, x, :3] = hex3("#28617E")
    for x0 in (9, 17):
        for j in range(4, 9):
            if j % 2 == 0:
                t[j, x0:x0 + 5, :3] = hex3("#1E4A60")
    for x0 in (2, 26):
        t[4:8, x0:x0 + 3, :3] = hex3("#2C3E48")
        t[4, x0:x0 + 3, :3] = hex3("#5C7F94")
    t[h - 1, :, :3] = hex3("#24566F")
    return t


def _glass_tex():
    """窗玻璃：上亮下暗的天空反光 + 两道斜向高光（像素化）。"""
    w, h = 12, 16
    t = np.ones((h, w, 4), np.float32)
    top, bot = np.array(hex3("#BFE0F2"), np.float32), np.array(hex3("#7FA9C4"), np.float32)
    for j in range(h):
        t[j, :, :3] = top * (1 - j / h) + bot * (j / h)
    for j in range(h):
        for i in range(w):
            if (i + j) % 11 in (0, 1) and j < 12:
                t[j, i, :3] = np.minimum(1, t[j, i, :3] + 0.12)
    return t


def _band_tex(base, win, w, h, rows):
    t = np.ones((h, w, 4), np.float32)
    t[..., :3] = hex3(base)
    for j in range(rows[0], rows[1]):
        for i in range(1, w - 1):
            if i % 3 != 0:
                t[j, i, :3] = hex3(win)
    return t


def _field_tex(base="#9CC46F", row="#86B05B", seed=110):
    rng = np.random.RandomState(seed)
    t = np.ones((16, 16, 4), np.float32)
    t[..., :3] = np.clip(np.array(hex3(base), np.float32) + (rng.rand(16, 16, 1) - 0.5) * 0.06, 0, 1)
    for j in range(0, 16, 3):
        t[j, :, :3] = hex3(row)
    return t


def _roof_tex():
    t = np.ones((8, 16, 4), np.float32)
    t[..., :3] = hex3("#3B4146")
    for j in range(0, 8, 2):
        t[j, :, :3] = hex3("#2A2F33")
    return t


def _bin_tex():
    """蓝色收纳箱侧面：箱体 + 把手凹槽 + 白色标签（无字）。"""
    rows = ["BBBBBBBBBBBB", "BbbbbbbbbbbB", "BBBBddddBBBB", "BBBBBBBBBBBB", "BBBWWWWWWBBB", "BBBWggggWBBB",
            "BBBWWWWWWBBB", "BBBBBBBBBBBB", "bbbbbbbbbbbb"]
    return ascii_tex(rows, {"B": "#5C9BD1", "b": "#4A86BB", "d": "#3C6F9C", "W": "#FBF8F1", "g": "#B4BABF"})


def _open_sea_tex():
    """开阔海面：块状深浅两色波纹 + 稀疏浪花，比港内海面更安静。"""
    rng = np.random.RandomState(17)
    w = h = 32
    t = np.ones((h, w, 4), np.float32)
    b = np.array(hex3("#4F97BE"), np.float32)
    t[..., :3] = np.clip(b + (rng.rand(h, w, 1) - 0.5) * 0.035, 0, 1)
    for _ in range(26):
        y, x = rng.randint(0, h), rng.randint(0, w - 6)
        t[y, x:x + rng.randint(3, 7), :3] = hex3("#62A9CD")
    for _ in range(9):
        y, x = rng.randint(0, h), rng.randint(0, w - 3)
        t[y, x:x + rng.randint(1, 3), :3] = hex3("#CDE7F1")
    return t


def _sheet_tex():
    """独立的签收/核验单（无可读文字，只有结构线和签名栏）。"""
    w, h = 30, 40
    t = np.ones((h, w, 4), np.float32)
    t[..., :3] = hex3("#FBF9F3")
    g = np.array(hex3("#B9C0C4"), np.float32)
    t[3:5, 4:20, :3] = hex3("#7E8A92")
    for j in range(9, 27, 3):
        t[j, 4:26, :3] = g
    t[33, 14:27, :3] = hex3("#7E8A92")      # 签名栏
    return t


# ------------------------------------------------------------------ 通用
def grid_plane(sc, x0, x1, z0, z1, y, tex, nx, nz, **kw):
    """水平大平面切成 nx×nz 块，每块贴一次贴图（避免超大平铺贴图占内存）。"""
    xs = np.linspace(x0, x1, nx + 1)
    zs = np.linspace(z0, z1, nz + 1)
    for i in range(nx):
        for k in range(nz):
            v = [(xs[i], y, zs[k + 1]), (xs[i + 1], y, zs[k + 1]), (xs[i + 1], y, zs[k]), (xs[i], y, zs[k])]
            sc.append(Quad(v, tex, (0, 1, 0), **kw))


def sky(w, h, top="#6FB2E6", bot="#EAF3F4", gamma=0.9):
    a, b = np.array(hex3(top), np.float32), np.array(hex3(bot), np.float32)
    g = np.linspace(0, 1, h, dtype=np.float32)[:, None, None] ** gamma
    return np.broadcast_to(a * (1 - g) + b * g, (h, w, 3)).copy()


def cloud(sc, cx, cy, cz, w, seed=0, **kw):
    """方块云：几层错落的白色长方体（自发光，不受雾以外的光影响）。"""
    rng = np.random.RandomState(seed)
    kw.setdefault("emissive", True)
    T = S.TEX["cloud"]
    box(sc, (cx, cy, cz), (cx + w, cy + w * 0.10, cz + w * 0.35), T, **kw)
    for k in range(3):
        a = rng.rand() * 0.5
        b = a + 0.25 + rng.rand() * 0.3
        box(sc, (cx + w * a, cy + w * 0.10, cz + w * 0.05), (cx + w * min(b, 0.95), cy + w * (0.17 + 0.04 * k), cz + w * 0.3), T, **kw)


def ship(sc, sx, sz, sea=SEA, scale=1.0, seed=4, **kw):
    """货轮：船首朝 +X（向右行驶），船尾在左侧带白色驾驶楼。sx 为船首 x。"""
    rng = np.random.RandomState(seed)
    L = 57000 * scale
    Wd = 11000 * scale
    x0 = sx - L
    hy = sea + 5200 * scale
    box(sc, (x0, sea, sz), (sx - 3000 * scale, hy, sz + Wd), X["hull"], **kw)
    # 船首收窄
    box(sc, (sx - 3000 * scale, sea + 800 * scale, sz + 1500 * scale), (sx, hy, sz + Wd - 1500 * scale), X["hull"], **kw)
    box(sc, (x0, hy, sz), (sx - 3000 * scale, hy + 500 * scale, sz + Wd), X["hull_red"], **kw)
    for i in range(7):
        for lv in range(rng.randint(2, 5)):
            t = S.TEX["cont%d" % rng.randint(0, 6)]
            box(sc, (x0 + 9000 * scale + i * 6500 * scale, hy + 500 * scale + lv * 2600 * scale, sz + 2000 * scale),
                (x0 + 15000 * scale + i * 6500 * scale, hy + 3100 * scale + lv * 2600 * scale, sz + 9000 * scale), t, **kw)
    # 驾驶楼（船尾，左侧）
    bx0, bx1 = x0 + 1500 * scale, x0 + 7500 * scale
    box(sc, (bx0, hy + 500 * scale, sz + 1500 * scale), (bx1, hy + 13000 * scale, sz + 9500 * scale),
        {"all": X["bridge_win"], "top": X["bridge"]}, **kw)
    box(sc, (bx0 - 800 * scale, hy + 11500 * scale, sz + 500 * scale), (bx1 + 800 * scale, hy + 12200 * scale, sz + 10500 * scale), X["bridge"], **kw)
    box(sc, (bx0 + 2200 * scale, hy + 13000 * scale, sz + 4500 * scale), (bx0 + 3600 * scale, hy + 16000 * scale, sz + 6000 * scale), X["hull_red"], **kw)


def wake(sc, sx, sz, T, sea=SEA, scale=1.0, n=26):
    """船尾白色航迹：沿两条外扩的 V 形线散布的小泡沫方块，随时间闪动。"""
    L = 57000 * scale
    cz = sz + 5500 * scale
    rng = np.random.RandomState(int(T * 6) % 97)
    for k in range(n):
        d = 1200 + k * 2600
        x = sx - L - d * scale
        sz_ = (1500 - 40 * k) * scale
        for side in (-1, 0, 1):
            if side == 0 and k > 8:
                continue
            z = cz + side * (2500 + 0.22 * d) * scale + (rng.rand() - 0.5) * 1800 * scale
            xx = x + (rng.rand() - 0.5) * 1500 * scale
            w = sz_ * (0.6 + 0.6 * rng.rand())
            sc.append(Quad([(xx - w, sea + 20, z + w * 0.5), (xx + w, sea + 20, z + w * 0.5),
                            (xx + w, sea + 20, z - w * 0.5), (xx - w, sea + 20, z - w * 0.5)],
                           X["foam"], (0, 1, 0), outdoor=True))


def gull(sc, p, T, span=900, phase=0.0, **kw):
    """方块海鸥：身体 + 两片上下拍动的翅膀。朝 +X 飞。"""
    p = np.asarray(p, np.float64)
    s = span / 900
    box(sc, p + np.array([-180, -60, -60]) * s, p + np.array([200, 60, 60]) * s, X["gull"], **kw)
    box(sc, p + np.array([200, -30, -40]) * s, p + np.array([260, 20, 40]) * s, S.TEX["yellow"], **kw)
    flap = math.sin(T * 9 + phase) * 0.6
    for side in (-1, 1):
        a = p + np.array([-60, 30, side * 60]) * s
        b = a + np.array([0, flap * 380 * s, side * 420 * s])
        c = b + np.array([180 * s, 0, 0])
        d = a + np.array([180 * s, 0, 0])
        sc.append(Quad([a, d, c, b], X["gull"], (0, 1, 0), double=True, **kw))
        tip = b + np.array([0, flap * 140 * s, side * 160 * s])
        sc.append(Quad([b, c, c + (tip - b), tip], X["gull_d"], (0, 1, 0), double=True, **kw))


def harbor(sc, T, ox=0.0, oz=0.0, n_cont=22, cranes=3):
    """码头：堆场集装箱 + 门式起重机（远景）。原点在码头前沿左端。"""
    rng = np.random.RandomState(4)
    kw = dict(outdoor=True)
    box(sc, (ox - 20000, SEA, oz), (ox + 180000, SEA + 2200, oz + 60000), S.TEX["quay"], **kw)
    qy = SEA + 2200
    for i in range(n_cont):
        bx = ox + i * 6600
        for lv in range(rng.randint(1, 5)):
            t = S.TEX["cont%d" % rng.randint(0, 6)]
            box(sc, (bx, qy + lv * 2600, oz + 14000), (bx + 6100, qy + 2600 + lv * 2600, oz + 16500), t, **kw)
    for k in range(cranes):
        cx = ox + 20000 + k * 26000
        t = S.TEX["crane" if k % 2 == 0 else "crane_b"]
        for dx in (-6300, 6300):
            box(sc, (cx + dx - 700, qy, oz + 3500), (cx + dx + 700, qy + 26000, oz + 4900), t, **kw)
        box(sc, (cx - 7000, qy + 24200, oz - 18000), (cx + 7000, qy + 26000, oz + 6000), t, **kw)
        tz = oz - 6000 + 5000 * math.sin(T * 0.55 + k * 2.1)
        box(sc, (cx - 1300, qy + 22800, tz - 1300), (cx + 1300, qy + 24200, tz + 1300), S.TEX["frame"], **kw)
        drop = 6000 + 3000 * math.sin(T * 0.8 + k * 1.3)
        box(sc, (cx - 150, qy + 22800 - drop, tz - 150), (cx + 150, qy + 22800, tz + 150), S.TEX["dark"], **kw)
        box(sc, (cx - 1250, qy + 20300 - drop, tz - 3000), (cx + 1250, qy + 22800 - drop, tz + 3000),
            S.TEX["cont%d" % ((k * 2 + 3) % 6)], **kw)


# ------------------------------------------------------------------ 镜 03：汉堡办公楼外立面
HERO = (-810.0, 650.0, 2300.0)      # 主窗中心 x、窗台 y、窗顶 y（室内地面 y=0）
FLOOR_H = 3300.0
COL_DX = 2400.0
WIN_W = 1140.0
FACADE_RIGHT = 2600.0


def build_facade(sc, open_amt=1.0):
    """外立面位于 z=0（朝 -Z），楼体在右缘 x=FACADE_RIGHT 处收角。主窗打开，室内可见。"""
    init()
    F = X["facade"]
    hx, sill, top = HERO
    rows = range(-3, 3)
    cols = range(-9, 2)
    xl = hx + cols[0] * COL_DX - COL_DX / 2
    # 整体背板（远处 / 下方），细节只做镜头附近
    box(sc, (xl - 60000, SEA, 0), (FACADE_RIGHT, -3 * FLOOR_H - 1000, 20000),
        {"front": tiled(F, 30, 20), "right": tiled(F, 10, 20), "top": F})
    box(sc, (FACADE_RIGHT - 400, 3 * FLOOR_H - 1000, -200), (FACADE_RIGHT + 200, 3 * FLOOR_H + 500, 20000), X["facade_d"])
    box(sc, (xl - 60000, 3 * FLOOR_H - 1000, 0), (FACADE_RIGHT - 400, 3 * FLOOR_H + 300, 20000),
        {"front": tiled(F, 30, 1), "top": X["facade_d"]})
    box(sc, (FACADE_RIGHT, -3 * FLOOR_H - 1000, 0), (FACADE_RIGHT + 1, 3 * FLOOR_H + 300, 20000), F)
    # 左侧远处无细节墙
    box(sc, (xl - 60000, -3 * FLOOR_H - 1000, 0), (xl, 3 * FLOOR_H - 1000, 20000), {"front": tiled(F, 30, 8)})
    for r in rows:
        y0 = r * FLOOR_H
        ws, wt = y0 + sill, y0 + top
        # 窗下墙带 + 楼层装饰线
        box(sc, (xl, y0 + top - FLOOR_H, 0), (FACADE_RIGHT, ws, 300), {"front": tiled(F, 12, 1), "top": F, "bottom": F})
        box(sc, (xl, ws - 120, -90), (FACADE_RIGHT, ws, 0), X["facade_d"])
        # 窗间墙
        xs = [hx + c * COL_DX for c in cols]
        prev = xl
        for c, xc in zip(cols, xs):
            box(sc, (prev, ws, 0), (xc - WIN_W / 2, wt, 300), {"front": F, "left": X["facade_d"], "right": X["facade_d"]})
            prev = xc + WIN_W / 2
        box(sc, (prev, ws, 0), (FACADE_RIGHT, wt, 300), {"front": F, "left": X["facade_d"]})
        for c, xc in zip(cols, xs):
            hero = (r == 0 and c == 0)
            x0, x1 = xc - WIN_W / 2, xc + WIN_W / 2
            # 窗洞顶面、底面（窗台）
            box(sc, (x0, wt - 1, 0), (x1, wt, 300), X["facade_d"])
            box(sc, (x0 - 60, ws - 40, -160), (x1 + 60, ws, 300), X["mullion"])
            if hero:
                continue
            m = X["mullion"]
            box(sc, (x0, ws, 200), (x1, ws + 50, 240), m)
            box(sc, (x0, wt - 50, 200), (x1, wt, 240), m)
            box(sc, (x0, ws, 200), (x0 + 50, wt, 240), m)
            box(sc, (x1 - 50, ws, 200), (x1, wt, 240), m)
            box(sc, (xc - 22, ws, 205), (xc + 22, wt, 235), m)
            sc.append(Quad([(x0, wt, 220), (x1, wt, 220), (x1, ws, 220), (x0, ws, 220)], X["glass"], (0, 0, -1), bright=1.0))
    # ---- 主窗：两扇窗向外打开 + 室内
    x0, x1 = hx - WIN_W / 2, hx + WIN_W / 2
    m = X["mullion"]
    ang = 70 * open_amt
    for side, hinge in ((-1, x0), (1, x1)):
        R = rot_y(side * ang)
        xf = Xf(R, (hinge, 0, 200))
        w = WIN_W / 2
        lo, hi = (0, sill, -20), (w, top, 20)
        if side == 1:
            lo, hi = (-w, sill, -20), (0, top, 20)
        a, b = lo[0], hi[0]
        box(sc, (a, sill, -20), (b, sill + 45, 20), m, xf)
        box(sc, (a, top - 45, -20), (b, top, 20), m, xf)
        box(sc, (a, sill, -20), (a + 45, top, 20), m, xf)
        box(sc, (b - 45, sill, -20), (b, top, 20), m, xf)
        sc.append(Quad(xf.pt([(a + 45, top - 45, 0), (b - 45, top - 45, 0), (b - 45, sill + 45, 0), (a + 45, sill + 45, 0)]),
                       X["glass"], xf.vec((0, 0, -1)), double=True))
    # 室内：后墙、侧墙、地面、天花，后墙上挂地图与展示框
    Z1 = 4200
    iw = X["inwall"]
    box(sc, (x0 - 1800, -60, 300), (x1 + 1800, 0, Z1), tiled(S.TEX["floor"], 4, 4))
    box(sc, (x0 - 1800, 0, Z1), (x1 + 1800, 3000, Z1 + 100), tiled(iw, 6, 6))
    box(sc, (x0 - 1900, 0, 300), (x0 - 1800, 3000, Z1), tiled(iw, 6, 6))
    box(sc, (x1 + 1800, 0, 300), (x1 + 1900, 3000, Z1), tiled(iw, 6, 6))
    box(sc, (x0 - 1800, 2900, 300), (x1 + 1800, 3000, Z1), tiled(iw, 6, 6))
    sc.append(Quad([(hx - 300, 1900, Z1 - 4), (hx + 260, 1900, Z1 - 4), (hx + 260, 1640, Z1 - 4), (hx - 300, 1640, Z1 - 4)],
                   S.TEX["map"], (0, 0, -1)))
    for i, k in enumerate(["doc", "box", "ship"]):
        fx0 = hx - 260 + i * 195
        box(sc, (fx0, 1400, Z1 - 16), (fx0 + 150, 1550, Z1), {"front": S.TEX["frame_" + k], "all": solid(hex3("#9A6B3F"))})
    # 室内窗台上的盆栽（左侧）
    px = x0 + 160
    box(sc, (px - 70, sill, 260), (px + 70, sill + 120, 400), S.TEX["pot"])
    for (dx, dy, dz, s, k) in [(0, 120, 0, 80, "leaf"), (-70, 190, 10, 80, "leaf2"), (60, 190, -10, 80, "leaf"),
                               (-10, 270, 0, 70, "leaf2"), (80, 260, 20, 60, "leaf2"), (-90, 270, 20, 60, "leaf")]:
        box(sc, (px + dx - s / 2, sill + dy, 330 + dz - s / 2), (px + dx + s / 2, sill + dy + s, 330 + dz + s / 2), S.TEX[k])
    # 室内：窗内侧的书桌一角
    box(sc, (x1 - 200, 740, 900), (x1 + 1400, 780, 1700), S.TEX["desk"])


def build_hamburg_exterior(sc, T):
    """镜 03 的海港远景：在楼体右侧可见的海面、码头和自左向右行驶的货轮。"""
    init()
    grid_plane(sc, -200000, 900000, -60000, 1500000, SEA, S.TEX["sea"], 22, 26, outdoor=True)
    # 楼前小码头地面
    box(sc, (-80000, SEA, -9000), (FACADE_RIGHT + 9000, SEA + 1500, 20000), S.TEX["quay"], outdoor=True)
    harbor(sc, T, ox=45000, oz=300000, n_cont=24)
    sx = 115000 + 2600 * T
    ship(sc, sx, 165000, outdoor=True)
    wake(sc, sx, 165000, T)
    for (cx, cy, cz, w, sd) in [(-30000, 60000, 700000, 70000, 1), (90000, 85000, 760000, 52000, 2),
                                (200000, 58000, 640000, 40000, 3), (300000, 95000, 820000, 60000, 4)]:
        cloud(sc, cx, cy, cz, w, seed=sd)


def sea_plane(sc, cam_x, cam_z, y=SEA, z_far=1600000, base=9000, d0=40000.0, half=1.25, tex=None):
    """开阔海面：按离镜头的距离逐行加大方块尺寸（远处像素不至于过密），网格与世界对齐、随镜头平移不抖动。"""
    tex = S.TEX["sea"] if tex is None else tex
    z = cam_z - 20000
    while z < z_far:
        ts = base * max(1.0, (z - cam_z) / d0)
        ts = base * 2 ** int(round(math.log2(ts / base)))
        z = math.floor(z / ts) * ts
        z1 = z + ts
        W = half * max(z1 - cam_z, 0) + 60000
        xa = math.floor((cam_x - W) / ts) * ts
        xb = math.ceil((cam_x + W) / ts) * ts
        for x in np.arange(xa, xb, ts):
            sc.append(Quad([(x, y, z1), (x + ts, y, z1), (x + ts, y, z), (x, y, z)], tex, (0, 1, 0), outdoor=True))
        z = z1


# ------------------------------------------------------------------ 镜 05：货轮甲板（甲板 y=0，船身沿 Z 方向，左舷栏杆在 x=RAIL_X）
RAIL_X = -2600.0
DECK_SEA = -14000.0


def build_deck(sc, cam_x, cam_z, T):
    init()
    # 甲板、左舷边缘、栏杆
    grid_plane(sc, RAIL_X, 30000, -9000, 60000, 0.0, X["deck"], 8, 14)
    box(sc, (RAIL_X - 120, -900, -9000), (RAIL_X, 0, 60000), {"all": X["hull_red"], "top": X["rail"]})
    box(sc, (RAIL_X - 120, -9000, -9000), (RAIL_X, -900, 60000), X["hull"])
    for z in np.arange(-9000, 60000, 1400):
        box(sc, (RAIL_X + 10, 0, z), (RAIL_X + 70, 1080, z + 60), X["rail"])
    for y in (1050, 560):
        box(sc, (RAIL_X - 10, y, -9000), (RAIL_X + 80, y + (50 if y > 600 else 34), 60000), X["rail"])
    # 系缆桩、救生圈箱（无字）
    for z in (-2500, 6500):
        box(sc, (RAIL_X + 300, 0, z), (RAIL_X + 700, 380, z + 400), X["wheel"])
        box(sc, (RAIL_X + 250, 380, z - 50), (RAIL_X + 750, 440, z + 450), X["wheel"])
    box(sc, (RAIL_X + 120, 0, 9200), (RAIL_X + 560, 600, 9600), {"all": S.TEX["yellow"], "top": X["rail"]})
    # 甲板上的集装箱（身后），多排错落
    rng = np.random.RandomState(12)
    for row, z0 in enumerate((4600, 10800, 17000)):
        for i in range(6):
            x0 = 700 + i * 2650
            for lv in range(rng.randint(2, 5) if row else rng.randint(1, 4)):
                t = S.TEX["cont%d" % rng.randint(0, 6)]
                box(sc, (x0, lv * 2600, z0), (x0 + 2440, (lv + 1) * 2600, z0 + 6000), t)
    # 驾驶楼（船尾，远处左侧）
    bz = 34000
    box(sc, (RAIL_X + 400, 0, bz), (9000, 15000, bz + 8000), {"all": X["bridge_win"], "top": X["bridge"]}, outdoor=True)
    box(sc, (RAIL_X - 600, 13600, bz - 800), (10000, 14300, bz + 9000), X["bridge"], outdoor=True)
    box(sc, (1800, 15000, bz + 2500), (3600, 19000, bz + 4500), X["hull_red"], outdoor=True)
    # 海面与远处
    sea_plane(sc, cam_x, cam_z, y=DECK_SEA, base=6000, d0=30000, tex=X["sea_open"])
    for (cx, cy, cz, w, sd) in [(-260000, 30000, 700000, 80000, 21), (-120000, 52000, 900000, 110000, 22),
                                (-60000, 26000, 500000, 50000, 23), (40000, 70000, 800000, 90000, 24)]:
        cloud(sc, cx, cy, cz, w, seed=sd)


# ------------------------------------------------------------------ 镜 06：上海口岸单证核验办公室（地面 y=0，Mei 面朝 -Z）
OFF_DESK_Y = 740.0
OFF_WALL_Z = 2000.0


def build_port_office(sc, T):
    init()
    W = X["office_wall"]
    zw = OFF_WALL_Z
    box(sc, (-5000, -60, -5000), (5000, 0, zw), tiled(X["office_floor"], 5, 4))
    # 后墙：左侧大窗洞（看见港口）
    wx0, wx1, wy0, wy1 = -3300.0, -300.0, 950.0, 2750.0
    box(sc, (-5000, 0, zw), (wx0, 3200, zw + 200), tiled(W, 3, 3))
    box(sc, (wx1, 0, zw), (5000, 3200, zw + 200), tiled(W, 5, 3))
    box(sc, (wx0, 0, zw), (wx1, wy0, zw + 200), tiled(W, 3, 1))
    box(sc, (wx0, wy1, zw), (wx1, 3200, zw + 200), W)
    m = X["mullion"]
    box(sc, (wx0 - 40, wy0 - 60, zw - 160), (wx1 + 40, wy0, zw + 200), m)
    for x in (wx0, (wx0 + wx1) / 2 - 25, wx1 - 50):
        box(sc, (x, wy0, zw + 60), (x + 50, wy1, zw + 120), m)
    box(sc, (wx0, wy1 - 50, zw + 60), (wx1, wy1, zw + 120), m)
    box(sc, (wx0, 1900, zw + 60), (wx1, 1950, zw + 120), m)
    # 窗台盆栽
    px = wx1 - 380
    box(sc, (px - 90, wy0, zw - 150), (px + 90, wy0 + 160, zw + 30), S.TEX["pot"])
    for (dx, dy, dz, s, k) in [(0, 160, 0, 110, "leaf"), (-90, 250, 10, 100, "leaf2"), (80, 250, -10, 100, "leaf"),
                               (-10, 350, 0, 90, "leaf2"), (100, 340, 20, 80, "leaf2")]:
        box(sc, (px + dx - s / 2, wy0 + dy, -60 + zw + dz - s / 2), (px + dx + s / 2, wy0 + dy + s, -60 + zw + dz + s / 2), S.TEX[k])
    # 右侧墙面：文件柜 + 墙上像素港口挂图
    for i in range(2):
        box(sc, (600 + i * 820, 0, zw - 560), (1380 + i * 820, 1150, zw), {"front": X["cabinet"], "all": X["lid"]})
    sc.append(Quad([(700, 2500, zw - 2), (1700, 2500, zw - 2), (1700, 1800, zw - 2), (700, 1800, zw - 2)],
                   S.TEX["map"], (0, 0, -1)))
    # 书桌
    y = OFF_DESK_Y
    box(sc, (-1350, y - 45, -820), (1150, y, 30), {"top": tiled(X["desk2"], 3, 1), "all": S.TEX["desk_side"]})
    for x in (-1300, 1080):
        box(sc, (x, 0, -780), (x + 50, y - 45, -20), S.TEX["desk_side"])
    box(sc, (-1300, 120, -60), (1130, y - 45, -20), S.TEX["desk_side"])
    # 桌上：笔筒、迷你集装箱摆件、水杯
    box(sc, (850, y, -250), (950, y + 140, -150), X["lid"])
    for k, c in enumerate(("cont2", "cont4")):
        box(sc, (820 - k * 10, y + k * 70, -620), (1010 - k * 10, y + 70 + k * 70, -545), S.TEX[c])
    box(sc, (-1150, y, -200), (-1060, y + 110, -110), S.TEX["mug"])
    # 窗外：上海港（远景）
    grid_plane(sc, -400000, 400000, 20000, 1500000, SEA, S.TEX["sea"], 16, 20, outdoor=True)
    harbor(sc, T, ox=-150000, oz=150000, n_cont=30, cranes=4)
    box(sc, (-400000, SEA, 260000), (400000, SEA + 3000, 300000), S.TEX["quay"], outdoor=True)
    for (cx, cy, cz, w, sd) in [(-250000, 60000, 800000, 90000, 31), (-80000, 90000, 900000, 70000, 32)]:
        cloud(sc, cx, cy, cz, w, seed=sd)


# ------------------------------------------------------------------ 铁路：轨道、接触网、货运列车（车头朝 +X）
def track(sc, x0, x1, zc, ry, sl0=None, sl1=None, s=1.0, **kw):
    """一条铁轨：道砟、枕木、两根钢轨。ry 为轨顶高度，s 为整体比例。枕木只在 [sl0, sl1] 内生成。"""
    for x in np.arange(x0, x1, 5000.0):
        box(sc, (x, ry - 650 * s, zc - 1900 * s), (min(x + 5000, x1), ry - 200 * s, zc + 1900 * s), X["gravel"],
            skip=("bottom", "back"), **kw)
    a = x0 if sl0 is None else max(x0, sl0)
    b = x1 if sl1 is None else min(x1, sl1)
    st = 650 * s
    for x in np.arange(a - (a % st), b, st):
        box(sc, (x, ry - 200 * s, zc - 1300 * s), (x + 260 * s, ry - 70 * s, zc + 1300 * s), X["sleeper"], skip=("bottom",), **kw)
    for k in (-1, 1):
        box(sc, (x0, ry - 70 * s, zc + (k * 717 - 36) * s), (x1, ry, zc + (k * 717 + 36) * s), X["steel"], skip=("bottom",), **kw)


def catenary(sc, x0, x1, zc, ry, pole_z, step=18000.0, phase=0.0, wire=5700.0, s=1.0, **kw):
    """接触网：立柱 + 腕臂 + 承力索/接触线（细长方块）。"""
    st = X["steel"]
    dk = solid(hex3("#3A4248"))
    for px in np.arange(x0 - ((x0 - phase) % step), x1, step):
        box(sc, (px - 110 * s, ry - 400 * s, pole_z - 110 * s), (px + 110 * s, ry + wire + 900 * s, pole_z + 110 * s), st, **kw)
        za, zb = sorted((pole_z, zc))
        box(sc, (px - 40 * s, ry + wire + 600 * s, za - 300 * s), (px + 40 * s, ry + wire + 680 * s, zb + 300 * s), st, **kw)
        box(sc, (px - 30 * s, ry + wire, zc - 30 * s), (px + 30 * s, ry + wire + 640 * s, zc + 30 * s), st, **kw)
    box(sc, (x0, ry + wire - 14 * s, zc - 14 * s), (x1, ry + wire + 14 * s, zc + 14 * s), dk, **kw)
    box(sc, (x0, ry + wire + 560 * s, zc - 14 * s), (x1, ry + wire + 590 * s, zc + 14 * s), dk, **kw)


def train(sc, head_x, zc, ry, n_wag=5, seed=7, wire=5700.0, s=1.0, **kw):
    """电力货运机车（蓝白）+ n 节平车，每节载两个 20 尺集装箱。车头朝 +X。
    几何在以车头前端/轨顶/中心线为原点的局部坐标里描述（单位按 s 缩放）。"""
    rng = np.random.RandomState(seed)

    def B(lo, hi, tex, **k):
        k2 = dict(kw); k2.update(k)
        box(sc, (head_x + lo[0] * s, ry + lo[1] * s, zc + lo[2] * s), (head_x + hi[0] * s, ry + hi[1] * s, zc + hi[2] * s), tex, **k2)

    L = 10500.0
    hw = 1500.0
    wh = X["wheel"]
    B((-L + 300, 260, -1250), (-300, 1100, 1250), wh)
    for bx in (-L + 1700, -1700):
        B((bx - 1300, 0, -1360), (bx + 1300, 820, 1360), wh)
        for k in (-1, 1):
            B((bx + k * 650 - 420, 40, -1400), (bx + k * 650 + 420, 880, -1360), X["steel"])
    B((-L, 1100, -hw), (-2600, 4200, hw), {"all": X["loco_side"], "left": X["loco"], "right": X["loco"], "top": X["loco_d"]})
    B((-2600, 1100, -hw), (0, 2600, hw), {"all": X["loco"], "top": X["loco_d"]})
    B((-2600, 2600, -hw), (0, 3700, hw), {"all": X["loco_win"], "top": X["loco_d"]})
    B((-2600, 3700, -hw), (0, 4200, hw), {"all": X["loco"], "top": X["loco_d"]})
    B((-L - 8, 1650, -hw - 8), (8, 1880, hw + 8), X["rail"], skip=("top", "bottom"))
    for k in (-1, 1):
        B((0, 1350, k * 950 - 160), (40, 1550, k * 950 + 160), S.TEX["yellow"], emissive=True)
    B((-L + 1600, 4200, -900), (-L + 5200, 4560, 900), X["steel"])
    # 受电弓：两段斜杆 + 顶部滑板，贴住接触线
    w_ = wire / s
    pb = np.array([head_x + (-L + 3000) * s, ry + 4560 * s, zc])
    pm = pb + np.array([700.0 * s, (w_ - 4560) * 0.5 * s, 0])
    pt = np.array([pb[0], ry + wire - 30 * s, zc])
    S.stick(sc, pb, pm, 45 * s, X["steel"])
    S.stick(sc, pm, pt, 40 * s, X["steel"])
    B((-L + 2850, w_ - 60, -520), (-L + 3150, w_ - 30, 520), X["steel"])
    # 平车 + 集装箱
    wl = 13400.0
    x = -L - 700
    for i in range(n_wag):
        a, b = x - wl, x
        B((a, 900, -1400), (b, 1250, 1400), X["wagon"])
        for bx in (a + 1800, b - 1800):
            B((bx - 1200, 0, -1300), (bx + 1200, 900, 1300), wh)
        B((b, 950, -200), (b + 700, 1150, 200), wh)
        for k in range(2):
            ca = a + 450 + k * 6300
            B((ca, 1250, -1220), (ca + 6050, 3850, 1220), S.TEX["cont%d" % rng.randint(0, 6)])
        x = a - 700
    return head_x - L * s


# ------------------------------------------------------------------ 镜 07：铁路货场站台（站台面 y=0，轨道在 Chen 身后）
RAIL_RY = -750.0       # 轨顶
RAIL_ZC = 3300.0       # 列车中心线
RAIL_Z2 = 8300.0       # 第二股道
TS = 0.8              # 列车/轨道相对人物的比例（方块人物偏矮）


def build_rail_yard(sc, T, dx=0.0):
    init()
    # 站台：混凝土方块 + 黄色安全线
    box(sc, (-30000, -1100, -9000), (40000, 0, 1300), X["platform"], skip=("top", "bottom"))
    grid_plane(sc, -30000, 40000, -9000, 1300, 0.0, X["platform"], 70, 10)
    box(sc, (-30000, -1100, 1300), (40000, 6, 1500), {"top": X["edge"], "all": X["stone"]})
    for x in np.arange(-30000, 40000, 2000):
        box(sc, (x + 980, -1100, 1300), (x + 1020, 0, 1500), X["stone"])
    # 站台上的手推车 + 牛皮纸箱
    cx, cz = -5600.0, -1400.0
    box(sc, (cx - 500, 280, cz - 350), (cx + 500, 340, cz + 350), X["steel"])
    for k in (-1, 1):
        box(sc, (cx + k * 420 - 40, 0, cz + k * 260 - 40), (cx + k * 420 + 40, 280, cz + k * 260 + 40), X["wheel"])
    box(sc, (cx + 440, 340, cz - 350), (cx + 500, 1000, cz + 350), X["steel"])
    for (ax, ay, az, s) in [(-420, 340, -300, 420), (40, 340, -280, 380), (-380, 760, -260, 360)]:
        box(sc, (cx + ax, ay, cz + az), (cx + ax + s, ay + s, cz + az + s), {"all": X["clip"], "top": S.TEX["desk_side"]})
    # 道砟面、两股道
    grid_plane(sc, -60000, 80000, 1500, 13000, RAIL_RY - 650 * TS, X["gravel"], 35, 4)
    s = TS
    track(sc, -60000, 80000, RAIL_ZC, RAIL_RY, sl0=-14000, sl1=22000, s=s)
    track(sc, -60000, 80000, RAIL_Z2, RAIL_RY, sl0=-30000, sl1=40000, s=s, outdoor=True)
    catenary(sc, -60000, 80000, RAIL_ZC, RAIL_RY, pole_z=5800.0, phase=-9000, wire=5700 * s, s=s)
    catenary(sc, -60000, 80000, RAIL_Z2, RAIL_RY, pole_z=5800.0, phase=-9000, wire=5700 * s, s=s)
    train(sc, 2200 + dx, RAIL_ZC, RAIL_RY, n_wag=4, seed=7, wire=5700 * s, s=s)
    train(sc, 46000, RAIL_Z2, RAIL_RY, n_wag=6, seed=19, wire=5700 * s, s=s, outdoor=True)
    # 远处：货场堆箱 + 门式起重机 + 树 + 山
    gy = RAIL_RY - 650 * TS
    grid_plane(sc, -300000, 400000, 13000, 700000, gy, X["grass"], 14, 14, outdoor=True)
    rng = np.random.RandomState(31)
    for i in range(18):
        bx = -50000 + i * 6600
        for lv in range(rng.randint(1, 4)):
            for row in range(2):
                t = S.TEX["cont%d" % rng.randint(0, 6)]
                box(sc, (bx, gy + lv * 2600, 20000 + row * 2600), (bx + 6100, gy + (lv + 1) * 2600, 22440 + row * 2600), t, outdoor=True)
    for gx in (-38000, -4000):
        for z in (36000, 47000):
            box(sc, (gx - 500, gy, z - 500), (gx + 500, gy + 17000, z + 500), S.TEX["crane"], outdoor=True)
        box(sc, (gx - 700, gy + 15500, 35500), (gx + 700, gy + 17200, 47500), S.TEX["crane_b"], outdoor=True)
        box(sc, (gx - 1300, gy + 14500, 39500), (gx + 1300, gy + 15500, 41500), S.TEX["frame"], outdoor=True)
    for i in range(26):
        tx = -90000 + i * 8000 + rng.rand() * 3000
        tz = 52000 + rng.rand() * 9000
        h = 5000 + rng.rand() * 3000
        box(sc, (tx - 300, gy, tz - 300), (tx + 300, gy + h * 0.45, tz + 300), X["trunk"], outdoor=True)
        box(sc, (tx - 2200, gy + h * 0.4, tz - 2200), (tx + 2200, gy + h, tz + 2200), X["tree" if i % 2 else "tree2"], outdoor=True)
    for (ix, iz, iw, ih) in [(-260000, 420000, 200000, 26000), (-20000, 480000, 260000, 36000), (240000, 440000, 180000, 22000)]:
        box(sc, (ix, gy, iz), (ix + iw, gy + ih, iz + 40000), X["hill"], outdoor=True)
        box(sc, (ix + iw * 0.3, gy + ih, iz), (ix + iw * 0.7, gy + ih * 1.5, iz + 40000), X["hill2"], outdoor=True)
    for (cx_, cy, cz_, w, sd) in [(-180000, 60000, 600000, 90000, 41), (-30000, 85000, 700000, 120000, 42),
                                  (150000, 55000, 560000, 80000, 43)]:
        cloud(sc, cx_, cy, cz_, w, seed=sd)


# ------------------------------------------------------------------ 镜 08：江南水乡铁路（轨顶 y=0，线路沿 X，镜头在 -Z 侧跟拍）
JN_GY = -1250.0          # 田地地面
JN_CELL = 16000.0


def _jn_house(sc, x0, z0, w, d, h, rng, **kw):
    """白墙黛瓦民居：白墙主体 + 阶梯式双坡屋顶 + 两端马头墙。"""
    gy = JN_GY
    W, R = X["whitewall"], X["roof"]
    box(sc, (x0, gy, z0), (x0 + w, gy + h, z0 + d), {"all": W, "top": R}, **kw)
    # 门窗（深色方块嵌在正面）
    for k in range(int(w // 2600)):
        wx = x0 + 900 + k * 2600
        box(sc, (wx, gy + h * 0.45, z0 - 30), (wx + 700, gy + h * 0.45 + 800, z0), solid(hex3("#3B4146")), **kw)
    # 屋顶：三级台阶收分
    for k in range(3):
        inset = k * d * 0.16
        box(sc, (x0 - 150, gy + h + k * 420, z0 + inset - (150 if k == 0 else 0)),
            (x0 + w + 150, gy + h + (k + 1) * 420, z0 + d - inset + (150 if k == 0 else 0)), R, **kw)
    # 马头墙：两端阶梯形白色山墙，墙头压黑瓦
    for ex in (x0 - 250, x0 + w - 50):
        for k in range(3):
            za = z0 + k * d * 0.17
            zb = z0 + d - k * d * 0.17
            top = gy + h + 700 + k * 650
            box(sc, (ex, gy + h, za), (ex + 300, top, zb), W, **kw)
            box(sc, (ex - 80, top, za - 80), (ex + 380, top + 160, zb + 80), R, **kw)


def _jn_tree(sc, x, z, h, k, **kw):
    gy = JN_GY
    box(sc, (x - 220, gy, z - 220), (x + 220, gy + h * 0.45, z + 220), X["trunk"], **kw)
    r = h * 0.32
    box(sc, (x - r, gy + h * 0.4, z - r), (x + r, gy + h, z + r), X["tree" if k % 2 else "tree2"], **kw)
    box(sc, (x - r * 0.6, gy + h, z - r * 0.6), (x + r * 0.6, gy + h * 1.18, z + r * 0.6), X["tree2" if k % 2 else "tree"], **kw)


def build_jiangnan(sc, cx, T):
    init()
    s = TS
    gy = JN_GY
    x0, x1 = cx - 60000, cx + 90000
    c0, c1 = int(math.floor(x0 / JN_CELL)), int(math.ceil(x1 / JN_CELL))
    # 路基（草坡）+ 轨道 + 近侧接触网
    for c in range(c0, c1):
        a, b = c * JN_CELL, (c + 1) * JN_CELL
        box(sc, (a, gy, -3000), (b, -520 * s, 3000), {"all": X["grass"], "top": X["gravel"]}, skip=("bottom", "back"))
    track(sc, x0, x1, 0.0, 0.0, sl0=cx - 30000, sl1=cx + 40000, s=s)
    catenary(sc, x0, x1, 0.0, 0.0, pole_z=-2700.0, step=40000.0, wire=5700 * s, s=s)
    for c in range(c0, c1):
        rng = np.random.RandomState(500 + c)
        a, b = c * JN_CELL, (c + 1) * JN_CELL
        # 近侧稻田（两色交替，中间田埂）
        grid_plane(sc, a, b - 400, -30000, -3000, gy, X["field" if c % 2 else "field2"], 2, 3)
        box(sc, (b - 400, gy, -30000), (b, gy + 60, -3000), X["grass2"])
        # 远侧草地、河道、石驳岸
        grid_plane(sc, a, b, 3000, 9000, gy, X["grass"], 2, 1)
        grid_plane(sc, a, b, 9000, 15000, gy - 300, X["water"], 2, 1, outdoor=True)
        for z in (9000, 14800):
            box(sc, (a, gy - 300, z), (b, gy + 40, z + 200), X["stone"], outdoor=True)
        grid_plane(sc, a, b, 15000, 60000, gy, X["grass2"], 2, 3, outdoor=True)
        # 石拱桥（每三格一座）
        if c % 3 == 0:
            bx = a + 5000
            for k, (dz, dy) in enumerate([(0, 0), (900, 250), (1900, 450), (2900, 450), (3900, 250), (4800, 0)]):
                box(sc, (bx, gy + dy - 200, 8800 + dz), (bx + 2600, gy + dy + 160, 8800 + dz + 1250), X["stone"], outdoor=True)
            box(sc, (bx + 300, gy - 300, 11300), (bx + 2300, gy + 120, 12500), solid(hex3("#55707A")), outdoor=True)
        # 民居与树
        hx = a + 600 + rng.rand() * 2500
        while hx < b - 4000:
            w = 5200 + rng.rand() * 4200
            h = 3000 + rng.rand() * 1600
            z0 = 16500 + rng.rand() * 2500
            _jn_house(sc, hx, z0, w, 5200 + rng.rand() * 1500, h, rng, outdoor=True)
            hx += w + 1500 + rng.rand() * 3000
            if rng.rand() < 0.6:
                _jn_tree(sc, hx - 800, z0 - 1200, 4200 + rng.rand() * 1800, c, outdoor=True)
        for k in range(2):
            _jn_tree(sc, a + rng.rand() * JN_CELL, 3800 + rng.rand() * 3500, 3600 + rng.rand() * 1500, c + k, outdoor=True)
        for k in range(3):
            _jn_tree(sc, a + rng.rand() * JN_CELL, 30000 + rng.rand() * 15000, 6000 + rng.rand() * 3000, c + k + 1, outdoor=True)
    # 远山、云（远景按镜头位置取整，避免整片山跟着镜头走）
    base = math.floor(cx / 200000.0) * 200000.0
    for k in range(-2, 4):
        ix = base + k * 200000
        rng = np.random.RandomState(900 + int(ix // 200000))
        ih = 30000 + rng.rand() * 30000
        box(sc, (ix, gy, 380000), (ix + 170000, gy + ih, 420000), X["hill"], outdoor=True)
        box(sc, (ix + 50000, gy + ih, 380000), (ix + 120000, gy + ih * 1.45, 420000), X["hill2"], outdoor=True)
    for k in range(-1, 4):
        ix = base + k * 200000
        cloud(sc, ix, 70000, 600000, 110000, seed=60 + int(ix // 200000) % 9)


# ------------------------------------------------------------------ 苏州收货仓库：收货台 + 货架 + 敞开的卷帘门
WH_DESK_Y = 820.0
WH_BACK_Z = 7000.0


def _rack(sc, x0, x1, z0, depth, levels, rng):
    """货架：橙色立柱 + 蓝色横梁，每层随机放蓝色收纳箱 / 牛皮纸箱。"""
    up, bm = X["shelf"], X["shelf_b"]
    n = int((x1 - x0) // 2400)
    for i in range(n + 1):
        x = x0 + i * 2400
        for dz in (0, depth - 90):
            box(sc, (x, 0, z0 + dz), (x + 90, levels[-1] + 120, z0 + dz + 90), up)
    for y in levels:
        for dz in (0, depth - 90):
            box(sc, (x0, y - 110, z0 + dz), (x0 + n * 2400 + 90, y, z0 + dz + 90), bm)
        box(sc, (x0, y - 40, z0), (x0 + n * 2400 + 90, y, z0 + depth), X["steel"])
    for i in range(n):
        for y in levels[:-1]:
            x = x0 + i * 2400 + 160
            while x < x0 + (i + 1) * 2400 - 600:
                k = rng.rand()
                if k < 0.45:
                    w, h = 560, 400
                    box(sc, (x, y, z0 + 120), (x + w, y + h, z0 + 120 + 560), {"all": X["bin"], "top": X["lid"]})
                elif k < 0.85:
                    w, h = 620, 450 + 200 * rng.rand()
                    box(sc, (x, y, z0 + 120), (x + w, y + h, z0 + 120 + 640), S.TEX["kraft"])
                else:
                    w = 500
                x += w + 60


def build_warehouse(sc, T):
    init()
    rng = np.random.RandomState(29)
    zb = WH_BACK_Z
    box(sc, (-12000, -60, -8000), (12000, 0, zb), tiled(X["wh_floor"], 10, 6))
    # 地面黄色通道线
    for x in (-1700.0, 1800.0):
        box(sc, (x, 0, -6000), (x + 120, 6, zb - 300), X["edge"])
    # 后墙 + 左侧敞开的卷帘门（门外是阳光下的院子）
    dx0, dx1, dh = -6200.0, -2600.0, 3600.0
    W = X["wh_wall"]
    box(sc, (-12000, 0, zb), (dx0, 7000, zb + 300), tiled(W, 3, 2))
    box(sc, (dx1, 0, zb), (12000, 7000, zb + 300), tiled(W, 6, 2))
    box(sc, (dx0, dh, zb), (dx1, 7000, zb + 300), W)
    box(sc, (dx0, dh, zb - 120), (dx1, dh + 420, zb), X["steel"])           # 卷帘收在门顶
    for x in (dx0 - 160, dx1):
        box(sc, (x, 0, zb - 120), (x + 160, dh + 420, zb), X["edge"])
    # 墙上高窗带
    for i in range(6):
        x = -1500 + i * 2200
        box(sc, (x, 4800, zb - 20), (x + 1500, 5600, zb), X["glass"])
    # 左墙
    box(sc, (-12300, 0, -8000), (-12000, 7000, zb), tiled(W, 4, 2))
    # 门外：院子、集装箱拖车、树
    grid_plane(sc, -40000, 20000, zb + 300, 60000, 0.0, X["platform"], 3, 3, outdoor=True)
    box(sc, (-7400, 1100, zb + 5200), (-1300, 3700, zb + 7600), S.TEX["cont2"], outdoor=True)
    box(sc, (-7400, 400, zb + 5300), (-1300, 1100, zb + 7500), X["wagon"], outdoor=True)
    for x in (-6800, -5600, -2600, -1900):
        box(sc, (x, 0, zb + 5150), (x + 500, 900, zb + 5300), X["wheel"], outdoor=True)
    box(sc, (-1300, 400, zb + 5300), (800, 3000, zb + 7500), {"all": X["loco"], "left": X["loco_win"]}, outdoor=True)
    for k in range(4):
        x, z, h = -9000 + k * 2900, zb + 14000 + 900 * (k % 2), 5200 + 700 * (k % 3)
        box(sc, (x - 220, 0, z - 220), (x + 220, h * 0.45, z + 220), X["trunk"], outdoor=True)
        r = h * 0.32
        box(sc, (x - r, h * 0.4, z - r), (x + r, h, z + r), X["tree" if k % 2 else "tree2"], outdoor=True)
    # 货架（收货台后方）
    _rack(sc, -1400.0, 9200.0, 2600.0, 1000.0, [0.0, 1250.0, 2500.0, 3750.0], rng)
    _rack(sc, -1400.0, 9200.0, 4900.0, 1000.0, [0.0, 1250.0, 2500.0, 3750.0], rng)
    # 刚到的这票货：托盘 + 蓝色收纳箱（20 箱）
    px, pz = -4300.0, 900.0
    box(sc, (px, 0, pz), (px + 1200, 150, pz + 1000), S.TEX["pallet"])
    for j in range(3):
        for a in range(2):
            for b in range(2):
                x = px + 40 + a * 580
                z = pz + 40 + b * 470
                box(sc, (x, 150 + j * 410, z), (x + 560, 150 + j * 410 + 400, z + 450), {"all": X["bin"], "top": X["lid"]})
    box(sc, (px + 40, 1380, pz + 40), (px + 600, 1780, pz + 490), {"all": X["bin"], "top": X["lid"]})
    box(sc, (px + 620, 1380, pz + 40), (px + 1180, 1780, pz + 490), {"all": X["bin"], "top": X["lid"]})
    # 收货台
    y = WH_DESK_Y
    box(sc, (-1500, y - 50, -820), (1300, y, 60), {"top": tiled(X["counter"], 3, 1), "all": X["counter"]})
    box(sc, (-1460, 0, -800), (1260, y - 50, -720), {"front": tiled(X["cabinet"], 3, 1), "all": X["lid"]})
    for x in (-1460, 1210):
        box(sc, (x, 0, -800), (x + 50, y - 50, 40), S.TEX["desk_side"])
    box(sc, (950, y, -300), (1050, y + 140, -200), X["lid"])                  # 笔筒
    box(sc, (-1300, y, -650), (-1080, y + 120, -430), S.TEX["pot"])           # 小盆栽
    for (dx, dy, s, k) in [(0, 120, 150, "leaf"), (-60, 210, 110, "leaf2"), (60, 220, 110, "leaf")]:
        box(sc, (-1190 + dx - s / 2, y + dy, -540 - s / 2), (-1190 + dx + s / 2, y + dy + s, -540 + s / 2), S.TEX[k])
