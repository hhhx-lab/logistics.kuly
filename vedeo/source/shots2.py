"""镜头 03–09：单据离开汉堡，随货船、口岸、铁路，最终到苏州收货人手中（体素方块风）。
用法：python3 shots2.py <镜号> <镜内时间t> [超采样]
"""
import math
import sys
import time
import numpy as np
import cairo

import common as C
from common import seg, ease, ease_out, lerp
import hud
import scene as S
import chars as K
import sets as E
import ui2 as U
from shots import post, dust
from voxel import Camera, Sun, render, norm, Xf, rot_y, rot_x

AMB_OUT = dict(ambient=(0.93, 0.91, 0.88), sun_col=(0.86, 0.72, 0.52), fog_col=(0.84, 0.91, 0.95))
AMB = dict(ambient=(1.04, 1.0, 0.95), sun_col=(0.82, 0.68, 0.48), fog_col=(0.84, 0.91, 0.95))


# ---------------------------------------------------------------- 通用工具
def spline(keys, t):
    """Catmull-Rom 插值。keys: [(时间, (x,y,z)), ...]，按时间升序。"""
    ts = [k[0] for k in keys]
    ps = [np.asarray(k[1], np.float64) for k in keys]
    if t <= ts[0]:
        return ps[0].copy()
    if t >= ts[-1]:
        return ps[-1].copy()
    i = max(j for j in range(len(ts) - 1) if ts[j] <= t)
    u = (t - ts[i]) / (ts[i + 1] - ts[i])
    p0 = ps[i - 1] if i > 0 else ps[i] - (ps[i + 1] - ps[i])
    p1, p2 = ps[i], ps[i + 1]
    p3 = ps[i + 2] if i + 2 < len(ps) else p2 + (p2 - p1)
    return 0.5 * ((2 * p1) + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u * u + (-p0 + 3 * p1 - 3 * p2 + p3) * u ** 3)


def look_cam(pos, target, fov, ss):
    return Camera(pos, target, fov, C.W * ss, C.H * ss), Camera(pos, target, fov, C.W, C.H)


def flying_doc(sc, pos, t, scale=1.0, yaw=0.0, tilt=0.0, amp=1.0, sleeve=True):
    """飘飞中的单据：轻微翻转 + 纸面波动。"""
    xf = K.doc_xf(pos, yaw=yaw + 14 * math.sin(t * 2.3) * amp, pitch=-90 + tilt + 10 * math.sin(t * 3.1) * amp,
                  roll=9 * math.sin(t * 2.7 + 1.0) * amp)
    return K.build_doc(sc, xf, wave=34 * amp, phase=t * 7.0, curl=18 * amp, sleeve=sleeve, scale=scale)


def trail(ctx, cam1, path, t, alpha, n=16, dt=0.035, t_min=-1e9, seed=3):
    pts = []
    for k in range(n):
        tk = t - k * dt
        if tk < t_min:
            break
        p, z = cam1.project(path(tk)[None])
        if z[0] > 60:
            pts.append(p[0])
    if len(pts) > 2:
        U.sparkle_trail(ctx, pts, alpha, seed=seed)


def blink_at(t, times, d=0.12):
    return any(a <= t < a + d for a in times)


def finish(img, ss):
    surf, keep = post(img, ss)
    return surf, keep, cairo.Context(surf)


# ---------------------------------------------------------------- 镜头 03 · 单据飞出窗外（4.5s）
def path03(t):
    hx = E.HERO[0]
    keys = [(0.0, (hx - 260, 1180, 520)), (0.55, (hx - 330, 1560, 60)), (1.15, (hx - 420, 2050, -760)),
            (1.75, (hx + 700, 1650, -1350)), (2.35, (hx + 1500, 2000, -900)), (3.2, (hx + 3300, 2350, 1200)),
            (4.5, (hx + 8200, 2500, 9500))]
    return spline(keys, t)


def shot03(t, ss=2, caption=True):
    E.init()
    hx, sill, top = E.HERO
    pan = ease(seg(t, 1.5, 4.4))
    target = np.array([lerp(hx + 300, hx + 4600, pan), lerp(1500, 1350, pan), lerp(0, 4200, pan)])
    pos = np.array([lerp(1900, 2300, pan), 1650.0, -6800.0])
    cam, cam1 = look_cam(pos, target, 36.0, ss)

    sc = []
    E.build_facade(sc, open_amt=1.0)
    E.build_hamburg_exterior(sc, T=8.0 + t)
    # Elena 站在窗内，送出单据后挥手
    hips = np.array([hx - 120, 720.0, 900.0])
    yaw = -22.0
    body = Xf(rot_y(yaw), hips)
    rel = ease(seg(t, 0.0, 0.5))
    hand_l = body.pt((lerp(-200, -230, rel), lerp(440, 240, rel), lerp(-420, -150, rel)))
    wave = ease(seg(t, 0.7, 1.1)) * (1 - ease(seg(t, 3.8, 4.3)))
    hand_r = body.pt((lerp(260, 330 + 90 * math.sin(t * 9.0), wave), lerp(-20, 860, wave), lerp(-40, -160, wave)))
    dp = path03(t)
    look = "center" if t > 1.4 else "right"
    K.build_person(sc, "elena", hips, yaw, head_yaw=lerp(-8, -22, ease(seg(t, 0.3, 1.5))),
                   head_pitch=lerp(-6, 4, ease(seg(t, 0.2, 1.2))), look=look,
                   blink=blink_at(t, (1.9, 3.6)), hand_l=hand_l, hand_r=hand_r)
    flying_doc(sc, dp, t, scale=lerp(1.0, 1.6, ease(seg(t, 0.2, 1.4))), yaw=-15 * pan, amp=lerp(0.4, 1.0, ease(seg(t, 0.0, 0.8))))
    sun = Sun(norm((0.75, 0.50, -0.45)), center=(hx, 1300, 500), extent=7000, depth=40000, res=2048)
    sun.build(sc)
    img, _ = render(sc, cam, sun, E.sky(cam.w, cam.h), fog_k=900000.0, **AMB_OUT)
    surf, keep, ctx = finish(img, ss)

    trail(ctx, cam1, path03, t, ease(seg(t, 0.4, 0.9)), t_min=0.2)
    # 物品栏淡出 → 交接链淡入
    ha = 1 - ease(seg(t, 0.0, 0.45))
    if ha > 0:
        hud.hotbar(ctx, ha, 5, {0, 1, 2, 3, 4}, note="交接下一站")
    U.chain_hud(ctx, ease(seg(t, 0.35, 0.85)), lerp(0, 0.35, ease(seg(t, 1.8, 4.5))), {0},
                pulse=0.5 + 0.5 * math.sin(t * 6), t=t)
    if caption:
        a = ease(seg(t, 0.6, 1.0)) * (1 - ease(seg(t, 4.0, 4.4)))
        C.caption(ctx, "带着核对结果，去下一站。", 84, 76, alpha=a, tag="HAMBURG · 08:32 · DEMO-INV-1008")
    surf.flush()
    return surf, keep


# ---------------------------------------------------------------- 镜头 04 · 海上航拍（5s）
def route_map(ctx, x, y, px, prog, alpha):
    """左下角像素世界地图：汉堡 → 上海 示意航线（片中图形，不是实时追踪数据）。"""
    if alpha <= 0:
        return
    rows = S.WORLD
    h, w = len(rows), len(rows[0])
    pad = 12
    hud._bevel(ctx, x - pad, y - pad, w * px + 2 * pad, h * px + 2 * pad + 30, alpha, fill=(0.10, 0.15, 0.19, 0.46))
    for j, r in enumerate(rows):
        for i, ch in enumerate(r):
            if ch == "#":
                ctx.rectangle(x + i * px, y + j * px, px - 1, px - 1)
                ctx.set_source_rgba(0.66, 0.79, 0.77, 0.85 * alpha)
                ctx.fill()
    a = np.array([x + 16.5 * px, y + 2.5 * px])
    b = np.array([x + 26.5 * px, y + 4.5 * px])
    n = 24
    for k in range(n + 1):
        u = k / n
        if u > prog:
            break
        p = a + (b - a) * u + np.array([0, math.sin(u * math.pi) * 4.6 * px])
        ctx.rectangle(p[0] - 2.5, p[1] - 2.5, 5, 5)
        ctx.set_source_rgba(*C.hexc("#FFD27A", alpha))
        ctx.fill()
    for p, lab in ((a, "汉堡"), (b, "上海")):
        ctx.rectangle(p[0] - 5, p[1] - 5, 10, 10)
        ctx.set_source_rgba(*C.hexc("#E0734A", alpha))
        ctx.fill()
    u = min(prog, 1.0)
    p = a + (b - a) * u + np.array([0, math.sin(u * math.pi) * 4.6 * px])
    U._mini_ship(ctx, p[0], p[1] - 9, 0.9, alpha)
    C.text(ctx, "汉堡 → 上海（示意）", x, y + h * px + 22, 16, color="#FFFFFF", bold=True, alpha=alpha)


def cam04(t):
    sx = 6000.0 * t
    pos = np.array([sx - 52000 + 1500 * t, E.SEA + 40000 - 1000 * t, -98000.0])
    target = np.array([sx - 30000 + 2200 * t, E.SEA + 15000, 6000.0])
    return sx, pos, target


def doc04(t):
    sx, pos, target = cam04(t)
    f = norm(target - pos)
    r = norm(np.cross((0, 1, 0), f))
    u = np.cross(f, r)
    lx = lerp(-2500, 1200, 0.55 * seg(t, 0.0, 5.0) + 0.45 * ease_out(seg(t, 0.0, 2.2))) + 70 * math.sin(t * 1.7)
    ly = 260 + 130 * math.sin(t * 2.2) + lerp(-300, 0, ease(seg(t, 0, 1.4)))
    return pos + f * 2700 + r * lx + u * ly


TILT04 = 22.0


def shot04(t, ss=2, caption=True):
    E.init()
    sx, pos, target = cam04(t)
    cam, cam1 = look_cam(pos, target, 40.0, ss)
    sc = []
    E.sea_plane(sc, pos[0], pos[2], base=12000, tex=E.X["sea_open"])
    E.ship(sc, sx, 0.0, outdoor=True)
    E.wake(sc, sx, 0.0, 20 + t)
    # 云：近处的几朵从镜头两侧掠过，远处一排压低在海平线
    for (cx, cy, cz, w, sd) in [(-52000, E.SEA + 33000, -26000, 16000, 5), (-6000, E.SEA + 38500, -14000, 20000, 6),
                                (24000, E.SEA + 34000, -30000, 14000, 7), (-30000, E.SEA + 47000, 30000, 30000, 8),
                                (40000, E.SEA + 50000, 60000, 34000, 9)]:
        E.cloud(sc, cx, cy, cz, w, seed=sd)
    for (cx, cy, cz, w, sd) in [(-200000, 20000, 900000, 90000, 11), (-40000, 35000, 1000000, 120000, 12),
                                (150000, 15000, 850000, 80000, 13), (320000, 40000, 1100000, 110000, 14)]:
        E.cloud(sc, cx, cy, cz, w, seed=sd)
    # 远处小岛（像素山丘）
    for (ix, iz, iw, ih) in [(-160000, 1050000, 120000, 18000), (210000, 1150000, 160000, 24000)]:
        from voxel import box as _box
        _box(sc, (ix, E.SEA, iz), (ix + iw, E.SEA + ih, iz + 40000), E.X["hill"], outdoor=True)
        _box(sc, (ix + iw * 0.25, E.SEA + ih, iz), (ix + iw * 0.7, E.SEA + ih * 1.6, iz + 40000), E.X["hill2"], outdoor=True)
    # 海鸥与镜头同速伴飞
    f = norm(target - pos)
    r = norm(np.cross((0, 1, 0), f))
    for k, (lx, ly, d, ph) in enumerate([(5200, 2600, 20000, 0.0), (8200, 3600, 26000, 1.7), (-7600, 4400, 24000, 3.1)]):
        gp = pos + f * d + r * (lx + 300 * math.sin(t * 0.9 + ph)) + np.array([0, ly + 250 * math.sin(t * 1.3 + ph), 0])
        E.gull(sc, gp, t, span=1100, phase=ph)
    dp = doc04(t)
    flying_doc(sc, dp, t + 3.0, scale=1.6, yaw=-18, tilt=TILT04)
    sun = Sun(norm((-0.35, 0.75, -0.55)), center=dp, extent=30000, depth=60000, res=1024)
    sun.build(sc)
    img, _ = render(sc, cam, sun, E.sky(cam.w, cam.h, top="#5FA8E0", bot="#E4F1F4"), fog_k=900000.0, **AMB_OUT)
    surf, keep, ctx = finish(img, ss)
    trail(ctx, cam1, doc04, t, 1.0, dt=0.03)
    route_map(ctx, 64, 790, 9, ease(seg(t, 0.4, 4.6)), ease(seg(t, 0.5, 0.9)) * (1 - ease(seg(t, 4.3, 4.8))))
    U.chain_hud(ctx, 1.0, lerp(0.35, 1.0, ease(seg(t, 0.0, 4.8))), {0}, active=1 if t > 4.4 else None,
                pulse=0.5 + 0.5 * math.sin(t * 6), t=t)
    if caption:
        a = ease(seg(t, 0.5, 0.9)) * (1 - ease(seg(t, 4.4, 4.85)))
        C.caption(ctx, "跨越语言，也跨越海洋。", 84, 76, alpha=a, tag="HAMBURG → SHANGHAI")
    surf.flush()
    return surf, keep


# ---------------------------------------------------------------- 镜头 05 · 甲板：船方 Lin 接住单据（5.5s）
LIN_HIPS = np.array([-250.0, 560.0, 400.0])
LIN_YAW = 18.0
T05_CATCH, T05_REL = 1.15, 4.15


def _lin_body():
    return Xf(rot_y(LIN_YAW), LIN_HIPS)


def _lin_hand_l(t):
    """画面左侧的手：抬起接单据 → 举着 → 松手后挥一下。"""
    b = _lin_body()
    rest = np.array([-250.0, -20, -40])
    up = np.array([-430.0, 400, -300])
    hold = np.array([-400.0, 300, -330])
    send = np.array([-300.0, 640, -360])
    u = ease(seg(t, 0.55, T05_CATCH))
    p = rest + (up - rest) * u
    p = p + (hold - up) * ease(seg(t, T05_CATCH + 0.1, T05_CATCH + 0.6))
    p = p + (send - hold) * ease(seg(t, T05_REL - 0.35, T05_REL + 0.05))
    p = p + (rest - send) * ease(seg(t, T05_REL + 0.5, T05_REL + 1.1))
    return b.pt(p)


def _lin_doc_hold(t):
    return _lin_hand_l(t) + np.array([0, 215.0, -10])


def path05(t):
    catch = _lin_doc_hold(T05_CATCH)
    if t < T05_CATCH:
        keys = [(0.0, (-5600, 2300, 0)), (0.45, (-3000, 2000, -300)), (0.85, (-1500, 1800, -450)), (T05_CATCH, catch)]
        return spline(keys, t)
    if t < T05_REL:
        return _lin_doc_hold(t)
    rel = _lin_doc_hold(T05_REL)
    keys = [(T05_REL, rel), (T05_REL + 0.45, rel + np.array([350, 600, -150])), (T05_REL + 0.9, rel + np.array([1500, 1100, 200])),
            (5.5, rel + np.array([4200, 1500, 1200]))]
    return spline(keys, t)


def shot05(t, ss=2, caption=True):
    E.init()
    pos = np.array([lerp(900, 700, ease(seg(t, 0, 5.5))), 1500.0, -4300.0])
    target = np.array([lerp(-500, -300, ease(seg(t, 0, 5.5))), 1180.0, 600.0])
    cam, cam1 = look_cam(pos, target, 36.0, ss)
    sc = []
    E.build_deck(sc, pos[0], pos[2], 30 + t)
    body = _lin_body()
    # 平板：先在腰间，问话时抬到胸前偏下，屏幕朝向本人
    lift = ease(seg(t, 1.05, 1.5)) * (1 - ease(seg(t, 4.5, 5.0)))
    tp = np.array([lerp(150, 90, lift), lerp(30, 150, lift), lerp(-260, -340, lift)])
    txf = body @ Xf(rot_y(lerp(-10, 0, lift)) @ rot_x(lerp(20, 52, lift)), tp)
    ui = C.surf_to_rgba(U.ui_ship(t, t_q=1.45))
    ui[..., 3] = 1.0
    scr = K.build_tablet(sc, txf, ui, w=300, h=200)
    hand_r = txf.pt((150, 0, -20))
    look_down = 1.5 < t < 4.0
    hy = lerp(14, -8, ease(seg(t, 0.4, 1.0)))
    if t > 1.3:
        hy = lerp(hy, 0, ease(seg(t, 1.3, 1.7)))
    if t > 4.0:
        hy = lerp(hy, -16, ease(seg(t, 4.0, 4.6)))
    hp = lerp(0, -16, ease(seg(t, 1.4, 1.8)) * (1 - ease(seg(t, 3.9, 4.2)))) + lerp(0, 6, ease(seg(t, 4.2, 4.6)))
    look = "down" if look_down else ("right" if t > 4.1 else "left" if t < 1.3 else "center")
    K.build_person(sc, "lin", LIN_HIPS, LIN_YAW, head_yaw=hy, head_pitch=hp, look=look,
                   blink=blink_at(t, (0.25, 2.6, 4.9)), hand_l=_lin_hand_l(t), hand_r=hand_r)
    dp = path05(t)
    held = T05_CATCH <= t < T05_REL
    amp = 0.18 if held else 1.0
    if t < T05_CATCH:
        amp = lerp(1.0, 0.18, ease(seg(t, T05_CATCH - 0.35, T05_CATCH)))
    elif t >= T05_REL:
        amp = lerp(0.18, 1.0, ease(seg(t, T05_REL, T05_REL + 0.4)))
    flying_doc(sc, dp, t, scale=1.15, yaw=LIN_YAW + 6, tilt=-4, amp=amp)
    sun = Sun(norm((-0.55, 0.62, -0.55)), center=(0, 1200, 1500), extent=9000, depth=40000, res=2048)
    sun.build(sc)
    img, _ = render(sc, cam, sun, E.sky(cam.w, cam.h, top="#5FA8E0", bot="#E4F1F4"), fog_k=700000.0, **AMB_OUT)
    surf, keep, ctx = finish(img, ss)
    if t < T05_CATCH + 0.1:
        trail(ctx, cam1, path05, t, 1 - ease(seg(t, T05_CATCH - 0.1, T05_CATCH + 0.1)), dt=0.03)
    if t > T05_REL:
        trail(ctx, cam1, path05, t, ease(seg(t, T05_REL, T05_REL + 0.3)), dt=0.03, t_min=T05_REL)
    # 接住瞬间：一圈像素光点
    ca = seg(t, T05_CATCH - 0.05, T05_CATCH + 0.45)
    if 0 < ca < 1:
        p, _ = cam1.project(_lin_doc_hold(T05_CATCH)[None])
        for k in range(10):
            ang = k * math.pi / 5 + 0.3
            r = 40 + 130 * ease_out(ca)
            ctx.rectangle(p[0][0] + math.cos(ang) * r - 5, p[0][1] + math.sin(ang) * r - 5, 10, 10)
            ctx.set_source_rgba(*C.hexc(C.SUN, 1 - ca))
            ctx.fill()
    sq, _ = cam1.project(scr)
    pa = ease(seg(t, 1.3, 1.7)) * (1 - ease(seg(t, 4.35, 4.75)))
    U.panel(ctx, U.ui_ship(t, t_q=1.45), 1230, 330, 600, pa, anchor=sq.mean(0), title="船方 · 同一票问答")
    done = {0, 1} if t > 4.0 else {0}
    U.chain_hud(ctx, 1.0, lerp(1.0, 1.35, ease(seg(t, 4.3, 5.5))), done, active=1 if t < 4.3 else None,
                pulse=0.5 + 0.5 * math.sin(t * 6), t=t)
    if caption:
        a = ease(seg(t, 0.4, 0.8)) * (1 - ease(seg(t, 4.9, 5.35)))
        C.caption(ctx, "交接的人，读懂同一票。", 84, 76, alpha=a, tag="海运 · 船方 · DEMO-INV-1008")
    surf.flush()
    return surf, keep


# ---------------------------------------------------------------- 镜头 06 · 上海口岸单证核验：Mei 逐项复核（6s）
MEI_HIPS = np.array([-150.0, 470.0, 380.0])
MEI_YAW = -12.0
T06_LAND, T06_CLICK, T06_LIFT = 1.1, 3.0, 5.0
DOC06 = np.array([-150.0, E.OFF_DESK_Y + 6, -600.0])
SHEET06 = np.array([-470.0, E.OFF_DESK_Y + 3, -130.0])


def _tab06():
    """桌上支架平板：屏幕朝向 Mei（+Z 偏上），观众看到背面。"""
    return Xf(rot_y(MEI_YAW) @ rot_x(68) @ rot_y(180), (230.0, E.OFF_DESK_Y + 175, -330.0))


def _uv_pt(corners, u, v):
    tl, tr, br, bl = corners
    top = tl + (tr - tl) * u
    bot = bl + (br - bl) * u
    return top + (bot - top) * v


def _sig_pts(u):
    """签名笔迹：签名栏上的一段连笔（纸面局部坐标，单位 mm）。"""
    n = max(2, int(60 * u))
    pts = []
    for k in range(n):
        s = k / 59.0
        x = -10 + 95 * s
        y = 14 * math.sin(s * 19.0) * (0.6 + 0.4 * math.sin(s * 5.0)) + 6 * math.sin(s * 41)
        pts.append((x, y))
    return pts


def path06(t):
    if t < T06_LAND:
        keys = [(0.0, (-4300, 2100, -900)), (0.45, (-2500, 1600, -1100)), (0.85, (-1000, 1150, -700)), (T06_LAND, DOC06 + (0, 40, 0))]
        return spline(keys, t)
    if t < T06_LIFT:
        return DOC06.copy()
    keys = [(T06_LIFT, DOC06), (T06_LIFT + 0.35, DOC06 + np.array([150, 450, -200])), (T06_LIFT + 0.7, DOC06 + np.array([900, 900, -500])),
            (6.0, DOC06 + np.array([3200, 1200, -300]))]
    return spline(keys, t)


def shot06(t, ss=2, caption=True):
    E.init()
    dl = ease(seg(t, 0, 6.0))
    pos = np.array([lerp(1650, 1500, dl), 1520.0, lerp(-3700, -3500, dl)])
    target = np.array([lerp(-350, -300, dl), 960.0, 0.0])
    cam, cam1 = look_cam(pos, target, 37.0, ss)
    sc = []
    E.build_port_office(sc, 40 + t)
    body = Xf(rot_y(MEI_YAW), MEI_HIPS)
    # 平板
    txf = _tab06()
    ut = t - 1.0
    ui = C.surf_to_rgba(U.ui_port(ut, t_flag=0.9, click=T06_CLICK - 1.0))
    ui[..., 3] = 1.0
    scr = K.build_tablet(sc, txf, ui, w=320, h=214)
    box_ = __import__("voxel").box
    box_(sc, (-30, -175, -40), (30, 0, 40), K.solid(K.hex3("#5A646B")), Xf(rot_y(MEI_YAW), (230.0, E.OFF_DESK_Y + 175, -260.0)))
    tap = _uv_pt(scr, U.PORT_TAP[0] / U.LW, U.PORT_TAP[1] / U.LH)
    # 右手（画面右）：点按「复核完成」
    rest_r = body.pt((260, 300, -330))
    near = tap + np.array([0, 40, 60])
    u1 = ease(seg(t, 2.55, 2.95))
    u2 = ease(seg(t, 3.2, 3.6))
    press = math.sin(seg(t, 2.92, 3.12) * math.pi)
    hand_r = rest_r + (near - rest_r) * u1 + (tap - near) * press * 0.9 * u1
    hand_r = hand_r + (rest_r - hand_r) * u2
    # 左手（画面左）：拿笔在独立签收单上签字
    sheet_c = SHEET06
    sig0 = sheet_c + np.array([-5, 0, -95])
    pen_rest = sheet_c + np.array([200, 8, 40])
    rest_l = body.pt((-260, 290, -330))
    sp = seg(t, 3.85, 4.75)
    sig = _sig_pts(max(sp, 0.02))
    sx_, sy_ = sig[-1]
    sxf = Xf(rot_y(MEI_YAW + 6), sheet_c)
    tip_sign = sxf.pt((sx_ * 1.0 - 30, 3, -95 + sy_))
    a1 = ease(seg(t, 3.35, 3.7))
    a2 = ease(seg(t, 3.7, 3.85))
    a3 = ease(seg(t, 4.85, 5.2))
    a4 = ease(seg(t, 5.2, 5.6))
    if t < 3.7:
        tip = pen_rest
        hand_l = rest_l + (pen_rest + np.array([0, 60, 30]) - rest_l) * a1
    elif t < 4.85:
        tip = pen_rest + (tip_sign + np.array([0, 25 * (1 - a2), 0]) - pen_rest) * a2 if t < 3.85 else tip_sign + np.array([0, 2 * math.sin(t * 40), 0])
        hand_l = tip + np.array([10, 90, 70])
    else:
        tip = tip_sign + (pen_rest - tip_sign) * a3
        hand_l = tip + np.array([10, 90, 70])
        hand_l = hand_l + (rest_l - hand_l) * a4
    if t > 5.2:
        tip = pen_rest
    # 签收单（独立纸张）
    hw, hh = 105.0, 148.0
    sc.append(K.Quad(sxf.pt([(-hw, 0, hh), (hw, 0, hh), (hw, 0, -hh), (-hw, 0, -hh)]), E.X["sheet"], (0, 1, 0)))
    pdir = norm((0.3, 1.0, 0.6))
    if t < 3.55 or t > 5.2:
        pdir = norm((0.15, 0.06, 1.0))      # 不用时平放在桌上
        tip = pen_rest + np.array([-40, 0, -60])
    S.stick(sc, tip, tip + pdir * 14, 3.0, K.solid(K.hex3("#C9CFD3")))
    S.stick(sc, tip + pdir * 14, tip + pdir * 160, 6.0, K.solid(K.hex3("#1F2A33")))
    # 视线
    if t < 1.3:
        hy, hp, look = lerp(10, 26, ease(seg(t, 0.4, 0.9))), lerp(0, -14, ease(seg(t, 0.8, 1.2))), "left"
    elif t < 3.35:
        hy, hp, look = lerp(26, -12, ease(seg(t, 1.2, 1.6))), -14.0, "down"
    elif t < 5.0:
        hy, hp, look = lerp(-12, 24, ease(seg(t, 3.3, 3.7))), -20.0, "down"
    else:
        hy, hp, look = lerp(24, -18, ease(seg(t, 5.0, 5.5))), lerp(-20, 4, ease(seg(t, 5.0, 5.4))), "right"
    K.build_person(sc, "mei", MEI_HIPS, MEI_YAW, head_yaw=hy, head_pitch=hp, look=look,
                   blink=blink_at(t, (0.2, 2.2, 5.7)), hand_l=hand_l, hand_r=hand_r, pose="sit", chair=S.TEX["chair"])
    # 单据：飞入 → 平放桌面 → 起飞
    dp = path06(t)
    if t < T06_LAND:
        fl = ease(seg(t, 0.6, T06_LAND))
        flying_doc(sc, dp, t, scale=1.0, yaw=lerp(0, MEI_YAW + 8, fl), tilt=lerp(0, 88, fl), amp=lerp(1.0, 0.05, fl))
    elif t < T06_LIFT:
        flying_doc(sc, dp, t, scale=1.0, yaw=MEI_YAW + 8, tilt=88, amp=0.0)
    else:
        fl = ease(seg(t, T06_LIFT, T06_LIFT + 0.5))
        flying_doc(sc, dp, t, scale=1.0, yaw=MEI_YAW + 8 - 20 * fl, tilt=lerp(88, 0, fl), amp=lerp(0.05, 1.0, fl))
    sun = Sun(norm((-0.55, 0.62, -0.50)), center=(-300, 900, 0), extent=7000, depth=30000, res=2048)
    sun.build(sc)
    img, _ = render(sc, cam, sun, E.sky(cam.w, cam.h, top="#68AEE3", bot="#E6F1F4"), fog_k=500000.0,
                    ambient=(0.90, 0.87, 0.83), sun_col=(0.74, 0.62, 0.45), fog_col=(0.84, 0.91, 0.95))
    surf, keep, ctx = finish(img, ss)
    dust(ctx, t)
    if t < T06_LAND + 0.1:
        trail(ctx, cam1, path06, t, 1 - ease(seg(t, T06_LAND - 0.15, T06_LAND + 0.1)), dt=0.03)
    if t > T06_LIFT:
        trail(ctx, cam1, path06, t, ease(seg(t, T06_LIFT, T06_LIFT + 0.3)), dt=0.03, t_min=T06_LIFT)
    # 签名笔迹（画在签收单平面上）
    if sp > 0:
        pts = np.array([sxf.pt((x - 30, 4, -95 + y)) for x, y in _sig_pts(sp)])
        p2, _ = cam1.project(pts)
        ctx.set_source_rgba(*C.hexc("#1D3A55", 0.95))
        ctx.set_line_width(2.4)
        ctx.move_to(*p2[0])
        for q in p2[1:]:
            ctx.line_to(*q)
        ctx.stroke()
    sq, _ = cam1.project(scr)
    pa = ease(seg(t, 1.3, 1.7)) * (1 - ease(seg(t, 3.75, 4.15)))
    U.panel(ctx, U.ui_port(ut, t_flag=0.9, click=T06_CLICK - 1.0), 1210, 300, 640, pa, anchor=sq.mean(0),
            title="原文与译文 · 逐项复核")
    U.place_label(ctx, "上海口岸 · 单证核验（情景演示）", 84, 236, ease(seg(t, 0.3, 0.7)) * (1 - ease(seg(t, 5.4, 5.85))))
    done = {0, 1, 2} if t > T06_CLICK + 0.1 else {0, 1}
    pos_h = lerp(1.35, 2.0, ease(seg(t, 0.0, 1.0)))
    if t > T06_LIFT:
        pos_h = lerp(2.0, 2.35, ease(seg(t, T06_LIFT, 6.0)))
    U.chain_hud(ctx, 1.0, pos_h, done, active=2 if t < T06_LIFT else None, pulse=0.5 + 0.5 * math.sin(t * 6), t=t)
    if caption:
        a = ease(seg(t, 0.4, 0.8)) * (1 - ease(seg(t, 5.4, 5.85)))
        C.caption(ctx, "原文、译文、疑点，逐项可查。", 84, 76, alpha=a, tag="SHANGHAI · DEMO-INV-1008")
    surf.flush()
    return surf, keep


# ---------------------------------------------------------------- 镜头 07 · 铁路货场：Chen 接过单据，列车启动（5.5s）
CHEN_HIPS = np.array([-1500.0, 560.0, 250.0])
CHEN_YAW = -24.0
T07_CATCH, T07_REL, T07_GO = 1.1, 4.2, 3.9


def _chen_body():
    return Xf(rot_y(CHEN_YAW), CHEN_HIPS)


def _catch_hand(body, t, tc, tr, t_end=99.0):
    """画面左侧的手：抬起接单据 → 举着 → 松手送出 → 放下。"""
    rest = np.array([-250.0, -20, -40])
    up = np.array([-430.0, 400, -300])
    hold = np.array([-400.0, 300, -330])
    send = np.array([-200.0, 660, -380])
    p = rest + (up - rest) * ease(seg(t, tc - 0.55, tc))
    p = p + (hold - up) * ease(seg(t, tc + 0.1, tc + 0.6))
    p = p + (send - hold) * ease(seg(t, tr - 0.35, tr + 0.05))
    p = p + (rest - send) * ease(seg(t, tr + 0.5, tr + 1.1))
    return body.pt(p)


def _chen_doc(t):
    return _catch_hand(_chen_body(), t, T07_CATCH, T07_REL) + np.array([0, 215.0, -10])


def train_dx(t):
    """列车启动：匀加速向右。"""
    u = max(0.0, t - T07_GO)
    return 0.5 * 2200.0 * u * u


def path07(t):
    catch = _chen_doc(T07_CATCH)
    if t < T07_CATCH:
        keys = [(0.0, (-6800, 2500, -500)), (0.45, (-4600, 2150, -650)), (0.85, (-3000, 1900, -560)), (T07_CATCH, catch)]
        return spline(keys, t)
    if t < T07_REL:
        return _chen_doc(t)
    rel = _chen_doc(T07_REL)
    keys = [(T07_REL, rel), (T07_REL + 0.4, rel + np.array([500, 650, 100])), (T07_REL + 0.85, rel + np.array([2200, 1250, 1000])),
            (5.5, rel + np.array([6200, 1700, 2000]))]
    return spline(keys, t)


def shot07(t, ss=2, caption=True):
    E.init()
    dl = ease(seg(t, 0, 5.5))
    pos = np.array([lerp(3600, 3900, dl), 1650.0, lerp(-5600, -5400, dl)])
    target = np.array([lerp(-1300, -900, dl), 1350.0, 2200.0])
    cam, cam1 = look_cam(pos, target, 38.0, ss)
    sc = []
    E.build_rail_yard(sc, 50 + t, dx=train_dx(t))
    body = _chen_body()
    lift = ease(seg(t, 1.05, 1.5)) * (1 - ease(seg(t, 4.5, 5.0)))
    tp = np.array([lerp(150, 90, lift), lerp(30, 150, lift), lerp(-260, -340, lift)])
    txf = body @ Xf(rot_y(lerp(-10, 0, lift)) @ rot_x(lerp(20, 52, lift)), tp)
    ut = t - 1.3
    ui = C.surf_to_rgba(U.ui_rail(ut, t_q=0.15))
    ui[..., 3] = 1.0
    scr = K.build_tablet(sc, txf, ui, w=300, h=200)
    hand_r = txf.pt((150, 0, -20))
    if t < 1.3:
        hy, hp, look = lerp(10, 24, ease(seg(t, 0.2, 0.8))), 0.0, "left"
    elif t < 3.95:
        hy, hp, look = lerp(24, 0, ease(seg(t, 1.25, 1.6))), lerp(0, -16, ease(seg(t, 1.35, 1.7))), "down"
    else:
        hy, hp, look = lerp(0, -26, ease(seg(t, 3.95, 4.5))), lerp(-16, 4, ease(seg(t, 3.95, 4.3))), "right"
    K.build_person(sc, "chen", CHEN_HIPS, CHEN_YAW, head_yaw=hy, head_pitch=hp, look=look,
                   blink=blink_at(t, (0.3, 2.8, 5.1)), hand_l=_catch_hand(body, t, T07_CATCH, T07_REL), hand_r=hand_r)
    dp = path07(t)
    held = T07_CATCH <= t < T07_REL
    amp = 0.18 if held else 1.0
    if t < T07_CATCH:
        amp = lerp(1.0, 0.18, ease(seg(t, T07_CATCH - 0.35, T07_CATCH)))
    elif t >= T07_REL:
        amp = lerp(0.18, 1.0, ease(seg(t, T07_REL, T07_REL + 0.4)))
    flying_doc(sc, dp, t, scale=1.15, yaw=CHEN_YAW + 6, tilt=-4, amp=amp)
    sun = Sun(norm((0.45, 0.62, -0.62)), center=(0, 1500, 2500), extent=16000, depth=40000, res=2048)
    sun.build(sc)
    img, _ = render(sc, cam, sun, E.sky(cam.w, cam.h, top="#5FA8E0", bot="#E4F1F4"), fog_k=600000.0, **AMB_OUT)
    surf, keep, ctx = finish(img, ss)
    if t < T07_CATCH + 0.1:
        trail(ctx, cam1, path07, t, 1 - ease(seg(t, T07_CATCH - 0.1, T07_CATCH + 0.1)), dt=0.03)
    if t > T07_REL:
        trail(ctx, cam1, path07, t, ease(seg(t, T07_REL, T07_REL + 0.3)), dt=0.03, t_min=T07_REL)
    ca = seg(t, T07_CATCH - 0.05, T07_CATCH + 0.45)
    if 0 < ca < 1:
        p, _ = cam1.project(_chen_doc(T07_CATCH)[None])
        for k in range(10):
            ang = k * math.pi / 5 + 0.3
            r = 40 + 130 * ease_out(ca)
            ctx.rectangle(p[0][0] + math.cos(ang) * r - 5, p[0][1] + math.sin(ang) * r - 5, 10, 10)
            ctx.set_source_rgba(*C.hexc(C.SUN, 1 - ca))
            ctx.fill()
    sq, _ = cam1.project(scr)
    pa = ease(seg(t, 1.3, 1.7)) * (1 - ease(seg(t, 4.25, 4.6)))
    U.panel(ctx, U.ui_rail(ut, t_q=0.15), 1290, 120, 540, pa, anchor=sq.mean(0), title="铁路 · 交给谁")
    U.place_label(ctx, "铁路货场 · 陆运交接（情景演示）", 84, 236, ease(seg(t, 0.3, 0.7)) * (1 - ease(seg(t, 4.9, 5.35))))
    done = {0, 1, 2, 3} if t > 3.75 else {0, 1, 2}
    pos_h = lerp(2.35, 3.0, ease(seg(t, 0.0, 1.1)))
    if t > T07_REL:
        pos_h = lerp(3.0, 3.3, ease(seg(t, T07_REL, 5.5)))
    U.chain_hud(ctx, 1.0, pos_h, done, active=3 if t < T07_REL else None, pulse=0.5 + 0.5 * math.sin(t * 6), t=t)
    if caption:
        a = ease(seg(t, 0.4, 0.8)) * (1 - ease(seg(t, 4.9, 5.35)))
        C.caption(ctx, "流程向前，记录可追。", 84, 76, alpha=a, tag="SHANGHAI → SUZHOU · 铁路")
    surf.flush()
    return surf, keep


# ---------------------------------------------------------------- 镜头 08 · 列车穿过江南水乡（4.5s）
V08 = 12000.0


def head08(t):
    return 20000.0 + V08 * t


def cam08(t):
    h = head08(t)
    pos = np.array([h + 4000.0 - 600 * t, 6200.0 - 150 * t, -25000.0])
    target = np.array([h - 9000.0, 900.0, 5000.0])
    return pos, target


def doc08(t):
    pos, target = cam08(t)
    f = norm(target - pos)
    r = norm(np.cross((0, 1, 0), f))
    u = np.cross(f, r)
    lx = lerp(-3300, 2300, 0.5 * seg(t, 0, 4.5) + 0.5 * ease(seg(t, 0.0, 4.5))) + 120 * math.sin(t * 1.9)
    ly = 420 + 160 * math.sin(t * 2.4)
    return pos + f * 6200 + r * lx + u * ly


def _trail08(t_now):
    def p(tk):
        return doc08(tk) + np.array([0.8 * V08 * (t_now - tk), 0, 0])
    return p


def shot08(t, ss=2, caption=True):
    E.init()
    pos, target = cam08(t)
    cam, cam1 = look_cam(pos, target, 34.0, ss)
    sc = []
    E.build_jiangnan(sc, pos[0] - 12000, 60 + t)
    E.train(sc, head08(t), 0.0, 0.0, n_wag=6, seed=7, wire=5700 * E.TS, s=E.TS)
    dp = doc08(t)
    flying_doc(sc, dp, t + 1.0, scale=1.7, yaw=-14, tilt=12)
    sun = Sun(norm((-0.40, 0.70, -0.58)), center=(pos[0] + 2000, 0, 2000), extent=26000, depth=60000, res=2048)
    sun.build(sc)
    img, _ = render(sc, cam, sun, E.sky(cam.w, cam.h, top="#5FA8E0", bot="#E8F2F2"), fog_k=500000.0, **AMB_OUT)
    surf, keep, ctx = finish(img, ss)
    trail(ctx, cam1, _trail08(t), t, 1.0, dt=0.03)
    U.place_label(ctx, "上海 → 苏州 · 铁路运输（示意）", 84, 236, ease(seg(t, 0.3, 0.7)) * (1 - ease(seg(t, 4.0, 4.4))))
    U.chain_hud(ctx, 1.0, lerp(3.3, 3.9, ease(seg(t, 0.0, 4.5))), {0, 1, 2, 3}, active=4 if t > 4.0 else None,
                pulse=0.5 + 0.5 * math.sin(t * 6), t=t)
    if caption:
        a = ease(seg(t, 0.35, 0.75)) * (1 - ease(seg(t, 3.95, 4.4)))
        C.caption(ctx, "同一张单据，随货同行。", 84, 76, alpha=a, tag="SHANGHAI → SUZHOU")
    surf.flush()
    return surf, keep


# ---------------------------------------------------------------- 镜头 09 · 苏州收货：Zhou 查看处理记录、签收 + 片尾（6.5s）
ZHOU_HIPS = np.array([-250.0, 560.0, 380.0])
ZHOU_YAW = -10.0
T09_LAND, T09_TAP = 1.1, 1.9
DOC09 = np.array([-260.0, E.WH_DESK_Y + 6, -560.0])
SHEET09 = np.array([-590.0, E.WH_DESK_Y + 3, -150.0])
TAB09 = np.array([380.0, E.WH_DESK_Y + 175, -330.0])


def path09(t):
    keys = [(0.0, (-4600, 2300, -1300)), (0.45, (-2800, 1750, -1300)), (0.85, (-1100, 1250, -800)), (T09_LAND, DOC09 + (0, 40, 0))]
    return spline(keys, t) if t < T09_LAND else DOC09.copy()


def end_card(ctx, a, t):
    """片尾卡：产品名 + 口号 + 体验入口（只在片尾出现）。"""
    if a <= 0:
        return
    x, y, w, h = 1060.0, 318.0, 760.0, 450.0
    dy = (1 - ease_out(a)) * 24
    ctx.save()
    ctx.translate(0, dy)
    for i in range(8):
        C.rrect(ctx, x - i, y - i + 8, w + 2 * i, h + 2 * i, 30 + i)
        ctx.set_source_rgba(0.08, 0.18, 0.26, 0.02 * a)
        ctx.fill()
    C.rrect(ctx, x, y, w, h, 30)
    ctx.set_source_rgba(1, 1, 0.985, 0.93 * a)
    ctx.fill()
    ctx.restore()
    if a < 0.01:
        return
    # 内容逐行浮现
    a1 = ease_out(seg(a, 0.25, 0.8))
    a2 = ease_out(seg(a, 0.45, 0.95))
    a3 = ease_out(seg(a, 0.6, 1.0))
    ctx.save()
    ctx.translate(0, dy)
    ctx.push_group()
    C._doc_icon(ctx, x + 64, y + 62, 92)
    ctx.pop_group_to_source()
    ctx.paint_with_alpha(a1)
    C.text(ctx, "物流单证智能体", x + 170, y + 128, 62, color=C.NAVY, bold=True, alpha=a1)
    C.rrect(ctx, x + 64, y + 190, 120, 6, 3)
    ctx.set_source_rgba(*C.hexc(C.TEAL, a2))
    ctx.fill()
    C.text(ctx, "让每次交接，都有据可查。", x + 64, y + 262, 40, color="#2F5670", bold=True, alpha=a2)
    bw = C.text_w(ctx, "体验单据翻译与校验", 30, bold=True) + 120
    C.rrect(ctx, x + 64, y + 318, bw, 78, 39)
    ctx.set_source_rgba(*C.hexc(C.NAVY, a3))
    ctx.fill()
    C.text(ctx, "体验单据翻译与校验", x + 64 + 44, y + 368, 30, color="#FFFFFF", bold=True, alpha=a3)
    C.text(ctx, "›", x + 64 + bw - 54, y + 370, 36, color="#FFFFFF", bold=True, alpha=a3)
    ctx.restore()


def shot09(t, ss=2, caption=True):
    E.init()
    de = ease(seg(t, 4.2, 6.2))
    dl = ease(seg(t, 0, 4.2))
    pos = np.array([lerp(1650, 1500, dl) + 700 * de, 2150.0 + 80 * de, lerp(-4300, -4050, dl) - 600 * de])
    target = np.array([lerp(-420, -340, dl) + 1150 * de, 960.0 + 40 * de, 0.0])
    cam, cam1 = look_cam(pos, target, 37.0, ss)
    sc = []
    E.build_warehouse(sc, 70 + t)
    body = Xf(rot_y(ZHOU_YAW), ZHOU_HIPS)
    # 支架平板：屏幕朝向 Zhou
    txf = Xf(rot_y(ZHOU_YAW) @ rot_x(68) @ rot_y(180), TAB09)
    ut = t - 1.0
    ui = C.surf_to_rgba(U.ui_receive(ut, tap=T09_TAP - 1.0))
    ui[..., 3] = 1.0
    scr = K.build_tablet(sc, txf, ui, w=320, h=214)
    box_ = __import__("voxel").box
    box_(sc, (-30, -175, -40), (30, 0, 40), K.solid(K.hex3("#5A646B")), Xf(rot_y(ZHOU_YAW), TAB09 + np.array([0, 0, 70])))
    tx, ty = U.receive_tab_pt()
    tap = _uv_pt(scr, tx / U.LW, ty / U.LH)
    # 右手：点「处理记录」
    rest_r = body.pt((260, 290, -330))
    near = tap + np.array([0, 40, 60])
    u1 = ease(seg(t, T09_TAP - 0.45, T09_TAP - 0.05))
    u2 = ease(seg(t, T09_TAP + 0.3, T09_TAP + 0.7))
    press = math.sin(seg(t, T09_TAP - 0.08, T09_TAP + 0.12) * math.pi)
    hand_r = rest_r + (near - rest_r) * u1 + (tap - near) * press * 0.9 * u1
    hand_r = hand_r + (rest_r - hand_r) * u2
    # 左手：在独立签收单上签字
    sheet_c = SHEET09
    pen_rest = sheet_c + np.array([200, 8, 40])
    rest_l = body.pt((-260, 280, -330))
    sp = seg(t, 3.3, 4.1)
    sig = _sig_pts(max(sp, 0.02))
    sx_, sy_ = sig[-1]
    sxf = Xf(rot_y(ZHOU_YAW + 6), sheet_c)
    tip_sign = sxf.pt((sx_ - 30, 3, -95 + sy_))
    a1 = ease(seg(t, 2.8, 3.15))
    a2 = ease(seg(t, 3.15, 3.3))
    a3 = ease(seg(t, 4.2, 4.55))
    a4 = ease(seg(t, 4.55, 4.95))
    if t < 3.15:
        tip = pen_rest
        hand_l = rest_l + (pen_rest + np.array([0, 60, 30]) - rest_l) * a1
    elif t < 4.2:
        tip = pen_rest + (tip_sign + np.array([0, 25 * (1 - a2), 0]) - pen_rest) * a2 if t < 3.3 else tip_sign + np.array([0, 2 * math.sin(t * 40), 0])
        hand_l = tip + np.array([10, 90, 70])
    else:
        tip = tip_sign + (pen_rest - tip_sign) * a3
        hand_l = tip + np.array([10, 90, 70])
        hand_l = hand_l + (rest_l - hand_l) * a4
    hw, hh = 105.0, 148.0
    sc.append(K.Quad(sxf.pt([(-hw, 0, hh), (hw, 0, hh), (hw, 0, -hh), (-hw, 0, -hh)]), E.X["sheet"], (0, 1, 0)))
    pdir = norm((0.3, 1.0, 0.6))
    if t < 3.0 or t > 4.55:
        pdir = norm((0.15, 0.06, 1.0))
        tip = pen_rest + np.array([-40, 0, -60])
    S.stick(sc, tip, tip + pdir * 14, 3.0, K.solid(K.hex3("#C9CFD3")))
    S.stick(sc, tip + pdir * 14, tip + pdir * 160, 6.0, K.solid(K.hex3("#1F2A33")))
    # 视线：看单据飞来 → 看平板 → 看签收单 → 抬头
    if t < 1.3:
        hy, hp, look = lerp(10, 28, ease(seg(t, 0.3, 0.8))), lerp(0, -14, ease(seg(t, 0.8, 1.2))), "left"
    elif t < 2.8:
        hy, hp, look = lerp(28, -14, ease(seg(t, 1.2, 1.6))), -14.0, "down"
    elif t < 4.5:
        hy, hp, look = lerp(-14, 22, ease(seg(t, 2.75, 3.15))), -20.0, "down"
    else:
        hy, hp, look = lerp(22, 6, ease(seg(t, 4.5, 5.0))), lerp(-20, 2, ease(seg(t, 4.5, 4.9))), "center"
    K.build_person(sc, "zhou", ZHOU_HIPS, ZHOU_YAW, head_yaw=hy, head_pitch=hp, look=look,
                   blink=blink_at(t, (0.2, 2.4, 5.3, 6.2)), hand_l=hand_l, hand_r=hand_r)
    # 单据：飞入 → 平放在收货台上（旅程终点）
    dp = path09(t)
    if t < T09_LAND:
        fl = ease(seg(t, 0.6, T09_LAND))
        flying_doc(sc, dp, t, scale=1.0, yaw=lerp(0, ZHOU_YAW + 8, fl), tilt=lerp(0, 88, fl), amp=lerp(1.0, 0.05, fl))
    else:
        flying_doc(sc, dp, t, scale=1.0, yaw=ZHOU_YAW + 8, tilt=88, amp=0.0)
    sun = Sun(norm((-0.5, 0.66, -0.55)), center=(-500, 900, 1000), extent=12000, depth=30000, res=2048)
    sun.build(sc)
    img, _ = render(sc, cam, sun, E.sky(cam.w, cam.h, top="#68AEE3", bot="#F2EEDF"), fog_k=500000.0,
                    ambient=(0.86, 0.83, 0.79), sun_col=(0.72, 0.60, 0.43), fog_col=(0.88, 0.91, 0.92))
    surf, keep, ctx = finish(img, ss)
    dust(ctx, t)
    if t < T09_LAND + 0.1:
        trail(ctx, cam1, path09, t, 1 - ease(seg(t, T09_LAND - 0.15, T09_LAND + 0.1)), dt=0.03)
    if sp > 0:
        pts = np.array([sxf.pt((x - 30, 4, -95 + y)) for x, y in _sig_pts(sp)])
        p2, _ = cam1.project(pts)
        ctx.set_source_rgba(*C.hexc("#1D3A55", 0.95))
        ctx.set_line_width(2.4)
        ctx.move_to(*p2[0])
        for q in p2[1:]:
            ctx.line_to(*q)
        ctx.stroke()
    sq, _ = cam1.project(scr)
    pa = ease(seg(t, 1.2, 1.6)) * (1 - ease(seg(t, 3.5, 3.9)))
    U.panel(ctx, U.ui_receive(ut, tap=T09_TAP - 1.0), 1210, 300, 640, pa, anchor=sq.mean(0), title="单票视图 · 处理记录")
    U.place_label(ctx, "苏州 · 收货仓库（情景演示）", 84, 236, ease(seg(t, 0.3, 0.7)) * (1 - ease(seg(t, 4.0, 4.4))))
    signed = t > 4.1
    done = {0, 1, 2, 3, 4} if signed else {0, 1, 2, 3}
    ha = 1 - ease(seg(t, 4.7, 5.1))
    U.chain_hud(ctx, ha, lerp(3.9, 4.0, ease(seg(t, 0.0, 1.1))), done, active=None if signed else 4,
                pulse=0.5 + 0.5 * math.sin(t * 6), t=t)
    if caption:
        a = ease(seg(t, 0.4, 0.8)) * (1 - ease(seg(t, 3.9, 4.3)))
        C.caption(ctx, "收货时，来龙去脉也看得清楚。", 84, 76, alpha=a, tag="SUZHOU · DEMO-INV-1008")
    end_card(ctx, ease(seg(t, 4.6, 5.5)), t)
    surf.flush()
    return surf, keep


SHOTS = {3: shot03, 4: shot04, 5: shot05, 6: shot06, 7: shot07, 8: shot08, 9: shot09}
DUR = {3: 4.5, 4: 5.0, 5: 5.5, 6: 6.0, 7: 5.5, 8: 4.5, 9: 6.5}

if __name__ == "__main__":
    n = int(sys.argv[1]); ss = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    for tt in sys.argv[2].split(","):
        t0 = time.time()
        s, _ = SHOTS[n](float(tt), ss)
        s.write_to_png("/sessions/wonderful-tender-hawking/mnt/outputs/t/s%02d_%.2f.png" % (n, float(tt)))
        print("t=%s %.1fs" % (tt, time.time() - t0), flush=True)
