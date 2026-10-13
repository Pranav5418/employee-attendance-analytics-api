"""End-to-end tests against a real MongoDB (uses MONGO_DB=attendance_test by default).

Run:  pytest -q        (needs MongoDB on MONGO_URI, default mongodb://localhost:27017)
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

os.environ["MONGO_DB"] = "attendance_test"
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, db  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def ms(day: str, hhmmss: str) -> int:
    """IST wall clock -> epoch ms."""
    dt = datetime.fromisoformat(f"{day}T{hhmmss}").replace(tzinfo=IST)
    return int(dt.timestamp() * 1000)


def utc(day: str, hhmmss: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmmss}").replace(tzinfo=IST).astimezone(timezone.utc)


@pytest.fixture()
def client():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    with TestClient(app) as c:  # runs the lifespan (index creation)
        yield c


def add_emp(c, code, dept="Eng", start="09:30", end="18:30", joined="2026-01-01", name=None):
    r = c.post("/employees", json={"emp_code": code, "name": name or code, "email": f"{code}@x.com",
                                   "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined})
    assert r.status_code == 201, r.text
    return r.json()


def seed_log(code, day, status="PRESENT", late=0, ot=0, wh=None, half=False, legacy=False):
    doc = {"emp_code": code, "date": day, "status": status,
           "punch_in": utc(day, "09:30:00") if status in ("PRESENT", "WFH", "ON_DUTY") else None,
           "punch_out": None, "work_hours": wh, "late_minutes": late, "overtime_minutes": ot}
    if not legacy:
        doc.update(half_day=half, history=[])
    db.attendance_logs.insert_one(doc)


# ---------------------------------------------------------------- employees --
def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_employee_create_validation_and_duplicate(client):
    e = add_emp(client, "EMP0001")
    assert isinstance(e["created_at"], int) and "_id" not in e
    assert client.post("/employees", json={**e, "created_at": None}).status_code == 409
    bad = {"emp_code": "E1", "name": "x", "email": "x@x.com", "department": "D", "joined_on": "2026-01-01"}
    assert client.post("/employees", json=bad).status_code == 422
    same_shift = {**bad, "emp_code": "EMP0002", "shift_start": "09:00", "shift_end": "09:00"}
    assert client.post("/employees", json=same_shift).status_code == 422


def test_employee_list_pagination(client):
    for i in range(5):
        add_emp(client, f"EMP000{5 - i}", dept="A" if i % 2 else "B")
    r = client.get("/employees", params={"page": 1, "page_size": 2}).json()
    assert [x["emp_code"] for x in r["items"]] == ["EMP0001", "EMP0002"] and r["total"] == 5
    r = client.get("/employees", params={"department": "A"}).json()
    assert r["total"] == 2
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


# ------------------------------------------------------------------ punch-in --
@pytest.mark.parametrize("clock,late", [("09:40:00", 0), ("09:40:01", 10), ("10:15:59", 45), ("09:00:00", 0)])
def test_late_minutes_r2(client, clock, late):
    add_emp(client, "EMP0001")
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", clock)})
    assert r.status_code == 201 and r.json()["late_minutes"] == late


def test_punch_in_truncates_and_validates(client):
    add_emp(client, "EMP0001")
    r = client.post("/attendance/punch-in",
                    json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", "09:40:00") + 900})
    body = r.json()
    assert body["late_minutes"] == 0 and body["punch_in"] % 1000 == 0 and body["history"] == []
    for bad in (1783312500, 1783312500000.0, "1783312500000", True):
        assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404


def test_punch_in_race(client):
    add_emp(client, "EMP0001")
    payload = {"emp_code": "EMP0001", "punched_at": ms("2026-07-06", "09:30:00")}
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: client.post("/attendance/punch-in", json=payload).status_code, range(10)))
    assert sorted(codes) == [201] + [409] * 9


def test_overnight_shift(client):
    add_emp(client, "EMP0005", start="22:00", end="06:00")
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms("2026-07-07", "00:30:00")})
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 150
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms("2026-07-07", "06:40:30")})
    assert r.status_code == 200
    assert r.json()["overtime_minutes"] == 40 and r.json()["work_hours"] == 6.18


# ----------------------------------------------------------------- punch-out --
def test_punch_out_rules(client):
    add_emp(client, "EMP0001")
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", "09:30:00")})
    too_late = client.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                                                          "punched_at": ms("2026-07-07", "09:30:01")})
    assert too_late.status_code == 422
    # 3:00:18 h = 3.005 h -> 3.01 half-up (Python's round(10818 / 3600, 2) gives 3.0)
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", "12:30:18")})
    assert r.json()["work_hours"] == 3.01 and r.json()["overtime_minutes"] == 0 and r.json()["half_day"] is True
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                                                      "punched_at": ms("2026-07-06", "19:00:00")}).status_code == 409


def test_overtime_threshold(client):
    add_emp(client, "EMP0001")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", "09:30:00")})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", "19:00:59")})
    assert r.json()["overtime_minutes"] == 30


def test_punch_out_race_and_half_day(client):
    add_emp(client, "EMP0001")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", "09:30:00")})
    payload = {"emp_code": "EMP0001", "punched_at": ms("2026-07-06", "13:59:00")}
    with ThreadPoolExecutor(8) as pool:
        responses = list(pool.map(lambda _: client.post("/attendance/punch-out", json=payload), range(8)))
    assert sorted(r.status_code for r in responses) == [200] + [409] * 7
    ok = next(r.json() for r in responses if r.status_code == 200)
    assert ok["work_hours"] == 4.48 and ok["half_day"] is True


# -------------------------------------------------------------- regularize --
def test_regularize(client):
    add_emp(client, "EMP0001")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07", "10:05:00")})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07", "18:30:00")})
    url = "/attendance/EMP0001/2026-07-07"
    r = client.patch(url, json={"punch_in": ms("2026-07-07", "09:28:00"), "reason": "biometric glitch",
                                "regularized_by": "hr.admin", "late_minutes": 99})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["late_minutes"] == 0 and len(rec["history"]) == 1
    changes = rec["history"][0]["changes"]
    assert set(changes) == {"punch_in", "late_minutes", "work_hours"}
    assert changes["punch_in"] == {"from": ms("2026-07-07", "10:05:00"), "to": ms("2026-07-07", "09:28:00")}

    same = {"punch_in": ms("2026-07-07", "09:28:00"), "reason": "again!", "regularized_by": "hr"}
    assert client.patch(url, json=same).status_code == 422  # changes nothing
    leave = client.patch(url, json={"status": "LEAVE", "reason": "approved", "regularized_by": "hr"}).json()
    assert leave["punch_in"] is None and leave["work_hours"] is None and len(leave["history"]) == 2
    bad = {"status": "ABSENT", "punch_in": ms("2026-07-07", "09:28:00"), "reason": "bad one", "regularized_by": "hr"}
    assert client.patch(url, json=bad).status_code == 422
    assert client.patch(url, json={"status": "PRESENT", "reason": "no punch", "regularized_by": "hr"}).status_code == 422
    other_day = {"status": "PRESENT", "punch_in": ms("2026-07-08", "09:30:00"), "reason": "wrong", "regularized_by": "h"}
    assert client.patch(url, json=other_day).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-01", json=same).status_code == 404


def test_regularize_concurrent_keeps_history(client):
    add_emp(client, "EMP0001")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07", "10:05:00")})
    bodies = [{"punch_in": ms("2026-07-07", f"09:{m:02d}:00"), "reason": "fix it", "regularized_by": f"u{m}"}
              for m in range(10, 20)]
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda b: client.patch("/attendance/EMP0001/2026-07-07", json=b).status_code, bodies))
    final = client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert codes.count(200) == len(final["history"]) and set(codes) <= {200, 409}


# --------------------------------------------------------------- list ------
def test_attendance_list(client):
    add_emp(client, "EMP0001")
    add_emp(client, "EMP0002")
    for day in ("2026-07-06", "2026-07-07"):
        seed_log("EMP0002", day)
        seed_log("EMP0001", day, legacy=True)
    r = client.get("/attendance", params={"page_size": 3}).json()
    assert [(x["date"], x["emp_code"]) for x in r["items"]] == [
        ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001")]
    assert r["total"] == 4 and r["items"][0]["history"] == [] and r["items"][0]["half_day"] is False
    assert "_id" not in r["items"][0] and "id" not in r["items"][0]
    assert client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-06"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "2026-07-07"}).json()["total"] == 2
    assert client.get("/attendance", params={"status": "NOPE"}).status_code == 422


# ----------------------------------------------------------------- analytics --
def build_month(c):
    """July 2026: 1st is a Wednesday, 23 weekdays."""
    add_emp(c, "EMP0001", dept="Eng")
    add_emp(c, "EMP0002", dept="Eng")
    add_emp(c, "EMP0003", dept="Eng", joined="2026-07-15")   # mid-month joiner
    add_emp(c, "EMP0004", dept="Eng")                        # no logs at all
    add_emp(c, "EMP0005", dept="Ops")
    add_emp(c, "EMP0006", dept="Ops", joined="2026-08-01")  # joins after the month
    add_emp(c, "EMP0007", dept="Sales", joined="2026-08-01")
    seed_log("EMP0001", "2026-07-06", late=30, wh=9.12)
    seed_log("EMP0001", "2026-07-07", status="WFH", wh=3.5, half=True)
    seed_log("EMP0001", "2026-07-11", late=20, ot=45, wh=8.0)  # Saturday
    seed_log("EMP0001", "2026-07-08", status="LEAVE")
    seed_log("EMP0002", "2026-07-06", late=50, wh=8.25)       # ties with EMP0001 (30 + 20)
    seed_log("EMP0003", "2026-07-15", status="ON_DUTY", late=15, legacy=True)
    seed_log("EMP0005", "2026-07-06", late=60, wh=8.0)
    seed_log("EMP0099", "2026-07-06", late=500)               # unknown employee


def test_employee_monthly(client):
    build_month(client)
    r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert r == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 1.5,
                 "leave_days": 1, "late_count": 2, "total_late_minutes": 50, "total_overtime_minutes": 45,
                 "attendance_pct": 6.52}
    r = client.get("/analytics/employees/EMP0003/monthly", params={"month": "2026-07"}).json()
    assert r["working_days"] == 13 and r["attendance_pct"] == 7.69
    r = client.get("/analytics/employees/EMP0006/monthly", params={"month": "2026-07"}).json()
    assert r["working_days"] == 0 and r["attendance_pct"] is None
    assert client.get("/analytics/employees/EMP0404/monthly", params={"month": "2026-07"}).status_code == 404
    assert client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422


def test_department_summary(client):
    build_month(client)
    items = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert [i["department"] for i in items] == ["Eng", "Ops"]  # Sales has headcount 0
    eng = items[0]
    assert eng["headcount"] == 4 and eng["present_days"] == 3.5
    assert eng["avg_work_hours"] == 7.22  # (9.12 + 3.5 + 8.0 + 8.25) / 4 = 7.2175
    assert (eng["late_count"], eng["total_late_minutes"], eng["leave_count"], eng["on_duty_count"]) == (4, 115, 1, 1)
    assert items[1]["headcount"] == 1
    only = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Ops"}).json()
    assert [i["department"] for i in only["items"]] == ["Ops"]


def test_leaderboard_ties(client):
    build_month(client)
    items = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in items] == [
        (1, "EMP0005", 60), (2, "EMP0001", 50), (2, "EMP0002", 50)]
    items = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3}).json()["items"]
    assert [i["rank"] for i in items] == [1, 2, 2]  # next rank would be 4
    items = client.get("/analytics/leaderboard/late",
                       params={"month": "2026-07", "department": "Eng"}).json()["items"]
    assert [(i["rank"], i["emp_code"]) for i in items] == [(1, "EMP0001"), (1, "EMP0002"), (3, "EMP0003")]


def test_department_trend(client):
    build_month(client)
    r = client.get("/analytics/departments/Eng/trend", params={"from": "2026-07-05", "to": "2026-07-16"})
    items = r.json()["items"]
    assert len(items) == 12 and items[0]["date"] == "2026-07-05" and items[-1]["date"] == "2026-07-16"
    sun, mon, tue = items[0], items[1], items[2]
    assert sun["is_working_day"] is False and sun["attendance_rate"] is None and sun["moving_avg_7d"] is None
    assert (mon["headcount"], mon["present_count"], mon["attendance_rate"], mon["moving_avg_7d"]) == (3, 2, 0.6667, 0.6667)
    assert (tue["present_count"], tue["attendance_rate"], tue["moving_avg_7d"]) == (0.5, 0.1667, 0.4167)
    sat = items[6]
    assert sat["date"] == "2026-07-11" and sat["present_count"] == 1 and sat["attendance_rate"] is None
    assert items[10]["date"] == "2026-07-15" and items[10]["headcount"] == 4
    assert client.get("/analytics/departments/Nope/trend",
                      params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert client.get("/analytics/departments/Eng/trend",
                      params={"from": "2026-07-02", "to": "2026-07-01"}).status_code == 422
    assert client.get("/analytics/departments/Eng/trend",
                      params={"from": "2026-01-01", "to": "2026-06-01"}).status_code == 422


# -------------------------------------------------------------------- explain --
@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"status": "LEAVE", "date_from": "2026-07-01"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_index(client, endpoint, params):
    build_month(client)
    r = client.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    text = str(r.json()["explain"])
    assert "IXSCAN" in text and "COLLSCAN" not in text


def test_explain_requires_params(client):
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/nope").status_code == 422
