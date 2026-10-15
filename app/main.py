"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, falling back to local defaults)

Implements the OpenAPI contract v2.0.0 and business rules R1 - R10 for HROne.
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Environment variables with environment taking priority
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Timezone: IST (UTC+05:30) per R1
IST = timezone(timedelta(hours=5, minutes=30))

# PyMongo client configured with tz_aware=True so all BSON dates are UTC-aware
client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]


# --------------------------------------------------------------------------- #
# Database Index Setup
# --------------------------------------------------------------------------- #
def init_db_indexes(database):
    """
    Creates all required indexes idempotently at application startup.
    Ensures unique constraints and optimal performance (IXSCAN) for queries.
    """
    # 1. employees: unique emp_code constraint
    database.employees.create_index([("emp_code", 1)], unique=True)
    # Compound index for department filtering and range queries on joined_on
    database.employees.create_index([("department", 1), ("joined_on", 1)])
    database.employees.create_index([("joined_on", 1)])

    # 2. attendance_logs: unique (emp_code, date) natural key (R1)
    database.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    # Compound index for list_attendance sorting by date desc, emp_code asc
    database.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    # Index for punch-out lookup (most recent punch-in for employee)
    database.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    # Index for late leaderboard query plan (IXSCAN on date range + late_minutes)
    database.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes on startup
    try:
        init_db_indexes(db)
    except Exception as e:
        print(f"Warning: Index creation encountered an issue: {e}")
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Business Logic Helpers (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float | Decimal], decimals: int = 2) -> Optional[float]:
    """
    R8: Rounds numbers to specified decimal places using round half-up
    (not banker's rounding, not floating-point rounding).
    """
    if val is None:
        return None
    d = Decimal(str(val)) if isinstance(val, float) else Decimal(val)
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    """
    R1: Instants are truncated to whole seconds (09:40:00.900 counts as 09:40:00).
    The truncated instant is stored and returned.
    """
    return dt.replace(microsecond=0)


def epoch_to_utc_datetime(ms: int) -> datetime:
    """Converts epoch milliseconds to a UTC datetime with whole-second truncation."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_epoch(dt: Optional[datetime]) -> Optional[int]:
    """Converts a datetime (naive UTC or aware) to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_hh_mm(time_str: str) -> tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is strictly earlier than shift_end belongs to the
    shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_in_ist.date()
    start_h, start_m = parse_hh_mm(shift_start)
    end_h, end_m = parse_hh_mm(shift_end)

    is_overnight = (end_h < start_h) or (end_h == start_h and end_m <= start_m)
    if is_overnight:
        punch_time_mins = punch_in_ist.hour * 60 + punch_in_ist.minute
        shift_end_mins = end_h * 60 + end_m
        if punch_time_mins < shift_end_mins:
            ist_date = ist_date - timedelta(days=1)

    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period is 10 minutes. A punch-in is late only if it is strictly
    more than 10:00 after shift_start. When late, late_minutes = whole minutes
    (floor) elapsed since shift_start (not since end of grace period). Otherwise 0.
    Example (shift 09:30): 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    # Shift start is on attendance_date in IST
    start_h, start_m = parse_hh_mm(shift_start)
    att_date = date.fromisoformat(attendance_date_str)
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, start_h, start_m, 0, tzinfo=IST)

    punch_in_trunc = truncate_to_seconds(punch_in_utc).astimezone(IST)
    diff_sec = (punch_in_trunc - shift_start_dt).total_seconds()

    # Grace period is strictly 10 minutes (600 seconds)
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    p_in = truncate_to_seconds(punch_in_utc)
    p_out = truncate_to_seconds(punch_out_utc)
    diff_sec = Decimal(int((p_out - p_in).total_seconds()))
    hours = diff_sec / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(
    punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date_str: str
) -> int:
    """
    R3: Overtime = whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    For overnight shifts, shift_end is on attendance_date + 1 day.
    """
    start_h, start_m = parse_hh_mm(shift_start)
    end_h, end_m = parse_hh_mm(shift_end)
    att_date = date.fromisoformat(attendance_date_str)

    is_overnight = (end_h < start_h) or (end_h == start_h and end_m <= start_m)
    end_date = att_date + timedelta(days=1) if is_overnight else att_date
    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, end_h, end_m, 0, tzinfo=IST)

    punch_out_trunc = truncate_to_seconds(punch_out_utc).astimezone(IST)
    diff_sec = (punch_out_trunc - shift_end_dt).total_seconds()
    if diff_sec >= 1800:  # 30 minutes minimum
        return int(diff_sec // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day = true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Working days are Mon-Fri, no holiday calendar.
    For an employee who joined mid-month, working days are counted from joined_on.
    """
    year, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    m_start = date(year, m, 1)
    m_end = date(year, m, num_days)

    joined_on = date.fromisoformat(joined_on_str)
    start = max(m_start, joined_on)
    if start > m_end:
        return 0

    count = 0
    cur = start
    while cur <= m_end:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += timedelta(days=1)
    return count


def format_history_entry_out(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Formats a stored history entry into API format with epoch milliseconds."""
    out_changes = {}
    for k, v in entry.get("changes", {}).items():
        c_from = v.get("from")
        c_to = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(c_from, datetime):
                c_from = datetime_to_epoch(c_from)
            if isinstance(c_to, datetime):
                c_to = datetime_to_epoch(c_to)
        out_changes[k] = {"from": c_from, "to": c_to}

    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = datetime_to_epoch(at_val)

    return {
        "at": at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": out_changes,
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Converts a MongoDB attendance document into the OpenAPI AttendanceRecord response schema.
    Handles legacy records missing history/half_day, strips _id/id, and converts datetimes to epoch ms.
    """
    punch_in_val = doc.get("punch_in")
    punch_out_val = doc.get("punch_out")

    raw_history = doc.get("history") or []
    formatted_history = [format_history_entry_out(h) for h in raw_history]

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch(punch_in_val) if isinstance(punch_in_val, datetime) else punch_in_val,
        "punch_out": datetime_to_epoch(punch_out_val) if isinstance(punch_out_val, datetime) else punch_out_val,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": formatted_history,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Shared Query and Pipeline Builders (used by Endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q: Dict[str, str] = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status:
        q["status"] = status
    return q


def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "is_present_status": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                },
                "half_day": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$project": {
                "present_val": {
                    "$cond": [
                        {"$and": ["$is_weekday", "$is_present_status"]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                },
                "leave_val": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                "late_val": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
                "late_minutes": 1,
                "overtime_minutes": 1,
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_val"},
                "leave_days": {"$sum": "$leave_val"},
                "late_count": {"$sum": "$late_val"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(
    month: str, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "logs_stats": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {
                            "is_weekday": {
                                "$in": [
                                    {
                                        "$dayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$$log.date"
                                            }
                                        }
                                    },
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                            "is_present_status": {
                                "$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]
                            },
                            "half_day": {"$ifNull": ["$$log.half_day", False]},
                            "status": "$$log.status",
                            "work_hours": "$$log.work_hours",
                            "late_minutes": {"$ifNull": ["$$log.late_minutes", 0]},
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs_stats",
                            "as": "ls",
                            "in": {
                                "$cond": [
                                    {"$and": ["$$ls.is_weekday", "$$ls.is_present_status"]},
                                    {"$cond": ["$$ls.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs_stats",
                            "as": "ls",
                            "in": {"$cond": [{"$gt": ["$$ls.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "total_late_minutes": {"$sum": "$logs_stats.late_minutes"},
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs_stats",
                            "as": "ls",
                            "in": {"$cond": [{"$eq": ["$$ls.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs_stats",
                            "as": "ls",
                            "in": {"$cond": [{"$eq": ["$$ls.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": "$logs_stats",
                                "as": "ls",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                "$$ls.is_present_status",
                                                {"$ne": ["$$ls.work_hours", None]},
                                            ]
                                        },
                                        "$$ls.work_hours",
                                        None,
                                    ]
                                },
                            }
                        },
                        "as": "wh",
                        "cond": {"$ne": ["$$wh", None]},
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "all_work_hours": {"$push": "$work_hours_list"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


def build_department_trend_pipeline(
    emp_codes: List[str],
    joined_on_dates: List[str],
    from_date: str,
    to_date: str,
) -> List[Dict[str, Any]]:
    from_dt = datetime.fromisoformat(f"{from_date}T00:00:00+00:00")
    to_dt = datetime.fromisoformat(f"{to_date}T00:00:00+00:00") + timedelta(days=1)

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$project": {
                "date": 1,
                "dt": {"$dateFromString": {"dateString": "$date"}},
                "is_present": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                },
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": "$dt",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            "$is_present",
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "dt": "$_id",
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$dt"},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": joined_on_dates,
                            "as": "j",
                            "cond": {"$lte": ["$$j", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """
    Readiness probe: returns 200 only if MongoDB answers a ping, otherwise 503.
    """
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Enforces unique emp_code via database index (returns 409 on conflict).
    Validates shift_start != shift_end (returns 422).
    Stores created_at as BSON datetime UTC, returns epoch ms.
    """
    if body.shift_start == body.shift_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="shift_start must differ from shift_end",
        )

    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Employee with emp_code '{body.emp_code}' already exists",
        )

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch(now_utc),
    }


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List employees sorted by emp_code ascending.
    total reflects the filtered set (R10).
    """
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort([("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = []
    for emp in cursor:
        items.append(
            {
                "emp_code": emp["emp_code"],
                "name": emp["name"],
                "email": emp["email"],
                "department": emp["department"],
                "shift_start": emp["shift_start"],
                "shift_end": emp["shift_end"],
                "joined_on": emp["joined_on"],
                "created_at": datetime_to_epoch(emp.get("created_at")),
            }
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Record a punch-in. Creates the day's record (R1, R2).
    Race-safe: concurrent duplicate punch-ins yield exactly one 201 and the rest 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{body.emp_code}' not found",
        )

    if body.punched_at is not None:
        punch_in_utc = epoch_to_utc_datetime(body.punched_at)
    else:
        punch_in_utc = truncate_to_seconds(datetime.now(timezone.utc))

    att_date = determine_attendance_date(
        punch_in_utc, emp["shift_start"], emp["shift_end"]
    )
    late_mins = compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already punched in for date {att_date}",
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes the employee's most recent record whose punch_in <= punched_at.
    Computes work_hours (R4), overtime_minutes (R3) and half_day (R5).
    Works across midnight for overnight shifts.
    Race-safe: two simultaneous punch-outs yield exactly one 200 and one 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{body.emp_code}' not found",
        )

    if body.punched_at is not None:
        punch_out_utc = epoch_to_utc_datetime(body.punched_at)
    else:
        punch_out_utc = truncate_to_seconds(datetime.now(timezone.utc))

    # Find the most recent record whose punch_in <= punch_out_utc
    rec = (
        db.attendance_logs.find(
            {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}}
        )
        .sort([("punch_in", -1)])
        .limit(1)
    )
    rec_list = list(rec)
    if not rec_list:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No matching punch-in found for punch-out",
        )

    target_rec = rec_list[0]
    if target_rec.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out",
        )

    punch_in_utc = target_rec["punch_in"]
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out must be strictly after punch_in",
        )

    duration_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if duration_sec > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out cannot be more than 24 hours after punch_in",
        )

    # Compute derived fields (R3, R4, R5)
    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    ot_mins = compute_overtime_minutes(
        punch_out_utc, emp["shift_start"], emp["shift_end"], target_rec["date"]
    )
    half_day = compute_half_day(work_hrs)

    update_fields = {
        "punch_out": punch_out_utc,
        "work_hours": work_hrs,
        "overtime_minutes": ot_mins,
        "half_day": half_day,
    }

    # Atomic conditional update prevents punch-out race conditions
    res = db.attendance_logs.update_one(
        {"_id": target_rec["_id"], "punch_out": None},
        {"$set": update_fields},
    )
    if res.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record was concurrently punched out",
        )

    target_rec.update(update_fields)
    return format_attendance_doc(target_rec)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[str] = Query(
        default=None, alias="status", pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Indexed and paginated for speed on 100k records.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to",
        )

    q = build_attendance_list_query(emp_code, date_from, date_to, status_filter)
    total = db.attendance_logs.count_documents(q)

    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields, validates attendance rules, appends history audit entry.
    Optimistic concurrency protection against silent lost updates.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{emp_code}' not found",
        )

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No attendance record found for employee '{emp_code}' on {date}",
        )

    target_status = body.status if body.status is not None else rec["status"]

    # Rule: setting status ABSENT or LEAVE clears both punch times,
    # so supplying punch_in or punch_out with it is 422
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = epoch_to_utc_datetime(body.punch_in)
        else:
            new_punch_in = rec.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in instant",
            )

        # punch_in must stay on the record's attendance date (R1)
        computed_att_date = determine_attendance_date(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if computed_att_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in belongs to attendance date {computed_att_date}, but path date is {date}",
            )

        new_late_minutes = compute_late_minutes(
            new_punch_in, emp["shift_start"], date
        )

        if body.punch_out is not None:
            new_punch_out = epoch_to_utc_datetime(body.punch_out)
        else:
            new_punch_out = rec.get("punch_out")

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in",
                )
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime_minutes(
                new_punch_out, emp["shift_start"], emp["shift_end"], date
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compute changes
    stored_changes: Dict[str, Dict[str, Any]] = {}
    old_status = rec["status"]
    if old_status != target_status:
        stored_changes["status"] = {"from": old_status, "to": target_status}

    old_p_in = rec.get("punch_in")
    if old_p_in != new_punch_in:
        stored_changes["punch_in"] = {"from": old_p_in, "to": new_punch_in}

    old_p_out = rec.get("punch_out")
    if old_p_out != new_punch_out:
        stored_changes["punch_out"] = {"from": old_p_out, "to": new_punch_out}

    old_wh = rec.get("work_hours")
    if old_wh != new_work_hours:
        stored_changes["work_hours"] = {"from": old_wh, "to": new_work_hours}

    old_lm = rec.get("late_minutes", 0)
    if old_lm != new_late_minutes:
        stored_changes["late_minutes"] = {"from": old_lm, "to": new_late_minutes}

    old_ot = rec.get("overtime_minutes", 0)
    if old_ot != new_overtime_minutes:
        stored_changes["overtime_minutes"] = {"from": old_ot, "to": new_overtime_minutes}

    old_hd = rec.get("half_day", False)
    if old_hd != new_half_day:
        stored_changes["half_day"] = {"from": old_hd, "to": new_half_day}

    if not stored_changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Regularization request made no changes to the record",
        )

    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    new_history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": stored_changes,
    }

    update_fields = {
        "status": target_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    # Optimistic concurrency: history must match current history array to avoid silent loss
    existing_history = rec.get("history") or []
    res = db.attendance_logs.update_one(
        {
            "emp_code": emp_code,
            "date": date,
            "history": existing_history,
        },
        {
            "$set": update_fields,
            "$push": {"history": new_history_entry},
        },
    )

    if res.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected on attendance record",
        )

    updated_doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    return format_attendance_doc(updated_doc)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee backed by MongoDB aggregation.
    R7 working days, R5-R7 present days (half day = 0.5, Mon-Fri only).
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{emp_code}' not found",
        )

    working_days = count_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
):
    """
    Department summary for a month via MongoDB aggregation across collections.
    Headcount per R9 (people with zero logs still count, people who join later do not).
    """
    pipeline = build_department_summary_pipeline(month, department)
    agg_res = list(db.employees.aggregate(pipeline))

    items = []
    for r in agg_res:
        raw_wh_lists = r.get("all_work_hours", [])
        flat_wh = [wh for sub in raw_wh_lists for wh in sub if wh is not None]
        avg_wh = (
            round_half_up(sum(flat_wh) / len(flat_wh), 2)
            if len(flat_wh) > 0
            else None
        )

        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": float(r["present_days"]),
                "avg_work_hours": avg_wh,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    """
    Late-comers leaderboard for a month.
    Uses standard competition ranking ($rank) where ties share a rank and the next
    rank is skipped (1, 2, 2, 4). limit applies after ranking.
    """
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department with gap-filling ($densify) and 7-day moving average.
    Returns exactly one row per calendar day from 'from' to 'to' inclusive.
    """
    if to_date < from_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must be greater than or equal to 'from' date",
        )

    f_d = date.fromisoformat(from_date)
    t_d = date.fromisoformat(to_date)
    if (t_d - f_d).days + 1 > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 92 days",
        )

    emp_docs = list(
        db.employees.find(
            {"department": department}, {"emp_code": 1, "joined_on": 1}
        )
    )
    if not emp_docs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Department '{department}' not found",
        )

    emp_codes = [e["emp_code"] for e in emp_docs]
    joined_on_dates = [e["joined_on"] for e in emp_docs]

    pipeline = build_department_trend_pipeline(
        emp_codes, joined_on_dates, from_date, to_date
    )
    res = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in res:
        rate = round_half_up(r.get("attendance_rate"), 4)
        m_avg = round_half_up(r.get("moving_avg_7d"), 4)
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": float(r["present_count"]),
                "late_count": int(r["late_count"]),
                "attendance_rate": rate,
                "moving_avg_7d": m_avg,
            }
        )

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    MongoDB explain for an endpoint's main query with executionStats verbosity.
    Uses the exact shared pipeline/query builder to guarantee consistency.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date_from cannot be greater than date_to",
            )
        q = build_attendance_list_query(emp_code, date_from, date_to, status_filter)
        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", -1), ("emp_code", 1)])
            .skip(skip)
            .limit(page_size)
        )
        raw_exp = cursor.explain()
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(raw_exp),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required for employee_monthly explain",
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        raw_exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(raw_exp),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for department_summary explain",
            )
        pipeline = build_department_summary_pipeline(month, department)
        raw_exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": jsonable_encoder(raw_exp),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for late_leaderboard explain",
            )
        pipeline = build_late_leaderboard_pipeline(month, limit or 10, department)
        raw_exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(raw_exp),
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required for department_trend explain",
            )
        if to_date < from_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="'to' date must be >= 'from' date",
            )
        emp_docs = list(
            db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1})
        )
        emp_codes = [e["emp_code"] for e in emp_docs]
        joined_on_dates = [e["joined_on"] for e in emp_docs]
        pipeline = build_department_trend_pipeline(
            emp_codes, joined_on_dates, from_date, to_date
        )
        raw_exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(raw_exp),
        }
