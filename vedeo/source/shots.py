"""镜头 01–02 渲染入口（体素方块风）。用法：python3 shots.py <镜号> <镜内时间t> [超采样]"""
import math
import sys
import time
import numpy as np
import cairo
from PIL import Image, ImageFilter

import common as C
from common import seg, ease, ease_out, lerp
import hud
import scene as S
from voxel import Camera, Sun, render, norm, Xf, rot_y


def post(img, ss):
    """软肩色调 + 缩小 + 泛光 + 暖色暗角，返回 (surface, 数组)。"""
    img = np.where(img > 0.75, 0.75 + (1 - np.exp(-(img - 0.75) / 0.25)) * 0.25, img)
    pil = Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))
    if ss != 1:
        pil = pil.resize((C.W, C.H), Image.LANCZOS)
    base = np.asarray(pil).astype(np.float32) / 255
    small = pil.resize((C.W // 4, C.H // 4), Image.BILINEAR)
    bright = np.asarray(small).astype(np.float32) / 255
    bright = np.clip((bright - 0.80) / 0.20, 0, 1) * bright
    glow = Image.fromarray((bright * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(14)).resize((C.W, C.H), Image.BILINEAR)
    glow = np.asarray(glow).astype(np.float32) / 255
    out = 1 - (1 - base) * (1 - glow * 0.55)
    yy, xx = np.mgrid[0:C.H, 0:C.W].astype(np.float32)
    vig = 1 - 0.16 * (((xx - C.W / 2) / (C.W / 2)) ** 2 + ((yy - C.H / 2) / (C.H / 2)) ** 2)
    out = out * vig[..., None] * np.array([1.02, 1.0, 0.965], np.float32)
    return C.rgb_to_surface(out)


def dust(ctx, t, n=40, region=(150, 120, 1100, 700)):
    rng = np.random.RandomState(11)
    ctx.set_operator(cairo.OPERATOR_SCREEN)
    for i in range(n):
        bx, by = rng.rand() * region[2] + region[0], rng.rand() * region[3] + region[1]
        ph = rng.rand() * 6.28
        px = bx + math.sin(t * 0.5 + ph) * 14 + t * 5
        py = by + math.cos(t * 0.4 + ph) * 10 - t * 3
        a = 0.25 + 0.25 * math.sin(t * 1.3 + ph)
        ctx.arc(px, py, 1.5 + rng.rand() * 2.2, 0, 6.283)
        ctx.set_source_rgba(1, 0.96, 0.82, max(0, a))
        ctx.fill()
    ctx.set_operator(cairo.OPERATOR_OVER)


# ---------------------------------------------------------------- 镜头 01
FIELDS01 = [("发货人", (-70, 0, 120)), ("收货人", (60, 0, 95)), ("品名", (-40, 0, 20)),
            ("数量", (50, 0, -40)), ("金额", (70, 0, -120))]


def shot01(t, ss=2, caption=True):
    """0–4s：放单据 → 点触控板上传 → 字段被识别并流向屏幕。"""
    S.init_tex()
    st = S.state(t)
    w, h = C.W * ss, C.H * ss
    dolly = ease(seg(t, 0.0, 4.0))
    target = np.array([-300.0, 1010.0, 600.0])
    look_at = target + np.array([lerp(0, 20, dolly), lerp(0, -18, dolly), 0])
    pos = target + norm((0.03, 0.19, -0.98)) * lerp(4500, 4150, dolly)
    cam = Camera(pos, look_at, 16.5, w, h)
    cam1 = Camera(pos, look_at, 16.5, C.W, C.H)

    sc = []
    S.build_room(sc)
    S.build_exterior(sc, T=t)
    S.build_desk(sc)
    ui = C.surf_to_rgba(C.ui_chat(t, scale=2.0))
    ui[..., 3] = 1.0
    scr_v = S.build_laptop(sc, ui)
    pap_v = S.build_paper(sc, st["paper"], st["paper_yaw"])
    S.build_sleeve(sc, S.SLEEVE_HOME[0], S.SLEEVE_HOME[1])
    S.build_elena(sc, np.array([-520.0, 450.0, 800.0]), -14.0, st["head_yaw"], st["head_pitch"], st["look"],
                  st["blink"], st["hand_l"], st["hand_r"])
    sun = Sun(S.SUN_DIR, center=(-300, 900, 400), extent=3600, depth=8000, res=2048)
    sun.build(sc)
    img, _ = render(sc, cam, sun, S.sky_image(w, h), ambient=(1.04, 1.0, 0.95), sun_col=(0.82, 0.68, 0.48),
                    fog_col=(0.84, 0.91, 0.95), fog_k=900000.0)
    surf, keep = post(img, ss)
    ctx = cairo.Context(surf)
    dust(ctx, t)

    # 单据被识别：扫描光扫过纸面（点击后）
    pq, _ = cam1.project(pap_v)
    sp = seg(t, 2.30, 2.95)
    sa = ease(seg(t, 2.25, 2.4)) * (1 - ease(seg(t, 3.05, 3.4)))
    hud.scan_paper(ctx, pq, ease(sp), sa)

    # 字段标签从单据飞向屏幕（左→右）
    sq, _ = cam1.project(scr_v)
    dst = sq.mean(0) + np.array([40, 10])
    pxf = Xf(rot_y(st["paper_yaw"]), (st["paper"][0], S.DESK_Y + 2, st["paper"][1]))
    for k, (label, lp) in enumerate(FIELDS01):
        t0 = 2.45 + k * 0.14
        u = seg(t, t0, t0 + 0.75)
        if u <= 0 or u >= 1:
            continue
        p0, _ = cam1.project(pxf.pt(np.array([lp], np.float64)))
        p0 = p0[0]
        mid = (p0 + dst) / 2 + np.array([0, -210 - 30 * k])
        p = hud.bezier(p0, mid, dst + np.array([0, (k - 2) * 26]), ease(u))
        a = ease(seg(u, 0, 0.18)) * (1 - ease(seg(u, 0.78, 1.0)))
        hud.field_tag(ctx, label, p[0], p[1], a, scale=lerp(0.85, 1.05, math.sin(u * math.pi)))

    # 流程物品栏
    hb_a = ease(seg(t, 0.35, 0.85))
    click = 2.2
    if t < click:
        hud.hotbar(ctx, hb_a, 0, set(), note="单据 · DEMO-INV-1008", pulse=0.5 + 0.5 * math.sin(t * 6))
    else:
        mv = ease(seg(t, click + 0.05, click + 0.45))
        prog = ease_out(seg(t, click + 0.4, 4.2)) * 0.62
        hud.hotbar(ctx, hb_a, lerp(0, 1, mv), {0}, progress=prog if mv > 0.5 else None,
                   note="正在识别原文" if mv > 0.5 else "已上传")
    if caption:
        a = ease(seg(t, 0.8, 1.2)) * (1 - ease(seg(t, 3.5, 3.85)))
        C.caption(ctx, "一张单据，从这里出发。", 84, 76, alpha=a, tag="HAMBURG · 08:30 · DEMO-INV-1008")
    surf.flush()
    return surf, keep


# ---------------------------------------------------------------- 镜头 02
CAM2 = dict(yaw=8.0, pitch=17.0, dist=2150.0, target=(-58.0, 892.0, 290.0), fov=16.5,
            hips=(-880.0, 450.0, 800.0), lean=4.0, callout=(1326, 30))


def _ui_pt(scr, x, y, lw=640, lh=400):
    """界面逻辑坐标 -> 屏幕四边形上的世界坐标（scr = TL,TR,BR,BL）。"""
    u, v = x / lw, y / lh
    top = scr[0] + (scr[1] - scr[0]) * u
    bot = scr[3] + (scr[2] - scr[3]) * u
    return top + (bot - top) * v


def shot02(t, ss=2, caption=True, cam_kw=None):
    """4–8s（镜内 0–4s）：对照原文与译文 → 发现疑点 → 人点击“确认修订” → 单据装入透明夹向右滑出。"""
    S.init_tex()
    st = S.state2(t)
    w, h = C.W * ss, C.H * ss
    ck = dict(CAM2); ck.update(cam_kw or {})
    th, pi_ = math.radians(ck["yaw"]), math.radians(ck["pitch"])
    f = np.array([math.sin(th) * math.cos(pi_), -math.sin(pi_), math.cos(th) * math.cos(pi_)])
    target = np.array(ck["target"], np.float64)
    pos = target - f * ck["dist"]
    cam = Camera(pos, target, ck["fov"], w, h)
    cam1 = Camera(pos, target, ck["fov"], C.W, C.H)

    sc = []
    S.build_room(sc)
    S.build_exterior(sc, T=4.0 + t)
    S.build_desk(sc, pencil=False)
    ui = C.surf_to_rgba(C.ui_review(t, click=2.1))
    ui[..., 3] = 1.0
    scr_v = S.build_laptop(sc, ui)
    S.build_paper(sc, st["paper"], st["paper_yaw"])
    S.build_sleeve(sc, st["sleeve"], st["sleeve_yaw"])
    S.build_elena(sc, np.array(ck.get("hips", (-520.0, 450.0, 800.0)), np.float64), st["yaw"], st["head_yaw"], st["head_pitch"], st["look"],
                  st["blink"], st["hand_l"], st["hand_r"], lean=st["lean"] + ck.get("lean", 0.0))
    S.build_pencil(sc, st["pencil_tip"], st["pencil_dir"])
    sun = Sun(S.SUN_DIR, center=(-300, 900, 400), extent=3600, depth=8000, res=2048)
    sun.build(sc)
    img, _ = render(sc, cam, sun, S.sky_image(w, h), ambient=(1.04, 1.0, 0.95), sun_col=(0.82, 0.68, 0.48),
                    fog_col=(0.84, 0.91, 0.95), fog_k=900000.0)
    surf, keep = post(img, ss)
    ctx = cairo.Context(surf)
    dust(ctx, t + 4.0, n=26, region=(100, 150, 900, 600))

    done = st["done"]
    pulse = 0.5 + 0.5 * math.sin(t * 7.0)
    # 纸面疑点定位框 + 纸面→屏幕的像素连线（核对同一处信息）
    x0, x1, z0, z1 = S.ROW_LOCAL
    rq, _ = cam1.project(st["pxf"].pt([(x0, 1, z1), (x1, 1, z1), (x1, 1, z0), (x0, 1, z0)]))
    la = ease(seg(t, 1.0, 1.25)) * (1 - ease(seg(t, 2.5, 2.75)))
    hud.locator(ctx, rq, done, la, pulse * (1 - done))
    row_l, _ = cam1.project(_ui_pt(scr_v, 20, 240))
    hud.dotted_link(ctx, (rq[1] + rq[2]) / 2 + np.array([10, 0]), row_l + np.array([-8, 0]), done,
                    ease(seg(t, 1.2, 1.6)), la, t)
    # 放大的疑点卡片
    ca = ease(seg(t, 0.95, 1.3)) * (1 - ease(seg(t, 3.1, 3.4)))
    row_t, _ = cam1.project(_ui_pt(scr_v, 628, 240))
    hud.callout(ctx, ck["callout"][0], ck["callout"][1], done, ca, anchor=row_t, w=572)

    # 流程物品栏：识别、翻译已完成 → 字段核对发现疑点 → 人工确认 → 交接下一站
    if t < 0.5:
        a = lerp(1, 3, ease(seg(t, 0.05, 0.45)))
        dn = {0} | ({1} if a > 1.5 else set()) | ({2} if a > 2.5 else set())
        hud.hotbar(ctx, 1.0, a, dn, note="中文译文" if a < 2.5 else "字段核对")
    elif t < 2.15:
        hud.hotbar(ctx, 1.0, 3, {0, 1, 2}, note="发现疑点 1 处 · 待人工处理" if t > 0.8 else "字段核对",
                   warn=3 if t > 0.8 else None, pulse=pulse if t > 0.8 else 0.0)
    elif t < 3.0:
        mv = ease(seg(t, 2.15, 2.5))
        hud.hotbar(ctx, 1.0, lerp(3, 4, mv), {0, 1, 2, 3}, note="已人工确认" if mv > 0.5 else "确认修订")
    else:
        mv = ease(seg(t, 3.0, 3.35))
        hud.hotbar(ctx, 1.0, lerp(4, 5, mv), {0, 1, 2, 3, 4}, note="交接下一站" if mv > 0.5 else "已人工确认")
    if caption:
        a = ease(seg(t, 0.45, 0.85)) * (1 - ease(seg(t, 3.5, 3.85)))
        C.caption(ctx, "识别疑点，人工确认。", 84, 76, alpha=a, tag="HAMBURG · 08:31 · DEMO-INV-1008")
    surf.flush()
    return surf, keep


SHOTS = {1: shot01, 2: shot02}

if __name__ == "__main__":
    n = int(sys.argv[1]); t = float(sys.argv[2]); ss = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    t0 = time.time()
    s, _ = SHOTS[n](t, ss)
    s.write_to_png("shot%02d_t%.2f.png" % (n, t))
    print("done %.1fs" % (time.time() - t0))
