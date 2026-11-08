"""体素方块风场景库：汉堡出口工作台（房间、窗外港口、桌面、人物、单据）。"""
import math
import numpy as np
import cairo
from PIL import Image, ImageFilter

import common as C
from common import seg, ease, ease_out, lerp
from voxel import (Xf, rot_y, rot_x, rot_z, box, Quad, Camera, Sun, render, ascii_tex, noise_tex,
                   solid, hex3, tiled, norm)

# ------------------------------------------------------------------ 贴图
HAIR, HAIR_D, HAIR_L = "#7A4A32", "#5E3626", "#9A623F"
SKIN, SKIN_D = "#F3CFB3", "#E2B090"
PAL = {
    "H": HAIR, "h": HAIR_D, "L": HAIR_L, "S": SKIN, "s": SKIN_D,
    "W": "#FFFFFF", "E": "#3E8298", "e": "#27505F", "P": "#33231F", "b": "#6B4030",
    "B": "#F2AFA3", "M": "#C97A6E", ".": None,
    "I": "#F5EFE1", "i": "#E4D9C3", "C": "#D5C7AC", "d": "#D9CDB6", "o": "#BBAA8E",
    "N": "#1D4867", "n": "#163A54",
}


def face_tex(look="right", blink=False):
    eye = {"right": ("WEe", "WEE"), "down": ("WWW", "EeW"), "center": ("EeW", "EEW")}[look]
    if blink:
        l8, l9, l10 = "SSS", "PPP", "SSS"
    else:
        l8, l9, l10 = "PPP", eye[0], eye[1]
    rows = [
        "HHHHHHHHHHHHHHHH",
        "HHHHHHHHHHHHHHHH",
        "HHLLHHHHHHHHLHHH",
        "HHHHHHHHHHHHHHHH",
        "HHHHHHhHHHHHHHHH",
        "HhSSHHHHHhSSSHHH",
        "HSSSSSHHSSSSSSSH",
        "HSSSSSSSSSSSSSSH",
        "HSS" + l8 + "SSSS" + l8 + "SSH",
        "HSS" + l9 + "SSSS" + l9 + "SSH",
        "HSS" + l10 + "SSSS" + l10 + "SSH",
        "HSBBSSSSSSSSBBSH",
        "HSSSSSSSsSSSSSSH",
        "hSSSSSSMMMSSSSSh",
        "hSSSSSSSSSSSSSSh",
        "hssssssssssssssh",
    ]
    return ascii_tex(rows, PAL)


def hair_side(seed):
    rng = np.random.RandomState(seed)
    rows = []
    for j in range(16):
        row = ""
        for i in range(16):
            if j >= 14 or (j == 13 and i % 4 == (seed % 3)) or (j == 12 and i % 5 == 4):
                ch = "." if j >= 13 else "h"
            else:
                r = rng.rand()
                ch = "L" if (j in (2, 3) and r < 0.45) else ("h" if r < 0.14 else "H")
            row += ch
        rows.append(row)
    return ascii_tex(rows, PAL)


HAIR_FRONT_OUT = ascii_tex([
    "HHHHHHHHHHHHHHHH",
    "HHLLLHHHHHHLLHHH",
    "HHHHHHHHHHHHHHHH",
    "HHHHHHhHHHHHHHHH",
    "HHHHHHHhHHHHHHHH",
    "HH..HHHHHH...HHH",
    "H.....HH.......H",
    "H..............H",
    "H..............H",
    "H..............H",
    "H..............H",
    "H..............H",
    "h..............h",
    "h..............h",
    "................",
    "................",
], PAL)
HAIR_TOP = ascii_tex([
    "HHHHHHHHHHHHHHHH",
    "HHHHhHHHHHHHHHHH",
    "HHHHhHHLLHHHHHHH",
    "HHHHhHHHLLHHHHHH",
    "HHHHhHHHHHHHHHHH",
    "HHHHHhHHHHHHHhHH",
    "HHLHHhHHHHHHHHHH",
    "HHLHHhHHHHHHHHHH",
    "HHHHHhHHHHLLHHHH",
    "HHHHHhHHHHHLHHHH",
    "HHHHHhHHHHHHHHHH",
    "HHHHhHHHHHHHHHHH",
    "HHHHhHHHHHHHHHHH",
    "HHHhHHHHHHHHHHHH",
    "HHHhHHHLLHHHHHHH",
    "HHHHHHHHHHHHHHHH",
], PAL)
HEAD_SIDE_IN = ascii_tex(["HHHHHHHHHHHHHHHH"] * 12 + ["SSSSSSSSHHHHHHHH"] * 2 + ["SSSSSSSSSSHHHHHH", "ssssssssssssssss"], PAL)
HEAD_SIDE_IN_R = HEAD_SIDE_IN[:, ::-1].copy()
HEAD_BACK_IN = ascii_tex(["HHHHHHHHHHHHHHHH"] * 14 + ["SSSSSSSSSSSSSSSS", "ssssssssssssssss"], PAL)

TORSO_FRONT = ascii_tex([
    "IIIIICssssCIIIII",
    "IIIIIICssCIIIIII",
    "IIIIIIICCIIIIIII",
    "IIIIIIIIdIIIIIII",
    "IIIIIIIIoIIIIIII",
    "IIIIIIIIdIIIIIII",
    "IIIIIIIIdIIIIIII",
    "iIIIIIIIdIIIIIIi",
    "iIIIIIIIoIIIIIIi",
    "iIIIIIIIdIIIIIIi",
    "iIIIIIIIdIIIIIIi",
    "iIIIIIIIdIIIIIIi",
    "iIIIIIIIoIIIIIIi",
    "iIIIIIIIdIIIIIIi",
    "iIIIIIIIdIIIIIIi",
    "iIIIIIIIdIIIIIIi",
    "iiIIIIIIoIIIIIii",
    "iiIIIIIIdIIIIIii",
    "iiIIIIIIdIIIIIii",
    "iiIIIIIIdIIIIIii",
    "iiiIIIIIoIIIIiii",
    "iiiiIIIIdIIIiiii",
    "iiiiiiiiiiiiiiii",
    "NNNNNNNNNNNNNNNN",
], PAL)
TORSO_SIDE = ascii_tex(["IIIIIIII"] * 6 + ["IiIIIIiI"] * 10 + ["iiIIIIii"] * 6 + ["iiiiiiii", "NNNNNNNN"], PAL)
TORSO_BACK = ascii_tex(["IIIIIIIIIIIIIIII"] * 3 + ["iiiiiiiiiiiiiiii"] + ["IIIIIIIIIIIIIIII"] * 18 + ["iiiiiiiiiiiiiiii", "NNNNNNNNNNNNNNNN"], PAL)
ARM = ascii_tex(["IIIIII"] * 3 + ["IiIIII"] * 8 + ["CCCCCC", "IIIIII", "CCCCCC"] + ["SSSSSS"] * 7 + ["SsSsSs", "ssssss", "ssssss"], PAL)

OAK = "#D9BD92"


def plank_tex(base, w=32, h=32, seed=1, planks=4, amp=0.035, vertical=False):
    rng = np.random.RandomState(seed)
    t = np.ones((h, w, 4), np.float32)
    b = np.array(hex3(base), np.float32)
    ph = h // planks
    for p in range(planks):
        tone = 1 + (rng.rand() - 0.5) * 0.10
        y0 = p * ph
        t[y0:y0 + ph, :, :3] = np.clip(b * tone + (rng.rand(ph, w, 1) - 0.5) * 2 * amp, 0, 1)
        t[y0, :, :3] *= 0.86
        jx = rng.randint(2, w - 2)
        t[y0:y0 + ph, jx, :3] *= 0.88
    if vertical:
        t = np.transpose(t, (1, 0, 2)).copy()
    return t


def paper_tex():
    """A4 样稿贴图：42×60 像素，左上角浅蓝折角；只有不可读的灰色结构线。"""
    w, h = 42, 60
    t = np.ones((h, w, 4), np.float32)
    t[..., :3] = hex3("#F8F5EC")
    g = np.array(hex3("#B4BABF"), np.float32)
    gd = np.array(hex3("#9FA8AE"), np.float32)
    t[6:8, 16:38, :3] = gd                    # 抬头
    for j, (a, b) in zip(range(13, 56, 3), [(5, 20), (5, 17), (24, 37), (24, 34), (5, 37), (5, 37), (5, 30),
                                             (5, 37), (5, 26), (5, 37), (5, 33), (26, 37), (26, 37), (5, 15), (5, 12)]):
        t[j, a:b, :3] = g
    n = 10
    for j in range(n):                         # 折角：阶梯状三角
        t[j, :n - j, :3] = hex3("#7DB8E8")
        t[j, n - j - 1, :3] = hex3("#6FA6CF")
    for j in range(n):                         # 折掉的纸角处透明
        pass
    return t


def keyboard_tex():
    w, h = 44, 30
    t = np.ones((h, w, 4), np.float32)
    t[..., :3] = hex3("#C9CFD3")
    key = np.array(hex3("#4A555C"), np.float32)
    for j in range(3, 17, 3):
        for i in range(3, 41, 3):
            t[j:j + 2, i:i + 2, :3] = key
    t[19:28, 15:29, :3] = hex3("#B5BDC2")
    t[19, 15:29, :3] = hex3("#A3ACB2")
    return t


def container_tex(base, seed):
    rng = np.random.RandomState(seed)
    w, h = 24, 10
    t = np.ones((h, w, 4), np.float32)
    b = np.array(hex3(base), np.float32)
    for i in range(w):
        t[:, i, :3] = b * (0.92 if i % 2 else 1.0)
    t[0, :, :3] *= 0.8
    t[-1, :, :3] *= 0.8
    t[:, 0, :3] *= 0.8
    return t


def sea_tex():
    rng = np.random.RandomState(7)
    w = h = 64
    t = np.ones((h, w, 4), np.float32)
    b = np.array(hex3("#5F9FC4"), np.float32)
    t[..., :3] = np.clip(b + (rng.rand(h, w, 1) - 0.5) * 0.05, 0, 1)
    for _ in range(90):
        y, x = rng.randint(0, h), rng.randint(0, w - 4)
        t[y, x:x + rng.randint(2, 4), :3] = hex3("#BFE0EE")
    return t


WORLD = [
    "................................",
    "....##...###......#####.#####...",
    "..#######.##....###############.",
    "..######.......#.#############..",
    "...#####.......###.##########...",
    "....###........#####..######....",
    "....##.........######..###..#...",
    ".....##.........#####...#...#...",
    "......###........###.........#..",
    ".......####......###........###.",
    ".......###.......##.........###.",
    ".......##........#...........#..",
    ".......#........................",
    "................................",
]


def map_tex(t=0.0):
    """墙上的像素世界地图，标出 汉堡 → 上海 的示意航线。"""
    h, w = len(WORLD), len(WORLD[0])
    s = 3
    tex = np.ones((h * s + 6, w * s + 6, 4), np.float32)
    tex[..., :3] = hex3("#EEF4F5")
    land = np.array(hex3("#A9C9C6"), np.float32)
    for j, row in enumerate(WORLD):
        for i, ch in enumerate(row):
            if ch == "#":
                tex[3 + j * s:3 + j * s + s - 1, 3 + i * s:3 + i * s + s - 1, :3] = land
    a, b = (3 + 16 * s, 3 + 2 * s + 1), (3 + 26 * s, 3 + 4 * s + 1)     # 汉堡、上海（示意）
    n = 22
    for k in range(n + 1):
        u = k / n
        x = a[0] + (b[0] - a[0]) * u
        y = a[1] + (b[1] - a[1]) * u + math.sin(u * math.pi) * 14
        if k % 2 == 0:
            tex[int(y), int(x), :3] = hex3("#1D4867")
    for (x, y) in (a, b):
        tex[y - 1:y + 2, x - 1:x + 2, :3] = hex3("#E0734A")
    tex[:2, :, :3] = hex3("#FFFFFF")
    tex[-2:, :, :3] = hex3("#FFFFFF")
    tex[:, :2, :3] = hex3("#FFFFFF")
    tex[:, -2:, :3] = hex3("#FFFFFF")
    return tex


def clock_tex(hour=8, minute=30):
    n = 24
    t = np.zeros((n, n, 4), np.float32)
    c = (n - 1) / 2
    for j in range(n):
        for i in range(n):
            d = math.hypot(i - c, j - c)
            if d <= 11.6:
                t[j, i] = (*hex3("#FFFFFF" if d < 10 else "#1D4867"), 1)
    for k in range(12):
        a = k * math.pi / 6
        t[int(round(c - math.cos(a) * 8.4)), int(round(c + math.sin(a) * 8.4)), :3] = hex3("#7BA7A5")

    def hand(ang, ln, col):
        for r in np.arange(0, ln, 0.5):
            t[int(round(c - math.cos(ang) * r)), int(round(c + math.sin(ang) * r)), :3] = hex3(col)
    hand((hour % 12 + minute / 60) * math.pi / 6, 5.5, "#1D4867")
    hand(minute * math.pi / 30, 8, "#1D4867")
    return t


ICON_PAL = {".": "#EADBB9", "W": "#FBF8F1", "g": "#B4BABF", "b": "#7DB8E8", "K": "#C9A46E", "k": "#A98654",
            "T": "#7BA7A5", "N": "#1D4867", "R": "#C9665A", "S": "#D9C8AA", "A": "#8FC3E0", "F": "#9A6B3F", "f": "#7C5330"}
ICONS = {
    "doc": ["............", "..bbWWWWW...", "..bWWWWWW...", "..WWWWWWW...", "..WgggggW...", "..WWWWWWW...",
            "..WggggWW...", "..WWWWWWW...", "..WgggggW...", "..WWWWWWW...", "..WWWWWWW...", "............"],
    "box": ["............", "............", "..KKKTTKKK..", "..KKKTTKKK..", "..kkkTTkkk..", "..KKKKKKKK..",
            "..KWWWKKKK..", "..KWgWKKKK..", "..KKKKKKKK..", "..kkkkkkkk..", "............", "............"],
    "ship": ["............", "............", ".......WW...", "..RRNN.WW...", "..TTRRSWW...", ".NNNNNNNNNN.",
             "..NNNNNNNNN.", "...NNNNNNN..", ".AAAAAAAAAA.", "............", "............", "............"],
}


def frame_tex(rows):
    """展示框（方块游戏语汇，原创像素）：木边 + 浅色衬底 + 12×12 图标。"""
    full = ["FFFFFFFFFFFFFFFF", "FffffffffffffffF"] + ["Ff" + r + "fF" for r in rows] + ["FffffffffffffffF", "FFFFFFFFFFFFFFFF"]
    return ascii_tex(full, ICON_PAL)


def parcel_tex():
    t = noise_tex("#C9A46E", amp=0.03, seed=12).copy()
    t[:, 7:9, :3] = hex3("#7BA7A5")
    t[8:13, 9:15, :3] = hex3("#FBF8F1")
    for i in (10, 12, 13):
        t[9:12, i, :3] = hex3("#3A4248")
    return t


def chest_tex():
    """储物箱（原创像素）：木板 + 深色包边 + 金属搭扣。"""
    f = noise_tex("#BD8A49", amp=0.04, seed=50).copy()
    d = np.array(hex3("#6E4A28"), np.float32)
    f[0, :, :3] = d; f[-1, :, :3] = d; f[:, 0, :3] = d; f[:, -1, :3] = d
    f[5, :, :3] = d
    f[8:10, 1:-1, :3] *= 0.92
    f[12:14, 1:-1, :3] *= 0.92
    f[4:8, 7:9, :3] = hex3("#D8DDE0")
    f[7, 7:9, :3] = hex3("#9AA3A8")
    top = noise_tex("#C4924F", amp=0.04, seed=51).copy()
    top[0, :, :3] = d; top[-1, :, :3] = d; top[:, 0, :3] = d; top[:, -1, :3] = d
    return f, top


TEX = {}


def init_tex():
    if TEX:
        return
    TEX["wall"] = noise_tex("#F3E7D2", amp=0.018, seed=3)
    TEX["floor"] = plank_tex("#B98F63", seed=5)
    TEX["desk"] = plank_tex(OAK, seed=2, planks=4)
    TEX["desk_side"] = plank_tex("#C4A577", seed=9, planks=2)
    TEX["frame"] = noise_tex("#FBF8F1", amp=0.012, seed=4)
    TEX["paper"] = paper_tex()
    TEX["kbd"] = keyboard_tex()
    TEX["silver"] = noise_tex("#C9CFD3", amp=0.012, seed=6)
    TEX["silver_d"] = noise_tex("#AEB6BB", amp=0.012, seed=6)
    TEX["sea"] = sea_tex()
    TEX["quay"] = noise_tex("#B9BDBD", amp=0.03, seed=8)
    TEX["crane"] = noise_tex("#E2874F", amp=0.03, seed=10)
    TEX["crane_b"] = noise_tex("#3F6F8E", amp=0.03, seed=11)
    TEX["cloud"] = solid((1, 1, 1))
    for i, c in enumerate(["#1D4867", "#D9C8AA", "#7BA7A5", "#C9665A", "#EDE6D6", "#2F6F8F"]):
        TEX["cont%d" % i] = container_tex(c, i)
    TEX["kraft"] = noise_tex("#C9A46E", amp=0.03, seed=12)
    TEX["bin"] = noise_tex("#5C9BD1", amp=0.02, seed=13)
    TEX["navy"] = noise_tex("#1D4867", amp=0.02, seed=14)
    TEX["teal"] = noise_tex("#7BA7A5", amp=0.02, seed=15)
    TEX["sand"] = noise_tex("#D9C8AA", amp=0.02, seed=16)
    TEX["pot"] = noise_tex("#C97B57", amp=0.03, seed=17)
    TEX["leaf"] = noise_tex("#5FA36A", amp=0.06, seed=18)
    TEX["leaf2"] = noise_tex("#7DBB78", amp=0.06, seed=19)
    TEX["mug"] = noise_tex("#FBF8F1", amp=0.01, seed=20)
    TEX["coffee"] = noise_tex("#6B4630", amp=0.03, seed=21)
    TEX["mouse"] = noise_tex("#F1F3F4", amp=0.01, seed=22)
    TEX["hair_l"] = hair_side(1)
    TEX["hair_r"] = hair_side(2)
    TEX["hair_b"] = hair_side(3)
    TEX["map"] = map_tex()
    TEX["clock"] = clock_tex()
    TEX["chair"] = noise_tex("#5E8C8A", amp=0.03, seed=23)
    TEX["pencil"] = ascii_tex(["PYYYYYYYYYYYR"], {"P": "#33231F", "Y": "#F0C04A", "R": "#D98A7E"})
    TEX["skin"] = noise_tex(SKIN, amp=0.0, seed=1)
    TEX["parcel"] = parcel_tex()
    TEX["chest"], TEX["chest_top"] = chest_tex()
    TEX["yellow"] = noise_tex("#F2B632", amp=0.03, seed=40)
    TEX["dark"] = noise_tex("#3A4248", amp=0.03, seed=41)
    TEX["pallet"] = plank_tex("#B48A58", w=16, h=16, seed=42, planks=4)
    for k, rows in ICONS.items():
        TEX["frame_" + k] = frame_tex(rows)


# ------------------------------------------------------------------ 场景
DESK_Y = 750.0
WIN = (-1380.0, -240.0, 960.0, 2300.0)      # 窗洞 x0,x1,y0,y1（后墙 z=1400）
WALL_Z = 1400.0
SUN_DIR = norm((-0.50, 0.56, 0.66))         # 指向太阳：左后上方，光从窗户斜射进来

LAPTOP_POS = (-60.0, 480.0)
LAPTOP_YAW = 30.0
SCREEN_PX = (640, 400)
_c, _s = math.cos(math.radians(LAPTOP_YAW)), math.sin(math.radians(LAPTOP_YAW))
TRACKPAD = (LAPTOP_POS[0] + (-95) * _s, DESK_Y + 20, LAPTOP_POS[1] + (-95) * _c)


def build_room(sc):
    x0, x1, y0, y1 = WIN
    wt = lambda w, h: tiled(TEX["wall"], w / 400, h / 400)
    Z = WALL_Z
    # 后墙（留出窗洞）
    box(sc, (-2600, 0, Z), (x0, 3000, Z + 150), wt(1200, 3000))
    box(sc, (x1, 0, Z), (2600, 3000, Z + 150), wt(2800, 3000))
    box(sc, (x0, 0, Z), (x1, y0, Z + 150), wt(1100, 960))
    box(sc, (x0, y1, Z), (x1, 3000, Z + 150), wt(1100, 700))
    # 左墙、天花板（主要用于挡光）
    box(sc, (-2750, 0, -3500), (-2600, 3000, Z), wt(4900, 3000))
    box(sc, (-2750, 3000, -3500), (2600, 3150, Z + 150), wt(5000, 4900))
    # 地板
    box(sc, (-2600, -60, -3500), (2600, 0, Z), tiled(TEX["floor"], 6, 6))
    # 窗框、窗台、窗棂
    f = TEX["frame"]
    box(sc, (x0 - 50, y0 - 50, Z - 40), (x1 + 50, y0, Z + 40), f)
    box(sc, (x0 - 50, y1, Z - 40), (x1 + 50, y1 + 50, Z + 40), f)
    box(sc, (x0 - 50, y0, Z - 40), (x0, y1, Z + 40), f)
    box(sc, (x1, y0, Z - 40), (x1 + 50, y1, Z + 40), f)
    xm = (x0 + x1) / 2
    box(sc, (xm - 22, y0, Z - 15), (xm + 22, y1, Z + 15), f)
    ym = y0 + (y1 - y0) * 0.52
    box(sc, (x0, ym - 22, Z - 15), (x1, ym + 22, Z + 15), f)
    box(sc, (x0 - 80, y0 - 80, Z - 130), (x1 + 80, y0 - 50, Z + 40), f)
    # 窗台上的盆栽
    px = x0 + 170
    box(sc, (px - 70, y0 - 50, Z - 120), (px + 70, y0 + 70, Z - 0), TEX["pot"])
    for (dx, dy, dz, s, k) in [(0, 70, -60, 80, "leaf"), (-80, 150, -60, 80, "leaf2"), (60, 150, -70, 80, "leaf"),
                               (-20, 230, -60, 80, "leaf2"), (90, 240, -50, 60, "leaf2"), (-100, 250, -70, 60, "leaf"),
                               (30, 310, -60, 60, "leaf")]:
        box(sc, (px + dx - s / 2, y0 + dy, Z - 60 + dz - s / 2 + 60), (px + dx + s / 2, y0 + dy + s, Z - 60 + dz + s / 2 + 60), TEX[k])
    # 右侧墙面：像素世界地图 + 挂钟 + 搁板（纸箱、蓝色收纳箱、文件夹）
    sc.append(Quad([(-90, 1560, Z - 4), (470, 1560, Z - 4), (470, 1300, Z - 4), (-90, 1300, Z - 4)], TEX["map"], (0, 0, -1)))
    box(sc, (520, 1150, Z - 220), (1500, 1180, Z), TEX["desk_side"])
    box(sc, (550, 1180, Z - 200), (760, 1370, Z - 20), {"top": TEX["chest_top"], "all": TEX["chest"]})
    box(sc, (790, 1180, Z - 190), (960, 1330, Z - 30), {"front": TEX["parcel"], "all": TEX["kraft"]})
    box(sc, (815, 1330, Z - 170), (935, 1430, Z - 50), {"front": TEX["parcel"], "all": TEX["kraft"]})
    for i, k in enumerate(["navy", "teal", "sand", "navy", "teal"]):
        box(sc, (990 + i * 62, 1180, Z - 200), (1045 + i * 62, 1440, Z - 20), TEX[k])
    # 展示框：单据 → 货物 → 船
    for i, k in enumerate(["doc", "box", "ship"]):
        fx0 = -70 + i * 195
        box(sc, (fx0, 1085, Z - 16), (fx0 + 150, 1235, Z), {"front": TEX["frame_" + k], "all": solid(hex3("#9A6B3F"))})


def build_exterior(sc, T=0.0):
    """窗外港口，按真实尺度（mm）：办公室在高层，海面在下方，远处是码头、岸桥和货轮。"""
    kw = dict(outdoor=True)
    SEA = -30000.0
    box(sc, (-900000, SEA - 500, 100000), (500000, SEA, 2500000), tiled(TEX["sea"], 60, 60), **kw)
    rng = np.random.RandomState(4)
    # 远处码头
    QZ = 285000.0
    box(sc, (-400000, SEA, QZ), (200000, SEA + 2200, QZ + 60000), TEX["quay"], **kw)
    qy = SEA + 2200
    for i in range(22):
        bx = -150000 + i * 6600
        for lv in range(rng.randint(1, 5)):
            t = TEX["cont%d" % rng.randint(0, 6)]
            box(sc, (bx, qy + lv * 2600, QZ + 14000), (bx + 6100, qy + 2600 + lv * 2600, QZ + 16500), t, **kw)
    # 堆场龙门吊：小车带着集装箱来回移动
    for k, (cx, col) in enumerate([(-62000, "crane"), (-41000, "crane_b"), (-21000, "crane")]):
        t = TEX[col]
        for dx in (-6300, 6300):
            box(sc, (cx + dx - 700, qy, QZ + 11500), (cx + dx + 700, qy + 19000, QZ + 12900), t, **kw)
        box(sc, (cx - 7000, qy + 17200, QZ + 11300), (cx + 7000, qy + 19000, QZ + 13100), t, **kw)
        tx = cx + 4200 * math.sin(T * 0.55 + k * 2.1)
        drop = 5200 + 2600 * math.sin(T * 0.8 + k * 1.3)
        box(sc, (tx - 1300, qy + 15800, QZ + 11000), (tx + 1300, qy + 17200, QZ + 13400), TEX["frame"], **kw)
        box(sc, (tx - 120, qy + 15800 - drop, QZ + 12000), (tx + 120, qy + 15800, QZ + 12300), TEX["dark"], **kw)
        box(sc, (tx - 3000, qy + 13300 - drop, QZ + 10900), (tx + 3000, qy + 15800 - drop, QZ + 13400), TEX["cont%d" % ((k * 2 + 3) % 6)], **kw)
    # 货轮（近一些，自左向右缓缓驶过）
    SZ = 215000.0
    sx = -14000.0 + 2500.0 * T
    box(sc, (sx - 66000, SEA, SZ), (sx - 9000, SEA + 5200, SZ + 11000), TEX["navy"], **kw)
    box(sc, (sx - 66000, SEA + 5200, SZ), (sx - 9000, SEA + 5700, SZ + 11000), noise_tex("#C9665A", amp=0.02, seed=33), **kw)
    for i in range(7):
        for lv in range(rng.randint(2, 5)):
            t = TEX["cont%d" % rng.randint(0, 6)]
            box(sc, (sx - 64000 + i * 6500, SEA + 5700 + lv * 2600, SZ + 2000), (sx - 58000 + i * 6500, SEA + 8300 + lv * 2600, SZ + 9000), t, **kw)
    box(sc, (sx - 17000, SEA + 5700, SZ + 1500), (sx - 11500, SEA + 19000, SZ + 9500), noise_tex("#F4F1E8", amp=0.02, seed=31), **kw)
    # 云
    for (cx, cy, cz, w) in [(-150000, 62000, 600000, 60000), (-75000, 84000, 650000, 45000), (-30000, 50000, 620000, 30000)]:
        box(sc, (cx, cy, cz), (cx + w, cy + 6000, cz + 9000), TEX["cloud"], emissive=True)
        box(sc, (cx + w * 0.2, cy + 6000, cz), (cx + w * 0.7, cy + 11000, cz + 9000), TEX["cloud"], emissive=True)


def build_desk(sc, pencil=True):
    y = DESK_Y
    box(sc, (-1500, y - 45, -240), (1300, y, 700), {"top": tiled(TEX["desk"], 3, 1), "all": TEX["desk_side"]})
    for x in (-1440, 1180):
        box(sc, (x, 0, 0), (x + 60, y - 45, 640), TEX["desk_side"])
    # 咖啡杯、深蓝笔记本、铅笔（放在电脑后侧，不抢单据）
    box(sc, (270, y, 540), (350, y + 85, 620), {"top": TEX["coffee"], "all": TEX["mug"]})
    box(sc, (350, y + 25, 565), (375, y + 65, 595), TEX["mug"])
    # 桌面摆件：迷你叉车托着包裹（朝右），后面两只迷你集装箱
    fk = Xf(rot_y(-18), (470, y, 400))
    for wx in (-48, 48):
        for wz in (-42, 42):
            box(sc, (wx - 18, 0, wz - 8), (wx + 18, 36, wz + 8), TEX["dark"], fk)
    box(sc, (-75, 14, -36), (60, 62, 36), TEX["yellow"], fk)
    box(sc, (-75, 62, -36), (-40, 84, 36), TEX["dark"], fk)
    for px_ in (-70, 20):
        for pz in (-34, 28):
            box(sc, (px_, 62, pz), (px_ + 7, 128, pz + 7), TEX["dark"], fk)
    box(sc, (-74, 128, -38), (30, 136, 38), TEX["yellow"], fk)
    for pz in (-30, 22):
        box(sc, (62, 0, pz), (70, 150, pz + 8), TEX["dark"], fk)
        box(sc, (70, 40, pz), (150, 46, pz + 8), TEX["silver_d"], fk)
    box(sc, (74, 46, -34), (146, 52, 34), TEX["pallet"], fk)
    box(sc, (78, 52, -30), (142, 112, 30), {"front": TEX["parcel"], "all": TEX["kraft"]}, fk)
    box(sc, (500, y, 560), (690, y + 70, 635), TEX["cont0"])
    box(sc, (520, y + 70, 560), (710, y + 140, 635), TEX["cont3"])
    nb = Xf(rot_y(-14), (-930, y, 470))
    box(sc, (-90, 0, -125), (90, 22, 125), TEX["navy"], nb)
    pc = Xf(rot_y(-30), (-760, y, 520))
    if pencil:
        box(sc, (-80, 0, -5), (80, 9, 5), {"top": TEX["pencil"], "all": solid(hex3("#F0C04A"))}, pc)


def build_laptop(sc, ui_rgba):
    y = DESK_Y
    xf = Xf(rot_y(LAPTOP_YAW), (LAPTOP_POS[0], y, LAPTOP_POS[1]))
    w, d = 440, 300
    box(sc, (-w / 2, 0, -d / 2), (w / 2, 20, d / 2), {"top": TEX["kbd"], "all": TEX["silver"]}, xf)
    lid = xf @ Xf(rot_x(13), (0, 20, d / 2 - 8))        # 屏幕后仰
    lh = 300
    box(sc, (-w / 2, 0, 0), (w / 2, lh, 14), {"front": solid(hex3("#2B3338")), "all": TEX["silver_d"]}, lid)
    m = 14
    v = lid.pt([(-w / 2 + m, lh - m, -1.5), (w / 2 - m, lh - m, -1.5), (w / 2 - m, m + 4, -1.5), (-w / 2 + m, m + 4, -1.5)])
    sc.append(Quad(v, ui_rgba, lid.vec((0, 0, -1)), emissive=True, smooth=True, bright=1.0))
    return v


def aim_arm(sc, shoulder, target, tex, fwd):
    """从肩部指向目标点的一段方块手臂。"""
    d = np.asarray(target, np.float64) - shoulder
    ln = float(np.linalg.norm(d))
    yv = -d / ln
    xv = norm(np.cross(yv, fwd))
    zv = np.cross(xv, yv)
    R = np.stack([xv, yv, zv], 1)
    xf = Xf(R, shoulder)
    box(sc, (-60, -ln, -60), (60, 30, 60), {"all": tex, "top": solid(hex3("#F5EFE1")), "bottom": solid(hex3(SKIN_D))}, xf)


def build_elena(sc, hips, yaw, head_yaw, head_pitch, look, blink, hand_l, hand_r, lean=0.0):
    body = Xf(rot_y(yaw), hips)
    seat = body
    # 椅子
    box(sc, (-230, -40, -60), (230, 0, 330), TEX["chair"], body)
    box(sc, (-220, 0, 250), (220, 520, 310), TEX["chair"], body)
    box(sc, (-30, -450, 100), (30, -40, 160), solid(hex3("#55626A")), body)
    # 大腿（伸到桌下）
    for sx in (-1, 1):
        box(sc, (sx * 85 - 75, 0, -420), (sx * 85 + 75, 150, 80), TEX["navy"], body)
    body = seat @ Xf(rot_x(lean), (0, 0, 0))
    # 躯干
    box(sc, (-160, 0, -80), (160, 480, 80),
        {"front": TORSO_FRONT, "back": TORSO_BACK, "left": TORSO_SIDE, "right": TORSO_SIDE,
         "top": solid(hex3("#F5EFE1")), "bottom": TEX["navy"]}, body)
    # 头
    head = body @ Xf(rot_y(head_yaw) @ rot_x(head_pitch), (0, 480, 0))
    box(sc, (-160, 0, -160), (160, 320, 160),
        {"front": face_tex(look, blink), "back": HEAD_BACK_IN, "left": HEAD_SIDE_IN, "right": HEAD_SIDE_IN_R,
         "top": HAIR_TOP, "bottom": solid(hex3(SKIN_D))}, head)
    e = 22
    box(sc, (-160 - e, 40, -160 - e), (160 + e, 320 + e, 160 + e),
        {"front": HAIR_FRONT_OUT, "back": TEX["hair_b"], "left": TEX["hair_l"], "right": TEX["hair_r"],
         "top": HAIR_TOP}, head, double=True)
    fwd = body.vec((0, 0, -1))
    aim_arm(sc, body.pt((-222, 425, 0)), hand_l, ARM, fwd)
    aim_arm(sc, body.pt((222, 425, 0)), hand_r, ARM, fwd)


def build_paper(sc, pos, yaw, lift=0.0):
    xf = Xf(rot_y(yaw), (pos[0], DESK_Y + 2 + lift, pos[1]))
    v = xf.pt([(-137, 0, 193), (137, 0, 193), (137, 0, -193), (-137, 0, -193)])
    sc.append(Quad(v, TEX["paper"], (0, 1, 0), double=True))
    return v


def sleeve_tex():
    """透明文件夹：极淡的蓝白薄膜 + 亮边 + 一道斜向反光。alpha 即混合强度。"""
    w, h = 46, 64
    t = np.zeros((h, w, 4), np.float32)
    t[..., :3] = (0.93, 0.97, 1.0)
    t[..., 3] = 0.16
    t[:2, :, 3] = 0.75; t[-2:, :, 3] = 0.75; t[:, :2, 3] = 0.75; t[:, -2:, 3] = 0.75
    t[:2, :, :3] = 1.0; t[-2:, :, :3] = 1.0; t[:, :2, :3] = 1.0; t[:, -2:, :3] = 1.0
    for j in range(h):
        for i in range(w):
            if 2 <= i < w - 2 and 2 <= j < h - 2 and (i + j) in (40, 41, 42, 43, 50, 51):
                t[j, i, 3] = 0.42
                t[j, i, :3] = 1.0
    return t


def build_sleeve(sc, pos, yaw, lift=0.0):
    if "sleeve" not in TEX:
        TEX["sleeve"] = sleeve_tex()
    xf = Xf(rot_y(yaw), (pos[0], DESK_Y + 5 + lift, pos[1]))
    v = xf.pt([(-150, 0, 205), (150, 0, 205), (150, 0, -205), (-150, 0, -205)])
    sc.append(Quad(v, TEX["sleeve"], (0, 1, 0), double=True, blend=True, noshadow=True))
    return v


def state(t):
    """镜头内时间 t(0..4s) -> 各动画量。"""
    slide = ease(seg(t, 0.7, 1.7))
    paper = (lerp(-640, -415, slide), lerp(400, 190, slide))
    paper_yaw = lerp(-30, -10, slide)
    back = ease(seg(t, 1.75, 2.3))
    on_paper = np.array([paper[0] - 30, DESK_Y + 42, paper[1] + 70])
    rest_l = np.array([-700, DESK_Y + 40, 470])
    hand_l = on_paper * (1 - back) + rest_l * back
    # 右手：先搭在桌沿，1.7s 起移到触控板，2.2s 点击
    click = math.sin(seg(t, 2.1, 2.4) * math.pi)
    reach = ease(seg(t, 1.5, 2.05))
    pad = np.array(TRACKPAD) + np.array([0, 44 - 12 * click, 0])
    rest_r = np.array([-250, DESK_Y + 40, 560])
    hand_r = rest_r * (1 - reach) + pad * reach
    look_screen = ease(seg(t, 1.8, 2.4))
    head_yaw = lerp(10, -20, look_screen)
    head_pitch = lerp(-10, -1, look_screen)       # 先低头看纸，再抬眼看屏幕
    look = "down" if t < 1.9 else "right"
    blink = (1.30 <= t < 1.42) or (3.30 <= t < 3.42)
    return dict(paper=paper, paper_yaw=paper_yaw, hand_l=hand_l, hand_r=hand_r, head_yaw=head_yaw,
                head_pitch=head_pitch, look=look, blink=blink, click=click)


def stick(sc, a, b, r, tex):
    """从 a 到 b 的一根细方棍（铅笔等）。"""
    a = np.asarray(a, np.float64); b = np.asarray(b, np.float64)
    d = b - a
    ln = float(np.linalg.norm(d))
    yv = d / ln
    ref = (0, 0, 1) if abs(yv[1]) > 0.9 else (0, 1, 0)
    xv = norm(np.cross(ref, yv))
    zv = np.cross(xv, yv)
    box(sc, (-r, 0, -r), (r, ln, r), tex, Xf(np.stack([xv, yv, zv], 1), a))


def build_pencil(sc, tip, tail_dir, length=170.0):
    d = norm(tail_dir)
    tip = np.asarray(tip, np.float64)
    stick(sc, tip, tip + d * 16, 3.2, solid(hex3("#3A4248")))
    stick(sc, tip + d * 16, tip + d * (length - 14), 5.0, solid(hex3("#F0C04A")))
    stick(sc, tip + d * (length - 14), tip + d * length, 5.2, solid(hex3("#E58C7A")))


# ---------------------------------------------------------------- 镜头 02 动画量
SLEEVE_HOME = (np.array([-160.0, 25.0]), -8.0)      # 透明文件夹在桌面上的初始位置（镜 01 中已预置）
PAPER_HOME = (np.array([-415.0, 190.0]), -10.0)     # 镜 01 结束时单据的位置
SLEEVE_END = (np.array([335.0, 110.0]), -3.0)        # 滑出画面右缘后的停靠位置（部分出画）
ROW_LOCAL = (-112.0, 112.0, 2.0, 45.0)              # 纸面“收货人地址”一行（局部 x0,x1,z0,z1）


def paper_pose(t):
    """镜 02：2.6s 起单据被推入透明夹，随后连同透明夹沿桌面向右滑行（先推后松手滑行）。"""
    (p0, y0), (p1, y1), (p2, y2) = PAPER_HOME, SLEEVE_HOME, SLEEVE_END
    l1, l2 = float(np.linalg.norm(p1 - p0)), float(np.linalg.norm(p2 - p1))
    d = (l1 + l2) * ease(seg(t, 2.6, 3.95))
    if d < l1:
        u = d / l1
        return p0 + (p1 - p0) * u, lerp(y0, y1, u), p1.copy(), y1
    u = (d - l1) / l2
    # 先贴着桌沿平移，绕过笔记本右前角之后再略向里靠（避免与笔记本底座穿插）
    p = np.array([lerp(p1[0], p2[0], u), lerp(p1[1], p2[1], ease(seg(u, 0.7, 1.0)))])
    return p, lerp(y1, y2, u), p, lerp(y1, y2, u)


def state2(t):
    paper, pyaw, sleeve, syaw = paper_pose(t)
    pxf = Xf(rot_y(pyaw), (paper[0], DESK_Y + 2, paper[1]))
    lp1 = ease(seg(t, 0.9, 1.25)) * (1 - ease(seg(t, 1.6, 1.95)))     # 第一次低头核对纸面
    lp2 = ease(seg(t, 2.45, 2.8))                                     # 装夹时看手上
    push = ease(seg(t, 2.4, 2.9)) * (1 - ease(seg(t, 3.25, 3.8)))
    # 画左手：拿铅笔，点在纸面“收货人地址”一行，轻敲两下；装夹前收回
    k = ease(seg(t, 0.9, 1.35)) * (1 - ease(seg(t, 2.25, 2.6)))
    tap = math.sin(seg(t, 1.38, 1.56) * math.pi) + math.sin(seg(t, 1.62, 1.8) * math.pi)
    tip_on = Xf(rot_y(PAPER_HOME[1]), (PAPER_HOME[0][0], DESK_Y + 2, PAPER_HOME[0][1])).pt((-78, 4, 24))
    grip = np.array([-60.0, 52.0, 78.0])
    rest_l = np.array([-615.0, DESK_Y + 40, 290.0])
    hand_l = rest_l * (1 - k) + (tip_on + grip) * k + np.array([0, 9 * tap * k + 14 * (1 - k), 0])
    pencil_tip = hand_l - grip
    # 画右手：触控板（移动指针 → 2.1s 点击）→ 按住单据推入透明夹 → 3.35s 松手收回
    click = math.sin(seg(t, 2.0, 2.3) * math.pi)
    mv = ease(seg(t, 1.1, 1.85))
    pad = np.array(TRACKPAD) + np.array([14 * mv, 44 - 12 * click, -10 * mv])
    t_rel = 3.2
    pp, py, _, _ = paper_pose(min(t, t_rel))
    on_paper = Xf(rot_y(py), (pp[0], DESK_Y + 2, pp[1])).pt((-50, 40, 80))
    go = ease(seg(t, 2.3, 2.6))
    rel = ease(seg(t, t_rel, 3.7))
    rest_r = np.array([-170.0, DESK_Y + 46, 330.0])
    hand_r = (pad * (1 - go) + on_paper * go) * (1 - rel) + rest_r * rel + np.array([0, 26 * math.sin(rel * math.pi), 0])
    head_yaw = -20 + 28 * lp1 + 16 * lp2
    head_pitch = -1 - 10 * lp1 - 9 * lp2
    look = "down" if (lp1 > 0.5 or lp2 > 0.5) else "right"
    blink = (0.96 <= t < 1.08) or (2.48 <= t < 2.6)
    done = ease_out(seg(t, 2.15, 2.5))
    return dict(paper=paper, paper_yaw=pyaw, sleeve=sleeve, sleeve_yaw=syaw, pxf=pxf, hand_l=hand_l, hand_r=hand_r,
                pencil_tip=pencil_tip, pencil_dir=grip + np.array([-30.0, 30.0, 40.0]), head_yaw=head_yaw,
                head_pitch=head_pitch, look=look, blink=blink, lean=3 + 4 * lp1 + 6 * push, yaw=-14 + 6 * push,
                done=done, click=click)


def sky_image(w, h):
    top, bot = np.array(hex3("#79B9EA"), np.float32), np.array(hex3("#EAF3F4"), np.float32)
    g = np.linspace(0, 1, h, dtype=np.float32)[:, None, None] ** 0.9
    img = top * (1 - g) + bot * g
    return np.broadcast_to(img, (h, w, 3)).copy()
