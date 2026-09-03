#!/usr/bin/env bash
# PaisaGuard One-Click Reproducible Demo Launcher
# Proves system integrity, sets up database, and boots services locally.

set -eo pipefail

# Text color formatting helpers
GREEN='\033[0;32m'
BLUE='\033[0;34m'
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

echo -e "\n[1/5] Setting up virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate

echo -e "\n[2/5] Installing package dependencies from PyPI..."
pip install --upgrade pip
pip install -r requirements.txt

echo -e "\n[3/5] Generating synthetic datasets & seeding SQLite DB (WAL Mode)..."
python seed_data.py

echo -e "\n[4/5] Executing automated verification tests..."
python -m unittest test_reconciliation.py

echo -e "\n[5/5] Booting background payment gateway services..."

# Ensure ports 8001 and 8501 are not currently blocked
if command -v lsof &>/dev/null; then
    if lsof -Pi :8001 -sTCP:LISTEN -t &>/dev/null; then
        echo -e "${RED}Warning: Port 8001 is already in use. Cleaning up duplicate processes...${NC}"
        kill -9 $(lsof -t -i:8001) || true
    fi
    if lsof -Pi :8501 -sTCP:LISTEN -t &>/dev/null; then
        echo -e "${RED}Warning: Port 8501 is already in use. Cleaning up duplicate processes...${NC}"
        kill -9 $(lsof -t -i:8501) || true
    fi
fi

# Run FastAPI Webhook Gateway in the background
echo -e "-> Starting FastAPI Webhook Ingestion on port 8001..."
uvicorn api:app --port 8001 --host 127.0.0.1 > /dev/null 2>&1 &
API_PID=$!

# Wait for FastAPI to be ready (up to 15 seconds) instead of a blind sleep
echo -e "-> Waiting for FastAPI gateway to be ready..."
MAX_WAIT=15
for i in $(seq 1 $MAX_WAIT); do
    if curl -sf http://127.0.0.1:8001/ > /dev/null 2>&1; then
        echo -e "-> FastAPI gateway is UP (${i}s)."
        break
    fi
    sleep 1
    if [ $i -eq $MAX_WAIT ]; then
        echo -e "${RED}Error: FastAPI gateway did not start in ${MAX_WAIT}s. Check logs.${NC}"
        kill $API_PID || true
        exit 1
    fi
done

# Replay simulated webhooks to prove real-time ingestion
echo -e "-> Replaying synthetic webhook payload list to FastAPI endpoint..."
PAYLOAD='{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_capt_demo","amount":150000,"currency":"INR","order_id":"ord_1001","status":"captured"}}}}'
SIG=$(python3 -c "import hmac, hashlib; print(hmac.new(b'rzp_sec_buildathon_2026_demo', b'$PAYLOAD', hashlib.sha256).hexdigest())")

curl -s -X POST http://127.0.0.1:8001/webhooks/razorpay \
     -H "Content-Type: application/json" \
     -H "X-Razorpay-Signature: $SIG" \
     -d "$PAYLOAD" || true

# Run Streamlit visual operator dashboard
echo -e "-> Launching Streamlit Operator Dashboard on port 8501..."
streamlit run app.py --server.port 8501 --server.address 127.0.0.1 &
STREAMLIT_PID=$!

echo -e "\n${GREEN}=====================================================================${NC}"
echo -e "${GREEN}SUCCESS: PaisaGuard Services Started Successfully!${NC}"
echo -e "FastAPI Webhook Gateway:  http://127.0.0.1:8001/docs"
echo -e "Streamlit Visual Console: http://127.0.0.1:8501"
echo -e "Press [CTRL+C] to gracefully stop both servers."
echo -e "${GREEN}=====================================================================${NC}"

# Wait for interrupt to clean up background processes
trap "echo -e '\nStopping background servers...'; kill $API_PID $STREAMLIT_PID || true; exit 0" INT
wait
