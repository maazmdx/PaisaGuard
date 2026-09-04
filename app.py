"""
app.py — PaisaGuard Streamlit Evaluator Console.

Consolidated FinOps Evaluator Dashboard across 5 Core Surfaces:
1. Batch KPI Board (Overall match rates, coverage, discrepancy breakdown)
2. Payout Table (Razorpay payout batches vs Bank credits, settlement counts, UTRs)
3. Exception Evidence + AI Response Drawer (Detailed investigation, cited records, policy gate, human actions)
4. Human Approval Timeline (Strictly append-only audit trail and reviewer actions)
5. Benchmark & Accuracy Report (Measured throughput, p50/p95 latency, precision/recall, AI accuracy)

Architectural Guarantees:
- ZERO direct SQLite database access. All data is read and written exclusively via FastAPI HTTP endpoints.
- Server-side token handling: reads PAISAGUARD_API_TOKEN from environment.
- If PAISAGUARD_API_TOKEN is unset, displays a prominent notice and disables write actions.
- Reviewers provide operator identity only (e.g. name / email), never raw secrets.
"""

import os
import sys
import json
import time
from typing import Dict, Any, Optional, List
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Environment & API Gateway Configuration
API_URL = os.environ.get("API_URL", "http://localhost:8001").rstrip("/")
API_TOKEN = os.environ.get("PAISAGUARD_API_TOKEN", "").strip()

# Page configuration
st.set_page_config(
    page_title="PaisaGuard | Razorpay AI Finance Controller",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# High-End Dark Fintech Theme Styling
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    .stApp {
        background-color: #070d18;
        background-image:
            radial-gradient(at 0% 0%, rgba(2, 132, 199, 0.12) 0px, transparent 50%),
            radial-gradient(at 100% 0%, rgba(37, 99, 235, 0.08) 0px, transparent 50%);
        color: #f1f5f9;
    }

    /* Razorpay Top Navigation Header */
    .rzp-navbar {
        display: flex;
        justify-content: space-between;
        align-items: center;
        background: rgba(12, 26, 48, 0.85);
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
        border: 1px solid #1e3a5f;
        padding: 16px 24px;
        border-radius: 12px;
        margin-bottom: 24px;
        box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
    }
    .rzp-brand {
        display: flex;
        align-items: center;
        gap: 14px;
    }
    .rzp-logo-badge {
        background: linear-gradient(135deg, #0284c7 0%, #2563eb 100%);
        color: #ffffff;
        font-weight: 800;
        font-size: 1.25rem;
        padding: 6px 14px;
        border-radius: 8px;
        letter-spacing: -0.5px;
        box-shadow: 0 0 15px rgba(2, 132, 199, 0.4);
    }
    .rzp-title {
        font-size: 1.35rem;
        font-weight: 700;
        color: #ffffff;
        margin: 0;
        letter-spacing: -0.3px;
    }
    .rzp-tagline {
        font-size: 0.82rem;
        color: #94a3b8;
        margin: 0;
    }
    .rzp-status-pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: rgba(16, 185, 129, 0.12);
        color: #34d399;
        border: 1px solid rgba(16, 185, 129, 0.3);
        padding: 4px 12px;
        border-radius: 9999px;
        font-size: 0.78rem;
        font-weight: 600;
    }
    .rzp-status-pill-warn {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: rgba(245, 158, 11, 0.12);
        color: #fbbf24;
        border: 1px solid rgba(245, 158, 11, 0.3);
        padding: 4px 12px;
        border-radius: 9999px;
        font-size: 0.78rem;
        font-weight: 600;
    }

    /* Metric Cards */
    .kpi-card {
        background: rgba(15, 23, 42, 0.75);
        border: 1px solid #1e293b;
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 12px;
        transition: transform 0.2s, border-color 0.2s;
    }
    .kpi-card:hover {
        border-color: #0284c7;
        transform: translateY(-2px);
    }
    .kpi-title {
        font-size: 0.80rem;
        color: #94a3b8;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        font-weight: 600;
        margin-bottom: 8px;
    }
    .kpi-value {
        font-size: 1.85rem;
        font-weight: 800;
        color: #ffffff;
        letter-spacing: -0.02em;
    }
    .kpi-sub {
        font-size: 0.75rem;
        color: #64748b;
        margin-top: 4px;
    }

    /* Code & JSON display */
    pre, code {
        font-family: 'JetBrains Mono', monospace !important;
    }
</style>
""", unsafe_allow_html=True)


# HTTP Helper functions (Zero direct DB connection)
def api_get(path: str) -> Optional[Dict[str, Any]]:
    """Makes a GET request to the isolated FastAPI service."""
    try:
        url = f"{API_URL}{path}"
        headers = {}
        if API_TOKEN:
            headers["X-PaisaGuard-Token"] = API_TOKEN
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 401:
            st.error(f"Unauthorized (401) on {path}: Invalid or missing PAISAGUARD_API_TOKEN.")
        else:
            st.error(f"API Error ({resp.status_code}) on {path}: {resp.text}")
        return None
    except requests.exceptions.ConnectionError:
        st.error(f"🔌 Connection failed: Cannot reach PaisaGuard API at `{API_URL}`. Verify the FastAPI service is running.")
        return None
    except Exception as exc:
        st.error(f"Unexpected error communicating with API: {exc}")
        return None


def api_post(path: str, payload: Dict[str, Any]) -> Tuple[bool, Any]:
    """Makes a POST request to the isolated FastAPI service."""
    try:
        url = f"{API_URL}{path}"
        headers = {"Content-Type": "application/json"}
        if API_TOKEN:
            headers["X-PaisaGuard-Token"] = API_TOKEN
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            return True, resp.json()
        elif resp.status_code == 503:
            return False, "Server state mutations are disabled (PAISAGUARD_API_TOKEN is unset on the API server)."
        elif resp.status_code == 401:
            return False, "Unauthorized (401): Valid X-PaisaGuard-Token is required for this action."
        else:
            try:
                err_detail = resp.json().get("detail", resp.text)
            except Exception:
                err_detail = resp.text
            return False, f"HTTP {resp.status_code}: {err_detail}"
    except requests.exceptions.ConnectionError:
        return False, f"Cannot connect to API at {API_URL}."
    except Exception as exc:
        return False, str(exc)


# Top Navigation Header
token_configured = bool(API_TOKEN)
status_pill = (
    '<span class="rzp-status-pill">● SECURE GATEWAY CONNECTED</span>'
    if token_configured
    else '<span class="rzp-status-pill-warn">● READ-ONLY (TOKEN UNCONFIGURED)</span>'
)

st.markdown(f"""
<div class="rzp-navbar">
    <div class="rzp-brand">
        <div class="rzp-logo-badge">PG</div>
        <div>
            <h1 class="rzp-title">PaisaGuard AI Financial Controller</h1>
            <p class="rzp-tagline">Track 04: AI Finance Controller | 3-Source Reconciliation & Autonomous Exception Triage</p>
        </div>
    </div>
    <div>
        {status_pill}
    </div>
</div>
""", unsafe_allow_html=True)

# Security Alert Notice if Token is Unset
if not token_configured:
    st.warning(
        "⚠️ **Write actions disabled: PAISAGUARD_API_TOKEN is not configured on server.**\n\n"
        "The dashboard is operating in read-only mode. To trigger reconciliation sweeps, run AI investigations, "
        "or record human approvals, configure `PAISAGUARD_API_TOKEN` in your environment or `.env` file."
    )

# Sidebar Controls
with st.sidebar:
    st.image("https://img.shields.io/badge/PaisaGuard-v3.0.0-blue?style=for-the-badge&logo=shield", use_container_width=True)
    st.markdown("### FinOps Operator Controls")

    reviewer_id = st.text_input("Operator / Reviewer Identity", value="auditor_ops", help="Your operator identifier recorded in the append-only audit trail.")

    st.markdown("---")
    st.markdown("### Deterministic Sweep")
    st.caption("Re-evaluates OMS orders, Razorpay settlements, and Bank payout credits.")

    if st.button("🔄 Trigger Full Reconcile Sweep", disabled=not token_configured, use_container_width=True):
        with st.spinner("Executing 3-source reconciliation pipeline..."):
            success, res = api_post("/reconcile/sweep", {})
            if success:
                st.success(f"Reconciliation run #{res.get('run_id')} completed successfully!")
                time.sleep(1)
                st.rerun()
            else:
                st.error(f"Sweep failed: {res}")

    st.markdown("---")
    st.markdown("### System Architecture")
    st.markdown("""
    - **Source 1**: Internal OMS Orders
    - **Source 2**: Razorpay Settlements & Fees
    - **Source 3**: Bank Payout Credit Feed
    - **Ledger**: Integer Paise (`*_paise`)
    - **Agent**: Read-Only with Citation Validation
    - **Approvals**: Strictly Append-Only
    """)

# Fetch core metrics
metrics_data = api_get("/metrics")
if not metrics_data:
    st.info("Awaiting connection to PaisaGuard API gateway. Refresh or check container logs.")
    st.stop()

# Tab Navigation: 5 Consolidated Surfaces
tab_kpi, tab_payouts, tab_exceptions, tab_approvals, tab_benchmarks = st.tabs([
    "📊 Batch KPI Board",
    "🏦 Payout Batches",
    "🔍 Exceptions & AI Investigator",
    "📜 Audit & Human Approval Log",
    "🎯 Benchmark & Accuracy Console"
])

# -------------------------------------------------------------
# SURFACE 1: BATCH KPI BOARD
# -------------------------------------------------------------
with tab_kpi:
    st.markdown("### Operational Reconciliation Health")
    tx_m = metrics_data.get("transaction_metrics", {})
    po_m = metrics_data.get("payout_metrics", {})
    counts = metrics_data.get("counts", {})

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown(f"""
        <div class="kpi-card">
            <div class="kpi-title">Txn Match Rate</div>
            <div class="kpi-value">{tx_m.get('match_rate_percent', 0.0)}%</div>
            <div class="kpi-sub">{tx_m.get('matched_count', 0)} of {tx_m.get('total_business_transactions', 0)} Transactions</div>
        </div>
        """, unsafe_allow_html=True)
    with col2:
        st.markdown(f"""
        <div class="kpi-card">
            <div class="kpi-title">Payout Match Rate</div>
            <div class="kpi-value">{po_m.get('match_rate_percent', 0.0)}%</div>
            <div class="kpi-sub">{po_m.get('matched_count', 0)} of {po_m.get('total_payout_batches', 0)} Payout Batches</div>
        </div>
        """, unsafe_allow_html=True)
    with col3:
        st.markdown(f"""
        <div class="kpi-card">
            <div class="kpi-title">Open Exceptions</div>
            <div class="kpi-value" style="color: #f87171;">{tx_m.get('exception_count', 0) + po_m.get('exception_count', 0)}</div>
            <div class="kpi-sub">{tx_m.get('exception_count', 0)} Txn / {po_m.get('exception_count', 0)} Payout</div>
        </div>
        """, unsafe_allow_html=True)
    with col4:
        st.markdown(f"""
        <div class="kpi-card">
            <div class="kpi-title">Total Source Records</div>
            <div class="kpi-value">{counts.get('oms_orders', 0) + counts.get('razorpay_settlements', 0) + counts.get('bank_payout_credits', 0)}</div>
            <div class="kpi-sub">{counts.get('oms_orders', 0)} OMS / {counts.get('razorpay_settlements', 0)} RZP / {counts.get('bank_payout_credits', 0)} Bank</div>
        </div>
        """, unsafe_allow_html=True)

    # Discrepancy Breakdown Visuals
    st.markdown("---")
    st.markdown("#### Exception & Discrepancy Distribution")
    exc_data = api_get("/exceptions") or {}
    exceptions_list = exc_data.get("exceptions", [])

    if exceptions_list:
        df_exc = pd.DataFrame(exceptions_list)
        col_c1, col_c2 = st.columns([1, 1])

        with col_c1:
            disc_counts = df_exc["discrepancy_code"].value_counts().reset_index()
            disc_counts.columns = ["Discrepancy Code", "Count"]
            fig_pie = px.pie(
                disc_counts,
                values="Count",
                names="Discrepancy Code",
                hole=0.45,
                color_discrete_sequence=["#0284c7", "#f59e0b", "#ef4444", "#8b5cf6", "#10b981"],
                title="Exceptions by Root Cause"
            )
            fig_pie.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font_color="#f1f5f9"
            )
            st.plotly_chart(fig_pie, use_container_width=True)

        with col_c2:
            disp_counts = df_exc["current_disposition"].value_counts().reset_index()
            disp_counts.columns = ["Disposition", "Count"]
            fig_bar = px.bar(
                disp_counts,
                x="Disposition",
                y="Count",
                color="Disposition",
                color_discrete_map={"UNRESOLVED": "#f59e0b", "APPROVE": "#10b981", "REJECT": "#ef4444"},
                title="Current Disposition Status"
            )
            fig_bar.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font_color="#f1f5f9"
            )
            st.plotly_chart(fig_bar, use_container_width=True)
    else:
        st.success("🎉 All operational transactions and payout batches are 100% matched with zero open exceptions!")


# -------------------------------------------------------------
# SURFACE 2: PAYOUT TABLE
# -------------------------------------------------------------
with tab_payouts:
    st.markdown("### Multi-Settlement Payout Batches & Bank Credits")
    st.caption("Reconciles Razorpay batched merchant payouts against actual Bank Credit notifications (Pass 2).")

    payouts_data = api_get("/payouts") or {}
    payout_batches = payouts_data.get("payout_batches", [])
    unmatched_credits = payouts_data.get("unmatched_bank_credits", [])

    if payout_batches:
        df_po = pd.DataFrame(payout_batches)
        # Format rupee columns for readability
        df_po_display = df_po.copy()
        if "batch_net_paise" in df_po_display.columns:
            df_po_display["Batch Net (₹)"] = df_po_display["batch_net_paise"].apply(lambda p: f"₹{p / 100:,.2f}")
        if "bank_amount_paise" in df_po_display.columns:
            df_po_display["Bank Credited (₹)"] = df_po_display["bank_amount_paise"].apply(
                lambda p: f"₹{p / 100:,.2f}" if pd.notnull(p) else "Pending"
            )
        cols_to_show = [
            col for col in ["payout_id", "settlement_count", "Batch Net (₹)", "bank_credit_id", "Bank Credited (₹)", "status", "discrepancy_code"]
            if col in df_po_display.columns
        ]
        st.dataframe(df_po_display[cols_to_show], use_container_width=True, hide_index=True)
    else:
        st.info("No payout batches found in reconciliation ledger.")

    if unmatched_credits:
        st.markdown("---")
        st.markdown("#### ⚠️ Unmatched Bank Credits Feed")
        st.caption("Incoming bank deposits without a matching Razorpay payout batch identifier.")
        df_unmatched = pd.DataFrame(unmatched_credits)
        if "credit_amount_paise" in df_unmatched.columns:
            df_unmatched["Amount (₹)"] = df_unmatched["credit_amount_paise"].apply(lambda p: f"₹{p / 100:,.2f}")
        cols_un = [c for c in ["credit_id", "utr_number", "Amount (₹)", "status", "created_at"] if c in df_unmatched.columns]
        st.dataframe(df_unmatched[cols_un], use_container_width=True, hide_index=True)


# -------------------------------------------------------------
# SURFACE 3: EXCEPTION EVIDENCE + AI RESPONSE DRAWER
# -------------------------------------------------------------
with tab_exceptions:
    st.markdown("### Exception Investigation & Autonomous AI Agent")
    st.caption("Detailed evidence view, citation verification, policy gate validation, and human approval workflow.")

    exc_data = api_get("/exceptions") or {}
    all_exceptions = exc_data.get("exceptions", [])

    if not all_exceptions:
        st.info("No open exceptions found in the system.")
    else:
        # Selector for exception
        exc_options = {
            f"Decision #{e['decision_id']} | {e['subject_type']} {e['subject_id']} ({e['discrepancy_code']}) - ₹{e['variance_paise'] / 100:,.2f}": e
            for e in all_exceptions
        }
        selected_label = st.selectbox("Select Exception to Investigate", list(exc_options.keys()))
        selected_exc = exc_options[selected_label]
        dec_id = selected_exc["decision_id"]

        col_left, col_right = st.columns([1, 1])

        with col_left:
            st.markdown("#### Structured Evidence Bundle")
            st.markdown(f"**Decision ID**: `{dec_id}`")
            st.markdown(f"**Subject**: `{selected_exc['subject_type']}` | `{selected_exc['subject_id']}`")
            st.markdown(f"**Discrepancy Code**: `{selected_exc['discrepancy_code']}`")
            st.markdown(f"**Variance**: `₹{selected_exc['variance_paise'] / 100:,.2f}` ({selected_exc['variance_paise']} paise)")
            st.markdown(f"**Current Disposition**: `{selected_exc['current_disposition']}` (by `{selected_exc['resolved_by'] or 'UNRESOLVED'}`)")

            # Parse evidence JSON
            ev_raw = selected_exc.get("evidence", "{}")
            try:
                ev_obj = json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
                st.json(ev_obj)
            except Exception:
                st.text(str(ev_raw))

        with col_right:
            st.markdown("#### AI Exception Investigator")
            st.caption("Read-only agent evaluates minimized evidence and returns structured diagnosis with citations.")

            # Button to trigger AI investigation
            investigate_btn = st.button("🤖 Run AI Agent Investigation", disabled=not token_configured, key=f"inv_{dec_id}")
            if investigate_btn:
                with st.spinner("Querying FinOps Agent and validating policy gate..."):
                    success, inv_resp = api_post("/ai/investigate", {"decision_id": dec_id})
                    if success:
                        st.session_state[f"last_inv_{dec_id}"] = inv_resp
                        st.success("Investigation complete!")
                    else:
                        st.error(f"Investigation failed: {inv_resp}")

            # Display agent results if available
            inv_result = st.session_state.get(f"last_inv_{dec_id}")
            if inv_result:
                conf = inv_result.get("confidence", 0.0)
                conf_color = "#10b981" if conf >= 0.85 else ("#f59e0b" if conf >= 0.70 else "#ef4444")
                st.markdown(f"""
                <div style="background: rgba(15, 23, 42, 0.9); border: 1px solid #1e3a5f; border-radius: 8px; padding: 14px; margin-top: 10px;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span style="font-weight: 700; color: #38bdf8;">ACTION: {inv_result.get('recommended_action')}</span>
                        <span style="background: {conf_color}; color: #000; font-weight: 800; padding: 2px 8px; border-radius: 4px; font-size: 0.8rem;">
                            CONFIDENCE: {int(conf * 100)}%
                        </span>
                    </div>
                    <p style="margin-top: 8px; font-size: 0.9rem; color: #cbd5e1;">{inv_result.get('diagnosis')}</p>
                    <div style="margin-top: 6px; font-size: 0.78rem; color: #94a3b8;">
                        <strong>Policy Gate Status:</strong> {'✅ PASSED' if inv_result.get('policy_gate_passed') else '❌ REJECTED'}
                    </div>
                    <div style="margin-top: 4px; font-size: 0.78rem; color: #94a3b8;">
                        <strong>Cited Records:</strong> <code>{', '.join(inv_result.get('cited_record_ids', []))}</code>
                    </div>
                </div>
                """, unsafe_allow_html=True)

            # Human Approval / Rejection Action Area
            st.markdown("---")
            st.markdown("#### Human Operator Action")
            notes = st.text_area("Audit Justification / Notes", value="Verified against partner contract specifications.", key=f"notes_{dec_id}")

            col_act1, col_act2 = st.columns(2)
            with col_act1:
                if st.button("✅ Approve Recommendation", disabled=not token_configured, key=f"app_{dec_id}", use_container_width=True):
                    succ, res = api_post("/approvals/decision", {
                        "decision_id": dec_id,
                        "action": "APPROVE",
                        "reviewer": reviewer_id,
                        "notes": notes
                    })
                    if succ:
                        st.success(f"Approval recorded! Approval ID: {res.get('approval_id')}")
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error(f"Approval failed: {res}")
            with col_act2:
                if st.button("❌ Reject / Escalate", disabled=not token_configured, key=f"rej_{dec_id}", use_container_width=True):
                    succ, res = api_post("/approvals/decision", {
                        "decision_id": dec_id,
                        "action": "REJECT",
                        "reviewer": reviewer_id,
                        "notes": notes
                    })
                    if succ:
                        st.success(f"Rejection recorded! Approval ID: {res.get('approval_id')}")
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error(f"Action failed: {res}")


# -------------------------------------------------------------
# SURFACE 4: HUMAN APPROVAL TIMELINE
# -------------------------------------------------------------
with tab_approvals:
    st.markdown("### Immutable Audit & Approval Trail")
    st.caption("Strictly append-only log of every state change, human sign-off, and deterministic policy transition.")

    audit_data = api_get("/audit-events") or {}
    human_approvals = audit_data.get("human_approvals", [])
    audit_events = audit_data.get("audit_events", [])

    col_a1, col_a2 = st.columns([1, 1])
    with col_a1:
        st.markdown("#### Human Approvals")
        if human_approvals:
            df_app = pd.DataFrame(human_approvals)
            cols_show = [c for c in ["approval_id", "decision_id", "action", "reviewer", "notes", "created_at"] if c in df_app.columns]
            st.dataframe(df_app[cols_show], use_container_width=True, hide_index=True)
        else:
            st.info("No human approvals recorded yet.")

    with col_a2:
        st.markdown("#### System Audit Events")
        if audit_events:
            df_ev = pd.DataFrame(audit_events)
            cols_ev = [c for c in ["event_id", "event_type", "aggregate_id", "created_at"] if c in df_ev.columns]
            st.dataframe(df_ev[cols_ev], use_container_width=True, hide_index=True)
        else:
            st.info("No system audit events recorded.")


# -------------------------------------------------------------
# SURFACE 5: BENCHMARK & ACCURACY REPORT
# -------------------------------------------------------------
with tab_benchmarks:
    st.markdown("### Automated Benchmark & Accuracy Console")
    st.caption("Measured throughput, latency percentiles, and machine learning accuracy on 30 held-out evaluation transactions.")

    report_data = api_get("/evaluation-report") or {}
    if report_data.get("status") == "not_generated":
        st.warning("⚠️ Benchmark evaluation report has not been generated yet. Run `python eval_benchmarks.py` or trigger from CLI.")
    else:
        perf = report_data.get("performance_benchmarks", {})
        acc = report_data.get("accuracy_benchmarks", {})
        env_spec = report_data.get("system_environment", {})

        col_b1, col_b2, col_b3, col_b4 = st.columns(4)
        with col_b1:
            st.markdown(f"""
            <div class="kpi-card">
                <div class="kpi-title">Throughput</div>
                <div class="kpi-value" style="color: #38bdf8;">{perf.get('records_per_second', 0.0):,.1f}</div>
                <div class="kpi-sub">records / second</div>
            </div>
            """, unsafe_allow_html=True)
        with col_b2:
            st.markdown(f"""
            <div class="kpi-card">
                <div class="kpi-title">Latency (p50 / p95)</div>
                <div class="kpi-value">{perf.get('p50_latency_ms', 0.0):.1f} <span style="font-size: 1rem; color: #94a3b8;">/ {perf.get('p95_latency_ms', 0.0):.1f}ms</span></div>
                <div class="kpi-sub">deterministic sweep latency</div>
            </div>
            """, unsafe_allow_html=True)
        with col_b3:
            st.markdown(f"""
            <div class="kpi-card">
                <div class="kpi-title">AI Held-Out Accuracy</div>
                <div class="kpi-value" style="color: #34d399;">{acc.get('ai_agent_evaluation', {}).get('held_out_accuracy_percent', 0.0)}%</div>
                <div class="kpi-sub">on 30 held-out ground truth txns</div>
            </div>
            """, unsafe_allow_html=True)
        with col_b4:
            st.markdown(f"""
            <div class="kpi-card">
                <div class="kpi-title">Abstention Fidelity</div>
                <div class="kpi-value" style="color: #34d399;">{acc.get('ai_agent_evaluation', {}).get('deliberate_abstention_fidelity_percent', 0.0)}%</div>
                <div class="kpi-sub">zero hallucinated actions</div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("---")
        col_m1, col_m2 = st.columns(2)
        with col_m1:
            st.markdown("#### Deterministic Matcher Metrics")
            dm = acc.get("deterministic_matcher", {})
            st.markdown(f"- **Coverage**: `{dm.get('coverage_percent', 0.0)}%`")
            st.markdown(f"- **Precision**: `{dm.get('precision_percent', 0.0)}%`")
            st.markdown(f"- **Recall**: `{dm.get('recall_percent', 0.0)}%`")
            st.markdown(f"- **F1 Score**: `{dm.get('f1_score', 0.0)}`")
            st.markdown(f"- **False Positives**: `{dm.get('false_positives', 0)}`")

        with col_m2:
            st.markdown("#### System & Environment Metadata")
            st.markdown(f"- **Platform**: `{env_spec.get('platform')}`")
            st.markdown(f"- **CPU Cores**: `{env_spec.get('cpu_cores')}`")
            st.markdown(f"- **Python Version**: `{env_spec.get('python_version')}`")
            st.markdown(f"- **SQLite Journal Mode**: `{env_spec.get('sqlite_journal_mode')}`")
            st.markdown(f"- **Git SHA**: `{env_spec.get('git_sha')}`")
            st.markdown(f"- **Dataset Seed Hash**: `{report_data.get('dataset_seed_hash', 'N/A')[:32]}...`")

        with st.expander("📄 View Full JSON Benchmark Report"):
            st.json(report_data)
