#!/usr/bin/env bash
# PaisaGuard One-Click Reproducible Demo Launcher
# Proves system integrity, sets up database, runs benchmarks, and boots services with health checks.

set -eo pipefail

# Text color formatting helpers
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=====================================================================${NC}"
echo -e "${BLUE}          PAISAGUARD REPRODUCIBLE OPERATIONS DEMO LAUNCHER           ${NC}"
echo -e "${BLUE}=====================================================================${NC}"

# Check Python version
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}Error: Python3 is not installed on your system.${NC}"
    exit 1
fi

# Detect whether we are running in a container
IN_CONTAINER=0
if [ -f /.dockerenv ] || [ "${PAISAGUARD_IN_CONTAINER}" = "1" ]; then
    IN_CONTAINER=1
fi

if [ "$IN_CONTAINER" -eq 1 ]; then
    echo -e "\n[1/5] Running inside container: using container environment..."
else
    echo -e "\n[1/5] Setting up virtual environment..."
    if [ ! -d ".venv" ]; then
        python3 -m venv .venv
    fi
    source .venv/bin/activate

    echo -e "\n[2/5] Verifying dependencies from PyPI..."
    pip install -q -r requirements.txt
fi

# -------------------------------------------------------------
# Load local .env when it exists (shell environment takes precedence)
# -------------------------------------------------------------
if [ -f .env ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        trimmed=$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        [[ "$trimmed" =~ ^#.*$ ]] && continue
        [ -z "$trimmed" ] && continue
        if [[ "$trimmed" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            var_name="${BASH_REMATCH[1]}"
            var_val="${BASH_REMATCH[2]}"
            var_val="${var_val%\"}"
            var_val="${var_val#\"}"
            var_val="${var_val%\'}"
            var_val="${var_val#\'}"
            if [ -z "${!var_name+x}" ]; then
                export "$var_name"="$var_val"
            fi
        fi
    done < .env
fi

# Fallback defaults for demo
export PAISAGUARD_API_TOKEN="${PAISAGUARD_API_TOKEN:-test_finops_token_2026}"
export RAZORPAY_WEBHOOK_SECRET="${RAZORPAY_WEBHOOK_SECRET:-rzp_sec_buildathon_2026_demo}"
export AI_PROVIDER="${AI_PROVIDER:-mock}"

# Fail-closed validation if live provider chosen without API key
if [ "$AI_PROVIDER" = "groq" ] && [ -z "${GROQ_API_KEY:-}" ]; then
    echo -e "${RED}Error: AI_PROVIDER is set to 'groq' but GROQ_API_KEY is not configured.${NC}"
    echo -e "${RED}Please set GROQ_API_KEY in your .env file or export it in your shell environment.${NC}"
    exit 1
fi

if [ "$AI_PROVIDER" = "gemini" ] && [ -z "${AI_API_KEY:-}" ]; then
    echo -e "${RED}Error: AI_PROVIDER is set to 'gemini' but AI_API_KEY is not configured.${NC}"
    echo -e "${RED}Please set AI_API_KEY in your .env file or export it in your shell environment.${NC}"
    exit 1
fi

echo -e "\n[3/5] Generating synthetic 3-source dataset & seeding SQLite (WAL Mode)..."
python3 seed_data.py --reset

echo -e "\n[4/5] Executing automated verification test suite & benchmarks..."
# CI and unit tests run against mock provider for deterministic reproducible assertions
AI_PROVIDER=mock pytest -q
AI_PROVIDER=mock python3 eval_benchmarks.py

echo -e "\n=== PaisaGuard Runtime Configuration ==="
echo -e "-> API Token          : [CONFIGURED]"
echo -e "-> Webhook Secret     : [CONFIGURED]"
echo -e "-> AI Provider        : ${AI_PROVIDER}"
if [ "$AI_PROVIDER" = "groq" ]; then
    echo -e "-> Groq API Key       : [CONFIGURED]"
    groq_model="${AI_MODEL:-llama-3.3-70b-versatile}"
    if [[ "$groq_model" == gemini* ]]; then groq_model="llama-3.3-70b-versatile"; fi
    echo -e "-> Groq Model         : ${groq_model}"
elif [ -n "${GROQ_API_KEY:-}" ]; then
    echo -e "-> Groq API Key       : [CONFIGURED]"
fi
if [ "$AI_PROVIDER" = "gemini" ]; then
    echo -e "-> Gemini API Key     : [CONFIGURED]"
    gemini_model="${AI_MODEL:-gemini-3.5-flash}"
    if [[ "$gemini_model" == llama* ]]; then gemini_model="gemini-3.5-flash"; fi
    echo -e "-> Gemini Model       : ${gemini_model}"
elif [ -n "${AI_API_KEY:-}" ]; then
    echo -e "-> Gemini API Key     : [CONFIGURED]"
fi
if [ "$AI_PROVIDER" = "mock" ]; then
    echo -e "-> Operational Mode   : Deterministic Mock Baseline (offline CI)"
fi
echo -e "========================================"

echo -e "\n[5/5] Booting background payment gateway services..."

# Safe Port Check Helper (never kills arbitrary processes)
check_port_safe() {
    local port=$1
    local name=$2
    if command -v lsof &>/dev/null; then
        local pid
        pid=$(lsof -Pi :"$port" -sTCP:LISTEN -t 2>/dev/null || true)
        if [ -n "$pid" ]; then
            local cmd
            cmd=$(ps -p "$pid" -o args= 2>/dev/null || true)
            if echo "$cmd" | grep -qE "uvicorn|streamlit|api:app"; then
                echo -e "${YELLOW}Notice: Terminating prior PaisaGuard $name process (PID $pid)...${NC}"
                kill "$pid" 2>/dev/null || true
                sleep 1
            else
                echo -e "${RED}Error: Port $port ($name) is currently bound by another application (PID $pid: $cmd).${NC}"
                echo -e "${RED}Please free port $port before launching the demo.${NC}"
                exit 1
            fi
        fi
    fi
}

check_port_safe 8001 "FastAPI Gateway"
check_port_safe 8501 "Streamlit Console"

# Determine bind host (0.0.0.0 ensures external container port mapping works)
BIND_HOST="${PAISAGUARD_HOST:-0.0.0.0}"

# Run FastAPI Webhook Gateway in the background
echo -e "-> Starting FastAPI Webhook Ingestion on port 8001 (${BIND_HOST})..."
uvicorn api:app --port 8001 --host "$BIND_HOST" > /dev/null 2>&1 &
API_PID=$!

# Cleanup trap
cleanup() {
    echo -e "\n${YELLOW}Stopping PaisaGuard background servers...${NC}"
    kill "$API_PID" 2>/dev/null || true
    if [ -n "${STREAMLIT_PID:-}" ]; then
        kill "$STREAMLIT_PID" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup INT TERM EXIT

# Wait for FastAPI to be ready
echo -e "-> Waiting for FastAPI gateway readiness..."
MAX_WAIT=20
for i in $(seq 1 $MAX_WAIT); do
    if curl -sf http://127.0.0.1:8001/ > /dev/null 2>&1; then
        echo -e "-> FastAPI gateway is UP and responding."
        break
    fi
    sleep 1
    if [ "$i" -eq $MAX_WAIT ]; then
        echo -e "${RED}Error: FastAPI gateway did not start within ${MAX_WAIT}s.${NC}"
        exit 1
    fi
done

# Replay simulated webhooks to prove real-time ingestion (Hex & Base64 with canonical integer paise)
echo -e "-> Replaying sample signed webhook payload (Hex HMAC)..."
PAYLOAD_HEX='{"event_id":"evt_demo_hex_001","event":"payment.captured","payment_id":"pay_demo_signed_hex","order_id":"ord_in_1001","amount_paise":150000,"currency":"INR","status":"captured"}'
# Compute HMAC in subshell; secret is never echoed or logged
SIG_HEX=$(python3 -c "import hmac,hashlib,os; s=os.environ['RAZORPAY_WEBHOOK_SECRET']; print(hmac.new(s.encode(),b'''${PAYLOAD_HEX}''',hashlib.sha256).hexdigest())")
RESP1=$(curl -sf -X POST http://127.0.0.1:8001/webhooks/razorpay \
     -H "Content-Type: application/json" \
     -H "X-Razorpay-Signature: $SIG_HEX" \
     -d "$PAYLOAD_HEX")
if echo "$RESP1" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('status') in ('ingested','duplicate_ignored')" 2>/dev/null; then
    echo -e "-> ${GREEN}✓ Webhook 1 (Hex HMAC) ingested successfully.${NC}"
else
    echo -e "${RED}Error: Webhook 1 ingestion failed. Response: $RESP1${NC}"
    exit 1
fi

echo -e "-> Replaying sample signed webhook payload (Base64 HMAC)..."
PAYLOAD_B64='{"event_id":"evt_demo_b64_001","event":"payment.captured","payment_id":"pay_demo_signed_b64","order_id":"ord_in_1002","amount_paise":245000,"fee_paise":4900,"tax_paise":882,"payment_method":"card"}'
SIG_B64=$(python3 -c "import hmac,hashlib,base64,os; s=os.environ['RAZORPAY_WEBHOOK_SECRET']; d=hmac.new(s.encode(),b'''${PAYLOAD_B64}''',hashlib.sha256).digest(); print(base64.b64encode(d).decode())")
RESP2=$(curl -sf -X POST http://127.0.0.1:8001/webhooks/razorpay \
     -H "Content-Type: application/json" \
     -H "X-Razorpay-Signature: $SIG_B64" \
     -d "$PAYLOAD_B64")
if echo "$RESP2" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('status') in ('ingested','duplicate_ignored')" 2>/dev/null; then
    echo -e "-> ${GREEN}✓ Webhook 2 (Base64 HMAC) ingested successfully.${NC}"
else
    echo -e "${RED}Error: Webhook 2 ingestion failed. Response: $RESP2${NC}"
    exit 1
fi

if [ "${1:-}" = "--exit-after-webhooks" ] || [ "${PAISAGUARD_DEMO_EXIT_AFTER_WEBHOOKS:-}" = "1" ]; then
    echo -e "\n-> Signed webhook demonstration passed; exiting demo cleanly."
    exit 0
fi


# Run Streamlit visual operator dashboard
echo -e "-> Launching Streamlit Operator Dashboard on port 8501 (${BIND_HOST})..."
streamlit run app.py --server.port 8501 --server.address "$BIND_HOST" --server.headless true > /dev/null 2>&1 &
STREAMLIT_PID=$!

# Wait for Streamlit to be ready
echo -e "-> Waiting for Streamlit dashboard readiness..."
for i in $(seq 1 $MAX_WAIT); do
    if curl -sf http://127.0.0.1:8501/_stcore/health > /dev/null 2>&1 || curl -sf http://127.0.0.1:8501/ > /dev/null 2>&1; then
        echo -e "-> Streamlit visual dashboard is UP and ready."
        break
    fi
    sleep 1
    if [ "$i" -eq $MAX_WAIT ]; then
        echo -e "${YELLOW}Warning: Streamlit readiness check timed out; continuing...${NC}"
    fi
done

echo -e "\n${GREEN}=====================================================================${NC}"
echo -e "${GREEN}SUCCESS: PaisaGuard Services Started & Verified!${NC}"
echo -e "FastAPI Webhook Gateway : http://localhost:8001/docs"
echo -e "Prometheus Metrics      : http://localhost:8001/metrics/prometheus"
echo -e "Streamlit Visual Console: http://localhost:8501"
echo -e "Press [CTRL+C] to gracefully stop both servers."
echo -e "${GREEN}=====================================================================${NC}"

# Wait indefinitely for signal
wait
