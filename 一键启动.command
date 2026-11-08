#!/bin/bash
cd "$(dirname "$0")" || exit 1
./scripts/start-local.sh
task_result=$?
if [ "$task_result" -ne 0 ]; then
  printf '\n启动未完成，请查看上方原因。按回车关闭窗口。\n'
  read -r
fi
exit "$task_result"
