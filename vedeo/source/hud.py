"""方块游戏语汇的叠加层（全部原创像素）：流程物品栏、字段标签、扫描光。
物品栏六格对应单据智能体的真实流程：单据 → 识别 → 翻译 → 核对 → 人工确认 → 交接。"""
import math
import numpy as np
import cairo

import common as C
from common import seg, ease, ease_out, lerp, hexc

IPAL = {
    "W": "#FFFFFF", "w": "#E6E9EA", "g": "#AEB7BC", "b": "#7DB8E8", "B": "#4F8FC4",
    "N": "#1D4867", "T": "#7BA7A5", "t": "#5E8C8A", "R": "#D9574A", "Y": "#FFD27A",
    "G": "#5DBB63", "K": "#C9A46E", "k": "#9C7A48", "H": "#7A4A32", "S": "#F3CFB3",
    "D": "#3A4248", "O": "#E2874F", "L": "#B9DDF0",
}

ICONS = {
    "doc": [
        "bbbWWWWW....",
        "bbWWWWWWW...",
        "bWWWWWWWWW..",
        "WWggggggWW..",
        "WWWWWWWWWW..",
        "WWggggWWWW..",
        "WWWWWWWWWW..",
        "WWggggggWW..",
        "WWWWWWWWWW..",
        "WWgggWWWWW..",
        "WWWWWWWWWW..",
        "wwwwwwwwww..",
    ],
    "scan": [
        "WWWWWWWW....",
        "WggggggW....",
        "WWWWWWWW....",
        "WggTTTTT....",
        "WWTLLLLLT...",
        "WgTLLLLLT...",
        "WWTLLLLLT...",
        "WgTLLLLLT...",
        "WWWTTTTTD...",
        ".......DDD..",
        "........DDD.",
        ".........DD.",
    ],
    "trans": [
        "NNNNNN......",
        "NNWWNN......",
        "NWNNWN......",
        "NWWWWN......",
        "NWNNWTTTTTT.",
        "NNNNNTWWWWT.",
        "......TTWTTT",
        "......TWWWWT",
        "......TTWTTT",
        "......TWTWTT",
        "......TTTTTT",
        "............",
    ],
    "check": [
        "...DDDD.....",
        "KKKDwwDKKK..",
        "KWWWWWWWWK..",
        "KWGWggggWK..",
        "KGWWWWWWWK..",
        "KWWWWWWWWK..",
        "KWRWggggWK..",
        "KWRWWWWWWK..",
        "KWWWWWWWWK..",
        "KWGWggggWK..",
        "KGWWWWWWWK..",
        "KKKKKKKKKK..",
    ],
    "human": [
        "...HHHH.....",
        "..HHHHHH....",
        "..HSSSSH....",
        "..HSDSDS....",
        "...SSSS.....",
        "..wwwwww...G",
        ".wwwwwwww.GG",
        ".wwwwwwwwGG.",
        ".SwwwwwwGG..",
        ".SwwwwGwG...",
        "..NNNNNGG...",
        "..NN..NN....",
    ],
    "handoff": [
        "............",
        "............",
        "NNNNNNN.....",
        "NTNTNTN.OO..",
        "NTNTNTN.OLO.",
        "NTNTNTN.OOOO",
        "NNNNNNN.OOOO",
        "DDDDDDDDDDDD",
        ".DD...DD..DD",
        "............",
        "...Y........",
        "...YYYYYYYY.",
    ],
}

SLOTS = [("doc", "单据"), ("scan", "识别原文"), ("trans", "中文译文"), ("check", "字段核对"),
         ("human", "人工确认"), ("handoff", "交接下一站")]


def pixel_icon(ctx, key, x, y, px, alpha=1.0):
    rows = ICONS[key]
    for j, row in enumerate(rows):
        for i, ch in enumerate(row):
            if ch == ".":
                continue
            ctx.rectangle(x + i * px, y + j * px, px + 0.3, px + 0.3)
            ctx.set_source_rgba(*hexc(IPAL[ch], alpha))
            ctx.fill()


def _bevel(ctx, x, y, w, h, alpha, fill=(0.16, 0.22, 0.27, 0.62)):
    ctx.rectangle(x, y, w, h)
    ctx.set_source_rgba(fill[0], fill[1], fill[2], fill[3] * alpha)
    ctx.fill()
    b = 4
    ctx.set_source_rgba(1, 1, 1, 0.22 * alpha)
    ctx.rectangle(x, y, w, b); ctx.fill()
    ctx.rectangle(x, y, b, h); ctx.fill()
    ctx.set_source_rgba(0, 0, 0, 0.30 * alpha)
    ctx.rectangle(x, y + h - b, w, b); ctx.fill()
    ctx.rectangle(x + w - b, y, b, h); ctx.fill()


def _check_badge(ctx, cx, cy, s, alpha, color="#5DBB63"):
    ctx.rectangle(cx - s / 2, cy - s / 2, s, s)
    ctx.set_source_rgba(*hexc(color, alpha))
    ctx.fill()
    ctx.set_source_rgba(1, 1, 1, alpha)
    ctx.set_line_width(s * 0.16)
    ctx.set_line_cap(cairo.LINE_CAP_SQUARE)
    ctx.move_to(cx - s * 0.26, cy + s * 0.02)
    ctx.line_to(cx - s * 0.06, cy + s * 0.22)
    ctx.line_to(cx + s * 0.28, cy - s * 0.20)
    ctx.stroke()


def hotbar(ctx, alpha, active, done, progress=None, note=None, pulse=0.0, warn=None):
    """底部流程物品栏。
    active: 当前选中格索引（可为小数用于过渡）；done: 已完成格集合；
    progress: 选中格上方进度条 0..1；note: 进度条上方提示文字；warn: 显示红色疑点角标的格索引。"""
    if alpha <= 0:
        return
    n, s, gap = len(SLOTS), 84, 6
    total = n * s + (n - 1) * gap
    x0 = (C.W - total) / 2
    y0 = C.H - 34 - s + (1 - ease_out(alpha)) * 30
    _bevel(ctx, x0 - 8, y0 - 8, total + 16, s + 16, alpha, fill=(0.10, 0.15, 0.19, 0.55))
    for i, (key, label) in enumerate(SLOTS):
        x = x0 + i * (s + gap)
        _bevel(ctx, x, y0, s, s, alpha, fill=(0.55, 0.62, 0.66, 0.50))
        ia = alpha * (1.0 if (i in done or abs(i - active) < 0.5) else 0.55)
        pixel_icon(ctx, key, x + 9, y0 + 9, 5.5, ia)
        if i in done:
            _check_badge(ctx, x + s - 16, y0 + 16, 22, alpha)
        if warn == i:
            ctx.rectangle(x + s - 27, y0 + 5, 22, 22)
            ctx.set_source_rgba(*hexc("#D9574A", alpha))
            ctx.fill()
            C.text(ctx, "!", x + s - 16, y0 + 23, 19, color="#FFFFFF", bold=True, alpha=alpha, align="center")
        if i < n - 1:     # 格间流向箭头（左→右）
            ax = x + s + gap / 2
            ctx.set_source_rgba(1, 1, 1, 0.5 * alpha)
            ctx.move_to(ax - 2, y0 + s / 2 - 5); ctx.line_to(ax + 3, y0 + s / 2); ctx.line_to(ax - 2, y0 + s / 2 + 5)
            ctx.close_path(); ctx.fill()
    # 选中框：暖阳黄粗边，轻微呼吸
    sx = x0 + active * (s + gap)
    lw = 6 + 2 * pulse
    ctx.set_line_width(lw)
    ctx.set_source_rgba(*hexc("#FFD27A", alpha))
    ctx.rectangle(sx - 4, y0 - 4, s + 8, s + 8)
    ctx.stroke()
    ctx.set_source_rgba(1, 1, 1, 0.9 * alpha)
    ctx.set_line_width(2)
    ctx.rectangle(sx - 4 - lw / 2, y0 - 4 - lw / 2, s + 8 + lw, s + 8 + lw)
    ctx.stroke()
    # 选中物品名（方块游戏式：显示在物品栏上方）
    ai = int(round(active))
    label = note or SLOTS[ai][1]
    tw = C.text_w(ctx, label, 26, bold=True)
    cx = x0 + total / 2
    ty = y0 - 26
    if progress is not None:
        bw = 360
        bx = cx - bw / 2
        by = y0 - 22
        ctx.rectangle(bx, by, bw, 9)
        ctx.set_source_rgba(0.08, 0.12, 0.15, 0.65 * alpha)
        ctx.fill()
        segs = 18
        filled = progress * segs
        for k in range(segs):
            f = min(1.0, max(0.0, filled - k))
            if f <= 0:
                break
            ctx.rectangle(bx + 1 + k * (bw / segs), by + 1, (bw / segs - 2) * f, 7)
            ctx.set_source_rgba(*hexc("#8FE0C8", alpha))
            ctx.fill()
        ty = by - 14
    C.text(ctx, label, cx + 2, ty + 2, 26, color="#1A2A33", bold=True, alpha=0.55 * alpha, align="center")
    C.text(ctx, label, cx, ty, 26, color="#FFFFFF", bold=True, alpha=alpha, align="center")
    return x0, y0, s, gap


def field_tag(ctx, label, x, y, alpha=1.0, scale=1.0, state="ok"):
    """像素描边的字段标签（从单据飞向屏幕，表示“识别出的字段”）。"""
    if alpha <= 0:
        return
    size = 22 * scale
    tw = C.text_w(ctx, label, size, bold=True)
    w, h = tw + 46 * scale, 38 * scale
    x0, y0 = x - w / 2, y - h / 2
    ctx.rectangle(x0 + 4 * scale, y0 + 4 * scale, w, h)
    ctx.set_source_rgba(0.1, 0.2, 0.25, 0.25 * alpha)
    ctx.fill()
    ctx.rectangle(x0, y0, w, h)
    ctx.set_source_rgba(1, 1, 0.98, 0.95 * alpha)
    ctx.fill()
    col = {"ok": "#5E8C8A", "warn": "#D9574A", "done": "#3F9E58"}[state]
    ctx.set_line_width(3 * scale)
    ctx.rectangle(x0, y0, w, h)
    ctx.set_source_rgba(*hexc(col, alpha))
    ctx.stroke()
    ctx.rectangle(x0 + 9 * scale, y0 + h / 2 - 6 * scale, 12 * scale, 12 * scale)
    ctx.fill()
    C.text(ctx, label, x0 + 30 * scale, y0 + h / 2 + size * 0.36, size, color=C.NAVY, bold=True, alpha=alpha)


def scan_paper(ctx, quad, p, alpha=1.0):
    """沿单据从上到下扫过的一道青色光。quad = 投影后的 [TL,TR,BR,BL]。"""
    if alpha <= 0 or p <= 0:
        return
    q = np.asarray(quad, np.float64)
    a = q[0] + (q[3] - q[0]) * p
    b = q[1] + (q[2] - q[1]) * p
    # 已扫过区域轻微提亮
    ctx.move_to(*q[0]); ctx.line_to(*q[1]); ctx.line_to(*b); ctx.line_to(*a); ctx.close_path()
    ctx.set_source_rgba(0.55, 0.88, 0.80, 0.16 * alpha)
    ctx.fill()
    # 扫描线 + 辉光
    for w_, al in ((22, 0.10), (10, 0.22), (3.5, 0.85)):
        ctx.set_line_width(w_)
        ctx.set_source_rgba(0.56, 0.92, 0.82, al * alpha)
        ctx.move_to(*(a + (a - b) * 0.04)); ctx.line_to(*(b + (b - a) * 0.04))
        ctx.stroke()
    # 四角定位框（像素直角）
    ctx.set_line_width(4)
    ctx.set_source_rgba(0.56, 0.92, 0.82, 0.9 * alpha)
    for k in range(4):
        c = q[k]
        n1, n2 = q[(k + 1) % 4] - c, q[(k + 3) % 4] - c
        ctx.move_to(*(c + n1 * 0.16)); ctx.line_to(*c); ctx.line_to(*(c + n2 * 0.12))
        ctx.stroke()


def bezier(p0, p1, p2, u):
    p0, p1, p2 = map(np.asarray, (p0, p1, p2))
    return (1 - u) ** 2 * p0 + 2 * (1 - u) * u * p1 + u * u * p2


def _mix(c0, c1, u):
    a, b = hexc(c0), hexc(c1)
    return tuple(lerp(a[i], b[i], u) for i in range(3))


def locator(ctx, quad, done, alpha, pulse=0.0):
    """纸面/屏幕上的疑点定位框：红（待核对）→ 绿（已人工确认）。quad = 投影后的四角。"""
    if alpha <= 0:
        return
    q = np.asarray(quad, np.float64)
    col = _mix(C.RED, C.GREEN, done)
    ctx.move_to(*q[0])
    for k in (1, 2, 3):
        ctx.line_to(*q[k])
    ctx.close_path()
    ctx.set_source_rgba(col[0], col[1], col[2], (0.16 + 0.08 * pulse) * alpha)
    ctx.fill()
    ctx.set_line_width(4)
    ctx.set_line_cap(cairo.LINE_CAP_SQUARE)
    ctx.set_source_rgba(col[0], col[1], col[2], alpha)
    for k in range(4):
        c = q[k]
        n1, n2 = q[(k + 1) % 4] - c, q[(k + 3) % 4] - c
        l1, l2 = np.linalg.norm(n1), np.linalg.norm(n2)
        ctx.move_to(*(c + n1 / l1 * min(22, l1 * 0.4))); ctx.line_to(*c); ctx.line_to(*(c + n2 / l2 * min(22, l2 * 0.4)))
        ctx.stroke()


def dotted_link(ctx, p0, p1, done, prog, alpha, t=0.0, arch=-70):
    """像素方点连线：把纸面上的一行与屏幕上的对应字段连起来（左→右流动）。"""
    if alpha <= 0 or prog <= 0:
        return
    p0, p1 = np.asarray(p0, np.float64), np.asarray(p1, np.float64)
    mid = (p0 + p1) / 2 + np.array([0, arch])
    col = _mix(C.RED, C.GREEN, done)
    n = int(np.linalg.norm(p1 - p0) / 20)
    ph = (t * 1.6) % 1.0
    for k in range(n):
        u = (k + ph) / n
        if u > prog:
            break
        p = bezier(p0, mid, p1, u)
        s = 8
        ctx.rectangle(p[0] - s / 2, p[1] - s / 2, s, s)
        ctx.set_source_rgba(1, 1, 1, 0.9 * alpha)
        ctx.fill()
        ctx.rectangle(p[0] - s / 2 + 1.5, p[1] - s / 2 + 1.5, s - 3, s - 3)
        ctx.set_source_rgba(col[0], col[1], col[2], alpha)
        ctx.fill()


def callout(ctx, x, y, done, alpha, anchor=None, w=640):
    """放大的疑点卡片（方块游戏提示框语汇）：让观众看清屏幕上那一行到底在核对什么。
    done=0：红色“收货人地址待核对”；done=1：绿色“已人工确认”（只在人工点击之后出现）。"""
    if alpha <= 0:
        return
    h = 168
    col = _mix(C.RED, C.GREEN, done)
    after = done >= 0.5
    pop = 1.0 + 0.05 * math.sin(min(1.0, done) * math.pi)
    ctx.save()
    ctx.translate(x + w / 2, y + h / 2 + (1 - ease_out(alpha)) * -16)
    ctx.scale(pop, pop)
    ctx.translate(-w / 2, -h / 2)
    # 像素阶梯边框的卡片
    ctx.rectangle(8, 8, w, h)
    ctx.set_source_rgba(0.08, 0.16, 0.22, 0.22 * alpha)
    ctx.fill()
    for inset, c, a in ((0, (0.11, 0.28, 0.40), 1.0), (5, col, 1.0), (11, (1.0, 1.0, 0.98), 0.97)):
        ctx.rectangle(inset, inset, w - 2 * inset, h - 2 * inset)
        ctx.set_source_rgba(c[0], c[1], c[2], a * alpha)
        ctx.fill()
    for cx_, cy_ in ((0, 0), (w - 5, 0), (0, h - 5), (w - 5, h - 5)):      # 削掉四角 → 像素圆角
        ctx.set_operator(cairo.OPERATOR_CLEAR)
        ctx.rectangle(cx_, cy_, 5, 5)
        ctx.fill()
        ctx.set_operator(cairo.OPERATOR_OVER)
    C.text(ctx, "收货人地址", 30, 46, 23, color="#5E8C8A", bold=True, alpha=alpha)
    C.text(ctx, "Consignee address", 160, 46, 20, color="#8FA3AD", alpha=alpha)
    base = "苏州市演示路 18 号  邮编 "
    wv = C.text(ctx, base, 30, 96, 35, color=C.NAVY, bold=True, alpha=alpha)
    if not after:
        w2 = C.text(ctx, "21500", 30 + wv, 96, 35, color=C.RED, bold=True, alpha=alpha)
        ctx.rectangle(30 + wv, 103, w2, 4)
        ctx.set_source_rgba(*hexc(C.RED, alpha))
        ctx.fill()
    else:
        C.text(ctx, "215000", 30 + wv, 96, 35, color=C.GREEN, bold=True, alpha=alpha)
    lab = "已人工确认" if after else "收货人地址待核对"
    tw = C.text_w(ctx, lab, 24, bold=True)
    ctx.rectangle(30, 116, tw + 56, 36)
    ctx.set_source_rgba(col[0], col[1], col[2], alpha)
    ctx.fill()
    if after:
        ctx.set_source_rgba(1, 1, 1, alpha)
        ctx.set_line_width(4)
        ctx.set_line_cap(cairo.LINE_CAP_SQUARE)
        ctx.move_to(42, 134); ctx.line_to(49, 141); ctx.line_to(61, 126)
        ctx.stroke()
    else:
        C.text(ctx, "!", 51, 143, 26, color="#FFFFFF", bold=True, alpha=alpha, align="center")
    C.text(ctx, lab, 72, 143, 24, color="#FFFFFF", bold=True, alpha=alpha)
    hint = "由人点击「确认修订」后生效" if after else "智能体只提示，等人来处理"
    C.text(ctx, hint, w - 28, 142, 20, color="#6B8796", alpha=alpha, align="right")
    ctx.restore()
    if anchor is not None:      # 卡片 → 屏幕疑点行的像素引线
        a0 = np.array([x + w * 0.22, y + h + 6])
        a1 = np.asarray(anchor, np.float64)
        n = max(2, int(np.linalg.norm(a1 - a0) / 16))
        for k in range(n + 1):
            p = a0 + (a1 - a0) * k / n
            ctx.rectangle(p[0] - 3.5, p[1] - 3.5, 7, 7)
            ctx.set_source_rgba(col[0], col[1], col[2], alpha)
            ctx.fill()
