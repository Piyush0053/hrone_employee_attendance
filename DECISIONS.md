# DECISIONS.md

1. **Indexes.**
We created unique `{emp_code: 1}` on `employees` for identity lookups and uniqueness, and `{department: 1, joined_on: 1}` for headcount queries. On `attendance_logs`, we created unique `{emp_code: 1, date: 1}` for concurrency and single-punch guarantees, `{date: 1, late_minutes: 1}` for leaderboard scans, and `{status: 1, date: -1}` for filtered pagination. We considered and rejected putting `{department: 1, date: 1}` on `attendance_logs`: department is an employee attribute; denormalizing it would introduce update anomalies during internal transfers.

2. **Punch-in race.**
Two simultaneous punch-in requests calculate the attendance date and attempt `insert_one`. MongoDB's unique index on `{emp_code: 1, date: 1}` guarantees atomicity at the storage engine level. Exactly one insert commits with 201 Created; the second triggers a `DuplicateKeyError`, which our exception handler intercepts to return HTTP 409 Conflict.

3. **Ties.**
The leaderboard uses MongoDB `$rank` inside `$setWindowFields` for competition ranking (1-2-2-4). Ties at the cutoff share the same rank and are ordered deterministically by `emp_code` ascending. `$limit` is applied after ranking.

4. **Headcount.**
Department summary aggregates from `employees` first, filtering `joined_on <= month_end` to capture total headcount before performing a `$lookup` to `attendance_logs`. Employees with zero logs are retained in the base headcount.

5. **One thing you would change at 100x data.**
Pre-compute daily attendance rollups using MongoDB Change Streams and shard `attendance_logs` on `{date: 1, emp_code: 1}`.
