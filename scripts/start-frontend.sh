#!/usr/bin/env bash
# 一键启动前端开发服务器。
# 默认连接项目后端；模拟层仅在显式配置时启用。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"
FRONTEND="$ROOT/src/frontend"

# Vite 的 envDir 指向项目根目录，.env 必须放这里，放 src/frontend 无效
ENV_FILE="$ROOT/.env"

die() { printf '\033[31m错误：%s\033[0m\n' "$1" >&2; exit 1; }
info() { printf '\033[36m%s\033[0m\n' "$1"; }

command -v node >/dev/null 2>&1 || die "未找到 node，请先安装 Node.js 20 或更高版本。"
command -v npm  >/dev/null 2>&1 || die "未找到 npm，请先安装 Node.js 20 或更高版本。"

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [ "$NODE_MAJOR" -lt 20 ]; then
  die "Node.js 版本过低（当前 $(node -v)），Vite 6 需要 20 或更高版本。"
fi

[ -d "$FRONTEND" ] || die "找不到前端目录：$FRONTEND"

# .env 不进版本库；启动过程不下载依赖或擅自切换数据源。
if [ ! -f "$ENV_FILE" ]; then
  die "未找到项目 .env，请参考 .env.example 配置后启动。"
fi

cd "$FRONTEND"

[ -d node_modules ] || die "前端依赖尚未安装，请先按项目依赖方案建立环境。"

info "启动开发服务器，默认地址 http://localhost:5173"
info "真实后端开发种子账号 demo 或 demo2，初始密码均为 demo；按 Ctrl+C 停止。"
exec npm run dev -- "$@"
