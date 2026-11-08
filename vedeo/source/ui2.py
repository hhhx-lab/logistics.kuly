"""镜 03–09 的产品界面（各站平板）、放大面板、交接链 HUD、单据飞行粒子尾迹。

事实边界：
- 界面里只出现产品已有的动作：对话问答、原文与译文对照复核（人工点「复核完成」）、单票查看（原件／译本／处理记录）。
- 不画“确认接收”“确认交接”“放行”之类未上线按钮；签字全部发生在独立纸张上，不在界面里。
- 全部为虚构样票 DEMO-INV-1008 的数据。
"""
import math
import cairo
import numpy as np

import common as C
from common import hexc, seg, ease, ease_out, lerp, rrect, text, text_w, _topbar, _doc_icon, _cursor
import hud

LW, LH = 640, 400


def _new(scale):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(LW * scale), int(LH * scale))
    ctx = cairo.Context(surf)
    ctx.scale(scale, scale)
    ctx.rectangle(0, 0, LW, LH)
    ctx.set_source_rgba(*hexc("#F4F7F8"))
    ctx.fill()
    return surf, ctx


def _card(ctx, x, y, w, h, a=1.0, fill="#FFFFFF", line="#DCE5E9", r=14):
    rrect(ctx, x, y, w, h, r)
    ctx.set_source_rgba(*hexc(fill, a))
    ctx.fill_preserve()
    ctx.set_source_rgba(*hexc(line, a))
    ctx.set_line_width(1.2)
    ctx.stroke()


def _check(ctx, x, y, s, a=1.0, col=C.GREEN):
    ctx.arc(x, y, s, 0, 2 * math.pi)
    ctx.set_source_rgba(*hexc(col, a))
    ctx.fill()
    ctx.set_source_rgba(1, 1, 1, a)
    ctx.set_line_width(s * 0.32)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.move_to(x - s * 0.45, y + s * 0.02)
    ctx.line_to(x - s * 0.1, y + s * 0.38)
    ctx.line_to(x + s * 0.48, y - s * 0.34)
    ctx.stroke()


def _input(ctx, typed=""):
    rrect(ctx, 20, LH - 52, LW - 96, 40, 20)
    ctx.set_source_rgba(1, 1, 1, 1)
    ctx.fill_preserve()
    ctx.set_source_rgba(*hexc("#D2DDE2"))
    ctx.set_line_width(1.2)
    ctx.stroke()
    if typed:
        text(ctx, typed, 40, LH - 26, 17, color=C.NAVY)
    else:
        text(ctx, "问问这票单据…", 40, LH - 26, 17, color="#9AAEB8")
    ctx.arc(LW - 44, LH - 32, 20, 0, 2 * math.pi)
    ctx.set_source_rgba(*hexc(C.TEAL))
    ctx.fill()
    ctx.set_source_rgba(1, 1, 1, 1)
    ctx.set_line_width(3)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.move_to(LW - 44, LH - 23); ctx.line_to(LW - 44, LH - 41)
    ctx.move_to(LW - 51, LH - 34); ctx.line_to(LW - 44, LH - 41); ctx.line_to(LW - 37, LH - 34)
    ctx.stroke()


def _doc_chip(ctx, x, y, label="DEMO-INV-1008 · 商业发票"):
    tw = text_w(ctx, label, 14.5, bold=True)
    rrect(ctx, x, y, tw + 50, 30, 15)
    ctx.set_source_rgba(*hexc("#E3EEF0"))
    ctx.fill()
    _doc_icon(ctx, x + 12, y + 6, 18)
    text(ctx, label, x + 36, y + 20, 14.5, color=C.NAVY, bold=True)


def _typed(s, t, t0, dur):
    n = int(seg(t, t0, t0 + dur) * len(s) + 0.5)
    return s[:n]


def _user_bubble(ctx, s, a, y, w=None):
    w = w or (text_w(ctx, s, 18.5) + 32)
    x0 = LW - 20 - w + (1 - a) * 24
    rrect(ctx, x0, y, w, 44, 14)
    ctx.set_source_rgba(*hexc("#D8EAEC", a))
    ctx.fill()
    text(ctx, s, x0 + 16, y + 28, 18.5, color=C.NAVY, alpha=a)


def _thinking(ctx, x, y, t, a):
    for k in range(3):
        b = 0.5 + 0.5 * math.sin(t * 9 - k * 0.9)
        ctx.arc(x + k * 14, y - b * 3, 4, 0, 2 * math.pi)
        ctx.set_source_rgba(*hexc(C.TEAL, a * (0.4 + 0.6 * b)))
        ctx.fill()


def _route(ctx, x, y, a, nodes=("汉堡", "上海", "苏州"), size=21):
    cx = x
    for i, n in enumerate(nodes):
        ctx.arc(cx + 7, y - 7, 6, 0, 2 * math.pi)
        ctx.set_source_rgba(*hexc(C.NAVY if i == 0 else C.TEAL, a))
        ctx.fill()
        w = text(ctx, n, cx + 20, y, size, color=C.NAVY, bold=True, alpha=a)
        cx += 20 + w + 10
        if i < len(nodes) - 1:
            ctx.set_source_rgba(*hexc("#8FB3B8", a))
            ctx.set_line_width(2)
            ctx.move_to(cx, y - 7); ctx.line_to(cx + 30, y - 7)
            ctx.stroke()
            ctx.move_to(cx + 30, y - 12); ctx.line_to(cx + 37, y - 7); ctx.line_to(cx + 30, y - 2)
            ctx.close_path(); ctx.fill()
            cx += 48
    return cx


# ---------------------------------------------------------------- 镜 05 船方：对话问同一票
SHIP_Q = "这票货发往哪里？有需要注意的字段吗？"


def ui_ship(t, t_q=0.9, scale=2.0):
    """船方负责人在同一票上提问。t_q：开始输入问题的时刻。"""
    surf, ctx = _new(scale)
    _topbar(ctx, LW, sub="演示样票")
    _doc_chip(ctx, 20, 64)
    typing = seg(t, t_q, t_q + 0.9)
    sent = t >= t_q + 1.0
    if 0 < typing and not sent:
        _input(ctx, _typed(SHIP_Q, t, t_q, 0.9))
    else:
        _input(ctx)
    if sent:
        a = ease_out(seg(t, t_q + 1.0, t_q + 1.25))
        _user_bubble(ctx, SHIP_Q, a, 106)
    ta = t_q + 1.35
    if sent and t < ta + 0.35:
        _card(ctx, 20, 164, 86, 40)
        _thinking(ctx, 44, 188, t, 1.0)
    ra = ease_out(seg(t, ta + 0.35, ta + 0.7))
    if ra > 0:
        y0 = 164 + (1 - ra) * 12
        _card(ctx, 20, y0, 452, 168, ra)
        text(ctx, "运输路线", 38, y0 + 30, 14, color="#7F97A3", bold=True, alpha=ra)
        _route(ctx, 38, y0 + 62, ra)
        a2 = ease_out(seg(t, ta + 0.6, ta + 0.9))
        _check(ctx, 46, y0 + 92, 8, ra * a2)
        text(ctx, "收货人地址：已由发货方人工确认", 62, y0 + 98, 17, color=C.NAVY, alpha=ra * a2)
        _check(ctx, 46, y0 + 120, 8, ra * a2, col=C.TEAL)
        text(ctx, "数量：20 箱 · 蓝色收纳箱", 62, y0 + 126, 17, color=C.NAVY, alpha=ra * a2)
        a3 = ease_out(seg(t, ta + 0.85, ta + 1.1))
        rrect(ctx, 38, y0 + 138, 236, 22, 11)
        ctx.set_source_rgba(*hexc("#EEF3F5", ra * a3))
        ctx.fill()
        text(ctx, "来源：商业发票／装箱单  ›", 50, y0 + 154, 14, color="#5E8C8A", bold=True, alpha=ra * a3)
    surf.flush()
    return surf


# ---------------------------------------------------------------- 镜 06 口岸：原文与译文复核
PORT_ROWS = [
    ("Consignee", "Suzhou Demo Storage Co., Ltd.", "收货人", "苏州演示仓储有限公司", "ok"),
    ("Consignee address", "No. 18 Demo Rd, Suzhou 215000", "收货人地址", "苏州市演示路 18 号 215000", "prev"),
    ("Quantity", "20 CTN", "数量", "20 箱", "flag"),
    ("Description", "Storage boxes, blue", "品名", "蓝色收纳箱", "ok"),
]


def ui_port(t, t_flag=0.9, click=3.0, scale=2.0):
    """上海口岸单证核验（情景演示）：原文与译文逐项对照。
    第 3 行（数量单位 CTN↔箱）黄色高亮“请复核”，人点「复核完成」后才显示“已人工确认”。"""
    surf, ctx = _new(scale)
    ctx.rectangle(0, 0, LW, LH)
    ctx.set_source_rgba(*hexc("#F1F5F6"))
    ctx.fill()
    _topbar(ctx, LW, sub="原文与译文")
    done = ease_out(seg(t, click + 0.05, click + 0.4))
    pulse = 0.5 + 0.5 * math.sin(t * 7.0)
    scan = seg(t, 0.2, t_flag)               # 逐行核对的扫描进度
    cols = [(14, "原文", "#6B8796"), (324, "中文译文", C.TEAL)]
    cw, y0, hh, rh = 302, 62, 34, 58
    AMB = "#E0A526"
    for ci, (cx, title, dot) in enumerate(cols):
        rrect(ctx, cx, y0, cw, hh + rh * 4 + 8, 12)
        ctx.set_source_rgba(1, 1, 1, 1)
        ctx.fill_preserve()
        ctx.set_source_rgba(*hexc("#D5DFE4"))
        ctx.set_line_width(1.2)
        ctx.stroke()
        ctx.rectangle(cx + 14, y0 + 12, 10, 10)
        ctx.set_source_rgba(*hexc(dot))
        ctx.fill()
        text(ctx, title, cx + 32, y0 + 23, 17, color=C.NAVY, bold=True)
        for ri, row in enumerate(PORT_ROWS):
            ry = y0 + hh + ri * rh
            label, value = (row[0], row[1]) if ci == 0 else (row[2], row[3])
            seen = scan * 4 > ri + 0.6
            if row[4] == "flag" and seen:
                c0, c1 = hexc(AMB), hexc(C.GREEN)
                col = tuple(lerp(c0[k], c1[k], done) for k in range(3))
                rrect(ctx, cx + 6, ry + 1, cw - 12, rh - 4, 8)
                ctx.set_source_rgba(col[0], col[1], col[2], 0.12 + 0.06 * pulse * (1 - done))
                ctx.fill_preserve()
                ctx.set_source_rgba(col[0], col[1], col[2], 1)
                ctx.set_line_width(2.4)
                ctx.stroke()
            elif ri > 0:
                ctx.rectangle(cx + 14, ry, cw - 28, 1)
                ctx.set_source_rgba(*hexc("#E6ECEF"))
                ctx.fill()
            text(ctx, label, cx + 16, ry + 21, 13.5, color="#7F97A3", bold=True)
            text(ctx, value, cx + 16, ry + 45, 17.5 if ci == 0 else 18.5, color=C.NAVY)
            if ci == 1 and seen and row[4] in ("ok", "prev"):
                _check(ctx, cx + cw - 24, ry + 30, 7.5, 1.0, col=C.TEAL if row[4] == "ok" else C.GREEN)
        # 逐行扫描线
        if 0 < scan < 1:
            sy = y0 + hh + scan * rh * 4
            ctx.rectangle(cx + 6, sy - 1.5, cw - 12, 3)
            ctx.set_source_rgba(*hexc(C.TEAL, 0.7))
            ctx.fill()
    # 已在汉堡人工确认过的那一行：小字注明来源（处理记录）
    ry = y0 + hh + 1 * rh
    if scan > 0.5:
        lab = "发货方已人工确认"
        tw = text_w(ctx, lab, 12.5, bold=True)
        rrect(ctx, 324 + cw - 40 - tw - 14, ry + 6, tw + 14, 20, 10)
        ctx.set_source_rgba(*hexc("#E4F2EC"))
        ctx.fill()
        text(ctx, lab, 324 + cw - 40 - tw - 7, ry + 20.5, 12.5, color=C.GREEN, bold=True)
    # 待复核标签
    ry = y0 + hh + 2 * rh
    if scan * 4 > 2.6:
        lab = "单位 CTN ↔ 箱 · 请复核" if done < 0.5 else "已人工确认"
        colh = AMB if done < 0.5 else C.GREEN
        k = abs(done - 0.5) * 2
        tw = text_w(ctx, lab, 14.5, bold=True)
        bw = (tw + 34) * lerp(0.85, 1.0, k)
        bx = 324 + cw - 10 - bw
        ry += 18
        rrect(ctx, bx, ry - 11, bw, 24, 12)
        ctx.set_source_rgba(*hexc(colh))
        ctx.fill()
        if done < 0.5:
            text(ctx, "!", bx + 14, ry + 6.5, 15, color="#FFFFFF", bold=True, align="center")
        else:
            ctx.set_source_rgba(1, 1, 1, 1)
            ctx.set_line_width(2.4)
            ctx.move_to(bx + 9, ry + 1); ctx.line_to(bx + 13, ry + 5.5); ctx.line_to(bx + 20, ry - 3.5)
            ctx.stroke()
        text(ctx, lab, bx + 25, ry + 6.5, 14.5, color="#FFFFFF", bold=True)
    # 底栏
    by = LH - 50
    colh = AMB if done < 0.5 else C.GREEN
    ctx.arc(26, by + 20, 6, 0, 2 * math.pi)
    ctx.set_source_rgba(*hexc(colh if scan > 0.7 else "#9AAEB8"))
    ctx.fill()
    msg = ("逐项核对中…" if scan < 0.7 else "待人工复核 1 处") if done < 0.5 else "复核完成 · 已人工确认"
    text(ctx, msg, 40, by + 18, 16.5, color=C.NAVY, bold=True)
    text(ctx, "辅助核对 · 结论由人工作出", 40, by + 38, 12.5, color="#7F97A3")
    press = math.sin(seg(t, click - 0.06, click + 0.16) * math.pi)
    bw_, bh_ = 148, 40
    bx_, by_ = LW - 16 - bw_, by
    if done < 0.5:
        rrect(ctx, bx_ + press * 2, by_ + press * 2, bw_ - press * 4, bh_ - press * 4, 10)
        ctx.set_source_rgba(*hexc("#2F7F86" if press > 0.3 else "#3B8F96"))
        ctx.fill()
        text(ctx, "复核完成", bx_ + bw_ / 2, by_ + 27, 18.5, color="#FFFFFF", bold=True, align="center")
    else:
        rrect(ctx, bx_, by_, bw_, bh_, 10)
        ctx.set_source_rgba(*hexc("#E4F2EC"))
        ctx.fill_preserve()
        ctx.set_source_rgba(*hexc(C.GREEN))
        ctx.set_line_width(1.6)
        ctx.stroke()
        _check(ctx, bx_ + 30, by_ + 20, 8)
        text(ctx, "复核完成", bx_ + bw_ / 2 + 12, by_ + 27, 18.5, color=C.GREEN, bold=True, align="center")
    rp = seg(t, click, click + 0.45)
    if 0 < rp < 1:      # 手指点按的涟漪（触屏，无鼠标指针）
        ctx.arc(bx_ + bw_ * 0.6, by_ + bh_ * 0.55, 6 + rp * 26, 0, 2 * math.pi)
        ctx.set_source_rgba(*hexc(C.TEAL, 0.8 * (1 - rp)))
        ctx.set_line_width(3)
        ctx.stroke()
    surf.flush()
    return surf


PORT_TAP = (LW - 16 - 148 * 0.4, LH - 50 + 22)        # 「复核完成」按钮在界面坐标中的位置


# ---------------------------------------------------------------- 镜 07 铁路：对话问交给谁
RAIL_Q = "这票货交给谁？"


def ui_rail(t, t_q=0.8, scale=2.0):
    surf, ctx = _new(scale)
    _topbar(ctx, LW, sub="演示样票")
    _doc_chip(ctx, 20, 64)
    typing = seg(t, t_q, t_q + 0.5)
    sent = t >= t_q + 0.6
    if 0 < typing and not sent:
        _input(ctx, _typed(RAIL_Q, t, t_q, 0.5))
    else:
        _input(ctx)
    if sent:
        _user_bubble(ctx, RAIL_Q, ease_out(seg(t, t_q + 0.6, t_q + 0.85)), 106)
    ta = t_q + 0.9
    if sent and t < ta + 0.3:
        _card(ctx, 20, 164, 86, 40)
        _thinking(ctx, 44, 188, t, 1.0)
    ra = ease_out(seg(t, ta + 0.3, ta + 0.65))
    if ra > 0:
        y0 = 164 + (1 - ra) * 12
        _card(ctx, 20, y0, 452, 168, ra)
        text(ctx, "收货人", 38, y0 + 30, 14, color="#7F97A3", bold=True, alpha=ra)
        text(ctx, "苏州演示仓储有限公司", 38, y0 + 62, 22, color=C.NAVY, bold=True, alpha=ra)
        a2 = ease_out(seg(t, ta + 0.55, ta + 0.85))
        _check(ctx, 46, y0 + 92, 8, ra * a2, col=C.TEAL)
        text(ctx, "20 箱 · 蓝色收纳箱 · 苏州市演示路 18 号", 62, y0 + 98, 16.5, color=C.NAVY, alpha=ra * a2)
        _check(ctx, 46, y0 + 120, 8, ra * a2)
        text(ctx, "处理记录 3 条：人工确认 2 处", 62, y0 + 126, 16.5, color=C.NAVY, alpha=ra * a2)
        a3 = ease_out(seg(t, ta + 0.8, ta + 1.05))
        rrect(ctx, 38, y0 + 138, 236, 22, 11)
        ctx.set_source_rgba(*hexc("#EEF3F5", ra * a3))
        ctx.fill()
        text(ctx, "来源：商业发票／处理记录  ›", 50, y0 + 154, 14, color="#5E8C8A", bold=True, alpha=ra * a3)
    surf.flush()
    return surf


# ---------------------------------------------------------------- 镜 09 收货：单票视图
RECORDS = [
    ("汉堡 · 发货方", "人工确认收货人地址"),
    ("海运 · 船方", "查阅同票摘要"),
    ("上海 · 口岸单证", "原译对照 · 人工复核完成"),
    ("铁路 · 陆运", "查阅收货人信息"),
]


def ui_receive(t, tap=1.2, scale=2.0):
    """收货人查看单票：「商业发票 · 中文译本」，标签 原件／译本／处理记录；tap 时刻切到处理记录。"""
    surf, ctx = _new(scale)
    _topbar(ctx, LW, sub="DEMO-INV-1008")
    text(ctx, "商业发票 · 中文译本", 22, 92, 22, color=C.NAVY, bold=True)
    sw = ease(seg(t, tap + 0.05, tap + 0.35))
    tabs = ["原件", "译本", "处理记录"]
    tx = 22
    xs = []
    for i, tb in enumerate(tabs):
        w = text_w(ctx, tb, 16, bold=True) + 30
        xs.append((tx, w))
        tx += w + 8
    ax = lerp(xs[1][0], xs[2][0], sw)
    aw = lerp(xs[1][1], xs[2][1], sw)
    rrect(ctx, ax, 106, aw, 32, 16)
    ctx.set_source_rgba(*hexc(C.NAVY))
    ctx.fill()
    for i, tb in enumerate(tabs):
        x, w = xs[i]
        on = (i == 1 and sw < 0.5) or (i == 2 and sw >= 0.5)
        text(ctx, tb, x + w / 2, 128, 16, color="#FFFFFF" if on else "#5E7A88", bold=True, align="center")
    # 内容：译本 → 处理记录（横向滑动切换）
    ctx.save()
    ctx.rectangle(14, 148, LW - 28, LH - 160)
    ctx.clip()
    off = -sw * (LW - 20)
    rows = [("收货人", "苏州演示仓储有限公司"), ("收货人地址", "苏州市演示路 18 号 215000"),
            ("数量", "20 箱"), ("品名", "蓝色收纳箱")]
    _card(ctx, 14 + off, 150, LW - 28, LH - 164)
    for i, (k, v) in enumerate(rows):
        y = 186 + i * 50
        text(ctx, k, 34 + off, y, 14, color="#7F97A3", bold=True)
        text(ctx, v, 34 + off, y + 24, 19, color=C.NAVY)
        if i:
            ctx.rectangle(30 + off, y - 22, LW - 60, 1)
            ctx.set_source_rgba(*hexc("#E6ECEF"))
            ctx.fill()
    o2 = off + (LW - 20)
    _card(ctx, 14 + o2, 150, LW - 28, LH - 164)
    for i, (who, what) in enumerate(RECORDS):
        y = 186 + i * 52
        a = ease_out(seg(t, tap + 0.35 + i * 0.16, tap + 0.6 + i * 0.16)) if sw > 0.5 else 1.0
        if i < len(RECORDS) - 1:
            ctx.rectangle(44 + o2, y + 2, 3, 50)
            ctx.set_source_rgba(*hexc("#CFE0E2", a))
            ctx.fill()
        _check(ctx, 45.5 + o2, y - 4, 9, a, col=C.GREEN if "人工" in what else C.TEAL)
        text(ctx, who, 66 + o2, y + 2, 17, color=C.NAVY, bold=True, alpha=a)
        text(ctx, what, 250 + o2, y + 2, 17, color="#3F6475", alpha=a)
    ctx.restore()
    rp = seg(t, tap, tap + 0.45)
    if 0 < rp < 1:
        x, w = xs[2]
        ctx.arc(x + w / 2, 122, 6 + rp * 24, 0, 2 * math.pi)
        ctx.set_source_rgba(*hexc(C.TEAL, 0.8 * (1 - rp)))
        ctx.set_line_width(3)
        ctx.stroke()
    surf.flush()
    return surf


def receive_tab_pt():
    """「处理记录」标签在界面坐标中的位置（用于手指点按）。"""
    tmp = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8))
    x = 22
    for tb in ["原件", "译本"]:
        x += text_w(tmp, tb, 16, bold=True) + 30 + 8
    return x + (text_w(tmp, "处理记录", 16, bold=True) + 30) / 2, 122


# ---------------------------------------------------------------- 放大面板（片中图形，不是产品界面）
def panel(ctx, ui_surf, x, y, w, alpha, anchor=None, title=None, col=C.TEAL):
    """把平板界面放大显示在画面一角，像素描边 + 引线指向平板，方便观众看清。"""
    if alpha <= 0:
        return
    sw, sh = ui_surf.get_width(), ui_surf.get_height()
    s = w / sw
    h = sh * s
    ctx.save()
    dy = (1 - ease_out(alpha)) * 18
    ctx.translate(0, dy)
    pad = 10
    ctx.rectangle(x - pad + 8, y - pad + 8, w + 2 * pad, h + 2 * pad)
    ctx.set_source_rgba(0.08, 0.16, 0.22, 0.22 * alpha)
    ctx.fill()
    c = hexc(col)
    for inset, cc in ((0, (0.11, 0.28, 0.40)), (5, c[:3])):
        ctx.rectangle(x - pad + inset, y - pad + inset, w + 2 * (pad - inset), h + 2 * (pad - inset))
        ctx.set_source_rgba(cc[0], cc[1], cc[2], alpha)
        ctx.fill()
    ctx.save()
    ctx.translate(x, y)
    ctx.scale(s, s)
    ctx.set_source_surface(ui_surf, 0, 0)
    ctx.get_source().set_filter(cairo.FILTER_GOOD)
    ctx.paint_with_alpha(alpha)
    ctx.restore()
    if title:
        tw = text_w(ctx, title, 20, bold=True)
        ctx.rectangle(x - pad, y - pad - 38, tw + 30, 34)
        ctx.set_source_rgba(0.11, 0.28, 0.40, 0.92 * alpha)
        ctx.fill()
        text(ctx, title, x - pad + 15, y - pad - 14, 20, color="#FFFFFF", bold=True, alpha=alpha)
    ctx.restore()
    if anchor is not None:
        # 引线从面板最近的边出发
        a1 = np.asarray(anchor, np.float64)
        ax = min(max(a1[0], x), x + w)
        ay = y + h + pad + 4 + dy if a1[1] > y + h else y - pad - 4 + dy
        a0 = np.array([ax, ay])
        if x <= a1[0] <= x + w and y <= a1[1] <= y + h:
            return
        n = max(2, int(np.linalg.norm(a1 - a0) / 16))
        for k in range(n + 1):
            p = a0 + (a1 - a0) * k / n
            ctx.rectangle(p[0] - 3.5, p[1] - 3.5, 7, 7)
            ctx.set_source_rgba(c[0], c[1], c[2], alpha * (0.5 + 0.5 * (k / n)))
            ctx.fill()
        ctx.rectangle(a1[0] - 8, a1[1] - 8, 16, 16)
        ctx.set_source_rgba(1, 1, 1, alpha)
        ctx.fill()
        ctx.rectangle(a1[0] - 5, a1[1] - 5, 10, 10)
        ctx.set_source_rgba(c[0], c[1], c[2], alpha)
        ctx.fill()


# ---------------------------------------------------------------- 交接链 HUD
# 像素头像：发色、肤色、上衣色、发型
AVATARS = [
    dict(hair="#5A3A2A", skin="#F1C9A5", top="#F5EFE1", acc="#1D4867", style="bob"),        # 汉堡发货人 Elena
    dict(hair="#2B2522", skin="#EDC6A2", top="#F6F6F2", acc="#24506F", style="short"),      # 船方 Lin
    dict(hair="#2E2420", skin="#F2CDB0", top="#BFD8EC", acc="#2F6F8F", style="long"),       # 口岸 Mei
    dict(hair="#26201C", skin="#E3B892", top="#6E8EA6", acc="#F28A3A", style="helmet"),     # 铁路 Chen
    dict(hair="#1F1B19", skin="#EEC7A4", top="#8FA889", acc="#7A9474", style="short"),      # 收货人 Zhou
]
NODES = [("汉堡", "发货人", "人工确认"), ("海运", "船方", "已读同票"), ("上海口岸", "单证核验", "人工复核"),
         ("铁路", "陆运", "已交接"), ("苏州", "收货人", "已签收")]


def pixel_avatar(ctx, x, y, px, av, alpha=1.0):
    rows = ["....HHHH....", "...HHHHHH...", "..HHHHHHHH..", "..HSSSSSSH..", "..SSESSESS..", "..SSSSSSSS..",
            "..SSSMMSSS..", "...SSSSSS...", "..TTTAATTT..", ".TTTTAATTTT.", ".TTTTAATTTT.", ".TTTTAATTTT."]
    if av["style"] == "bob":
        rows[3] = ".HHSSSSSSHH."; rows[4] = ".HSSESSESSH."; rows[5] = ".HSSSSSSSSH."
    if av["style"] == "bun":
        rows[0] = ".....HH....."; rows[1] = "...HHHHHH..."
    if av["style"] == "long":
        rows[3] = ".HHSSSSSSHH."; rows[4] = ".HSSESSESSH."; rows[5] = ".HSSSSSSSSH."
        rows[6] = ".HSSSMMSSSH."; rows[7] = ".HHSSSSSSHH."; rows[8] = ".HTTTAATTTH."
    if av["style"] == "helmet":
        rows[0] = "....WWWW...."; rows[1] = "...WWWWWW..."; rows[2] = ".WWWWWWWWWW."
        rows[9] = ".AAATTTTAAA."; rows[10] = ".AAATTTTAAA."; rows[11] = ".ATATTTTATA."
        rows[8] = "..AATTTTAA.."
    pal = {"H": av["hair"], "S": av["skin"], "E": "#2A2420", "M": "#C97A6E", "T": av["top"], "A": av["acc"],
           "W": "#F4F4F0"}
    for j, r in enumerate(rows):
        for i, ch in enumerate(r):
            if ch == ".":
                continue
            ctx.rectangle(x + i * px, y + j * px, px + 0.3, px + 0.3)
            ctx.set_source_rgba(*hexc(pal[ch], alpha))
            ctx.fill()


def _mini_ship(ctx, x, y, s, a):
    ctx.set_source_rgba(*hexc("#1D4867", a))
    ctx.move_to(x - 12 * s, y); ctx.line_to(x + 12 * s, y); ctx.line_to(x + 9 * s, y + 5 * s); ctx.line_to(x - 9 * s, y + 5 * s)
    ctx.close_path(); ctx.fill()
    for k, c in enumerate(["#D9C8AA", "#7BA7A5", "#C9665A"]):
        ctx.rectangle(x - 10 * s + k * 5 * s, y - 4 * s, 4.4 * s, 4 * s)
        ctx.set_source_rgba(*hexc(c, a)); ctx.fill()
    ctx.rectangle(x + 6 * s, y - 7 * s, 4 * s, 7 * s)
    ctx.set_source_rgba(*hexc("#F4F1E8", a)); ctx.fill()


def _mini_train(ctx, x, y, s, a):
    ctx.rectangle(x + 4 * s, y - 7 * s, 9 * s, 7 * s)
    ctx.set_source_rgba(*hexc("#2F6F8F", a)); ctx.fill()
    for k, c in enumerate(["#D9C8AA", "#1D4867"]):
        ctx.rectangle(x - 14 * s + k * 9 * s, y - 5 * s, 8 * s, 5 * s)
        ctx.set_source_rgba(*hexc(c, a)); ctx.fill()
    ctx.set_source_rgba(*hexc("#33393E", a))
    for k in range(5):
        ctx.arc(x - 12 * s + k * 6 * s, y + 1.5 * s, 1.6 * s, 0, 6.283); ctx.fill()


def chain_hud(ctx, alpha, pos, done, active=None, pulse=0.0, t=0.0, y=None):
    """底部交接链：5 个人物节点，单据标记沿链条前进。
    pos：单据标记位置（0..4，可为小数）；done：已完成节点集合；active：当前高亮节点。
    这是片中解说图形，不是产品界面。"""
    if alpha <= 0:
        return
    n, s, gap = 5, 64, 158
    total = n * s + (n - 1) * gap
    x0 = (C.W - total) / 2
    y0 = (y if y is not None else C.H - 122) + (1 - ease_out(alpha)) * 30
    hud._bevel(ctx, x0 - 70, y0 - 10, total + 140, s + 62, alpha, fill=(0.10, 0.15, 0.19, 0.46))
    cy = y0 + s / 2
    # 连线
    for i in range(n - 1):
        xa, xb = x0 + i * (s + gap) + s + 8, x0 + (i + 1) * (s + gap) - 8
        prog = clampf(pos - i)
        for k in range(int((xb - xa) / 12)):
            px_ = xa + k * 12
            on = (px_ - xa) / (xb - xa) <= prog
            ctx.rectangle(px_, cy - 3, 7, 6)
            ctx.set_source_rgba(*(hexc("#8FE0C8", alpha) if on else (1, 1, 1, 0.28 * alpha)))
            ctx.fill()
        mx = (xa + xb) / 2
        if i == 0:
            _mini_ship(ctx, mx, cy - 16, 1.2, alpha * 0.9)
        elif i == 3:
            _mini_train(ctx, mx, cy - 14, 1.2, alpha * 0.9)
        elif i == 1:
            _mini_ship(ctx, mx, cy - 16, 1.2, alpha * 0.9)
    # 节点
    for i in range(n):
        x = x0 + i * (s + gap)
        is_act = active is not None and abs(i - active) < 0.5
        hud._bevel(ctx, x, y0, s, s, alpha, fill=(0.55, 0.62, 0.66, 0.55 if (i in done or is_act) else 0.30))
        ia = alpha * (1.0 if (i in done or is_act) else 0.5)
        pixel_avatar(ctx, x + 5, y0 + 5, 4.5, AVATARS[i], ia)
        if i in done:
            hud._check_badge(ctx, x + s - 12, y0 + 12, 20, alpha)
        if is_act:
            lw = 5 + 2 * pulse
            ctx.set_line_width(lw)
            ctx.set_source_rgba(*hexc("#FFD27A", alpha))
            ctx.rectangle(x - 4, y0 - 4, s + 8, s + 8)
            ctx.stroke()
        place, role, st = NODES[i]
        lab = place + "·" + role
        C.text(ctx, lab, x + s / 2, y0 + s + 22, 16, color="#FFFFFF", bold=True, alpha=ia, align="center")
        if i in done:
            C.text(ctx, st, x + s / 2, y0 + s + 43, 14.5, color="#9BE3C4", bold=True, alpha=alpha, align="center")
        elif is_act:
            C.text(ctx, "进行中", x + s / 2, y0 + s + 43, 14.5, color="#FFD27A", bold=True, alpha=alpha, align="center")
    # 单据标记（带蓝折角）沿链条移动
    seg_i = min(n - 2, int(math.floor(pos)))
    f = pos - seg_i
    xa = x0 + seg_i * (s + gap) + s / 2
    xb = x0 + (seg_i + 1) * (s + gap) + s / 2
    mx = lerp(xa, xb, f)
    my = y0 - 26 - 8 * math.sin(f * math.pi) - 2 * math.sin(t * 5)
    ctx.save()
    ctx.translate(mx - 13, my - 17)
    ctx.push_group()
    _doc_icon(ctx, 0, 0, 34)
    ctx.pop_group_to_source()
    ctx.paint_with_alpha(alpha)
    ctx.restore()
    return x0, y0


def clampf(x):
    return max(0.0, min(1.0, x))


# ---------------------------------------------------------------- 单据飞行尾迹
def sparkle_trail(ctx, pts, alpha=1.0, seed=3):
    """pts：从新到旧的屏幕坐标序列；画出青色/白色像素方块尾迹。"""
    rng = np.random.RandomState(seed)
    n = len(pts)
    for k, p in enumerate(pts):
        if k == 0:
            continue
        f = 1 - k / n
        for j in range(2):
            ox, oy = (rng.rand() - 0.5) * 30 * (1 - f + 0.3), (rng.rand() - 0.5) * 30 * (1 - f + 0.3)
            sz = 4 + 7 * f * rng.rand()
            col = "#BFF3E4" if (k + j) % 3 else "#FFFFFF"
            ctx.rectangle(p[0] + ox - sz / 2, p[1] + oy - sz / 2, sz, sz)
            ctx.set_source_rgba(*hexc(col, alpha * f * 0.85))
            ctx.fill()


def place_label(ctx, s, x, y, alpha=1.0):
    """左上角地点小标签（片中字幕层，不在产品界面内）。"""
    if alpha <= 0:
        return
    tw = text_w(ctx, s, 22, bold=True)
    rrect(ctx, x, y, tw + 40, 44, 22)
    ctx.set_source_rgba(0.11, 0.28, 0.40, 0.78 * alpha)
    ctx.fill()
    ctx.arc(x + 20, y + 22, 5, 0, 2 * math.pi)
    ctx.set_source_rgba(*hexc("#FFD27A", alpha))
    ctx.fill()
    text(ctx, s, x + 32, y + 30, 22, color="#FFFFFF", bold=True, alpha=alpha)
