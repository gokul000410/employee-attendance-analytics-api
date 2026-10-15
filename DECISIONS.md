# DECISIONS.md

1. **Indexes.**
   On `employees`, unique `{"emp_code": 1}` prevents duplicate employees, while `{"department": 1, "joined_on": 1}` supports filtered headcount scans. On `attendance_logs`, unique `{"emp_code": 1, "date": 1}` enforces the natural key constraint; `{"date": -1, "emp_code": 1}` satisfies pagination and default sort without blocking in-memory sorts; `{"emp_code": 1, "punch_in": -1}` enables O(1) punch-out resolution; and `{"date": 1, "late_minutes": 1, "emp_code": 1}` backs the leaderboard via an index-covered scan. I considered and rejected a standalone index on `status` due to low cardinality and poor selectivity.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `db.attendance_logs.insert_one()`. Because `(emp_code, date)` has a database-level unique index, MongoDB atomically accepts the first write (201 Created). The second concurrent write immediately raises a `DuplicateKeyError`, which our application catches to return 409 Conflict. No check-then-insert gap exists.

3. **Ties.**
   The aggregation uses `$setWindowFields` with `$rank` for standard competition ranking (1, 2, 2, 4). The `$match: {"rank": {"$lte": limit}}` filter applies strictly after ranking. If employees tie at the cutoff rank, all tied employees are returned, exceeding `limit` as required by the specification. Rows are deterministically sorted by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
   The pipeline originates on the `employees` collection matching `joined_on <= period_end`, capturing every active employee. A subsequent `$lookup` executes a left outer join into `attendance_logs`. Employees with no attendance logs have an empty `logs` array but still increment `$group`'s `headcount: {"$sum": 1}`.

5. **One thing you would change for 100x data.**
   At 10 million attendance records, calculating aggregations on-the-fly across large collections becomes unsustainable. I would introduce an incremental pre-aggregated daily and monthly rollup collection, updated asynchronously via MongoDB Change Streams upon punch and correction events.
