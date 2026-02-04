#!/bin/bash

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 显示帮助
if [[ "$1" == "-h" ]] || [[ "$1" == "--help" ]]; then
    echo "Usage: $0 [REMOTE_HOST] [REMOTE_PORT] [LOCAL_PORT]"
    echo ""
    echo "Test connection to remote Strategy Manager API and WebSocket"
    echo ""
    echo "Arguments:"
    echo "  REMOTE_HOST    Remote server IP/hostname (default: 192.168.1.100)"
    echo "  REMOTE_PORT    Remote API port (default: 5000)"
    echo "  LOCAL_PORT     quantFinance port (default: 3001)"
    echo ""
    echo "Examples:"
    echo "  $0                          # Use all defaults"
    echo "  $0 192.168.1.200            # Custom remote host"
    echo "  $0 192.168.1.200 8080       # Custom host and port"
    echo "  $0 192.168.1.200 8080 8001  # All custom"
    exit 0
fi

# 默认值
DEFAULT_REMOTE_HOST="192.168.1.100"
DEFAULT_REMOTE_PORT="5000"
DEFAULT_LOCAL_PORT="3001"

# 从命令行参数获取，如果没有则使用默认值
REMOTE_HOST="${1:-$DEFAULT_REMOTE_HOST}"
REMOTE_PORT="${2:-$DEFAULT_REMOTE_PORT}"
LOCAL_PORT="${3:-$DEFAULT_LOCAL_PORT}"

echo "========================================="
echo "Remote Strategy Manager Diagnosis"
echo "========================================="
echo "Remote Host: $REMOTE_HOST"
echo "Remote Port: $REMOTE_PORT"
echo "Local Port:  $LOCAL_PORT"
echo "========================================="

# 1. 测试网络连通性
echo -e "\n${YELLOW}[1/8]${NC} Testing network connectivity..."
if ping -c 3 $REMOTE_HOST > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Network reachable"
else
    echo -e "${RED}✗${NC} Cannot reach $REMOTE_HOST"
    exit 1
fi

# 2. 测试 API 端口可访问性
echo -e "\n${YELLOW}[2/8]${NC} Testing API port accessibility..."
# 使用 curl 测试 /health 端点，比 nc 更可靠
if curl -s --connect-timeout 5 http://$REMOTE_HOST:$REMOTE_PORT/health > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Port $REMOTE_PORT is accessible"
else
    echo -e "${RED}✗${NC} Port $REMOTE_PORT is not accessible"
    echo "  Trying with nc..."
    if nc -zv -w 5 $REMOTE_HOST $REMOTE_PORT 2>&1 | grep -qE 'succeeded|open'; then
        echo -e "${GREEN}✓${NC} Port is open (nc test passed)"
    else
        echo -e "${RED}✗${NC} Cannot connect to $REMOTE_HOST:$REMOTE_PORT"
        exit 1
    fi
fi

# 3. 测试远程 API 端点
echo -e "\n${YELLOW}[3/8]${NC} Testing remote API endpoint..."
REMOTE_RESPONSE=$(curl -s -w "\n%{http_code}" http://$REMOTE_HOST:$REMOTE_PORT/api/workers)
REMOTE_HTTP_CODE=$(echo "$REMOTE_RESPONSE" | tail -n1)
REMOTE_BODY=$(echo "$REMOTE_RESPONSE" | head -n-1)

if [ "$REMOTE_HTTP_CODE" == "200" ]; then
    echo -e "${GREEN}✓${NC} Remote API accessible (HTTP $REMOTE_HTTP_CODE)"
    WORKER_COUNT=$(echo "$REMOTE_BODY" | jq -r '.workers | length' 2>/dev/null || echo "0")
    echo "  Workers found: $WORKER_COUNT"
else
    echo -e "${RED}✗${NC} Remote API failed (HTTP $REMOTE_HTTP_CODE)"
    echo "$REMOTE_BODY"
    exit 1
fi

# 4. 检查 quantFinance 服务
echo -e "\n${YELLOW}[4/8]${NC} Checking quantFinance service..."
if lsof -Pi :$LOCAL_PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} quantFinance is running on port $LOCAL_PORT"
else
    echo -e "${RED}✗${NC} quantFinance is NOT running on port $LOCAL_PORT"
    echo "  Start it with: uvicorn main:app --host 0.0.0.0 --port $LOCAL_PORT"
    exit 1
fi

# 5. 测试 quantFinance 代理端点
echo -e "\n${YELLOW}[5/8]${NC} Testing quantFinance proxy endpoint..."
LOCAL_RESPONSE=$(curl -s -w "\n%{http_code}" http://localhost:$LOCAL_PORT/api/strategy/workers)
LOCAL_HTTP_CODE=$(echo "$LOCAL_RESPONSE" | tail -n1)
LOCAL_BODY=$(echo "$LOCAL_RESPONSE" | head -n-1)

if [ "$LOCAL_HTTP_CODE" == "200" ]; then
    echo -e "${GREEN}✓${NC} quantFinance proxy working (HTTP $LOCAL_HTTP_CODE)"
else
    echo -e "${RED}✗${NC} quantFinance proxy failed (HTTP $LOCAL_HTTP_CODE)"
    echo "$LOCAL_BODY"
    echo -e "\n${YELLOW}Troubleshooting:${NC}"
    echo "  1. Check .env file has: REMOTE_WORKER_API=http://$REMOTE_HOST:$REMOTE_PORT/api/workers"
    echo "  2. Restart quantFinance to reload .env"
    exit 1
fi

# 6. 检查 WebSocket URLs
echo -e "\n${YELLOW}[6/8]${NC} Checking WebSocket URLs..."
WS_URLS=$(echo "$REMOTE_BODY" | jq -r '.workers[] | select(.log_stream_url) | .log_stream_url' 2>/dev/null)

if [ -z "$WS_URLS" ]; then
    echo -e "${YELLOW}⚠${NC} No WebSocket URLs found (workers may not have log streaming enabled)"
else
    echo "Found WebSocket URLs:"
    echo "$WS_URLS" | while read url; do
        echo "  - $url"
    done
    
    # 检查 URL 格式
    if echo "$WS_URLS" | grep -q "0.0.0.0\|localhost\|127.0.0.1"; then
        echo -e "\n${RED}✗${NC} WARNING: WebSocket URL contains localhost/0.0.0.0!"
        echo "  Remote clients cannot connect to these URLs."
        echo -e "\n${YELLOW}Fix:${NC}"
        echo "  1. Set in quant-strategy-manager/config/.env:"
        echo "     PUBLIC_HOST=$REMOTE_HOST"
        echo "  2. Restart api_server.py"
    else
        echo -e "${GREEN}✓${NC} WebSocket URLs are externally accessible"
    fi
fi

# 7. 测试 WebSocket 端口连通性
echo -e "\n${YELLOW}[7/8]${NC} Testing WebSocket port connectivity..."
if [ -n "$WS_URLS" ]; then
    for url in $WS_URLS; do
        # 提取端口号和主机
        WS_PORT=$(echo $url | grep -oP ':\K[0-9]+' | tail -1)
        WS_HOST=$(echo $url | grep -oP '://\K[^:]+')
        
        if [ -n "$WS_PORT" ] && [ -n "$WS_HOST" ]; then
            echo -n "  Testing $WS_HOST:$WS_PORT... "
            # 尝试多种方式测试端口
            if timeout 3 bash -c "echo > /dev/tcp/$WS_HOST/$WS_PORT" 2>/dev/null; then
                echo -e "${GREEN}✓ Reachable${NC}"
            elif nc -zv -w 3 $WS_HOST $WS_PORT 2>&1 | grep -qE 'succeeded|open'; then
                echo -e "${GREEN}✓ Reachable (nc)${NC}"
            else
                echo -e "${YELLOW}⚠ Cannot verify (may still work)${NC}"
                echo "    Note: WebSocket port detection may fail due to firewall"
                echo "    Try connecting from browser to confirm"
            fi
        fi
    done
else
    echo "  Skipped (no WebSocket URLs found)"
fi

# 8. 对比远程和本地返回的 URLs
echo -e "\n${YELLOW}[8/8]${NC} Comparing remote vs local WebSocket URLs..."
LOCAL_WS_URLS=$(echo "$LOCAL_BODY" | jq -r '.workers[] | select(.log_stream_url) | .log_stream_url' 2>/dev/null)

if [ "$WS_URLS" == "$LOCAL_WS_URLS" ]; then
    echo -e "${GREEN}✓${NC} URLs match (proxy working correctly)"
else
    echo -e "${YELLOW}⚠${NC} URLs differ between remote and local"
    echo -e "\nRemote URLs:"
    echo "$WS_URLS" | sed 's/^/  /'
    echo -e "\nLocal (proxied) URLs:"
    echo "$LOCAL_WS_URLS" | sed 's/^/  /'
fi

echo -e "\n========================================="
echo -e "${GREEN}Diagnosis Complete${NC}"
echo "========================================="

# 总结
echo -e "\n${YELLOW}Summary:${NC}"
echo "  Remote API: http://$REMOTE_HOST:$REMOTE_PORT/api/workers"
echo "  Local Proxy: http://localhost:$LOCAL_PORT/api/strategy/workers"
echo "  Workers: $WORKER_COUNT"

if [ -n "$WS_URLS" ]; then
    echo -e "\n${YELLOW}Next Steps:${NC}"
    echo "  • Test real-time logs in browser/frontend"
    echo "  • Check browser console for WebSocket errors"
fi