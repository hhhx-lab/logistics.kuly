#!/usr/bin/env bash
# 使用项目已有环境；不会修改全局PATH或安装依赖。
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_PYTHON="$TASK_ROOT/src/backend/.venv/bin/python"
[ -x "$TASK_PYTHON" ] || { printf '项目Python环境缺失，请先准备src/backend/.venv。\n' >&2; exit 1; }

# Finder双击启动时没有登录终端的Conda路径，仅为当前子进程补齐。
for task_bin in "${CONDA_PREFIX:-/nonexistent}/bin" "$HOME/miniforge3/bin" "$HOME/miniconda3/bin" "$HOME/anaconda3/bin"; do
  if [ -x "$task_bin/node" ] && [ -x "$task_bin/npm" ]; then
    export PATH="$task_bin:$PATH"
    break
  fi
done
if ! command -v docker >/dev/null 2>&1 && [ -x "$HOME/.orbstack/bin/docker" ]; then
  export PATH="$HOME/.orbstack/bin:$PATH"
fi
exec "$TASK_PYTHON" "$TASK_ROOT/scripts/local_runtime.py" "$@"
