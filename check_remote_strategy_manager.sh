#!/usr/bin/env bash
set -euo pipefail

# 用法:
#   ./check_remote_strategy_manager.sh [ENV_FILE]
# 例子:
#   ./check_remote_strategy_manager.sh /home/shuyolin/trading/quantFinance/.env
#
# 可选环境变量:
#   TEST_SUB="shuyolin"                  # JWT sub
#   TEST_USER_ID="689ea9741e5758cc9f847296"  # JWT user_id

ENV_FILE="${1:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "❌ 找不到 env 文件: $ENV_FILE"
  exit 1
fi

# 从 .env 中读取变量（支持 KEY = value / KEY=value / 带引号）
get_env() {
  local key="$1"
  python3 - "$ENV_FILE" "$key" <<'PY'
import re, sys
env_file, key = sys.argv[1], sys.argv[2]
val = ""
pattern = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')
with open(env_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = pattern.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        if k != key:
            continue
        # 去掉行尾注释（仅在非引号场景）
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        else:
            v = v.split('#', 1)[0].strip()
        val = v
print(val)
PY
}

REMOTE_WORKER_API="$(get_env REMOTE_WORKER_API)"
JWT_SECRET_KEY="$(get_env JWT_SECRET_KEY)"
USE_NGINX_WEBSOCKET="$(get_env USE_NGINX_WEBSOCKET)"
PUBLIC_HOST="$(get_env PUBLIC_HOST)"
USE_HTTPS="$(get_env USE_HTTPS)"
USE_REMOTE_ONLY="$(get_env USE_REMOTE_ONLY)"

if [[ -z "${REMOTE_WORKER_API}" ]]; then
  echo "❌ REMOTE_WORKER_API 未配置"
  exit 1
fi

if [[ -z "${JWT_SECRET_KEY}" ]]; then
  echo "❌ JWT_SECRET_KEY 未配置（quantFinance 与 quant-strategy-manager 必须一致）"
  exit 1
fi

echo "=== 配置检查 ==="
echo "ENV_FILE=$ENV_FILE"
echo "REMOTE_WORKER_API=$REMOTE_WORKER_API"
echo "USE_REMOTE_ONLY=${USE_REMOTE_ONLY:-<empty>}"
echo "USE_NGINX_WEBSOCKET=${USE_NGINX_WEBSOCKET:-<empty>}"
echo "PUBLIC_HOST=${PUBLIC_HOST:-<empty>}"
echo "USE_HTTPS=${USE_HTTPS:-<empty>}"
echo

BASE_URL="${REMOTE_WORKER_API%/api/workers}"
HEALTH_URL="${BASE_URL}/health"

echo "1) 探活远端 Strategy Manager: $HEALTH_URL"
HEALTH_BODY="$(mktemp)"
HEALTH_CODE="$(curl -sS -m 10 -o "$HEALTH_BODY" -w '%{http_code}' "$HEALTH_URL" || true)"
if [[ "$HEALTH_CODE" != "200" ]]; then
  echo "❌ 健康检查失败，HTTP=$HEALTH_CODE"
  echo "返回内容:"
  cat "$HEALTH_BODY" || true
  rm -f "$HEALTH_BODY"
  exit 1
fi
echo "✅ 健康检查通过"
rm -f "$HEALTH_BODY"
echo

echo "2) 生成测试 JWT（与 quantFinance/quant-strategy-manager 同算法）"
if ! python3 -c "import jwt" >/dev/null 2>&1; then
  echo "❌ 缺少 PyJWT，请先安装: pip install PyJWT"
  exit 1
fi

# 默认使用当前线上验证通过的用户标识，避免因 user_id 过滤导致误判。
# 如需验证其他用户，可在执行时覆盖：
#   TEST_SUB=<username> TEST_USER_ID=<user_id> ./check_remote_strategy_manager.sh ./.env
TEST_SUB="${TEST_SUB:-shuyolin}"
TEST_USER_ID="${TEST_USER_ID:-689ea9741e5758cc9f847296}"

TEST_TOKEN="$(python3 - "$JWT_SECRET_KEY" "$TEST_SUB" "$TEST_USER_ID" <<'PY'
import sys, jwt
from datetime import datetime, timedelta
secret, sub, user_id = sys.argv[1], sys.argv[2], sys.argv[3]
payload = {
    "sub": sub,
    "user_id": user_id,
    "exp": datetime.utcnow() + timedelta(minutes=10)
}
print(jwt.encode(payload, secret, algorithm="HS256"))
PY
)"

if [[ -z "$TEST_TOKEN" ]]; then
  echo "❌ 生成 JWT 失败"
  exit 1
fi
echo "✅ 测试 JWT 生成成功"
echo

echo "3) 模拟 quantFinance 访问远端 /api/workers"
WORKERS_BODY="$(mktemp)"
WORKERS_CODE="$(curl -sS -m 15 \
  -H "Authorization: Bearer $TEST_TOKEN" \
  -o "$WORKERS_BODY" \
  -w '%{http_code}' \
  "$REMOTE_WORKER_API" || true)"

if [[ "$WORKERS_CODE" != "200" ]]; then
  echo "❌ /api/workers 调用失败，HTTP=$WORKERS_CODE"
  echo "返回内容:"
  cat "$WORKERS_BODY" || true
  rm -f "$WORKERS_BODY"
  exit 1
fi
echo "✅ /api/workers 调用成功"
echo

echo "4) 校验返回 JSON 结构和 WebSocket URL 形态"
python3 - "$WORKERS_BODY" "${USE_NGINX_WEBSOCKET,,}" "$PUBLIC_HOST" "${USE_HTTPS,,}" <<'PY'
import json, sys
body_file, use_nginx, public_host, use_https = sys.argv[1:5]

with open(body_file, "r", encoding="utf-8") as f:
    txt = f.read().strip()

try:
    data = json.loads(txt) if txt else {}
except Exception as e:
    print(f"❌ 返回不是合法 JSON: {e}")
    sys.exit(1)

if "workers" not in data or not isinstance(data["workers"], dict):
    print("❌ 返回缺少 workers 字段或类型不对")
    sys.exit(1)

print(f"✅ workers 字段存在，数量: {len(data['workers'])}")

# 抽查 log_stream_url，并输出更清晰的诊断信息
workers = data["workers"]
alive_count = 0
url_count = 0
sample_url = None
sample_worker_key = None
sample_worker = None

for key, w in workers.items():
    if not isinstance(w, dict):
        continue
    if w.get("alive") is True:
        alive_count += 1
    url = w.get("log_stream_url")
    if url:
        url_count += 1
        if not sample_url:
            sample_url = url
            sample_worker_key = key
            sample_worker = w

print(f"ℹ️ worker 统计: total={len(workers)}, alive={alive_count}, with_log_stream_url={url_count}")

if sample_url:
    print(f"ℹ️ 抽查 worker: {sample_worker_key}")
    print(f"ℹ️ 抽查 log_stream_url: {sample_url}")
    if use_nginx == "true" and public_host:
        expected_scheme = "wss://" if use_https == "true" else "ws://"
        expected_prefix = f"{expected_scheme}{public_host}/ws/"
        if not sample_url.startswith(expected_prefix):
            print(f"❌ URL 形态不符合预期，应以 {expected_prefix} 开头")
            sys.exit(1)
        print("✅ WebSocket URL 形态与 USE_NGINX_WEBSOCKET/PUBLIC_HOST/USE_HTTPS 一致")
else:
    if alive_count > 0:
        print("⚠️ 检测到有活跃 worker，但响应中没有 log_stream_url。")
        print("   这通常不是连通性问题，而是后端日志流未暴露（例如 LogStreamServer 未启动、")
        print("   worker 实例未绑定 _log_server，或服务端返回结构被过滤）。")
    else:
        print("ℹ️ 当前没有活跃 worker，且没有可抽查的 log_stream_url。")
    print("ℹ️ 本次仅完成 API 连通性验证，未完成 WebSocket URL 可用性验证。")
PY

rm -f "$WORKERS_BODY"

echo
echo "🎉 验证通过：quantFinance 机器可按当前参数访问远端 quant-strategy-manager"
echo "   这通常意味着 REMOTE_WORKER_API / JWT_SECRET_KEY / WS 相关参数配置是可工作的。"