#!/usr/bin/env bash
# 本地开发：--reload 仅监视代码子目录，不监视仓库根下的 hermes-data（常为 docker 挂载且权限为 700，
# watchfiles 递归当前工作区时会 PermissionError 刷屏）。
#
# 未监视：仓库根目录的 *.py（如 main.py、auth.py、db.py）。修改这些文件后请手动重启本进程。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RELOAD_ARGS=()
for d in routers services models utils tools config monitoring; do
  if [[ -d "$d" ]]; then
    RELOAD_ARGS+=(--reload-dir "$d")
  fi
done

if [[ ${#RELOAD_ARGS[@]} -eq 0 ]]; then
  echo "warn: 未找到任何 --reload-dir 子目录，回退为监视当前目录（可能仍会访问 hermes-data）" >&2
  exec uvicorn main:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-3001}" \
    --reload \
    "$@"
else
  exec uvicorn main:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-3001}" \
    --reload \
    "${RELOAD_ARGS[@]}" \
    "$@"
fi
