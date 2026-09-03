#!/usr/bin/env bash
"""":
exec python3 "$0" "$@"
"""
"""
PaisaGuard Webhook Replayer
Simulates live Razorpay webhook traffic against the PaisaGuard Ingestion Gateway.
Demonstrates:
  1. Hex-encoded HMAC SHA256 signature verification.
  2. Base64-encoded HMAC SHA256 signature verification.
  3. Defensive parsing of missing/None fee and tax fields.
  4. Instant rejection of tampered/unauthorized payloads (HTTP 401).
  5. Atomic SQLite relational upserts.
"""

import os
import sys
import json
import hmac
import hashlib
import base64
import argparse
import urllib.request
import urllib.error
from datetime import datetime

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8001/webhooks/razorpay"
WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "rzp_sec_buildathon_2026_demo")

SAMPLE_EVENTS = [
    {
        "name": "Standard UPI Clean Match (Hex Signature)",
        "encoding": "hex",
        "tamper": False,
        "payload": {
            "event": "payment.captured",
            "payment_id": "pay_live_upi_101",
            "order_id": "ord_in_1001",
            "amount": 1500.00,
            "fee": 30.00,
            "tax": 5.40,
            "payment_method": "upi"
        }
    },
    {
        "name": "Debit Card Settlement (Base64 Signature)",
        "encoding": "base64",
        "tamper": False,
        "payload": {
            "event": "payment.captured",
            "payment_id": "pay_live_card_102",
            "order_id": "ord_in_1002",
            "amount": 2450.00,
            "fee": 49.00,
            "tax": 8.82,
            "payment_method": "card"
        }
    },
    {
        "name": "Defensive Ingestion: Missing Fee & Tax (None)",
        "encoding": "hex",
        "tamper": False,
        "payload": {
            "event": "payment.captured",
            "payment_id": "pay_live_defensive_103",
            "order_id": "ord_in_1003",
            "amount": 899.00,
            "fee": None,
            "tax": None,
            "payment_method": "netbanking"
        }
    },
    {
        "name": "Razorpay Native Nested Structure (Base64 Signature)",
        "encoding": "base64",
        "tamper": False,
        "payload": {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_live_nested_104",
                        "order_id": "ord_in_1004",
                        "amount": 350000,
                        "fee": 7000,
                        "tax": 1260,
                        "method": "card"
                    }
                }
            }
        }
    },
    {
        "name": "Tampered Payload / Man-in-the-Middle Attack (Expect 401 Rejection)",
        "encoding": "hex",
        "tamper": True,
        "payload": {
            "event": "payment.captured",
            "payment_id": "pay_exploit_999",
            "order_id": "ord_in_1005",
            "amount": 99999.00,
            "fee": 1.00,
            "tax": 0.18,
            "payment_method": "upi"
        }
    }
]

def replay_event(event_info: dict, target_url: str):
    payload_bytes = json.dumps(event_info["payload"]).encode("utf-8")
    
    # Generate signature
    hmac_obj = hmac.new(WEBHOOK_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256)
    if event_info["encoding"] == "base64":
        sig = base64.b64encode(hmac_obj.digest()).decode("utf-8")
    else:
        sig = hmac_obj.hexdigest()

    if event_info.get("tamper", False):
        sig = "tampered_fake_signature_hash_0000000000"

    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": sig
    }

    req = urllib.request.Request(target_url, data=payload_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            status_code = response.getcode()
            body = json.loads(response.read().decode("utf-8"))
            return status_code, body
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8")) if e.fp else {"detail": str(e)}
        return e.code, body
    except Exception as e:
        return 0, {"error": str(e)}

def main():
    parser = argparse.ArgumentParser(description="PaisaGuard Synthetic Webhook Replayer")
    parser.add_argument("--url", default=DEFAULT_GATEWAY_URL, help="Target FastAPI webhook endpoint")
    args = parser.parse_args()

    print("=" * 75)
    print("       PAISAGUARD LIVE WEBHOOK REPLAY & SECURITY TEST HARNESS")
    print("=" * 75)
    print(f"Target Gateway : {args.url}")
    print(f"Webhook Secret : {WEBHOOK_SECRET[:12]}***")
    print("-" * 75)

    for i, event in enumerate(SAMPLE_EVENTS, start=1):
        print(f"\n[{i}/{len(SAMPLE_EVENTS)}] Simulating: {event['name']}...")
        status_code, body = replay_event(event, args.url)
        
        if status_code == 200:
            print(f"  Result : \033[92mHTTP {status_code} OK\033[0m")
            print(f"  Details: Payment ID: {body.get('payment_id')} | HMAC Verified: {body.get('hmac_verified')}")
        elif status_code == 401 and event.get("tamper"):
            print(f"  Result : \033[92mHTTP {status_code} UNAUTHORIZED (Security Verified - Attack Blocked!)\033[0m")
            print(f"  Details: {body.get('detail')}")
        else:
            print(f"  Result : \033[91mHTTP {status_code}\033[0m")
            print(f"  Details: {body}")

    print("\n" + "=" * 75)
    print("Replay simulation completed successfully.")
    print("=" * 75)

if __name__ == "__main__":
    main()
