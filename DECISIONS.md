# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity and duplicate 409s), `department + emp_code` (department filter, sorted listing, trend), and `joined_on` (summary headcount). `attendance_logs`: unique `emp_code + date` (one record per day, punch-in race, PATCH lookup, monthly), `date desc + emp_code` (list sort, month ranges for summary and leaderboard), and `status + date + emp_code` (status filter). I rejected `emp_code + punch_in` for punch-out: dates follow punch-in time, so walking the existing `emp_code + date` index backwards finds the same record.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. MongoDB's unique index lets exactly one in. The other gets `DuplicateKeyError`, which I turn into 409. I never check first.

3. **Ties.** `$rank` sorts by total only, so tied employees share a rank and the next rank is skipped. I filter `rank <= limit` afterwards, so everyone tied at the cutoff is returned, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` filtered by `joined_on`, joins logs with `$lookup`, and unwinds with `preserveNullAndEmptyArrays`, so employees with no logs remain and count.

5. **At 100x.** Store a monthly per-employee summary that is updated on every write, so the analytics read thousands of small documents instead of millions of logs.
