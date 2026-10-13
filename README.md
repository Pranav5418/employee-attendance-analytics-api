# Employee Attendance & Analytics API

A REST API for employee attendance tracking and analytics, built with **FastAPI** and **MongoDB** using PyMongo.

## Requirements

- Python 3.13+
- MongoDB 6.0+

## Setup

Create and activate a virtual environment.

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and configure `MONGO_URI` and `MONGO_DB` for your MongoDB instance. Do not commit `.env` or database credentials.

Ensure MongoDB is running, then start the API:

```bash
uvicorn app.main:app --port 8000
```

Interactive API documentation: http://localhost:8000/docs

## Tests

Install development dependencies and run the test suite:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -x -vv
```

The tests cover validation, attendance rules, concurrency, regularization, monthly analytics, department summaries, trends, leaderboards, and query-plan index usage.

## Design Notes

- Unique indexes protect employee creation and attendance punch-in.
- Conditional updates protect punch-out from concurrent requests.
- Optimistic concurrency control protects attendance regularization history.
- Exact rounding is used for work-hour and analytics calculations.
- MongoDB indexes are created at application startup.

See [DECISIONS.md](DECISIONS.md) for implementation decisions and [REVIEW.md](REVIEW.md) for the code review.
