# Changelog

All notable changes to the **PaisaGuard** platform are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.1.0] - 2026-09-03 - Hardened Release (Track 04 Submission Standard)

### Added
- **Automated Continuous Integration**: Added zero-dependency GitHub Actions workflow (`.github/workflows/reconcile-ci.yml`) automating build, database initialization, unit testing, and output artifact verification on every PR and push.
- **Dual-Encoding HMAC SHA256 Engine**: Upgraded `api.py` webhook gateway to natively verify both **Hex** and **Base64** digests against raw unparsed request byte buffers.
- **Defensive API Ingestion**: Added automatic fallback normalization for `None` or omitted `fee`, `tax`, and payment method fields.
- **Prometheus Telemetry**: Added `/metrics/prometheus` plain text exposition endpoint alongside JSON `/metrics` endpoint.
- **Persistent Sub-Paise Accumulator**: Added `reconciliation_runs` table and schema tracking rolling multi-cycle drift persistence and audit trail across recurring reconciliation sweeps.
- **Machine-Readable Benchmark Artifact**: Enhanced `concurrency_tester.py` to record hardware and system specifications and export `out/concurrency-benchmark.json`.
- **Reproducible Webhook Replayer**: Created `replay_webhooks.py` supporting live simulation of signed Razorpay webhook streams and security attack blocking.
- **Container Infrastructure**: Added production `Dockerfile` and `docker-compose.yml` for zero-friction containerized deployment.
- **Open-Source Compliance**: Added OSI-approved `LICENSE` (MIT) and `CONTRIBUTING.md`.

### Changed
- **PyPI Dependency Sanitization**: Eliminated all local `file:///tmp/wheels/...` path dependencies from `requirements.txt`, replacing them with stable PyPI package specifications.
- **Documentation**: Updated `README.md` and `README-v2.md` with competitive forensic audit tables against *LedgerMatch AI* and *Revenue Resilience AI*, architecture flowcharts, and 3-minute judging walkthrough guide.

---

## [2.0.0] - 2026-09-03 - Core Engine & WAL Concurrency Release

### Added
- 4-Pass Hybrid Reconciliation Pipeline (`recon_engine.py`).
- Sub-Paise Aggregate Rounding Accumulator ($\pm ₹0.50$ tolerance).
- Dual-Layer GSTR-2B ITC Safeguard Audit (Section 16(2)(aa) CGST Act).
- SQLite Write-Ahead Logging (WAL) backend with `synchronous = NORMAL` and `busy_timeout = 5000`.
- Streamlit Visual Operator Console (`app.py`) with 1-click manual exception override cache.
- Multi-threaded Concurrency Benchmark Harness (`concurrency_tester.py`).
