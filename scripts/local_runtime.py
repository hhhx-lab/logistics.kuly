"""监督本机开发服务；只停止自己启动的进程，不接管已有服务。"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "src/backend"
FRONTEND = ROOT / "src/frontend"
PYTHON = BACKEND / ".venv/bin/python"
STATE = ROOT / ".runtime/local"
LOCK = STATE / "launcher.lock"
sys.path.insert(0, str(BACKEND))


def info(message):
    print(message, flush=True)


def command(args, *, cwd=ROOT, timeout=15):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"命令超时：{Path(args[0]).name}（请检查本地依赖服务）") from None
    if result.returncode:
        # 不回显可能含连接串的标准错误；完整业务日志写入独立文件。
        raise RuntimeError(f"命令失败：{Path(args[0]).name}，退出码{result.returncode}")
    return result.stdout.strip()


def process_command(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return result.stdout.strip()


def lock_owner():
    try:
        data = json.loads(LOCK.read_text())
        pid = int(data["pid"])
        args = shlex.split(process_command(pid))
        if len(args) < 2:
            return None
        probe = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True)
        directory = next((Path(line[1:]) for line in probe.stdout.splitlines() if line.startswith("n")), None)
        if directory is None:
            return None
        interpreter, script = (Path(value) if Path(value).is_absolute() else directory / value for value in args[:2])
        return pid if interpreter.resolve() == PYTHON.resolve() and script.resolve() == Path(__file__).resolve() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


class Runtime:
    def __init__(self):
        self.children = {}
        self.logs = []
        self.reused = {}
        self.stopping = False
        self.locked = False
        self.lock_file = None
        self.http = build_opener(ProxyHandler({}))

    def acquire(self):
        STATE.mkdir(parents=True, exist_ok=True)
        self.lock_file = LOCK.open("a+")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            info("一键启动器已运行或正在启动，请查看其终端，无需重复启动。")
            return False
        self.lock_file.seek(0)
        self.lock_file.truncate()
        json.dump({"pid": os.getpid()}, self.lock_file)
        self.lock_file.flush()
        self.locked = True
        signal.signal(signal.SIGINT, self.stop_signal)
        signal.signal(signal.SIGTERM, self.stop_signal)
        return True

    def stop_signal(self, *_):
        self.stopping = True

    def check(self):
        if self.stopping:
            raise InterruptedError
        for name, proc in self.children.items():
            if proc.poll() is not None:
                raise RuntimeError(f"{name}已退出，查看{STATE / (name + '.log')}")
        for name, pid in self.reused.items():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                raise RuntimeError(f"复用的{name}已退出，请重新启动；不会自动重投任务") from None

    def write_state(self):
        path = STATE / "processes.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"launcher": os.getpid(), "started": {k: p.pid for k, p in self.children.items()}, "reused": self.reused}, indent=2))
        temp.replace(path)

    def spawn(self, name, args, cwd):
        self.check()
        handle = (STATE / f"{name}.log").open("a")
        self.logs.append(handle)
        handle.write(f"\n--- 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        handle.flush()
        self.children[name] = subprocess.Popen(args, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        self.write_state()
        info(f"启动{name}，PID {self.children[name].pid}")

    def probe(self, url, *, ready=False):
        try:
            # 服务端readiness最多45秒，避免短超时重复触发尚未结束的探针。
            with self.http.open(url, timeout=50 if ready else 5) as response:
                return response.status == 200 and (not ready or json.load(response).get("status") == "ready")
        except Exception:
            return False

    def wait_http(self, url, *, ready=False):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self.check()
            if self.probe(url, ready=ready):
                return
            time.sleep(1)
        raise RuntimeError(f"服务在等待窗口内未就绪：{url}，查看{STATE}")

    def cleanup(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pending = [(name, proc) for name, proc in self.children.items() if proc.poll() is None]
        if pending:
            info("正在停止本次启动的服务；Worker暖退出，等待当前任务安全结束……")
        for _, proc in pending:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        last_note = time.monotonic()
        while any(proc.poll() is None for _, proc in pending):
            time.sleep(1)
            if time.monotonic() - last_note >= 30:
                info("仍在等待任务暖退出；不会强杀或重新投递任务。")
                last_note = time.monotonic()
        for handle in self.logs:
            handle.close()
        if self.locked:
            (STATE / "processes.json").unlink(missing_ok=True)
            self.lock_file.seek(0)
            self.lock_file.truncate()
            self.lock_file.close()
        info("本次服务已停止；依赖容器及启动前已有服务保留。")


def existing_services():
    processes = command(["ps", "-axo", "pid=,command="]).splitlines()
    result = {}

    def cwd(pid):
        probe = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=15)
        # 服务可能在 ps 快照之后完成暖退出；此时不再复用该进程。
        return next((line[1:] for line in probe.stdout.splitlines() if line.startswith("n")), "") if probe.returncode == 0 else ""

    for name, port, directory, marker in [("api", 8000, BACKEND, "-m uvicorn app.api.main:app"), ("frontend", 5173, FRONTEND, "vite")]:
        probe = subprocess.run(["lsof", "-nP", "-tiTCP:" + str(port), "-sTCP:LISTEN"], capture_output=True, text=True)
        pids = set(probe.stdout.split())
        if pids:
            if len(pids) != 1:
                raise RuntimeError(f"端口{port}有多个占用进程，请先核对")
            pid = int(pids.pop())
            text = process_command(pid)
            if cwd(pid) != str(directory) or marker not in text:
                raise RuntimeError(f"端口{port}被其他服务占用（PID {pid}），不会终止它")
            result[name] = pid
    for line in processes:
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid, text = int(parts[0]), parts[1]
        if not (text.startswith(str(PYTHON) + " ") or text.startswith(".venv/bin/python ")):
            continue
        name = None
        if "-m celery -A app.workflows.worker:queue worker" in text:
            for queue in ("documents", "conversation", "maintenance"):
                if f"-n {queue}@" in text:
                    name = queue
        elif "ops/runtime.py monitor" in text:
            name = "monitor"
        if name and cwd(pid) == str(BACKEND):
            if name in result:
                raise RuntimeError(f"已存在多个{name}进程，请先核对，避免重复消费队列")
            result[name] = pid
    return result


def prepare_dependencies():
    info("检查PostgreSQL、Redis、S3容器……")
    command(["docker", "info"], timeout=10)
    names = ["logistics-postgres", "logistics-redis", "logistics-s3"]

    def healthy():
        try:
            rows = json.loads(command(["docker", "inspect", *names], timeout=10))
            return all(row.get("State", {}).get("Health", {}).get("Status") == "healthy" for row in rows)
        except (RuntimeError, ValueError):
            return False

    if not healthy():
        command(["docker", "compose", "up", "--pull", "never", "--no-build", "-d"], timeout=60)
        deadline = time.monotonic() + 120
        while not healthy():
            if time.monotonic() >= deadline:
                raise RuntimeError("依赖容器未达到健康状态，请检查docker compose logs")
            time.sleep(2)


def check_schema(settings):
    import psycopg
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from app.infra.database import sync_database_url
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    expected = set(ScriptDirectory.from_config(cfg).get_heads())
    url = sync_database_url(settings).replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(url, connect_timeout=5, options="-c statement_timeout=5000") as conn:
            current = {row[0] for row in conn.execute("SELECT version_num FROM public.alembic_version")}
    except Exception:
        raise RuntimeError("无法核对数据库迁移版本，请检查依赖；不会运行中迁移") from None
    if current != expected:
        raise RuntimeError("数据库迁移落后，请先停止已有API/Worker，再运行一键启动")


def start(runtime, no_browser):
    from app.config import Settings
    import celery, fastapi, langgraph, sqlalchemy  # noqa: F401
    if not (ROOT / ".env").is_file():
        raise RuntimeError("根目录.env缺失，请按.env.example填写")
    if not (FRONTEND / "node_modules/.bin/vite").exists():
        raise RuntimeError("前端依赖缺失，请先按文档准备；启动器不安装依赖")
    for name in ("node", "npm", "docker", "lsof"):
        if not shutil.which(name):
            raise RuntimeError(f"缺少{name}；请先准备现有环境")
    if int(command(["node", "-p", 'process.versions.node.split(".")[0]'])) < 20:
        raise RuntimeError("Node.js需要20或更高版本")
    settings = Settings()
    runtime.reused = existing_services()
    runtime.write_state()
    for name, pid in runtime.reused.items():
        info(f"复用本项目{name}，PID {pid}；本次停止不会关闭它")
    prepare_dependencies()
    if any(name in runtime.reused for name in ("api", "documents", "conversation", "maintenance")):
        check_schema(settings)
    else:
        info("执行领域迁移与LangGraph检查点初始化……")
        runtime.spawn("migrate", [str(PYTHON), str(ROOT / "ops/runtime.py"), "migrate"], BACKEND)
        proc = runtime.children["migrate"]
        deadline = time.monotonic() + 120
        while proc.poll() is None:
            if runtime.stopping:
                raise InterruptedError
            if time.monotonic() >= deadline:
                raise RuntimeError(f"迁移超时，请检查{STATE / 'migrate.log'}")
            time.sleep(1)
        if proc.returncode:
            raise RuntimeError(f"迁移失败，请检查{STATE / 'migrate.log'}")
        del runtime.children["migrate"]
    if settings.using_fake_externals():
        info("当前.env启用了假解析/假模型，相关产物仅供工程联调。")
    for name in ("documents", "conversation", "maintenance"):
        if name not in runtime.reused:
            queues = "documents,celery" if name == "documents" else name
            runtime.spawn(name, [str(PYTHON), "-m", "celery", "-A", "app.workflows.worker:queue", "worker", "-n", name + "@local", "-Q", queues, "--pool=solo", "--concurrency=1", "--loglevel=WARNING"], BACKEND)
    if "monitor" not in runtime.reused:
        runtime.spawn("monitor", [str(PYTHON), str(ROOT / "ops/runtime.py"), "monitor"], BACKEND)
    if "api" not in runtime.reused:
        runtime.spawn("api", [str(PYTHON), "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1", "--port", "8000"], BACKEND)
    runtime.wait_http("http://127.0.0.1:8000/api/v1/health/ready", ready=True)
    if "frontend" not in runtime.reused:
        runtime.spawn("frontend", [shutil.which("node"), str(FRONTEND / "node_modules/vite/bin/vite.js"), "--host", "localhost", "--port", "5173", "--strictPort"], FRONTEND)
    runtime.wait_http("http://localhost:5173/")
    runtime.write_state()
    info(f"\n已就绪：http://localhost:5173\nAPI：http://127.0.0.1:8000\n日志：{STATE}\n账号：demo / demo（已有账号以部署为准）\n按Ctrl+C停止本次启动的服务。")
    if sys.platform == "darwin" and not no_browser:
        subprocess.run(["open", "http://localhost:5173"], check=False)
    while not runtime.stopping:
        runtime.check()
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="物流单证智能体本地一键启动")
    parser.add_argument("action", nargs="?", choices=["start", "stop"], default="start")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.action == "stop":
        pid = lock_owner()
        if pid:
            os.kill(pid, signal.SIGTERM)
            info("已请求暖停止，结果与日志请查看启动终端；已有服务不会关闭。")
        else:
            info("没有本脚本管理的启动器；已有手动启动服务请在原终端停止。")
        return 0
    runtime = Runtime()
    try:
        if not runtime.acquire():
            return 0
        start(runtime, args.no_browser)
        return 0
    except (InterruptedError, KeyboardInterrupt):
        return 0
    except Exception as exc:
        info(f"启动失败：{exc}" if isinstance(exc, RuntimeError) else f"启动失败：{type(exc).__name__}，请检查配置与日志{STATE}")
        return 1
    finally:
        if runtime.locked:
            runtime.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
