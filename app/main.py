"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout of this file (everything lives here on purpose, see README):
  1. Config and Mongo client
  2. Index setup (runs at startup)
  3. Time helpers and business rules R1 - R5
  4. Validation types and request models
  5. Output converters (BSON -> API JSON)
  6. Query / pipeline builders (shared by the endpoints and /admin/explain)
  7. Endpoints
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from math import floor
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Config and Mongo client
# --------------------------------------------------------------------------- #
load_dotenv(override=False)  # real environment variables take priority over .env

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENCE = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = 4.5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92


# --------------------------------------------------------------------------- #
# 2. Indexes (idempotent: create_index is a no-op when the index already exists)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    # employees
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # attendance_logs
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_code_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_code")
    logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp_code"
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep starting; /health reports 503 while Mongo is down
        log.error("index creation failed: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 3. Time helpers and business rules
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch millis -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return (value - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST calendar date of the punch-in; early-morning punches of an overnight shift belong to the day before."""
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_window(emp: dict, day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    seconds = (punch_in - shift_start) // timedelta(seconds=1)
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours rounded to 2 decimals half-up (Decimal, not float round())."""
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: floor minutes after shift end, only when >= 30."""
    minutes = ((punch_out - shift_end) // timedelta(seconds=1)) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive_fields(emp: dict, day: date, status: str, punch_in, punch_out) -> dict:
    """All derived fields (R2 - R5) for a record's final state."""
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start, end = shift_window(emp, day)
    out = {"work_hours": None, "late_minutes": compute_late_minutes(punch_in, start),
           "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        out.update(work_hours=hours, overtime_minutes=compute_overtime(punch_out, end),
                   half_day=hours < HALF_DAY_HOURS)
    return out


def round_half_up(value: Fraction, places: int) -> float:
    """R8 rounding on an exact fraction (values here are never negative)."""
    scale = 10 ** places
    return floor(value * scale + Fraction(1, 2)) / scale


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    nxt = date(year + mon // 12, mon % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def count_weekdays(start: date, end: date) -> int:
    days = (end - start).days + 1
    return sum(1 for i in range(max(days, 0)) if (start + timedelta(days=i)).weekday() < 5)


def validation_error(msg: str, loc: tuple = ("query",)) -> RequestValidationError:
    """422 with FastAPI's standard {"detail": [...]} body, for checks that span several fields."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


# --------------------------------------------------------------------------- #
# 4. Validation types and request models
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


def _check_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ValueError("expected a date as YYYY-MM-DD")
    date.fromisoformat(value)  # rejects impossible dates such as 2026-02-30
    return value


DateStr = Annotated[str, AfterValidator(_check_date)]
# StrictInt rejects floats, strings and booleans; the bounds reject seconds-looking values.
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Month = Annotated[str, Query(pattern=MONTH_PATTERN)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: DateStr

    @model_validator(mode="after")
    def shift_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOut(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class Regularize(BaseModel):
    model_config = ConfigDict(extra="ignore")  # derived fields sent by a client are ignored

    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} can not be null")
        return self


# --------------------------------------------------------------------------- #
# 5. Output converters
# --------------------------------------------------------------------------- #
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")


def employee_out(doc: dict) -> dict:
    out = {k: doc.get(k) for k in EMPLOYEE_FIELDS}
    out["created_at"] = dt_to_ms(doc.get("created_at"))
    return out


def _instant_out(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def record_out(doc: dict) -> dict:
    """Stored record -> API record. Missing legacy fields get their documented defaults."""
    history = []
    for entry in doc.get("history") or []:
        changes = {
            field: {"from": _instant_out(c.get("from")), "to": _instant_out(c.get("to"))}
            for field, c in (entry.get("changes") or {}).items()
        }
        history.append({"at": dt_to_ms(entry.get("at")), "by": entry.get("by"),
                        "reason": entry.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# 6. Query and pipeline builders (each endpoint and /admin/explain call the same builder)
# --------------------------------------------------------------------------- #
LATE = {"$ifNull": ["$late_minutes", 0]}
OVERTIME = {"$ifNull": ["$overtime_minutes", 0]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
# R5/R6: a presence record is worth 1 day, or 0.5 when it is a half day; other statuses are worth 0.
PRESENCE_WEIGHT = {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                             {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def half_up_int(numerator: Any, denominator: Any) -> dict:
    """Mongo expression: round(numerator / denominator) half-up, for non-negative values.
    floor((2n + d) / 2d) works on integers, so it avoids float errors and $round's round-half-to-even."""
    return {"$floor": {"$divide": [{"$add": [{"$multiply": [numerator, 2]}, denominator]},
                                   {"$multiply": [denominator, 2]}]}}


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    if date_from and date_to and date_from > date_to:
        raise validation_error("date_from must not be after date_to")
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


def monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENCE_WEIGHT, 0]}},  # R7: Mon-Fri only
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if({"$gt": [LATE, 0]}),
            "total_late_minutes": {"$sum": LATE},
            "total_overtime_minutes": {"$sum": OVERTIME},
        }},
    ]


def summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    """Starts from employees so that people with zero logs are still counted (R9)."""
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        match["department"] = department
    has_hours = {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$isNumber": "$work_hours"}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENCE_WEIGHT, 0]}},
                    # work_hours summed in whole hundredths so the department average is exact
                    "wh_cents": {"$sum": {"$cond": [has_hours,
                                                    {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                    "wh_records": count_if(has_hours),
                    "late_count": count_if({"$gt": [LATE, 0]}),
                    "total_late_minutes": {"$sum": LATE},
                    "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                    "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
                }},
            ],
            "as": "stats",
        }},
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},  # keep employees with no logs
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$stats.present_days"},
            "wh_cents": {"$sum": "$stats.wh_cents"},
            "wh_records": {"$sum": "$stats.wh_records"},
            "late_count": {"$sum": "$stats.late_count"},
            "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
            "leave_count": {"$sum": "$stats.leave_count"},
            "on_duty_count": {"$sum": "$stats.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4). sortBy has only the total, so ties share a rank.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def trend_pipeline(department: str, start: date, end: date) -> list:
    """One row per day from start to end. Each employee contributes their logs plus one 'joined' event
    (+1 headcount on max(joined_on, start)); a running sum of those events gives the headcount per day."""
    start_s, end_s = start.isoformat(), end.isoformat()
    joined_in_range = {"$lte": ["$joined_on", end_s]}
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start_s, "$lte": end_s}}},
                {"$project": {"_id": 0, "d": "$date", "p": PRESENCE_WEIGHT,
                              "l": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}}},
            ],
            "as": "logs",
        }},
        {"$project": {"_id": 0, "events": {"$concatArrays": ["$logs", [{
            "d": {"$cond": [joined_in_range, {"$max": ["$joined_on", start_s]}, start_s]},
            "h": {"$cond": [joined_in_range, 1, 0]},
        }]]}}},
        {"$unwind": "$events"},
        {"$group": {"_id": "$events.d", "h": {"$sum": "$events.h"},
                    "p": {"$sum": "$events.p"}, "l": {"$sum": "$events.l"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # gap filling: one document per calendar day (upper bound is exclusive)
        {"$densify": {"field": "day", "range": {
            "step": 1, "unit": "day",
            "bounds": [datetime.combine(start, time(), timezone.utc),
                       datetime.combine(end + timedelta(days=1), time(), timezone.utc)]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "headcount": {"$sum": "$h", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$set": {
            "present_count": {"$ifNull": ["$p", 0]},
            "late_count": {"$ifNull": ["$l", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        # attendance_rate kept as an integer in 1/10000 units until the end, to round half-up exactly
        {"$set": {"rate_e4": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_int({"$multiply": ["$present_count", 10000]}, "$headcount"),
            None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "win_sum": {"$sum": "$rate_e4", "window": {"documents": [-6, 0]}},
            "win_n": {"$sum": {"$cond": [{"$eq": ["$rate_e4", None]}, 0, 1]}, "window": {"documents": [-6, 0]}},
        }}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate_e4", None]}, None, {"$divide": ["$rate_e4", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$win_n", 0]}, None,
                                        {"$divide": [half_up_int("$win_sum", "$win_n"), 10000]}]},
        }},
    ]


def trend_range(start: str, end: str) -> tuple[date, date]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise validation_error("to must not be before from")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise validation_error(f"range must not exceed {MAX_TREND_DAYS} days")
    return first, last


# --------------------------------------------------------------------------- #
# 7. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    return {"status": "ok"}


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# ---- employees ------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department else {}
    cursor = employees.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": employees.count_documents(query),
            "page": page, "page_size": page_size}


# ---- attendance ------------------------------------------------------------ #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": emp["emp_code"],
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(emp, day, body.status, ts, None),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): concurrent duplicates get DuplicateKeyError
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOut):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record that was punched in at or before ts. Dates follow punch-in time (R1),
    # so "latest date" is "latest punch_in"; this walks the (emp_code, date) index backwards.
    record = logs.find_one({"emp_code": emp["emp_code"], "punch_in": {"$lte": ts}}, sort=[("date", DESCENDING)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    started = record["punch_in"].replace(microsecond=0)
    if ts <= started or ts - started > MAX_SHIFT:
        raise validation_error("punched_at must be after punch_in and within 24 hours", ("body", "punched_at"))

    derived = derive_fields(emp, date.fromisoformat(record["date"]), record["status"], started, ts)
    update = {"punch_out": ts, "work_hours": derived["work_hours"],
              "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}
    # The punch_out: None condition makes the update atomic: of two parallel punch-outs only one matches.
    updated = logs.find_one_and_update({"_id": record["_id"], "punch_out": None}, {"$set": update},
                                       return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    cursor = logs.find(query).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    total = logs.count_documents(query) if query else logs.estimated_document_count()
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
FIELD_DEFAULTS = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Annotated[DateStr, Path()], body: Regularize):
    record_date = date
    emp = get_employee(emp_code)
    record = logs.find_one({"emp_code": emp_code, "date": record_date})
    if record is None:
        raise HTTPException(404, "no attendance record for this date")
    day = datetime.fromisoformat(record_date).date()

    status = body.status or record["status"]
    if status in NON_PRESENCE:
        if body.punch_in is not None or body.punch_out is not None:
            raise validation_error(f"{status} can not have punch times", ("body",))
        punch_in_at = punch_out_at = None
    else:
        punch_in_at = ms_to_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        punch_out_at = ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")
        if punch_in_at is None:
            raise validation_error(f"{status} requires a punch_in", ("body", "punch_in"))
        if body.punch_in is not None and attendance_date(punch_in_at, emp) != day:
            raise validation_error("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if punch_out_at is not None and not (punch_in_at < punch_out_at <= punch_in_at + MAX_SHIFT):
            raise validation_error("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))

    new = {"status": status, "punch_in": punch_in_at, "punch_out": punch_out_at,
           **derive_fields(emp, day, status, punch_in_at, punch_out_at)}
    changes = {}
    for field in TRACKED_FIELDS:
        old = record.get(field, FIELD_DEFAULTS.get(field))
        if old is None and field in FIELD_DEFAULTS:
            old = FIELD_DEFAULTS[field]
        if old != new[field]:
            changes[field] = {"from": old, "to": new[field]}
    if not changes:
        raise validation_error("the request does not change anything", ("body",))

    entry = {"at": datetime.now(timezone.utc), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: only update if nobody changed the record since we read it.
    # A parallel correction makes this match nothing, and the loser gets 409 instead of losing an entry.
    history_len = len(record.get("history") or [])
    guard = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
    }
    updated = logs.find_one_and_update(guard, {"$set": new, "$push": {"history": entry}},
                                       return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was changed by another request, please retry")
    return record_out(updated)


# ---- analytics ------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    rows = list(logs.aggregate(monthly_pipeline(emp_code, first, last)))
    stats = rows[0] if rows else {}

    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last)  # R7: counted from joined_on
    present = Fraction(stats.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present),
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(present * 100 / working_days, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for row in employees.aggregate(summary_pipeline(first, last, department)):
        records = row["wh_records"]
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": float(row["present_days"]),
            "avg_work_hours": round_half_up(Fraction(int(row["wh_cents"]), records * 100), 2) if records else None,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    first, last = month_bounds(month)
    return {"month": month, "items": list(logs.aggregate(leaderboard_pipeline(first, last, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[DateStr, Query(alias="from")],
                     to: Annotated[DateStr, Query()]):
    first, last = trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": list(employees.aggregate(trend_pipeline(department, first, last)))}


# ---- admin ----------------------------------------------------------------- #
def _require(**params) -> None:
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise RequestValidationError([{"type": "missing", "loc": ("query", name), "msg": "Field required",
                                       "input": None} for name in missing])


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[Month] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        collection = logs.name
        command = {"find": collection, "filter": attendance_filter(emp_code, date_from, date_to, status),
                   "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            collection, pipeline = logs.name, monthly_pipeline(emp_code, *month_bounds(month))
        elif endpoint == "department_summary":
            _require(month=month)
            collection, pipeline = employees.name, summary_pipeline(*month_bounds(month), department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            collection, pipeline = logs.name, leaderboard_pipeline(*month_bounds(month), limit, department)
        else:
            _require(department=department, **{"from": from_, "to": to})
            collection, pipeline = employees.name, trend_pipeline(department, *trend_range(from_, to))
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    # explain output contains BSON types (Timestamp, Int64, ...); relaxed Extended JSON turns them into plain JSON
    plain = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": plain}
