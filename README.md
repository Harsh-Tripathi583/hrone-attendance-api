# Employee Attendance & Analytics API

FastAPI + MongoDB backend implementation for employee attendance tracking, regularization, and multi-stage aggregation analytics.

## Tech Stack
- **Python**: 3.11+ (FastAPI, Pydantic v2, PyMongo)
- **Database**: MongoDB 6.0+ (Tested against MongoDB 8.x)

## Setup & Running

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables**:
   By default, the service connects to `mongodb://localhost:27017` with database `attendance_db`. Override if necessary:
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Seed Database** (optional for local testing):
   ```bash
   python sample_seed.py
   ```

4. **Start the API Server**:
   ```bash
   uvicorn app.main:app --port 8000
   ```

## API Highlights & Implementation

- **Readiness Probe**: `GET /health` pings MongoDB and returns 200 when ready or 503 if unavailable.
- **Single-File Architecture**: All application code, business rules R1–R10, data serialization, and models reside in `app/main.py`.
- **Database Indexing**: Automatic idempotent index creation during startup:
  - `employees`: Unique index on `emp_code`, compound indexes on `(department, emp_code)`, `(department, joined_on)`.
  - `attendance_logs`: Unique compound index on `(emp_code, date)`, compound sort index on `(date DESC, emp_code ASC)`, late leaderboard index on `(date, late_minutes)`.
- **Concurrency Protections**:
  - Simultaneous punch-ins: Handled by unique compound index on `(emp_code, date)`, ensuring exactly one 201 and 409 for duplicates.
  - Simultaneous punch-outs: Handled by atomic conditional `find_one_and_update` on `{"punch_out": None}`, yielding exactly one 200 and 409 for conflicts.
  - Simultaneous regularizations: Optimistic locking on audit history array length prevents lost updates.
- **Analytics & Explain**:
  - Aggregation pipelines power employee monthly summaries, department summaries (with zero-log employee headcount), competition rank leaderboard ties (`$rank`), and daily gap-filled trends (`$densify`, 7-day moving average).
  - `GET /admin/explain/{endpoint}` validates index execution (`IXSCAN`) with zero collection scans (`COLLSCAN`).

## Documentation Files
- `REVIEW.md`: Detailed defect register of all identified and resolved starter issues.
- `DECISIONS.md`: Concise architectural rationale on indexes, concurrency, ranking, and scalability.
