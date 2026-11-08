"""逐帧渲染并编码。

用法：
    python render.py --out film.mp4                   # 渲染全片（镜 01–09，共 45.5 秒）
    python render.py --shots 7 --ss 1 --out s7.mp4    # 只渲染镜 07，低质量快速预览
参数：
    --ss       超采样倍数（2 = 成片质量，1 = 快速预览）
    --workers  并行进程数（ss=2 时每个进程峰值约占 2 GB 内存，按“可用内存 ÷ 2GB”设置；
               内存不足时进程会被系统杀掉并导致卡住，此时改小 --workers 重新运行即可，已渲染的帧会跳过）
    --frames   帧缓存目录（默认系统临时目录，编码完成后自动删除；加 --keep 保留）
需要系统已安装 ffmpeg。
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from multiprocessing import Pool

import common as C

# 每镜时长（秒）：01 发货 / 02 确认疑点 / 03 出窗 / 04 海上 / 05 船方 / 06 口岸 / 07 铁路 / 08 江南 / 09 收货+片尾
DUR = {1: 4.0, 2: 4.0, 3: 4.5, 4: 5.0, 5: 5.5, 6: 6.0, 7: 5.5, 8: 4.5, 9: 6.5}


def _shot_fn(shot):
    if shot <= 2:
        import shots
        return shots.SHOTS[shot]
    import shots2
    return shots2.SHOTS[shot]


def _job(args):
    idx, shot, t, ss, out_dir = args
    path = os.path.join(out_dir, "f%05d.png" % idx)
    if os.path.exists(path):
        return idx
    surf, _keep = _shot_fn(shot)(t, ss)
    tmp = path + ".part"
    surf.write_to_png(tmp)
    os.replace(tmp, path)
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="1,2,3,4,5,6,7,8,9")
    ap.add_argument("--ss", type=int, default=int(os.environ.get("DOC_ANIM_SS") or 2))
    ap.add_argument("--workers", type=int, default=int(os.environ.get("DOC_ANIM_WORKERS") or 2))
    ap.add_argument("--out", default="preview.mp4")
    ap.add_argument("--frames", default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--no-encode", action="store_true")
    a = ap.parse_args()

    shot_ids = [int(s) for s in a.shots.split(",")]
    frames = a.frames or tempfile.mkdtemp(prefix="docjourney_")
    os.makedirs(frames, exist_ok=True)
    jobs = []
    base = 0
    for sid in shot_ids:
        n = int(round(DUR[sid] * C.FPS))
        for i in range(n):
            jobs.append((base + i, sid, i / C.FPS, a.ss, frames))
        base += n
    # 已渲染的帧直接跳过（不交给进程池，避免大量瞬时任务触发子进程频繁重启）
    jobs = [j for j in jobs if not os.path.exists(os.path.join(frames, "f%05d.png" % j[0]))]
    t0 = time.time()
    done = 0
    with Pool(a.workers, maxtasksperchild=12) as pool:
        for _ in pool.imap_unordered(_job, jobs):
            done += 1
            if done % 12 == 0 or done == len(jobs):
                print("%d/%d  %.0fs" % (done, len(jobs), time.time() - t0), flush=True)
    if a.no_encode:
        return
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(C.FPS), "-i", os.path.join(frames, "f%05d.png"),
           "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(C.FPS),
           "-movflags", "+faststart", a.out]
    subprocess.run(cmd, check=True)
    print("encoded ->", a.out, flush=True)
    if not a.keep and a.frames is None:
        shutil.rmtree(frames, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
