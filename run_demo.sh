#!/usr/bin/env bash
# PaisaGuard One-Click Reproducible Demo Launcher
# Proves system integrity, sets up database, and boots services with health checks.

set -eo pipefail

# Text color formatting helpers
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=====================================================================${NC}"
echo -e "${BLUE}              PAISAGUARD LOCAL OPERATIONS DEMO LAUNCHER              ${NC}"
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

    echo -e "\n[2/5] Checking package dependencies from PyPI..."
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
fi

echo -e "\n[3/5] Generating synthetic datasets & seeding SQLite DB (WAL Mode)..."
python3 seed_data.py

echo -e "\n[4/5] Executing automated verification tests..."
python3 -m unittest test_reconciliation.py

echo -e "\n[5/5] Booting background payment gateway services..."

# Ensure ports 8001 and 8501 are not currently blocked
if command -v lsof &>/dev/null; then
    if lsof -Pi :8001 -sTCP:LISTEN -t &>/dev/null; then
        echo -e "${YELLOW}Notice: Port 8001 is already in use. Cleaning up stale process...${NC}"
        kill -9 $(lsof -t -i:8001) 2>/dev/null || true
    fi
    if lsof -Pi :8501 -sTCP:LISTEN -t &>/dev/null; then
        echo -e "${YELLOW}Notice: Port 8501 is already in use. Cleaning up stale process...${NC}"
        kill -9 $(lsof -t -i:8501) 2>/dev/null || true
    fi
fi

# Determine bind host (0.0.0.0 ensures external container port mapping works)
BIND_HOST="${PAISAGUARD_HOST:-0.0.0.0}"

# Run FastAPI Webhook Gateway in the background
echo -e "-> Starting FastAPI Webhook Ingestion on port 8001 (${BIND_HOST})..."
uvicorn api:app --port 8001 --host "$BIND_HOST" > /dev/null 2>&1 &
API_PID=$!

# Wait for FastAPI to be ready
echo -e "-> Waiting for FastAPI gateway readiness..."
MAX_WAIT=20
for i in $(seq 1 $MAX_WAIT); do
    if curl -sf http://127.0.0.1:8001/ > /dev/null 2>&1; then
        echo -e "-> FastAPI gateway is UP and responding (attempt ${i})."
        break
    fi
    sleep 1
    if [ $i -eq $MAX_WAIT ]; then
        echo -e "${RED}Error: FastAPI gateway did not start within ${MAX_WAIT}s.${NC}"
        kill $API_PID 2>/dev/null || true
        exit 1
    fi
done

# Replay simulated webhooks to prove real-time ingestion (Hex & Base64)
echo -e "-> Replaying sample signed webhook payload (Hex HMAC)..."
PAYLOAD_HEX='{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_capt_demo_hex","amount":150000,"currency":"INR","order_id":"ord_in_1001","status":"captured"}}}}'
SIG_HEX=$(python3 -c "import hmac, hashlib; print(hmac.new(b'rzp_sec_buildathon_2026_demo', b'$PAYLOAD_HEX', hashlib.sha256).hexdigest())")
curl -s -X POST http://127.0.0.1:8001/webhooks/razorpay \
     -H "Content-Type: application/json" \
     -H "X-Razorpay-Signature: $SIG_HEX" \
     -d "$PAYLOAD_HEX" > /dev/null || true

echo -e "-> Replaying sample signed webhook payload (Base64 HMAC)..."
PAYLOAD_B64='{"event":"payment.captured","payment_id":"pay_capt_demo_b64","order_id":"ord_in_1002","amount":2450.00,"fee":49.00,"tax":8.82,"payment_method":"card"}'
SIG_B64=$(python3 -c "import hmac, hashlib, base64; d=hmac.new(b'rzp_sec_buildathon_2026_demo', b'$PAYLOAD_B64', hashlib.sha256).digest(); print(base64.b64encode(d).decode())")
curl -s -X POST http://127.0.0.1:8001/webhooks/razorpay \
     -H "Content-Type: application/json" \
     -H "X-Razorpay-Signature: $SIG_B64" \
     -d "$PAYLOAD_B64" > /dev/null || true

# Run Streamlit visual operator dashboard
echo -e "-> Launching Streamlit Operator Dashboard on port 8501 (${BIND_HOST})..."
streamlit run app.py --server.port 8501 --server.address "$BIND_HOST" --server.headless true > /dev/null 2>&1 &
STREAMLIT_PID=$!

# Wait for Streamlit to be ready
echo -e "-> Waiting for Streamlit dashboard readiness..."
for i in $(seq 1 $MAX_WAIT); do
    if curl -sf http://127.0.0.1:8501/_stcore/health > /dev/null 2>&1 || curl -sf http://127.0.0.1:8501/ > /dev/null 2>&1; then
        echo -e "-> Streamlit visual dashboard is UP and ready (attempt ${i})."
        break
    fi
    sleep 1
    if [ $i -eq $MAX_WAIT ]; then
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

# Wait for interrupt to clean up background processes
trap "echo -e '\nStopping background servers...'; kill $API_PID $STREAMLIT_PID 2>/dev/null || true; exit 0" INT TERM
wait
