"""容器入口：迁移、廉价进程探针及独立监控。凭据只由项目Settings读取。"""
import argparse
import socket
import sys
import time
from pathlib import Path
from urllib.request import urlopen

BACKEND = Path(__file__).resolve().parents[1] / "src/backend"
sys.path.insert(0, str(BACKEND))


def migrate():
    from alembic import command
    from alembic.config import Config
    from app.config import get_settings
    from app.infra.checkpoints import checkpoint_database_url, initialize_checkpointer
    settings = get_settings()
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(config, "head")
    initialize_checkpointer(checkpoint_database_url(settings))
    print("领域迁移与检查点初始化完成")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("migrate", "api-health", "worker-health", "monitor"))
    parser.add_argument("queue", nargs="?", choices=("documents", "conversation", "maintenance"))
    args = parser.parse_args()
    if args.action == "migrate":
        migrate()
    elif args.action == "api-health":
        with urlopen("http://127.0.0.1:8000/api/health/live", timeout=3) as response:
            if response.status != 200:
                raise RuntimeError("api_not_live")
    elif args.action == "worker-health":
        if not args.queue:
            parser.error("worker-health必须指定队列")
        from app.workflows.worker import queue
        target = f"{args.queue}@{socket.gethostname()}"
        replies = queue.control.ping(destination=[target], timeout=5)
        if not any(reply.get(target, {}).get("ok") == "pong" for reply in replies):
            raise RuntimeError("worker_not_live")
    else:
        from app.ops.observability import collect_operational_metrics, record_cycle
        from app.config import get_settings
        while True:
            try:
                report = collect_operational_metrics(get_settings(), check_workers=True)
                record_cycle(get_settings(), "monitor", success=True)
                # 不含票据ID、凭据、来源内容，只保可操作告警类型和数量。
                print("operational_monitor " + str({"status": report["status"], "alerts": report["alerts"]}), flush=True)
            except Exception as exc:
                record_cycle(get_settings(), "monitor", success=False)
                print("operational_monitor_failed " + type(exc).__name__, flush=True)
            time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("runtime_failed " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
