import os
import math
import calendar
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, EmailStr
import pymongo
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# --------------------------------------------------------------------------- #
# Startup: Create Indexes Idempotently
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    try:
        # Unique business identifier for employee
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        # Department lookups and headcount date filters
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])

        # Natural key for attendance records: exactly one per (emp_code, date)
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        # /attendance listing: sorted by date desc, emp_code asc
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        # Fast lookup of most recent punch_in for punch-out
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        # Analytics filtering by date range and late minutes
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
        db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)])
    except Exception as e:
        print(f"Index creation note: {e}")

# --------------------------------------------------------------------------- #
# Date & Time Utilities
# --------------------------------------------------------------------------- #
MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000

def validate_epoch_ms(val: Optional[int]) -> None:
    if val is not None:
        if not isinstance(val, int) or isinstance(val, bool) or val < MIN_EPOCH_MS or val > MAX_EPOCH_MS:
            raise HTTPException(422, "Invalid epoch milliseconds timestamp")

def ms_to_dt_utc(ms: int) -> datetime:
    validate_epoch_ms(ms)
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=UTC)

def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)

def now_utc_truncated() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)

def round_half_up(val: float, decimals: int = 2) -> float:
    if val is None:
        return None
    q = Decimal("10") ** -decimals
    return float(Decimal(str(val)).quantize(q, rounding=ROUND_HALF_UP))

def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (end_h, end_m, 0):
            att_date = (punch_in_ist.date() - timedelta(days=1)).isoformat()
            return att_date
    return punch_in_ist.date().isoformat()

def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    punch_in_ist = punch_in_utc.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    att_d = date.fromisoformat(attendance_date)
    shift_start_dt = datetime(att_d.year, att_d.month, att_d.day, h, m, 0, tzinfo=IST)
    diff_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(math.floor(diff_sec / 60))
    return 0

def compute_work_hours(punch_in_utc: Optional[datetime], punch_out_utc: Optional[datetime]) -> Optional[float]:
    if punch_out_utc is None or punch_in_utc is None:
        return None
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours = (Decimal(str(diff_sec)) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)

def compute_overtime(punch_out_utc: Optional[datetime], shift_start: str, shift_end: str, attendance_date: str) -> int:
    if punch_out_utc is None:
        return 0
    punch_out_ist = punch_out_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    h, m = map(int, shift_end.split(":"))
    att_d = date.fromisoformat(attendance_date)
    end_d = att_d + timedelta(days=1) if is_overnight else att_d
    shift_end_dt = datetime(end_d.year, end_d.month, end_d.day, h, m, 0, tzinfo=IST)
    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    diff_min = int(math.floor(diff_sec / 60))
    return diff_min if diff_min >= 30 else 0

def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50

def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    history = []
    for h in doc.get("history", []):
        at_val = h.get("at")
        if isinstance(at_val, datetime):
            at_ms = dt_to_ms(at_val)
        else:
            at_ms = at_val
        
        changes = {}
        for k, v in h.get("changes", {}).items():
            from_v = v.get("from")
            to_v = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_v, datetime):
                    from_v = dt_to_ms(from_v)
                if isinstance(to_v, datetime):
                    to_v = dt_to_ms(to_v)
            changes[k] = {"from": from_v, "to": to_v}
            
        history.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes
        })

    p_in = doc.get("punch_in")
    p_out = doc.get("punch_out")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(p_in) if isinstance(p_in, datetime) else p_in,
        "punch_out": dt_to_ms(p_out) if isinstance(p_out, datetime) else p_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history
    }

def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    c_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(c_at) if isinstance(c_at, datetime) else c_at
    }

# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)

class RegularizeIn(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")

    doc = body.model_dump()
    doc["created_at"] = now_utc_truncated()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    except Exception:
        if db.employees.find_one({"emp_code": body.emp_code}):
            raise HTTPException(409, "emp_code already exists")
        raise
    return serialize_employee_doc(doc)

@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    punch_in_utc = ms_to_dt_utc(body.punched_at) if body.punched_at else now_utc_truncated()
    att_date = get_attendance_date(punch_in_utc, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    late_min = compute_late_minutes(punch_in_utc, emp.get("shift_start", "09:30"), att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    except Exception:
        if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": att_date}):
            raise HTTPException(409, "already punched in for this date")
        raise

    return serialize_attendance_doc(doc)

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    punch_out_utc = ms_to_dt_utc(body.punched_at) if body.punched_at else now_utc_truncated()

    # Find the most recent record whose punch_in is at or before punch_out
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(404, "no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "that record is already punched out")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=UTC)

    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    att_date = record["date"]
    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    work_h = compute_work_hours(punch_in_utc, punch_out_utc)
    ot_min = compute_overtime(punch_out_utc, shift_start, shift_end, att_date)
    h_day = compute_half_day(work_h)

    # Concurrency safe update
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_h,
                "overtime_minutes": ot_min,
                "half_day": h_day
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(409, "that record is already punched out")

    return serialize_attendance_doc(updated)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    # Determine candidate new state
    new_status = body.status if body.status is not None else record["status"]
    
    # Check rules
    # Setting status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch_in or punch_out when status is ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            final_punch_in = ms_to_dt_utc(body.punch_in)
        else:
            final_punch_in = record.get("punch_in")
            if final_punch_in and final_punch_in.tzinfo is None:
                final_punch_in = final_punch_in.replace(tzinfo=UTC)

        if final_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        # punch_in must stay on record's attendance date
        shift_start = emp.get("shift_start", "09:30")
        shift_end = emp.get("shift_end", "18:30")
        calc_att_date = get_attendance_date(final_punch_in, shift_start, shift_end)
        if calc_att_date != date_str:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        # punch_out
        if body.punch_out is not None:
            final_punch_out = ms_to_dt_utc(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")
            if final_punch_out and final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=UTC)

        if final_punch_out is not None:
            diff_sec = (final_punch_out - final_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    # Recompute derived fields
    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    if new_status in ("ABSENT", "LEAVE"):
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        final_late_minutes = compute_late_minutes(final_punch_in, shift_start, date_str)
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(final_punch_out, shift_start, shift_end, date_str)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    # Compute changes
    changes = {}
    def check_change(field_name, old_v, new_v):
        if old_v != new_v:
            changes[field_name] = {"from": old_v, "to": new_v}

    old_p_in = record.get("punch_in")
    if old_p_in and old_p_in.tzinfo is None:
        old_p_in = old_p_in.replace(tzinfo=UTC)
    old_p_out = record.get("punch_out")
    if old_p_out and old_p_out.tzinfo is None:
        old_p_out = old_p_out.replace(tzinfo=UTC)

    check_change("status", record.get("status"), new_status)
    check_change("punch_in", old_p_in, final_punch_in)
    check_change("punch_out", old_p_out, final_punch_out)
    check_change("work_hours", record.get("work_hours"), final_work_hours)
    check_change("late_minutes", record.get("late_minutes", 0), final_late_minutes)
    check_change("overtime_minutes", record.get("overtime_minutes", 0), final_overtime_minutes)
    check_change("half_day", record.get("half_day", False), final_half_day)

    if not changes:
        raise HTTPException(422, "a request that changes nothing is 422")

    history_entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Atomically update and append history
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    return serialize_attendance_doc(res)

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    joined_on = emp["joined_on"]

    working_days = 0
    for day in range(1, num_days + 1):
        cur_date = f"{year:04d}-{m:02d}-{day:02d}"
        d_obj = date(year, m, day)
        if d_obj.weekday() < 5 and cur_date >= joined_on:
            working_days += 1

    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6]
                    ]
                },
                "is_present_status": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_present_status"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    agg = res[0] if res else {
        "present_days": 0.0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0
    }

    present_days = float(agg["present_days"])
    if working_days > 0:
        pct = (Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        attendance_pct = float(pct)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": agg["leave_days"],
        "late_count": agg["late_count"],
        "total_late_minutes": agg["total_late_minutes"],
        "total_overtime_minutes": agg["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }

@app.get("/analytics/departments/summary")
def departments_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_emp = {"joined_on": {"$lte": end_date}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp_code"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"}
            }
        },
        {
            "$project": {
                "department": "$_id",
                "_id": 0,
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "logs": 1,
                "is_weekday": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]},
                        False
                    ]
                },
                "is_present_status": {
                    "$cond": [
                        {"$ifNull": ["$logs.status", False]},
                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        False
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$first": "$headcount"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_present_status"]},
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_present_status",
                                    {"$ne": [{"$type": "$logs.work_hours"}, "missing"]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {"$sort": {"_id": 1}}
    ]

    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        wh_list = d.get("work_hours_list", [])
        avg_wh = None
        if wh_list:
            avg_val = Decimal(str(sum(wh_list))) / Decimal(str(len(wh_list)))
            avg_wh = float(avg_val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        items.append({
            "department": d["_id"],
            "headcount": d["headcount"],
            "present_days": float(d["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": d["late_count"],
            "total_late_minutes": d["total_late_minutes"],
            "leave_count": d["leave_count"],
            "on_duty_count": d["on_duty_count"]
        })

    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def leaderboard_late(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=100),
    department: Optional[str] = None
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ]

    if department:
        pipeline.append({"$match": {"department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        }
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    if to_date < from_date:
        raise HTTPException(422, "to date must be >= from date")

    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "Department not found")

    dt_start = datetime.fromisoformat(from_date + "T00:00:00Z")
    dt_end = datetime.fromisoformat(to_date + "T00:00:00Z") + timedelta(days=1)

    # Aggregation with $densify
    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date_dt": {"$literal": dt_start}}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_start, dt_end]
                }
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}}
                ],
                "as": "dept_employees"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date", "emp_codes": "$dept_employees.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$cur_date"]},
                                    {"$in": ["$emp_code", "$$emp_codes"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs"
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6]
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]}
                        }
                    }
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                                    1,
                                    0
                                ]
                            }
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
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
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": 1}}
    ]

    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        att_rate = d.get("attendance_rate")
        if att_rate is not None:
            att_rate = float(Decimal(str(att_rate)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        
        m_avg = d.get("moving_avg_7d")
        if m_avg is not None:
            m_avg = float(Decimal(str(m_avg)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        items.append({
            "date": d["date"],
            "is_working_day": d["is_working_day"],
            "headcount": d["headcount"],
            "present_count": float(d["present_count"]),
            "late_count": d["late_count"],
            "attendance_rate": att_rate,
            "moving_avg_7d": m_avg
        })

    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return Response(content=json_util.dumps(res), media_type="application/json")

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date}
                }
            },
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "half_day": {"$ifNull": ["$half_day", False]}
                }
            }
        ]
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return Response(content=json_util.dumps(res), media_type="application/json")

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        end_date = f"{month}-{num_days:02d}"
        match_emp = {"joined_on": {"$lte": end_date}}
        if department:
            match_emp["department"] = department
        pipeline = [
            {"$match": match_emp},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "logs"
                }
            }
        ]
        res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return Response(content=json_util.dumps(res), media_type="application/json")

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}
                }
            }
        ]
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return Response(content=json_util.dumps(res), media_type="application/json")

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required")
        pipeline = [
            {"$match": {"department": department}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "logs"
                }
            }
        ]
        res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return Response(content=json_util.dumps(res), media_type="application/json")

    else:
        raise HTTPException(422, f"Unknown endpoint '{endpoint}'")

