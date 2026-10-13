# REVIEW.md

Review of the starter `app/main.py`. Line numbers refer to the original starter file.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` L22 | Client is not `tz_aware`, so PyMongo returns **naive** datetimes. Mixing them with aware ones raises `TypeError`, and naive values are silently treated as UTC or local time depending on where they are used. | `compute_overtime` subtracts a naive `punch_out` from an IST-aware shift end → `TypeError` → 500. | `MongoClient(..., tz_aware=True, tzinfo=UTC)`; every instant in the code is aware UTC. |
| 2 | `compute_late_minutes` L33-37 | Builds shift start with `punch_in.replace(hour, minute)` on a UTC/naive instant, not on the IST date. On a UTC server everybody is 5:30 off. | Punch at 10:00 IST (04:30 UTC), shift 09:30 → returns 0, should be 30. | Shift start = `datetime.combine(attendance_date, shift_start, IST)`. |
| 3 | `compute_late_minutes` L37 | Grace check is on **whole minutes** (`minutes > 10`) instead of "strictly more than 10:00". | 09:40:30 → 0, should be 10 (R2: 09:40:01 is already late). | Compare elapsed **seconds** `> 600`, then floor to minutes. |
| 4 | `compute_late_minutes` | Overnight shifts not handled: a 00:30 punch for a 22:00 shift is compared with 22:00 the same day → negative → 0. | Shift 22:00-06:00, punch 00:30 → 0, should be 150. | Attendance date from R1 (previous day when before `shift_end`), shift start taken on that date. |
| 5 | `compute_work_hours` L41 | Python `round()` on a float is not half-up (binary floats + round-half-even). | 3h 0m 18s = 3.005 h → `round()` gives 3.0, R4 wants 3.01. | `Decimal(seconds) / 3600` quantized with `ROUND_HALF_UP`. |
| 6 | `compute_overtime` L47 | No 30-minute minimum (R3). | Out at 18:45 for an 18:30 shift → 15, should be 0. | Return minutes only when `>= 30`. |
| 7 | `compute_overtime` L46 | Overnight shift end is put on the same calendar day as the shift start. | Shift 22:00-06:00 dated 07-06, out 06:40 on 07-07 → 1480, should be 40. | Shift end +1 day when `shift_end <= shift_start`. |
| 8 | All helpers / `punch_in` | Instants not truncated to whole seconds (R1). | `punched_at` = 09:40:00.900 → stored with ms, later math slightly off. | `ms_to_dt` does `ms // 1000`; the truncated value is stored and returned. |
| 9 | `EmployeeIn` L53-60 | No validation at all: emp_code format, email, lengths, `HH:MM`, `joined_on` date, `shift_start == shift_end`. | `{"emp_code":"x","shift_start":"99:99",...}` → 201, then later punches crash with 500. | Pydantic `Field` patterns/lengths, a date validator, and a model validator for equal shift times. |
| 10 | `PunchInIn` L63-66 | `punched_at: Optional[int]` accepts seconds-looking values, floats like `1.7e12` and numeric strings (lax mode); `status: str` accepts `ABSENT`/garbage. | `punched_at: 1783312500` (seconds) → record in 1970; `"status": "ABSENT"` → 201. | `StrictInt` with `ge=1e11, le=4102444800000`; `status: Literal["PRESENT","WFH","ON_DUTY"]`. |
| 11 | `health` L74 | Always returns 200 without contacting MongoDB. | Stop MongoDB → `/health` still 200. | `ping` the server; 503 on failure (short `serverSelectionTimeoutMS` so it fails fast). |
| 12 | `create_employee` L79-81 | Check-then-insert: two parallel creates both pass `find_one` and both insert. No unique index. | 10 parallel POSTs with the same emp_code → several 201s, duplicate employees. | Unique index on `emp_code`; insert and map `DuplicateKeyError` → 409. |
| 13 | `create_employee` L82, L85 | `created_at = datetime.now()` is naive **local** time (stored as if UTC → shifted by 5:30 on an IST machine) and returned as an ISO string, not epoch ms. | Response has `"created_at": "2026-...T..."`. | `datetime.now(UTC)`, returned with `dt_to_ms`. |
| 14 | `list_employees` L93 | `skip = page * page_size` skips the first page. | `page=1` returns employees 21-40. | `(page - 1) * page_size`. |
| 15 | `list_employees` L94 | `total` counts the whole collection, ignoring the department filter (R10). | `?department=Ops` → `total` = all employees. | `count_documents(query)`. |
| 16 | `list_employees` L95 | No sort, so order (and pages) are not stable; contract says sort by `emp_code`. | Insert EMP0002 then EMP0001 → listed in insert order. | `.sort("emp_code", 1)`. |
| 17 | `list_employees`, `list_attendance` | No bounds on `page`/`page_size`. | `page=0` → negative skip → 500; `page_size=0` → `limit(0)` = no limit; `page_size=10000` allowed. | `Query(ge=1)` and `Query(ge=1, le=100)` → 422. |
| 18 | `punch_in` L101 | Unknown employee: `emp` is `None` and `emp["shift_start"]` raises → 500. | Punch-in for `EMP9999` → 500 instead of 404. | Look up first, 404 if missing. |
| 19 | `punch_in` L102-103 | `datetime.fromtimestamp(ms / 1000)` uses the **server's** time zone; the attendance date is the server-local date, not the IST date (R1), and overnight shifts are ignored. | On a UTC server, a punch at 02:00 IST is dated the previous day. | Convert to IST and apply R1 (`attendance_date`). |
| 20 | `punch_in` L104-118 | Check-then-insert race: two simultaneous punch-ins both see no record and both insert. | 10 parallel identical punch-ins → several 201s, duplicate (emp_code, date) docs. | Unique index on `(emp_code, date)`; insert, `DuplicateKeyError` → 409. |
| 21 | `punch_in` L119-121 | Response leaks the Mongo `_id` as `id` and returns datetimes instead of epoch ms. | Response contains `"id": "66a..."`, `"punch_in": "2026-..."`. | `record_out()` builds the contract shape; no ids. |
| 22 | `list_attendance` L144-147 | Loads **every** matching document into Python and sorts in memory. | Slow / high memory at 100k records; explain would be a COLLSCAN. | `find().sort(date -1, emp_code 1).skip().limit()` backed by indexes. |
| 23 | `list_attendance` L145 | Sort has no `emp_code` tie-break, so records of the same day come back in random order. | Two employees on one date swap between calls / pages. | Sort `[("date", -1), ("emp_code", 1)]`. |
| 24 | `list_attendance` | No validation: invalid dates, `date_from > date_to`, unknown status all return 200. | `?status=FOO` → 200 with empty list; `?date_from=2026-13-45` accepted. | `DateStr` validator, `Status` literal, explicit `date_from > date_to` → 422. |
| 25 | `list_attendance` L148-149 | Returns `id` (from `_id`), raw datetimes, and passes legacy records through without `history` / `half_day`. | Legacy sample #9 comes back without `half_day` and `history`. | `record_out()` fills defaults (`[]`, `false`, `0`). |
| 26 | Whole file | No indexes are created, so every query is a collection scan and nothing enforces uniqueness. | `explain` shows `COLLSCAN`. | `ensure_indexes()` in the FastAPI lifespan (idempotent). |

## Looked at and decided **not** a defect

- `load_dotenv()` default is `override=False`, so real environment variables already win over `.env`. Kept (made the argument explicit).
- `IST = timezone(timedelta(hours=5, minutes=30))` is correct: IST has no daylight saving time.
- Using `HTTPException(409, ...)` for duplicates is the right code; only how the duplicate is detected was wrong.
- Storing `date` as a `"YYYY-MM-DD"` string is intended by the data model, and ISO strings sort and compare correctly, so range filters on strings are fine.
- `doc.pop("_id", None)` after `insert_one` in `create_employee` was correct (insert mutates the dict); replaced only because the response is now built by `employee_out`.
