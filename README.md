

A robust, production-grade FastAPI and MongoDB service implementing the HROne Employee Attendance & Analytics API (OpenAPI v2.0.0 contract) with full adherence to business rules R1 – R10, database-enforced concurrency guarantees, and aggregation pipelines.

---

## 1. Project Overview

This service manages employee attendance operations (punch-in, punch-out, regularization audit trails) and provides analytics across departments and individual employees (monthly summaries, department trends, and late leaderboard rankings).

Key Architectural Highlights:
- **Strict OpenAPI 2.0.0 Compliance:** Request and response schemas, path parameters, query filters, and status codes (200, 201, 404, 409, 422, 503).
- **Business Rules R1 – R10:** Precise implementation of IST conversions, overnight shifts crossing midnight, whole-second truncation, grace periods, round-half-up work duration, half-day flags, mid-month joiner working days, and gap-filling.
- **Pure Aggregation Pipelines:** Analytics endpoints leverage MongoDB aggregation (`$lookup`, `$densify`, `$setWindowFields` with `$rank`, and `$avg` moving windows) without in-memory Python iteration.
- **Concurrency & Atomicity:** Database-level unique constraints and atomic update operations eliminate race conditions for punch operations and audit logging.
- **Index-Backed Performance:** Query plans verified via `/admin/explain/{endpoint}` show `IXSCAN` utilization and 0 `COLLSCAN` across 100,000 attendance records.

---

## 2. Prerequisites

- **Python:** 3.11 or newer (tested with Python 3.12)
- **MongoDB:** 6.0 or newer (tested with MongoDB 9.0.2; MongoDB Atlas M0 free tier or local instance)

---

## 3. Installation

Clone the repository and install dependencies:

```bash
pip install -r requirements.txt
```

*Note: Dependencies in `requirements.txt` are kept minimal (`fastapi`, `uvicorn`, `pymongo`, `pydantic`, `python-dotenv`, `httpx`, `pytest`).*

---

## 4. Environment Variables

The application reads configuration dynamically from environment variables (system environment variables take priority, falling back to optional local `.env`):

| Variable | Description | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection URI string | `mongodb://localhost:27017` |
| `MONGO_DB` | MongoDB database name | `attendance_db` |

*Security notice: Do not commit `.env` files or credentials into the repository.*

---

## 5. Starting MongoDB & Seeding Data

If running MongoDB locally or via Docker:

```bash
# Docker example:
docker run -d --name mongo -p 27017:27017 mongo:7

# Seed the starter sample documents:
python sample_seed.py
```

---

## 6. Running the Application

Start the FastAPI application from the repository root:

```bash
uvicorn app.main:app --port 8000 --reload
```

The application is ready when `GET http://localhost:8000/health` responds with `{"status": "ok"}` (HTTP 200).

---

## 7. Interactive API Documentation

Interactive OpenAPI / Swagger documentation is automatically generated:
- **Swagger UI:** `http://localhost:8000/docs`
- **ReDoc:** `http://localhost:8000/redoc`
- **OpenAPI JSON:** `http://localhost:8000/openapi.json`

---

## 8. Running Automated Tests

Run the full pytest suite covering CRUD, business rules, edge cases, explain plans, and concurrency:

```bash
pytest -v
```

All 38 automated test cases execute against the database and validate:
- Status codes (200, 201, 404, 409, 422, 503)
- Concurrency protection under simultaneous parallel requests
- Overnight shift boundaries and grace period logic
- Leaderboard competition ties (1, 2, 2, 4)
- Department trends with daily gap-filling
- Query plan verification (`IXSCAN` present, `COLLSCAN` absent)

---

## 9. How Indexes Are Created

Indexes are created **automatically and idempotently** during application startup using the FastAPI `lifespan` hook:

1. **`employees` Collection:**
   - `{"emp_code": 1}` (Unique): Enforces uniqueness of customer employee codes; supports O(1) point lookups.
   - `{"department": 1, "joined_on": 1}`: Supports department filtering and joined_on period cutoff matching.
   - `{"joined_on": 1}`: Accelerates headcount evaluations across all departments.

2. **`attendance_logs` Collection:**
   - `{"emp_code": 1, "date": 1}` (Unique): Natural key constraint ensuring at most one attendance record per employee per day; prevents duplicate punch-in race conditions.
   - `{"date": -1, "emp_code": 1}`: Backs `GET /attendance` default sorting and pagination without in-memory sorting.
   - `{"emp_code": 1, "punch_in": -1}`: Accelerates punch-out record resolution.
   - `{"date": 1, "late_minutes": 1, "emp_code": 1}`: Powers the late leaderboard aggregation pipeline via an index-covered scan.

---

## 10. Implementation Status

### Implemented Features (100% Core Contract)
- [x] **Readiness Probe (`GET /health`):** Pings MongoDB, returning 200 on success or 503 on database unavailability.
- [x] **Employee Management (`POST /employees`, `GET /employees`):** Database-enforced uniqueness, shift validation, pagination, department filtering, sorted by `emp_code` asc.
- [x] **Punch-In Operations (`POST /attendance/punch-in`):** R1 IST date determination with overnight shift support, R2 10-minute grace period, race condition protection (201/409).
- [x] **Punch-Out Operations (`POST /attendance/punch-out`):** Resolves most recent punch-in across midnight, R3 overtime (>=30 min threshold), R4 half-up work hours, R5 half-day flag, atomic race protection.
- [x] **Attendance Records List (`GET /attendance`):** Range filtering, status filtering, stable ordering (`date` desc, `emp_code` asc), index-backed pagination without collection loading.
- [x] **Regularization & Audit Trail (`PATCH /attendance/{emp_code}/{date}`):** Validates attendance rules, recomputes derived metrics, appends immutable `{at, by, reason, changes}` history entries with optimistic concurrency control.
- [x] **Employee Monthly Analytics (`GET /analytics/employees/{emp_code}/monthly`):** Aggregation pipeline, R7 Mon-Fri working days from `joined_on`, weekend log exclusion for present days, handles 0-log employees.
- [x] **Department Summary Analytics (`GET /analytics/departments/summary`):** Cross-collection aggregation, R9 headcount including 0-log staff, record-level work hours mean.
- [x] **Late Leaderboard (`GET /analytics/leaderboard/late`):** Standard competition ranking (`$rank`), handles tied scores (1, 2, 2, 4), cutoff limit applied strictly after ranking.
- [x] **Department Daily Trend (`GET /analytics/departments/{department}/trend`):** `$densify` gap-filling for weekends and days without logs, rolling 7-day moving average.
- [x] **Explain Endpoint (`GET /admin/explain/{endpoint}`):** Shared query builders running `explain` with `executionStats`, returning raw explain documents with verified index scans.
- [x] **Documentation Deliverables:** Fully detailed `REVIEW.md` and `DECISIONS.md`.

### Remaining / Future Enhancements (10–20% Production Extensions)
- **Authentication & Role-Based Access Control (RBAC):** The assignment specification intentionally specifies no authentication; adding JWT / OAuth2 for production deployment.
- **Asynchronous Rollup Pipelines:** Pre-aggregating historical monthly statistics via Change Streams for ultra-high throughput at 10M+ records.
- **Holiday Calendar Customization:** Extending R7 working day calculations with company-specific holiday lists beyond Monday–Friday weekdays.
