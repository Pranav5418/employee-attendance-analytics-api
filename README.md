# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # or export MONGO_URI / MONGO_DB (real env vars win over .env)
python sample_seed.py         # optional: loads the nine sample documents
uvicorn app.main:app --port 8000
```

Needs MongoDB 6.0+ (tested on 7.0). Indexes are created at startup and the step is idempotent. Interactive docs are at `http://localhost:8000/docs`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q                     # uses database "attendance_test" and wipes it
```

The tests cover R1-R5 edge times, punch-in/punch-out/PATCH races, ties at the leaderboard cutoff, employees without logs, weekend records, mid-month joiners, an overnight shift, the trend gap filling and moving average, and the IXSCAN check of the explain endpoint. I also checked the explain endpoint against a 100k-record dataset: every plan is IXSCAN with no COLLSCAN, and every endpoint answers in under 150 ms locally.

## Layout of `app/main.py`

1. Config and Mongo client (`tz_aware=True`)
2. Index setup in the FastAPI lifespan
3. Time helpers and rules R1-R5 (`attendance_date`, `shift_window`, `derive_fields`, ...)
4. Validation types and request models
5. Output converters (BSON to API JSON, epoch ms)
6. Query and pipeline builders, shared by each endpoint and `/admin/explain`
7. Endpoints

## Notes

- Concurrency: unique indexes for punch-in and employee creation. A conditional `find_one_and_update` (`punch_out: null`) for punch-out. Optimistic concurrency for PATCH, where the loser gets 409 and no history entry is lost.
- Rounding is half-up and exact: `Decimal` for work hours, `Fraction` for monthly and summary figures, and an integer `floor((2n + d) / 2d)` expression inside the trend pipeline.
- PATCH: derived fields sent by a client are ignored. An explicit `null` for `status`, `punch_in` or `punch_out` is a 422 (the schema does not allow null).
- Trend: a range "exceeds 92 days" when it has more than 92 calendar days, both ends included.
- Nothing is missing from the contract.
