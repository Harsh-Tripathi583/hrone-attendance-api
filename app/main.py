"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (local .env supported; real environment variables take precedence)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Configuration & Database Connection
# --------------------------------------------------------------------------- #
# load_dotenv without override so real environment variables take precedence
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}


def create_indexes() -> None:
    """Creates all candidate application indexes idempotently at startup."""
    # Employees collection
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_emp_dept_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_emp_dept_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="idx_emp_joined_on")

    # Attendance logs collection
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_attendance_unique")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_attendance_list_sort")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)], name="idx_attendance_date_late")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_attendance_emp_punch_in")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure candidate indexes are created idempotently
    try:
        create_indexes()
    except Exception as e:
        print(f"Warning: Index creation at startup failed or skipped: {e}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Shared Calculation & Conversion Helpers (R1 - R5, R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[Union[float, Decimal]], decimals: int = 2) -> Optional[float]:
    """R8: Rounding of reported numbers: 2 decimals half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val)) if not isinstance(val, Decimal) else val
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def truncate_epoch_ms(ms: int) -> int:
    """R1: Instants are truncated to whole seconds before calculation, storage, and return."""
    return (ms // 1000) * 1000


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a UTC BSON datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """Truncates epoch ms to whole seconds and returns a UTC datetime."""
    truncated_ms = truncate_epoch_ms(ms)
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def parse_hh_mm(hh_mm: str) -> tuple[int, int]:
    h, m = map(int, hh_mm.split(":"))
    return h, m


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in.astimezone(IST)
    cal_date = punch_in_ist.date()

    sh_h, sh_m = parse_hh_mm(shift_start)
    eh_h, eh_m = parse_hh_mm(shift_end)

    if (eh_h, eh_m) <= (sh_h, sh_m):
        # Overnight shift
        end_time = time(eh_h, eh_m)
        if punch_in_ist.time() < end_time:
            return (cal_date - timedelta(days=1)).isoformat()

    return cal_date.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. A punch-in is late only if it is strictly more than 10:00
    after shift_start; then late_minutes = whole minutes (floor) since shift_start,
    not since the grace period ended. Otherwise 0.
    """
    punch_in_ist = punch_in.astimezone(IST)
    y, m, d = map(int, attendance_date.split("-"))
    sh_h, sh_m = parse_hh_mm(shift_start)
    shift_start_dt = datetime(y, m, d, sh_h, sh_m, 0, tzinfo=IST)

    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_seconds > 600:  # strictly more than 10 minutes
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = diff_sec / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    """
    R3: Whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0.
    R1: Shift end of an overnight shift falls on the next calendar day.
    """
    punch_out_ist = punch_out.astimezone(IST)
    y, m, d = map(int, attendance_date.split("-"))
    sh_h, sh_m = parse_hh_mm(shift_start)
    eh_h, eh_m = parse_hh_mm(shift_end)

    shift_end_dt = datetime(y, m, d, eh_h, eh_m, 0, tzinfo=IST)
    if (eh_h, eh_m) <= (sh_h, sh_m):
        shift_end_dt += timedelta(days=1)

    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    diff_min = int(diff_sec // 60)
    return diff_min if diff_min >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day = true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def calculate_working_days(joined_on: str, month: str) -> int:
    """
    R7: Monday - Friday, no holiday calendar; counted from joined_on for mid-month joiners.
    """
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    first_of_month = date(y, m, 1)
    last_of_month = date(y, m, num_days)

    joined_date = date.fromisoformat(joined_on)
    start_date = max(first_of_month, joined_date)
    if start_date > last_of_month:
        return 0

    count = 0
    cur = start_date
    while cur <= last_of_month:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


def last_day_of_month(month: str) -> int:
    y, m = map(int, month.split("-"))
    return calendar.monthrange(y, m)[1]


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(created_at) if isinstance(created_at, datetime) else created_at,
    }


def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")

    # Serialize history array
    history_out = []
    for h in doc.get("history") or []:
        h_at = h.get("at")
        changes_out = {}
        for field, change in (h.get("changes") or {}).items():
            fr = change.get("from")
            to = change.get("to")
            if field in ("punch_in", "punch_out"):
                fr = to_epoch_ms(fr) if isinstance(fr, datetime) else fr
                to = to_epoch_ms(to) if isinstance(to, datetime) else to
            changes_out[field] = {"from": fr, "to": to}

        history_out.append({
            "at": to_epoch_ms(h_at) if isinstance(h_at, datetime) else h_at,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_out,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(punch_in) if isinstance(punch_in, datetime) else punch_in,
        "punch_out": to_epoch_ms(punch_out) if isinstance(punch_out, datetime) else punch_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shift_difference(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    now_utc = from_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    return {
        "items": [serialize_employee(x) for x in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = from_epoch_ms(body.punched_at)
    else:
        punch_in_utc = from_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))

    attendance_date = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_utc, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already punched in for this date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = from_epoch_ms(body.punched_at)
    else:
        punch_out_utc = from_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))

    # Find the employee's most recent record whose punch_in <= punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )

    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="that record is already punched out")

    punch_in_utc = rec["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punched_at must be after punch_in and within 24 hours")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_min = compute_overtime(punch_out_utc, emp["shift_end"], emp["shift_start"], rec["date"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_utc,
            "work_hours": work_hours,
            "overtime_minutes": overtime_min,
            "half_day": half_day,
        }},
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="that record is already punched out")

    return serialize_attendance(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must be <= date_to")

    if status_filter and status_filter not in ALL_STATUSES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid status")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status_filter:
        q["status"] = status_filter
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [serialize_attendance(x) for x in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    # 1. Evaluate target status & punch times
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="setting status ABSENT or LEAVE clears both punch times")
        target_status = body.status
        target_punch_in = None
        target_punch_out = None
    else:
        target_status = body.status if body.status is not None else rec["status"]
        if target_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Cannot provide punch times for ABSENT or LEAVE")
            target_punch_in = None
            target_punch_out = None
        else:
            # Presence status requires a punch_in
            if body.punch_in is not None:
                target_punch_in = from_epoch_ms(body.punch_in)
            else:
                target_punch_in = rec.get("punch_in")

            if target_punch_in is None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A presence status requires a punch_in")

            if target_punch_in.tzinfo is None:
                target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

            # punch_in must stay on the record's attendance date (R1)
            eval_date = get_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
            if eval_date != date:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_in must stay on the record's attendance date")

            # punch_out
            if body.punch_out is not None:
                target_punch_out = from_epoch_ms(body.punch_out)
            else:
                target_punch_out = rec.get("punch_out")

            if target_punch_out is not None:
                if target_punch_out.tzinfo is None:
                    target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)
                diff_sec = (target_punch_out - target_punch_in).total_seconds()
                if diff_sec <= 0 or diff_sec > 86400:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be after punch_in and within 24 hours")

    # 2. Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime(target_punch_out, emp["shift_end"], emp["shift_start"], date)
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    # 3. Detect changes
    changes_db: Dict[str, Any] = {}
    changes_api: Dict[str, Any] = {}

    def record_change(field_name: str, old_val: Any, new_val: Any, is_instant: bool = False):
        if is_instant:
            old_ms = to_epoch_ms(old_val) if isinstance(old_val, datetime) else old_val
            new_ms = to_epoch_ms(new_val) if isinstance(new_val, datetime) else new_val
            if old_ms != new_ms:
                changes_db[field_name] = {"from": old_val, "to": new_val}
                changes_api[field_name] = {"from": old_ms, "to": new_ms}
        else:
            if old_val != new_val:
                changes_db[field_name] = {"from": old_val, "to": new_val}
                changes_api[field_name] = {"from": old_val, "to": new_val}

    record_change("status", rec.get("status"), target_status)
    record_change("punch_in", rec.get("punch_in"), target_punch_in, is_instant=True)
    record_change("punch_out", rec.get("punch_out"), target_punch_out, is_instant=True)
    record_change("work_hours", rec.get("work_hours"), target_work_hours)
    record_change("late_minutes", rec.get("late_minutes", 0), target_late_minutes)
    record_change("overtime_minutes", rec.get("overtime_minutes", 0), target_overtime_minutes)
    record_change("half_day", rec.get("half_day", False), target_half_day)

    if not changes_db:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="a request that changes nothing is 422")

    # 4. Construct audit history entry
    now_utc = from_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # 5. Optimistic locking filter
    curr_history = rec.get("history") or []
    if len(curr_history) == 0:
        filter_q = {
            "_id": rec["_id"],
            "$or": [
                {"history": {"$size": 0}},
                {"history": {"$exists": False}},
            ],
        }
    else:
        filter_q = {
            "_id": rec["_id"],
            "history": {"$size": len(curr_history)},
        }

    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": target_punch_in,
            "punch_out": target_punch_out,
            "work_hours": target_work_hours,
            "late_minutes": target_late_minutes,
            "overtime_minutes": target_overtime_minutes,
            "half_day": target_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated = db.attendance_logs.find_one_and_update(filter_q, update_doc, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent regularizations conflict")

    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    working_days = calculate_working_days(emp["joined_on"], month)
    last_day = last_day_of_month(month)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": month_start, "$lte": month_end},
        }},
        {"$addFields": {
            "dateObj": {"$dateFromString": {"dateString": "$date"}},
            "isPresence": {"$in": ["$status", list(PRESENCE_STATUSES)]},
            "isHalfDay": {"$ifNull": ["$half_day", False]},
            "lateMin": {"$ifNull": ["$late_minutes", 0]},
            "otMin": {"$ifNull": ["$overtime_minutes", 0]},
        }},
        {"$addFields": {
            "dayOfWeek": {"$dayOfWeek": "$dateObj"},
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            "$isPresence",
                            {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                        ]},
                        {"$cond": ["$isHalfDay", 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$lateMin", 0]}, 1, 0]}
            },
            "total_late_minutes": {"$sum": "$lateMin"},
            "total_overtime_minutes": {"$sum": "$otMin"},
        }},
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if not res:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0
    else:
        row = res[0]
        present_days = round_half_up(row["present_days"], 2)
        leave_days = row["leave_days"]
        late_count = row["late_count"]
        total_late_minutes = row["total_late_minutes"]
        total_overtime_minutes = row["total_overtime_minutes"]

    if working_days == 0:
        attendance_pct = None
    else:
        pct_raw = (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100)
        attendance_pct = float(pct_raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    last_day = last_day_of_month(month)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"codes": "$emp_codes"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$in": ["$emp_code", "$$codes"]},
                            {"$gte": ["$date", month_start]},
                            {"$lte": ["$date", month_end]},
                        ]
                    }
                }},
                {"$addFields": {
                    "dateObj": {"$dateFromString": {"dateString": "$date"}},
                    "isPresence": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                    "isHalfDay": {"$ifNull": ["$half_day", False]},
                    "lateMin": {"$ifNull": ["$late_minutes", 0]},
                }},
                {"$addFields": {
                    "dayOfWeek": {"$dayOfWeek": "$dateObj"},
                }},
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": "$_id",
            "headcount": 1,
            "present_days": {
                "$reduce": {
                    "input": "$logs",
                    "initialValue": 0.0,
                    "in": {
                        "$add": [
                            "$$value",
                            {"$cond": [
                                {"$and": [
                                    "$$this.isPresence",
                                    {"$in": ["$$this.dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]},
                                {"$cond": ["$$this.isHalfDay", 0.5, 1.0]},
                                0.0,
                            ]},
                        ]
                    },
                }
            },
            "work_hours_list": {
                "$filter": {
                    "input": "$logs",
                    "as": "l",
                    "cond": {
                        "$and": [
                            "$$l.isPresence",
                            {"$ne": ["$$l.work_hours", None]},
                        ]
                    },
                }
            },
            "late_count": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {"$gt": ["$$l.lateMin", 0]},
                    }
                }
            },
            "total_late_minutes": {
                "$reduce": {
                    "input": "$logs",
                    "initialValue": 0,
                    "in": {"$add": ["$$value", "$$this.lateMin"]},
                }
            },
            "leave_count": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {"$eq": ["$$l.status", "LEAVE"]},
                    }
                }
            },
            "on_duty_count": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                    }
                }
            },
        }},
        {"$addFields": {
            "avg_work_hours": {
                "$cond": [
                    {"$gt": [{"$size": "$work_hours_list"}, 0]},
                    {"$avg": "$work_hours_list.work_hours"},
                    None,
                ]
            }
        }},
        {"$project": {"work_hours_list": 0, "_id": 0}},
        {"$sort": {"department": 1}},
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for it in raw_items:
        items.append({
            "department": it["department"],
            "headcount": it["headcount"],
            "present_days": round_half_up(it["present_days"], 2),
            "avg_work_hours": round_half_up(it["avg_work_hours"], 2),
            "late_count": it["late_count"],
            "total_late_minutes": it["total_late_minutes"],
            "leave_count": it["leave_count"],
            "on_duty_count": it["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    last_day = last_day_of_month(month)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {"$match": {
            "date": {"$gte": month_start, "$lte": month_end},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$project": {
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
            "_id": 0,
        }},
    ]

    if department:
        pipeline.append({"$match": {"department": department}})

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}},
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date format YYYY-MM-DD")

    if d_to < d_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to must be >= from")

    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="the range exceeds 92 days")

    # 404 for unknown department (no employees)
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    emp_codes = [e["emp_code"] for e in dept_emps]

    dt_start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    dt_end_plus1 = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {"$documents": [{"dateObj": dt_start}]},
        {"$densify": {
            "field": "dateObj",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [dt_start, dt_end_plus1],
            },
        }},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dateObj"}},
            "dayOfWeek": {"$dayOfWeek": "$dateObj"},
        }},
        {"$addFields": {
            "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"d_str": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$d_str"]},
                        ]
                    }
                }}
            ],
            "as": "active_emps",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d_str": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$in": ["$emp_code", emp_codes]},
                            {"$eq": ["$date", "$$d_str"]},
                        ]
                    }
                }}
            ],
            "as": "logs",
        }},
        {"$project": {
            "date": 1,
            "is_working_day": 1,
            "headcount": {"$size": "$active_emps"},
            "present_count": {
                "$reduce": {
                    "input": "$logs",
                    "initialValue": 0.0,
                    "in": {
                        "$add": [
                            "$$value",
                            {"$cond": [
                                {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                                {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]},
                                0.0,
                            ]},
                        ]
                    },
                }
            },
            "late_count": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                    }
                }
            },
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None,
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, "current"],
                    },
                }
            },
        }},
        {"$project": {"_id": 0}},
    ]

    raw_items = list(db.aggregate(pipeline))
    items = []
    for it in raw_items:
        items.append({
            "date": it["date"],
            "is_working_day": it["is_working_day"],
            "headcount": it["headcount"],
            "present_count": round_half_up(it["present_count"], 2),
            "late_count": it["late_count"],
            "attendance_rate": round_half_up(it["attendance_rate"], 4),
            "moving_avg_7d": round_half_up(it["moving_avg_7d"], 4),
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status_filter:
            q["status"] = status_filter
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="emp_code and month required")

        last_day = last_day_of_month(month)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }},
            {"$group": {"_id": None, "count": {"$sum": 1}}},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month required")

        last_day = last_day_of_month(month)
        month_end = f"{month}-{last_day:02d}"
        emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department:
            emp_match["department"] = department

        pipeline = [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month required")

        last_day = last_day_of_month(month)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = [
            {"$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department, from, to required")

        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown endpoint")
