#!/usr/bin/env bash
# 启动已有项目环境，执行迁移并启动API与独立任务队列。
# 实际适配器模式由Settings读取并显式提示。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PROJECT_ROOT="$(pwd)"
BACKEND="$PROJECT_ROOT/src/backend"
ENV_FILE="$PROJECT_ROOT/.env"

die() { printf '\033[31m错误：%s\033[0m\n' "$1" >&2; exit 1; }
info() { printf '\033[36m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }

command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装并启动 OrbStack 或 Docker Desktop。"
docker info >/dev/null 2>&1 || die "Docker daemon 未运行，请先启动 OrbStack 或 Docker Desktop。"
[ -x "$BACKEND/.venv/bin/python" ] || die "项目环境未准备，请先按部署文档授权准备uv环境。"

[ -f "$ENV_FILE" ] || die ".env 不存在。可从 .env.example 复制一份并按注释填写，或先运行一次本脚本前准备好它。"

info "1/4 拉起依赖服务（PostgreSQL、Redis、S3）……"
docker compose up -d
# 等到三个服务都健康，否则迁移会连不上
for name in logistics-postgres logistics-redis logistics-s3; do
  for _ in $(seq 1 40); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$name" 2>/dev/null || echo missing)"
    [ "$status" = "healthy" ] && break
    [ "$status" = "missing" ] && die "容器 $name 未创建，请检查 docker compose 输出。"
    sleep 3
  done
  [ "$(docker inspect --format '{{.State.Health.Status}}' "$name")" = "healthy" ] \
    || die "$name 在等待窗口内未达到健康状态。"
done

info "2/4 检查现有 Python 独立环境……"
cd "$BACKEND"
PYTHON="$BACKEND/.venv/bin/python"
"$PYTHON" -c 'import fastapi,celery,langgraph,sqlalchemy'

info "3/4 执行数据库迁移……"
if [ -d alembic ]; then
  "$PYTHON" -m alembic upgrade head
else
  warn "尚未创建 alembic 迁移目录，跳过这一步。"
fi

# 读取实际模式，包含未填写适配器变量时的默认值与环境覆盖。
FAKE_EXTERNALS="$("$PYTHON" -c 'from app.config import get_settings; print("1" if get_settings().using_fake_externals() else "0")')"
if [ "$FAKE_EXTERNALS" = "1" ]; then
  warn "注意：当前使用确定性假解析／假模型适配器，产出内容仅供打通工程链路，"
  warn "      不是真实识别与翻译结果。填写 MinerU 与模型 Key 后改为 mineru／openai_compatible。"
fi

"$PYTHON" - <<'PYCODE'
from app.config import get_settings
from app.infra.checkpoints import initialize_checkpointer
url=get_settings().checkpoint_database_url
if not url: raise SystemExit("CHECKPOINT_DATABASE_URL未配置")
initialize_checkpointer(url.replace("postgresql+psycopg://","postgresql://").replace("postgresql+asyncpg://","postgresql://"))
PYCODE
info "4/4 启动 API、独立对话/单证/清理队列及运维监控，按 Ctrl+C 停止。"
# macOS采用solo避免fork资源混用；生产Linux按文档使用prefork并配置进程数。
TASK_POOL="solo"
[ "$(uname -s)" = "Linux" ] && TASK_POOL="prefork"
"$PYTHON" -m celery -A app.workflows.worker:queue worker -n 'documents@%h' -Q documents,celery --pool="$TASK_POOL" --concurrency=1 --loglevel=WARNING &
DOCUMENT_WORKER_PID=$!
"$PYTHON" -m celery -A app.workflows.worker:queue worker -n 'conversation@%h' -Q conversation --pool="$TASK_POOL" --concurrency=1 --loglevel=WARNING &
CONVERSATION_WORKER_PID=$!
"$PYTHON" -m celery -A app.workflows.worker:queue worker -n 'maintenance@%h' -Q maintenance --pool="$TASK_POOL" --concurrency=1 --loglevel=WARNING &
MAINTENANCE_WORKER_PID=$!
"$PYTHON" "$PROJECT_ROOT/ops/runtime.py" monitor &
MONITOR_PID=$!
trap 'kill "$DOCUMENT_WORKER_PID" "$CONVERSATION_WORKER_PID" "$MAINTENANCE_WORKER_PID" "$MONITOR_PID" 2>/dev/null || true' EXIT
"$PYTHON" -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
