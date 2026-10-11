# DECISIONS.md (about 150-200 words in total - plain language, your own words)

1. **Indexes.** We created unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` for entity identity and concurrency safety. Compound index `attendance_logs(date DESC, emp_code ASC)` backs sorted listing, while `(date, late_minutes)` supports the leaderboard aggregation. We considered indexing individual punch timestamps but rejected it because queries filter on calendar dates, rendering timestamp indexes redundant.

2. **Punch-in race.** Both requests verify employee existence and attempt insertion. MongoDB's unique index on `(emp_code, date)` serializes write operations: exactly one document commits with HTTP 201, while the competing request triggers a `DuplicateKeyError`, caught and translated into HTTP 409.

3. **Ties.** Using MongoDB's `$rank` window operator, tied employees share a rank while subsequent ranks are skipped (1, 2, 2, 4). Filtering occurs after ranking (`rank <= limit`), returning all tied employees at the boundary even if the list exceeds `limit`, per contract.

4. **Headcount.** The department aggregation pipeline originates on the `employees` collection, grouping by department to establish baseline headcount before performing a `$lookup` into `attendance_logs`. Employees with zero logs are preserved with full headcount representation.

5. **One thing you would change:** Pre-aggregate daily and monthly department rollups upon punch-out and regularization, avoiding real-time multi-stage aggregations across 10 million attendance logs.
