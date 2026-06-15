#!/usr/bin/env bash
# 本地开发：--reload 仅监视代码子目录，不监视仓库根下的 hermes-data（常为 docker 挂载且权限为 700，
# watchfiles 递归当前工作区时会 PermissionError 刷屏）。
#
# 未监视：仓库根目录的 *.py（如 main.py、auth.py、db.py）。修改这些文件后请手动重启本进程。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PORT="${PORT:-3001}"

clear_stale_dev_port() {
  local port="$1"
  if [[ "${UVICORN_DEV_CLEAR_PORT:-true}" != "true" ]]; then
    return
  fi

  python - "$ROOT" "$port" <<'PY'
import os
import signal
import sys
import time
from pathlib import Path

root = Path(sys.argv[1]).resolve()
port = int(sys.argv[2])


def _listening_inodes(path: str) -> set[str]:
    inodes: set[str] = set()
    try:
        lines = Path(path).read_text().splitlines()[1:]
    except FileNotFoundError:
        return inodes
    for line in lines:
        parts = line.split()
        if len(parts) < 10:
            continue
        local_addr, state, inode = parts[1], parts[3], parts[9]
        if state != "0A":
            continue
        try:
            local_port = int(local_addr.rsplit(":", 1)[1], 16)
        except ValueError:
            continue
        if local_port == port:
            inodes.add(inode)
    return inodes


def _pids_for_inodes(inodes: set[str]) -> set[int]:
    pids: set[int] = set()
    if not inodes:
        return pids
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        fd_dir = proc / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    pids.add(int(proc.name))
                    break
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return pids


def _proc_info(pid: int) -> dict[str, str]:
    proc = Path("/proc") / str(pid)
    try:
        cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        cmd = ""
    try:
        cwd = str((proc / "cwd").resolve())
    except OSError:
        cwd = ""
    try:
        env_items = (proc / "environ").read_bytes().split(b"\0")
    except OSError:
        env_items = []
    env = {}
    for item in env_items:
        if b"=" in item:
            key, value = item.split(b"=", 1)
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return {"cmd": cmd, "cwd": cwd, "pwd": env.get("PWD", ""), "venv": env.get("VIRTUAL_ENV", "")}


def _is_safe_dev_process(info: dict[str, str]) -> bool:
    cwd = Path(info["cwd"] or info["pwd"] or "/").resolve()
    venv = info["venv"]
    cmd = info["cmd"]
    in_repo = cwd == root or root in cwd.parents
    repo_venv = bool(venv) and (Path(venv).resolve() == root / ".env311" or root in Path(venv).resolve().parents)
    looks_like_uvicorn = "uvicorn" in cmd or "multiprocessing.spawn" in cmd or "multiprocessing.resource_tracker" in cmd
    looks_like_python = "python" in cmd
    return in_repo and repo_venv and looks_like_python and looks_like_uvicorn


inodes = _listening_inodes("/proc/net/tcp") | _listening_inodes("/proc/net/tcp6")
pids = _pids_for_inodes(inodes)
if not pids:
    sys.exit(0)

infos = {pid: _proc_info(pid) for pid in sorted(pids)}
unsafe = {pid: info for pid, info in infos.items() if not _is_safe_dev_process(info)}
if unsafe:
    print(f"error: port {port} is already in use by a non-dev process:", file=sys.stderr)
    for pid, info in unsafe.items():
        print(f"  pid={pid} cwd={info['cwd']} cmd={info['cmd']}", file=sys.stderr)
    print("Refusing to kill it automatically. Stop it manually or set another PORT.", file=sys.stderr)
    sys.exit(1)

for pid in infos:
    print(f"warn: killing stale local uvicorn process on port {port}: pid={pid}", file=sys.stderr)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

deadline = time.time() + 2
while time.time() < deadline:
    if not any(Path("/proc", str(pid)).exists() for pid in infos):
        break
    time.sleep(0.1)

for pid in infos:
    if Path("/proc", str(pid)).exists():
        print(f"warn: force killing stale local uvicorn process on port {port}: pid={pid}", file=sys.stderr)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
PY
}

clear_stale_dev_port "$PORT"

# 本地开发默认由 uvicorn 进程内嵌 APScheduler，避免 Strategy Lab Loop、
# 定时复盘等功能因为独立 scheduler_runner 未启动而静默不推进。
export ENABLE_EMBEDDED_SCHEDULER="${ENABLE_EMBEDDED_SCHEDULER:-true}"

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
    --port "$PORT" \
    --reload \
    "$@"
else
  exec uvicorn main:app \
    --host "${HOST:-0.0.0.0}" \
    --port "$PORT" \
    --reload \
    "${RELOAD_ARGS[@]}" \
    "$@"
fi
