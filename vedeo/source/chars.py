"""通用方块角色、飘动的单据（带透明夹）、平板电脑。全部原创像素，不使用任何第三方游戏素材。

角色比例与镜 01–02 的 Elena 一致：头 320、躯干 480、手臂由肩部指向目标点。
"""
import math
import numpy as np

import scene as S
from voxel import Xf, rot_y, rot_x, rot_z, box, Quad, ascii_tex, noise_tex, solid, hex3, norm

# ------------------------------------------------------------------ 角色设定
# 角色只用虚构人物；服装无徽章、无品牌、无官方标识。
CHARS = {
    # 船方负责人 Lin：深蓝马甲 + 白衬衫，短黑发
    "lin": dict(hair="#2B2522", hair_d="#1A1513", hair_l="#45392F", skin="#EDC6A2", skin_d="#D8A884",
                eye="#3B2A20", eye_d="#21160F", style="short", top="vest",
                shirt="#F6F6F2", shirt_d="#E1E4E6", vest="#24506F", vest_d="#1A3D56", pants="#3A4248"),
    # 口岸单证审核 Mei：浅蓝衬衫、无徽记挂绳，深色头发扎起
    "mei": dict(hair="#2E2420", hair_d="#1C1512", hair_l="#4A3A31", skin="#F2CDB0", skin_d="#DDAE8E",
                eye="#3B2A20", eye_d="#21160F", style="bun", top="lanyard", lash=True, long=True,
                shirt="#BFD8EC", shirt_d="#A6C4DC", lanyard="#2F6F8F", pants="#5A6670"),
    # 铁路货运负责人 Chen：橙色反光背心 + 灰蓝工作衫 + 白色安全帽（无标识）
    "chen": dict(hair="#26201C", hair_d="#171310", hair_l="#3E342D", skin="#E3B892", skin_d="#CC9B74",
                 eye="#3B2A20", eye_d="#21160F", style="short", top="hivis", helmet="#F4F4F0",
                 shirt="#6E8EA6", shirt_d="#5C7A90", vest="#F28A3A", vest_d="#D9732A", pants="#3F4A55"),
    # 苏州收货人 Zhou：灰绿色工装外套，短黑发
    "zhou": dict(hair="#1F1B19", hair_d="#121010", hair_l="#38302B", skin="#EEC7A4", skin_d="#D9AA86",
                 eye="#3B2A20", eye_d="#21160F", style="short", top="jacket",
                 shirt="#8FA889", shirt_d="#7A9474", pants="#3B4650"),
}

_CACHE = {}


def _pal(ch):
    p = dict(S.PAL)
    p.update({"H": ch["hair"], "h": ch["hair_d"], "L": ch["hair_l"], "S": ch["skin"], "s": ch["skin_d"],
              "E": ch["eye"], "e": ch["eye_d"], "b": ch["hair_d"],
              "I": ch["shirt"], "i": ch["shirt_d"], "C": ch["shirt_d"], "d": ch["shirt_d"], "o": "#9AA3A8",
              "N": ch["pants"], "n": ch["pants"],
              "V": ch.get("vest", ch["shirt"]), "v": ch.get("vest_d", ch["shirt_d"]),
              "R": "#E9EEF0", "Y": ch.get("lanyard", "#2F6F8F"), "c": "#FFFFFF", "g": "#9FB0BA",
              "Z": "#5F6E66", "J": ch["shirt"], "j": ch["shirt_d"], ".": None})
    return p


def _face_rows(ch, look, blink):
    eye = {"right": ("WEe", "WEE"), "left": ("eEW", "EEW"), "down": ("WWW", "EeW"),
           "center": ("EeW", "EEW"), "up": ("EeW", "WWW")}[look]
    lash = "PPP" if ch.get("lash") else "SSS"
    if blink:
        l8, l9, l10 = "SSS", "PPP", "SSS"
    else:
        l8, l9, l10 = lash, eye[0], eye[1]
    if ch["style"] == "bun":
        top = ["HHHHHHHHHHHHHHHH", "HHHHHHHHHHHHHHHH", "HHHLHHHHhHHHLHHH", "HHHHHHHhHHHHHHHH",
               "HHHHHHhHHHHHHHHH", "HHSSSHhSSSSSSSHH", "HSSSSSSSSSSSSSSH"]
        side = "H"
    else:
        top = ["HHHHHHHHHHHHHHHH", "HHHHHHHHHHHHHHHH", "HHLLHHHHHHHHLHHH", "HHHHHHHHHHHHHHHH",
               "HHHhHHHHHHhHHHHH", "HSSSSHHHSSSSSSSH", "SSSSSSSSSSSSSSSS"]
        side = "S"
    rows = top + [
        side + "SSbbbSSSSbbbSS" + side,
        side + "SS" + l8 + "SSSS" + l8 + "SS" + side,
        "SSS" + l9 + "SSSS" + l9 + "SSS",
        "SSS" + l10 + "SSSS" + l10 + "SSS",
        "SS" + ("BB" if ch.get("lash") else "SS") + "SSSSSSSS" + ("BB" if ch.get("lash") else "SS") + "SS",
        "SSSSSSSsSSSSSSSS",
        "SSSSSSMMMSSSSSSS",
        "ssssssssssssssss",
    ]
    return rows


def _side_rows(ch):
    """左侧面：第 0 列在脑后，第 15 列在脸前。"""
    rows = []
    for j in range(16):
        r = ""
        for i in range(16):
            back_hair = i < (7 if ch["style"] == "short" else 9) and j < (11 if ch["style"] == "short" else 12)
            if j < 4 or back_hair or (i < 11 and j < 5):
                r += "L" if (j == 2 and i % 5 == 1) else ("h" if (j == 4 and i % 3 == 0) else "H")
            elif 7 <= i <= 8 and 7 <= j <= 10:
                r += "s"
            elif 9 <= i <= 10 and j <= 7 and ch["style"] == "short":
                r += "h"
            else:
                r += "s" if j == 15 else "S"
        rows.append(r)
    return rows


def _torso_rows(ch):
    base = ["IIIIICssssCIIIII", "IIIIIICssCIIIIII", "IIIIIIICCIIIIIII"] + ["IIIIIIIIdIIIIIII"] * 4 + \
           ["iIIIIIIIdIIIIIIi"] * 9 + ["iiIIIIIIdIIIIIii"] * 4 + ["iiiIIIIIdIIIIiii", "iiiiIIIIdIIIiiii", "iiiiiiiiiiiiiiii"]
    base = base[:23] + ["NNNNNNNNNNNNNNNN"]
    rows = [list(r) for r in base]
    top = ch["top"]
    if top in ("vest", "hivis"):
        for j in range(1, 23):
            for i in list(range(0, 5)) + list(range(11, 16)):
                rows[j][i] = "V"
            rows[j][4] = "v"; rows[j][11] = "v"
        if top == "hivis":
            for j in (13, 14, 19):
                for i in list(range(0, 5)) + list(range(11, 16)):
                    rows[j][i] = "R"
        else:
            for j in (14,):
                for i in (1, 2, 3, 12, 13, 14):
                    rows[j][i] = "v"
    elif top == "lanyard":
        for j in range(0, 10):
            rows[j][4 + int(j * 0.34)] = "Y"
            rows[j][11 - int(j * 0.34)] = "Y"
        for j in range(10, 15):
            for i in range(6, 10):
                rows[j][i] = "c"
        rows[12][7] = "g"; rows[12][8] = "g"
    elif top == "jacket":
        for j in range(0, 23):
            for i in range(16):
                rows[j][i] = "j" if (i in (0, 15) or j >= 21) else "J"
            rows[j][8] = "Z"
        rows[0][5:11] = list("SssssS")
        rows[1][6:10] = list("jSSj")
        for j in (15, 16):
            for i in (2, 3, 4, 5, 10, 11, 12, 13):
                rows[j][i] = "j"
    return ["".join(r) for r in rows]


def _arm_rows(ch):
    sl = "V" if ch["top"] == "jacket" else "I"
    sd = "v" if ch["top"] == "jacket" else "i"
    if ch["top"] == "jacket":
        sl, sd = "J", "j"
    rows = [sl * 6] * 3 + [sl + sd + sl * 4] * 8 + [sd * 6, sl * 6, sd * 6] + ["SSSSSS"] * 7 + ["SsSsSs", "ssssss", "ssssss"]
    return rows


def _elena_tex():
    """镜 01–02 的 Elena（发货人），沿用原贴图。"""
    S.init_tex()
    T = dict(side_l=S.HEAD_SIDE_IN, side_r=S.HEAD_SIDE_IN_R, back=S.HEAD_BACK_IN, top=S.HAIR_TOP,
             torso=S.TORSO_FRONT, tside=S.TORSO_SIDE, tback=S.TORSO_BACK, arm=S.ARM, pants=S.TEX["navy"],
             shoe=noise_tex("#2E3438", amp=0.03, seed=62), sleeve_top=solid(hex3("#F5EFE1")),
             skin_d=solid(hex3(S.SKIN_D)), shell_f=S.HAIR_FRONT_OUT, shell_b=S.TEX["hair_b"],
             shell_l=S.TEX["hair_l"], shell_r=S.TEX["hair_r"], faces={}, elena=True,
             ch=dict(style="bob", top="shirt"))
    return T


def char_tex(name):
    if name in _CACHE:
        return _CACHE[name]
    if name == "elena":
        _CACHE[name] = _elena_tex()
        return _CACHE[name]
    ch = CHARS[name]
    p = _pal(ch)
    side = _side_rows(ch)
    T = dict(
        side_l=ascii_tex(side, p),
        side_r=ascii_tex([r[::-1] for r in side], p),
        back=ascii_tex(["HHHHHHHHHHHHHHHH"] * 4 + ["HHhHHHHHHHHhHHHH"] + ["HHHHHHHHHHHHHHHH"] * 7 +
                       ["hHHHHHHHHHHHHHHh", "SSSSSSSSSSSSSSSS", "SSSSSSSSSSSSSSSS", "ssssssssssssssss"], p),
        top=ascii_tex(["HHHHHHHHHHHHHHHH", "HHHHhHHHHHHLHHHH", "HHHHHHHLLHHHHHHH", "HHhHHHHHHHHHHhHH"] * 4, p),
        torso=ascii_tex(_torso_rows(ch), p),
        tside=ascii_tex((["VVVVVVVV"] if ch["top"] in ("vest", "hivis") else ["IIIIIIII"]) * 22 + ["iiiiiiii", "NNNNNNNN"], p),
        tback=ascii_tex(([("VVVVVVVVVVVVVVVV" if ch["top"] in ("vest", "hivis") else "IIIIIIIIIIIIIIII")] * 12 +
                         ([("RRRRRRRRRRRRRRRR" if ch["top"] == "hivis" else "IIIIIIIIIIIIIIII")] * 2) +
                         [("VVVVVVVVVVVVVVVV" if ch["top"] in ("vest", "hivis") else "IIIIIIIIIIIIIIII")] * 8 +
                         ["iiiiiiiiiiiiiiii", "NNNNNNNNNNNNNNNN"]), p),
        arm=ascii_tex(_arm_rows(ch), p),
        pants=noise_tex(ch["pants"], amp=0.03, seed=61),
        shoe=noise_tex("#2E3438", amp=0.03, seed=62),
        sleeve_top=solid(hex3(ch["shirt"])),
        skin_d=solid(hex3(ch["skin_d"])),
        hair=noise_tex(ch["hair"], amp=0.04, seed=63),
    )
    # 发壳（让头发有体积）：正面只有刘海，侧面和背面盖住上半部分
    fr = ["HHHHHHHHHHHHHHHH", "HHHHHHHHHHHHHHHH", "HHHLHHHHHHHLHHHH", "HH.HHHHHHHH.HHHH",
          "H...HH..HH....HH", "H..............H", "................", "................"]
    sd = ["HHHHHHHHHHHHHHHH", "HHHHLHHHHHHHHHHH", "HHHHHHHHHHHHHHHH", "HHHHHHHHHHHHHH.H",
          "HHHHHHHHHH.H....", "HHHHHHH.........", "HHHHHH..........", "HHHH............"]
    if ch["style"] == "bun":
        fr[3] = "HHHHHHHhHHHHHHHH"
        fr[4] = "HH..HH.hH....HHH"
    T["shell_f"] = ascii_tex(fr, p)
    T["shell_l"] = ascii_tex(sd, p)
    T["shell_r"] = ascii_tex([r[::-1] for r in sd], p)
    T["shell_b"] = ascii_tex(["HHHHHHHHHHHHHHHH"] * 7 + ["hHHHHHHHHHHHHHHh"], p)
    T["faces"] = {}
    T["ch"] = ch
    _CACHE[name] = T
    return T


def face(name, look, blink):
    T = char_tex(name)
    k = (look, bool(blink))
    if k not in T["faces"]:
        if T.get("elena"):
            T["faces"][k] = S.face_tex(look if look in ("right", "down", "center") else "center", blink)
        else:
            T["faces"][k] = ascii_tex(_face_rows(T["ch"], look, blink), _pal(T["ch"]))
    return T["faces"][k]


def _arm(sc, shoulder, target, T, fwd):
    d = np.asarray(target, np.float64) - shoulder
    ln = float(np.linalg.norm(d))
    yv = -d / ln
    xv = norm(np.cross(yv, fwd))
    zv = np.cross(xv, yv)
    xf = Xf(np.stack([xv, yv, zv], 1), shoulder)
    box(sc, (-60, -ln, -60), (60, 30, 60), {"all": T["arm"], "top": T["sleeve_top"], "bottom": T["skin_d"]}, xf)


def build_person(sc, name, hips, yaw, head_yaw=0.0, head_pitch=0.0, look="center", blink=False,
                 hand_l=None, hand_r=None, lean=0.0, pose="stand", chair=None):
    """pose: 'stand' 站立（腿长 520）/ 'sit' 坐姿（大腿伸向前方）。hand_* 为世界坐标目标点，None 表示自然下垂。"""
    T = char_tex(name)
    ch = T["ch"]
    hips = np.asarray(hips, np.float64)
    seat = Xf(rot_y(yaw), hips)
    if pose == "stand":
        for sx in (-1, 1):
            box(sc, (sx * 80 - 75, -520, -75), (sx * 80 + 75, 0, 75), T["pants"], seat)
            box(sc, (sx * 80 - 78, -520, -110), (sx * 80 + 78, -460, 80), T["shoe"], seat)
    else:
        if chair is not None:
            box(sc, (-230, -40, -60), (230, 0, 330), chair, seat)
            box(sc, (-220, 0, 250), (220, 520, 310), chair, seat)
            box(sc, (-30, -450, 100), (30, -40, 160), solid(hex3("#55626A")), seat)
        for sx in (-1, 1):
            box(sc, (sx * 85 - 75, 0, -420), (sx * 85 + 75, 150, 80), T["pants"], seat)
    body = seat @ Xf(rot_x(lean), (0, 0, 0))
    box(sc, (-160, 0, -80), (160, 480, 80),
        {"front": T["torso"], "back": T["tback"], "left": T["tside"], "right": T["tside"],
         "top": T["sleeve_top"], "bottom": T["pants"]}, body)
    head = body @ Xf(rot_y(head_yaw) @ rot_x(head_pitch), (0, 480, 0))
    box(sc, (-160, 0, -160), (160, 320, 160),
        {"front": face(name, look, blink), "back": T["back"], "left": T["side_l"], "right": T["side_r"],
         "top": T["top"], "bottom": T["skin_d"]}, head)
    e = 22 if T.get("elena") else 16
    box(sc, (-160 - e, 40 if T.get("elena") else 200, -160 - e), (160 + e, 320 + e, 160 + e),
        {"front": T["shell_f"], "back": T["shell_b"], "left": T["shell_l"], "right": T["shell_r"], "top": T["top"]},
        head, double=True)
    if ch["style"] == "bun":
        box(sc, (-70, 170, 176), (70, 300, 270), T["hair"], head)
    if ch.get("long"):
        # 披肩长发：两侧发束垂到肩部 + 脑后发片
        hd = noise_tex(ch["hair_d"], amp=0.04, seed=64)
        for sx in (-1, 1):
            box(sc, (sx * 160 - (40 if sx > 0 else -40) * 0 - (0 if sx > 0 else 46), -150, -150),
                (sx * 160 + (46 if sx > 0 else 0), 300, 120), {"all": T["hair"], "bottom": hd}, head)
        box(sc, (-176, -170, 120), (176, 300, 190), {"all": T["hair"], "bottom": hd}, head)
    if ch.get("helmet"):
        hm = solid(hex3(ch["helmet"]))
        hd = solid(hex3("#D9DCD8"))
        box(sc, (-190, 300, -190), (190, 420, 190), {"all": hm, "bottom": hd}, head)
        box(sc, (-190, 300, -250), (190, 325, -190), {"all": hm, "bottom": hd}, head)
        box(sc, (-60, 420, -150), (60, 445, 150), hd, head)
    fwd = body.vec((0, 0, -1))
    shL, shR = body.pt((-222, 425, 0)), body.pt((222, 425, 0))
    if hand_l is None:
        hand_l = body.pt((-250, -20, -40))
    if hand_r is None:
        hand_r = body.pt((250, -20, -40))
    _arm(sc, shL, hand_l, T, fwd)
    _arm(sc, shR, hand_r, T, fwd)
    return body, head


# ------------------------------------------------------------------ 飘动的单据
def doc_xf(pos, yaw=0.0, pitch=-90.0, roll=0.0):
    """单据姿态。pitch=-90 时纸面竖立、正面朝向 -Z（镜头），折角在左上。"""
    return Xf(rot_y(yaw) @ rot_x(pitch) @ rot_z(roll), pos)


def build_doc(sc, xf, wave=0.0, phase=0.0, curl=0.0, sleeve=True, scale=1.0):
    """单据 + 透明夹，沿纸张长边做 N 段弯曲（飘动感）。返回纸面四角（TL,TR,BR,BL，世界坐标）。
    wave：波动幅度（mm）；curl：两端同向卷曲（mm）。"""
    S.init_tex()
    if "sleeve" not in S.TEX:
        S.TEX["sleeve"] = S.sleeve_tex()
    n = 6
    zs = np.linspace(193, -193, n + 1) * scale
    u = np.linspace(0, 1, n + 1)
    ys = wave * np.sin(u * math.pi * 1.6 + phase) + curl * ((u - 0.5) * 2) ** 2
    hw = 137 * scale
    P = S.TEX["paper"]
    corners = None
    for k in range(n):
        v = xf.pt([(-hw, ys[k], zs[k]), (hw, ys[k], zs[k]), (hw, ys[k + 1], zs[k + 1]), (-hw, ys[k + 1], zs[k + 1])])
        d1 = v[1] - v[0]
        d2 = v[3] - v[0]
        nn = norm(np.cross(d2, d1))
        uv = [(0, u[k]), (1, u[k]), (1, u[k + 1]), (0, u[k + 1])]
        sc.append(Quad(v, P, nn, double=True, uv=uv))
    corners = xf.pt([(-hw, ys[0], zs[0]), (hw, ys[0], zs[0]), (hw, ys[-1], zs[-1]), (-hw, ys[-1], zs[-1])])
    if sleeve:
        sw = 150 * scale
        zz = np.linspace(205, -205, n + 1) * scale
        for side in (5.0, -5.0):
            for k in range(n):
                v = xf.pt([(-sw, ys[k] + side, zz[k]), (sw, ys[k] + side, zz[k]),
                           (sw, ys[k + 1] + side, zz[k + 1]), (-sw, ys[k + 1] + side, zz[k + 1])])
                uv = [(0, u[k]), (1, u[k]), (1, u[k + 1]), (0, u[k + 1])]
                sc.append(Quad(v, S.TEX["sleeve"], xf.vec((0, 1, 0)), double=True, blend=True, noshadow=True, uv=uv))
    return corners


# ------------------------------------------------------------------ 平板电脑
def build_tablet(sc, xf, ui_rgba, w=300.0, h=200.0, bright=1.0):
    """平板：本地 XZ 平面，屏幕朝 +Y，+Z 为屏幕上方。返回屏幕四角（TL,TR,BR,BL）。"""
    dark = solid(hex3("#2B3338"))
    box(sc, (-w / 2, -8, -h / 2), (w / 2, 6, h / 2), {"top": dark, "all": solid(hex3("#5A646B"))}, xf)
    m = 12
    v = xf.pt([(-w / 2 + m, 7, h / 2 - m), (w / 2 - m, 7, h / 2 - m), (w / 2 - m, 7, -h / 2 + m), (-w / 2 + m, 7, -h / 2 + m)])
    sc.append(Quad(v, ui_rgba, xf.vec((0, 1, 0)), emissive=True, smooth=True, bright=bright, noshadow=True))
    return v


def build_pen(sc, tip, tail_dir, length=150.0):
    d = norm(tail_dir)
    tip = np.asarray(tip, np.float64)
    S.stick(sc, tip, tip + d * 14, 3.0, solid(hex3("#C9CFD3")))
    S.stick(sc, tip + d * 14, tip + d * length, 5.5, solid(hex3("#1F2A33")))
