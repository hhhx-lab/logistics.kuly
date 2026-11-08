"""共用工具：缓动、颜色、文字、字幕、产品界面（UI）绘制。

所有可读文字（界面、字幕）都在这里用矢量绘制，保证中文清晰、内容可控。
"""
import math
import os
import cairo
import numpy as np


def _load_env():
    """读取本目录下的 .env（若存在）。已在系统环境中设置的变量优先，不覆盖。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

W, H, FPS = 1920, 1080, 24
FONT = os.environ.get("DOC_ANIM_FONT") or "Noto Sans CJK SC"

# 创意总案锁定的锚色
PAPER = "#F6F4ED"
NAVY = "#1D4867"
TEAL = "#7BA7A5"
SAND = "#D9C8AA"
SUN = "#FFD27A"
FOLD_BLUE = "#9CC7E4"
RED = "#D9534F"
GREEN = "#3E9B7B"


def hexc(h, a=1.0):
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, a)


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def seg(t, a, b):
    """把 t 在 [a,b] 内归一化到 0..1"""
    if b <= a:
        return 1.0 if t >= b else 0.0
    return clamp((t - a) / (b - a))


def ease(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def ease_out(x):
    x = clamp(x)
    return 1 - (1 - x) ** 3


def lerp(a, b, t):
    return a + (b - a) * t


def rrect(ctx, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    ctx.close_path()


def text(ctx, s, x, y, size, color=NAVY, bold=False, alpha=1.0, align="left", font=FONT):
    ctx.select_font_face(font, cairo.FONT_SLANT_NORMAL,
                         cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(size)
    ext = ctx.text_extents(s)
    if align == "center":
        x -= ext.x_advance / 2
    elif align == "right":
        x -= ext.x_advance
    r, g, b, _ = hexc(color)
    ctx.set_source_rgba(r, g, b, alpha)
    ctx.move_to(x, y)
    ctx.show_text(s)
    return ext.x_advance


def text_w(ctx, s, size, bold=False, font=FONT):
    ctx.select_font_face(font, cairo.FONT_SLANT_NORMAL,
                         cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(size)
    return ctx.text_extents(s).x_advance


# ---------------------------------------------------------------- 字幕 / 标题
def caption(ctx, main, x, y, alpha=1.0, tag=None, size=54):
    """左上角标题字幕：浅色磨砂底 + 青色竖条 + 深海蓝字，可带一行小标签。"""
    if alpha <= 0:
        return
    ctx.save()
    wmain = text_w(ctx, main, size, bold=True)
    pad_x, pad_y = 34, 22
    tag_h = 34 if tag else 0
    bw, bh = wmain + pad_x * 2 + 16, size + pad_y * 2 + tag_h
    dy = (1 - ease_out(alpha)) * 18
    ctx.translate(0, dy)
    # 柔和投影
    for i in range(6):
        rrect(ctx, x - i, y - i + 6, bw + 2 * i, bh + 2 * i, 22 + i)
        ctx.set_source_rgba(0.1, 0.2, 0.3, 0.018 * alpha)
        ctx.fill()
    rrect(ctx, x, y, bw, bh, 22)
    ctx.set_source_rgba(1, 1, 0.98, 0.86 * alpha)
    ctx.fill()
    rrect(ctx, x + 16, y + 18, 7, bh - 36, 3.5)
    ctx.set_source_rgba(*hexc(TEAL, alpha))
    ctx.fill()
    tx = x + pad_x + 12
    if tag:
        text(ctx, tag, tx, y + pad_y + 18, 22, color="#5E8C8A", bold=True, alpha=alpha)
    text(ctx, main, tx, y + pad_y + tag_h + size * 0.86, size, color=NAVY, bold=True, alpha=alpha)
    ctx.restore()


# ---------------------------------------------------------------- 产品界面
def _doc_icon(ctx, x, y, s, fold=FOLD_BLUE):
    """带浅蓝折角的单据小图标（与片中主角单据呼应）。"""
    w, h = s * 0.76, s
    f = s * 0.28
    ctx.new_path()
    ctx.move_to(x + f, y)
    ctx.line_to(x + w, y)
    ctx.line_to(x + w, y + h)
    ctx.line_to(x, y + h)
    ctx.line_to(x, y + f)
    ctx.close_path()
    ctx.set_source_rgba(*hexc("#FFFFFF"))
    ctx.fill_preserve()
    ctx.set_source_rgba(*hexc("#B9C7CF"))
    ctx.set_line_width(max(1.0, s * 0.04))
    ctx.stroke()
    ctx.move_to(x + f, y)
    ctx.line_to(x + f, y + f)
    ctx.line_to(x, y + f)
    ctx.close_path()
    ctx.set_source_rgba(*hexc(fold))
    ctx.fill()
    ctx.set_source_rgba(*hexc("#C5D0D6"))
    for i in range(3):
        yy = y + h * (0.48 + i * 0.16)
        ctx.rectangle(x + w * 0.2, yy, w * (0.6 if i < 2 else 0.4), max(1.2, s * 0.05))
        ctx.fill()


def _topbar(ctx, w, title_size=22, h=52, sub=None):
    ctx.rectangle(0, 0, w, h)
    ctx.set_source_rgba(*hexc(NAVY))
    ctx.fill()
    _doc_icon(ctx, 18, 11, 30)
    text(ctx, "物流单证智能体", 54, h / 2 + title_size * 0.36, title_size, color="#FFFFFF", bold=True)
    if sub:
        tw = text_w(ctx, sub, title_size * 0.68)
        rrect(ctx, w - tw - 44, h / 2 - 15, tw + 28, 30, 15)
        ctx.set_source_rgba(1, 1, 1, 0.16)
        ctx.fill()
        text(ctx, sub, w - tw - 30, h / 2 + title_size * 0.25, title_size * 0.68, color="#DCEBF0")


def ui_chat(t, scale=2.0, lw=640, lh=400):
    """镜头 01 的对话工作台。t 为镜头内时间（秒）。返回 ARGB 图层。
    事实边界：只表现“上传已有样稿并发起任务”，不出现自动导出/自动放行。"""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(lw * scale), int(lh * scale))
    ctx = cairo.Context(surf)
    ctx.scale(scale, scale)
    ctx.rectangle(0, 0, lw, lh)
    ctx.set_source_rgba(*hexc("#F4F7F8"))
    ctx.fill()
    _topbar(ctx, lw, sub="演示样票")

    # 助手问候
    rrect(ctx, 20, 68, 330, 46, 14)
    ctx.set_source_rgba(1, 1, 1, 1)
    ctx.fill_preserve()
    ctx.set_source_rgba(*hexc("#DCE5E9"))
    ctx.set_line_width(1.2)
    ctx.stroke()
    text(ctx, "上传单据，告诉我要做什么。", 36, 98, 20, color="#34566B")

    click = 2.2  # 点击触控板的时刻
    a_att = ease_out(seg(t, click, click + 0.35))
    if a_att > 0:
        # 附件卡
        x0, y0 = lw - 20 - 300 + (1 - a_att) * 30, 126
        rrect(ctx, x0, y0, 300, 62, 14)
        ctx.set_source_rgba(1, 1, 1, a_att)
        ctx.fill_preserve()
        ctx.set_source_rgba(*hexc(TEAL, a_att))
        ctx.set_line_width(1.6)
        ctx.stroke()
        ctx.push_group()
        _doc_icon(ctx, x0 + 16, y0 + 10, 42)
        ctx.pop_group_to_source()
        ctx.paint_with_alpha(a_att)
        text(ctx, "DEMO-INV-1008.pdf", x0 + 62, y0 + 28, 19, color=NAVY, bold=True, alpha=a_att)
        text(ctx, "商业发票样稿 · 1 页", x0 + 62, y0 + 50, 15, color="#6B8796", alpha=a_att)
    msg = "请把这票商业发票译成中文，"
    msg2 = "并核对收货人和数量"
    a_msg = seg(t, click + 0.3, click + 0.55)
    if a_msg > 0:
        n = int(seg(t, click + 0.35, click + 1.0) * (len(msg) + len(msg2)) + 0.5)
        s1, s2 = msg[:n], msg2[:max(0, n - len(msg))]
        x0, y0 = lw - 20 - 300, 198
        rrect(ctx, x0, y0, 300, 76, 14)
        ctx.set_source_rgba(*hexc("#D8EAEC", a_msg))
        ctx.fill()
        text(ctx, s1, x0 + 16, y0 + 31, 20.5, color=NAVY, alpha=a_msg)
        text(ctx, s2, x0 + 16, y0 + 60, 20.5, color=NAVY, alpha=a_msg)
    a_rep = ease_out(seg(t, click + 1.05, click + 1.35))
    if a_rep > 0:
        x0, y0 = 20, 288
        rrect(ctx, x0, y0, 290, 50, 14)
        ctx.set_source_rgba(1, 1, 1, a_rep)
        ctx.fill_preserve()
        ctx.set_source_rgba(*hexc("#DCE5E9", a_rep))
        ctx.set_line_width(1.2)
        ctx.stroke()
        text(ctx, "收到，正在识别原文", x0 + 16, y0 + 25, 18, color="#34566B", alpha=a_rep)
        # 扫描进度条（克制的科技感）
        rrect(ctx, x0 + 16, y0 + 35, 258, 5, 2.5)
        ctx.set_source_rgba(*hexc("#E3ECEF", a_rep))
        ctx.fill()
        ph = (t * 0.9) % 1.0
        bx = x0 + 16 + ph * 190
        rrect(ctx, bx, y0 + 35, 68, 5, 2.5)
        ctx.set_source_rgba(*hexc(TEAL, a_rep))
        ctx.fill()

    # 输入栏
    rrect(ctx, 20, lh - 52, lw - 96, 40, 20)
    ctx.set_source_rgba(1, 1, 1, 1)
    ctx.fill_preserve()
    ctx.set_source_rgba(*hexc("#D2DDE2"))
    ctx.set_line_width(1.2)
    ctx.stroke()
    text(ctx, "描述你的单据任务…", 40, lh - 26, 17, color="#9AAEB8")
    ctx.arc(lw - 44, lh - 32, 20, 0, 2 * math.pi)
    ctx.set_source_rgba(*hexc(TEAL))
    ctx.fill()
    ctx.set_source_rgba(1, 1, 1, 1)
    ctx.set_line_width(3)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.move_to(lw - 44, lh - 23)
    ctx.line_to(lw - 44, lh - 41)
    ctx.move_to(lw - 51, lh - 34)
    ctx.line_to(lw - 44, lh - 41)
    ctx.line_to(lw - 37, lh - 34)
    ctx.stroke()
    surf.flush()
    return surf



def _cursor(ctx, x, y, s=1.0, alpha=1.0):
    """像素感的指针箭头。"""
    pts = [(0, 0), (0, 17), (4.5, 13), (8, 20), (11, 18.5), (7.5, 12), (13, 12)]
    ctx.new_path()
    for i, (px, py) in enumerate(pts):
        (ctx.move_to if i == 0 else ctx.line_to)(x + px * s, y + py * s)
    ctx.close_path()
    ctx.set_source_rgba(1, 1, 1, alpha)
    ctx.fill_preserve()
    ctx.set_source_rgba(*hexc("#16303F", alpha))
    ctx.set_line_width(1.6 * s)
    ctx.stroke()


REVIEW_ROWS = [
    ("Shipper", "Nordhafen Demo Trading GmbH", "发货人", "北港演示贸易有限公司"),
    ("Consignee", "Suzhou Demo Storage Co., Ltd.", "收货人", "苏州演示仓储有限公司"),
    ("Consignee address", "No. 18 Demo Rd, Suzhou ", "收货人地址", "苏州市演示路 18 号 邮编 "),
    ("Quantity", "20 CTN · storage boxes, blue", "数量", "20 箱 · 蓝色收纳箱"),
]


def ui_review(t, click=2.1, scale=2.5, lw=640, lh=400):
    """镜头 02 的“原文与译文”复核页。左：识别原文；右：纯中文译文。
    一处红色疑点“收货人地址待核对”，只有在 click 时刻点击“确认修订”之后才变为“已人工确认”。
    全部为虚构样票数据。"""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(lw * scale), int(lh * scale))
    ctx = cairo.Context(surf)
    ctx.scale(scale, scale)
    ctx.rectangle(0, 0, lw, lh)
    ctx.set_source_rgba(*hexc("#F1F5F6"))
    ctx.fill()
    _topbar(ctx, lw, sub="DEMO-INV-1008 · 演示样票")
    done = ease_out(seg(t, click + 0.05, click + 0.4))       # 0→1：点击后状态切换
    pulse = 0.5 + 0.5 * math.sin(t * 7.0)

    cols = [(14, "识别原文", "#6B8796"), (324, "中文译文", TEAL)]
    cw, y0, hh, rh = 302, 62, 34, 58
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
        text(ctx, title, cx + 32, y0 + 23, 17, color=NAVY, bold=True)
        for ri, row in enumerate(REVIEW_ROWS):
            ry = y0 + hh + ri * rh
            label, value = (row[0], row[1]) if ci == 0 else (row[2], row[3])
            if ri == 2:
                # 疑点行：红框（待核对）→ 绿框（已人工确认）
                r_, g_, b_, _ = hexc(RED)
                r2, g2, b2, _ = hexc(GREEN)
                col = (lerp(r_, r2, done), lerp(g_, g2, done), lerp(b_, b2, done))
                rrect(ctx, cx + 6, ry + 1, cw - 12, rh - 4, 8)
                ctx.set_source_rgba(col[0], col[1], col[2], 0.10 + 0.05 * pulse * (1 - done))
                ctx.fill_preserve()
                ctx.set_source_rgba(col[0], col[1], col[2], 1)
                ctx.set_line_width(2.4)
                ctx.stroke()
            elif ri > 0:
                ctx.rectangle(cx + 14, ry, cw - 28, 1)
                ctx.set_source_rgba(*hexc("#E6ECEF"))
                ctx.fill()
            text(ctx, label, cx + 16, ry + 21, 13.5, color="#7F97A3", bold=True)
            wv = text(ctx, value, cx + 16, ry + 45, 17.5 if ci == 0 else 18.5, color=NAVY)
            if ri == 2:
                # 邮编：修订前 21500（红），确认修订后 215000（绿）
                size = 17.5 if ci == 0 else 18.5
                if done < 0.5:
                    w2 = text(ctx, "21500", cx + 16 + wv, ry + 45, size, color=RED, bold=True)
                    ctx.rectangle(cx + 16 + wv, ry + 49, w2, 2)
                    ctx.set_source_rgba(*hexc(RED))
                    ctx.fill()
                else:
                    text(ctx, "215000", cx + 16 + wv, ry + 45, size, color=GREEN, bold=True)
    # 疑点标签（挂在译文列疑点行右上角）
    ry = y0 + hh + 2 * rh
    lab = "收货人地址待核对" if done < 0.5 else "已人工确认"
    colh = RED if done < 0.5 else GREEN
    k = abs(done - 0.5) * 2                                   # 切换瞬间做一次缩放
    tw = text_w(ctx, lab, 14.5, bold=True)
    bw = (tw + 34) * lerp(0.85, 1.0, k)
    bx = 324 + cw - 10 - bw
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

    # 底栏：状态 + 操作按钮
    by = lh - 50
    ctx.arc(26, by + 20, 6, 0, 2 * math.pi)
    ctx.set_source_rgba(*hexc(colh))
    ctx.fill()
    text(ctx, "疑点 1 处 · 待人工处理" if done < 0.5 else "疑点已处理 · 人工确认 1 处", 40, by + 26, 16.5,
         color=NAVY, bold=True)
    press = math.sin(seg(t, click - 0.06, click + 0.16) * math.pi)
    bw_, bh_ = 148, 40
    bx_, by_ = lw - 16 - bw_, by
    if done < 0.5:
        rrect(ctx, bx_ + press * 2, by_ + press * 2, bw_ - press * 4, bh_ - press * 4, 10)
        ctx.set_source_rgba(*hexc("#2F7F86" if press > 0.3 else "#3B8F96"))
        ctx.fill()
        text(ctx, "确认修订", bx_ + bw_ / 2, by_ + 27, 18.5, color="#FFFFFF", bold=True, align="center")
    else:
        rrect(ctx, bx_, by_, bw_, bh_, 10)
        ctx.set_source_rgba(*hexc("#E4F2EC"))
        ctx.fill_preserve()
        ctx.set_source_rgba(*hexc(GREEN))
        ctx.set_line_width(1.6)
        ctx.stroke()
        text(ctx, "已确认", bx_ + bw_ / 2, by_ + 27, 18.5, color=GREEN, bold=True, align="center")
    # 指针：从疑点行移到按钮，点击时有一圈涟漪
    mv = ease(seg(t, click - 1.0, click - 0.25))
    cx_ = lerp(470, bx_ + bw_ * 0.80, mv)
    cy_ = lerp(250, by_ + bh_ * 0.55, mv)
    rp = seg(t, click, click + 0.45)
    if 0 < rp < 1:
        ctx.arc(cx_, cy_, 6 + rp * 26, 0, 2 * math.pi)
        ctx.set_source_rgba(*hexc(TEAL, 0.8 * (1 - rp)))
        ctx.set_line_width(3)
        ctx.stroke()
    _cursor(ctx, cx_, cy_, 1.15)
    surf.flush()
    return surf


def surf_to_rgba(surf):
    """cairo ARGB32(预乘, BGRA 字节序) -> float32 RGBA(非预乘) 数组"""
    w, h = surf.get_width(), surf.get_height()
    buf = np.frombuffer(surf.get_data(), np.uint8).reshape(h, surf.get_stride() // 4, 4)[:, :w]
    a = buf[..., 3:4].astype(np.float32) / 255.0
    rgb = buf[..., [2, 1, 0]].astype(np.float32) / 255.0
    rgb = np.where(a > 1e-4, rgb / np.maximum(a, 1e-4), 0)
    return np.concatenate([rgb, a], axis=2)


def rgb_to_surface(img):
    """float RGB(0..1) HxWx3 -> cairo RGB24 surface（返回 surface 和底层数组，需保持引用）"""
    h, w = img.shape[:2]
    arr = np.empty((h, w, 4), np.uint8)
    u8 = np.clip(img * 255.0 + 0.5, 0, 255).astype(np.uint8)
    arr[..., 0] = u8[..., 2]
    arr[..., 1] = u8[..., 1]
    arr[..., 2] = u8[..., 0]
    arr[..., 3] = 255
    surf = cairo.ImageSurface.create_for_data(memoryview(arr), cairo.FORMAT_ARGB32, w, h, w * 4)
    return surf, arr


def warp_quad(src, quad, out_w, out_h):
    """把 cairo surface 透视贴到四边形 quad=[TL,TR,BR,BL]（设备坐标）。
    返回 (surface, 底层数组, x0, y0)。用于把界面贴进有透视的屏幕。"""
    sw, sh = src.get_width(), src.get_height()
    sbuf = np.frombuffer(src.get_data(), np.uint8).reshape(sh, src.get_stride() // 4, 4)[:, :sw].astype(np.float32)
    q = np.array(quad, np.float64)
    s = np.array([[0, 0], [sw, 0], [sw, sh], [0, sh]], np.float64)
    A = []
    for (x, y), (u, v) in zip(q, s):          # 目标 -> 源 的单应
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    _, _, vt = np.linalg.svd(np.array(A))
    Hm = vt[-1].reshape(3, 3)
    x0, y0 = int(max(0, math.floor(q[:, 0].min()))), int(max(0, math.floor(q[:, 1].min())))
    x1, y1 = int(min(out_w, math.ceil(q[:, 0].max()))), int(min(out_h, math.ceil(q[:, 1].max())))
    if x1 <= x0 or y1 <= y0:
        return None
    xs, ys = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
    d = Hm[2, 0] * xs + Hm[2, 1] * ys + Hm[2, 2]
    u = (Hm[0, 0] * xs + Hm[0, 1] * ys + Hm[0, 2]) / d - 0.5
    v = (Hm[1, 0] * xs + Hm[1, 1] * ys + Hm[1, 2]) / d - 0.5
    inside = (u >= -0.5) & (u <= sw - 0.5) & (v >= -0.5) & (v <= sh - 0.5)
    u = np.clip(u, 0, sw - 1.001)
    v = np.clip(v, 0, sh - 1.001)
    iu, iv = u.astype(np.int32), v.astype(np.int32)
    fu, fv = (u - iu)[..., None], (v - iv)[..., None]
    out = (sbuf[iv, iu] * (1 - fu) * (1 - fv) + sbuf[iv, iu + 1] * fu * (1 - fv)
           + sbuf[iv + 1, iu] * (1 - fu) * fv + sbuf[iv + 1, iu + 1] * fu * fv)
    out *= inside[..., None]
    arr = np.ascontiguousarray(np.clip(out + 0.5, 0, 255).astype(np.uint8))
    surf = cairo.ImageSurface.create_for_data(memoryview(arr), cairo.FORMAT_ARGB32, x1 - x0, y1 - y0, (x1 - x0) * 4)
    return surf, arr, x0, y0
