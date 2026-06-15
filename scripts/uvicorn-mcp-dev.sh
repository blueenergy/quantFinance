#!/usr/bin/env bash
# 本地 MCP 开发：一条命令同时启动 read/actions 两个 MCP 服务。
# 这样 Hermes 可以继续使用 http://localhost:3002/api/mcp 和
# http://localhost:3003/api/mcp，而不用反复 rebuild Docker image。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

READ_PORT="${MCP_READ_PORT:-3002}"
ACTIONS_PORT="${MCP_ACTIONS_PORT:-3003}"

read_pid=""
actions_pid=""

cleanup() {
  local status=$?
  trap - INT TERM EXIT

  if [[ -n "$read_pid" ]] && kill -0 "$read_pid" 2>/dev/null; then
    kill "$read_pid" 2>/dev/null || true
  fi
  if [[ -n "$actions_pid" ]] && kill -0 "$actions_pid" 2>/dev/null; then
    kill "$actions_pid" 2>/dev/null || true
  fi

  wait "$read_pid" "$actions_pid" 2>/dev/null || true
  exit "$status"
}

trap cleanup INT TERM EXIT

echo "Starting MCP read server on :$READ_PORT"
ENABLE_EMBEDDED_SCHEDULER=false \
ENABLE_MCP_SERVER=true \
MCP_SERVER_MODE=read \
PORT="$READ_PORT" \
./scripts/uvicorn-dev.sh "$@" \
  > >(sed -u "s/^/[read:$READ_PORT] /") \
  2> >(sed -u "s/^/[read:$READ_PORT] /" >&2) &
read_pid=$!

echo "Starting MCP actions server on :$ACTIONS_PORT"
ENABLE_EMBEDDED_SCHEDULER=false \
ENABLE_MCP_SERVER=true \
MCP_SERVER_MODE=actions \
PORT="$ACTIONS_PORT" \
./scripts/uvicorn-dev.sh "$@" \
  > >(sed -u "s/^/[actions:$ACTIONS_PORT] /") \
  2> >(sed -u "s/^/[actions:$ACTIONS_PORT] /" >&2) &
actions_pid=$!

wait -n "$read_pid" "$actions_pid"
