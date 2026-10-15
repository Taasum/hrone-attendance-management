"""Employee Attendance & Analytics API - HROne assignment contract v2.0."""
import os, re, calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
client = MongoClient(os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"), serverSelectionTimeoutMS=2000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def epoch_valid(v):
    if type(v) is not int or not (100_000_000_000 <= v <= 4_102_444_800_000):
        raise ValueError("must be an epoch-milliseconds integer in the supported range")
    return v

def from_ms(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)

def to_ms(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)

def utc_dt(dt):
    if dt is None: return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))

def shift_datetime(day: str, hhmm: str, next_day=False):
    d = date.fromisoformat(day) + (timedelta(days=1) if next_day else timedelta())
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(d, time(h, m), IST).astimezone(UTC)

def attendance_day(ts, emp):
    local = utc_dt(ts).astimezone(IST)
    start = emp["shift_start"]; end = emp["shift_end"]
    if end <= start and local.strftime("%H:%M") < end:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()

def late_minutes(punch_in, shift_start):
    local = utc_dt(punch_in).astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = int((local - start).total_seconds() // 60)
    return delta if (local - start).total_seconds() > 600 else 0

def derive(emp, pin, pout):
    late = late_minutes(pin, emp["shift_start"]) if pin else 0
    if not pin or not pout: return late, None, 0, False
    seconds = int((utc_dt(pout) - utc_dt(pin)).total_seconds())
    hours = round_half_up(seconds / 3600, 2)
    local_in = utc_dt(pin).astimezone(IST)
    start_h, start_m = map(int, emp["shift_start"].split(":"))
    end_h, end_m = map(int, emp["shift_end"].split(":"))
    overnight = emp["shift_end"] <= emp["shift_start"]
    shift_end = datetime.combine(local_in.date(), time(end_h, end_m), IST)
    if overnight: shift_end += timedelta(days=1)
    # For overnight punch-ins before shift_end, the shift began the previous day.
    if overnight and local_in.strftime("%H:%M") < emp["shift_end"]:
        shift_end = datetime.combine(local_in.date(), time(end_h, end_m), IST)
    overtime_raw = int((utc_dt(pout) - shift_end.astimezone(UTC)).total_seconds() // 60)
    overtime = overtime_raw if overtime_raw >= 30 else 0
    return late, hours, overtime, hours < 4.50

def clean_employee(d):
    d.pop("_id", None)
    if "created_at" in d: d["created_at"] = to_ms(d["created_at"])
    return d

def clean_log(d):
    d.pop("_id", None)
    d.setdefault("history", []); d.setdefault("half_day", False)
    for key in ("punch_in", "punch_out"):
        d[key] = to_ms(d.get(key))
    for h in d.get("history", []):
        h["at"] = to_ms(h.get("at"))
        for ch in h.get("changes", {}).values():
            for key in ("from", "to"):
                if isinstance(ch.get(key), datetime): ch[key] = to_ms(ch[key])
    d.setdefault("work_hours", None); d.setdefault("late_minutes", 0); d.setdefault("overtime_minutes", 0)
    return d

def validate_date(s):
    try: date.fromisoformat(s)
    except Exception: raise HTTPException(422, "date must be YYYY-MM-DD")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s): raise HTTPException(422, "date must be YYYY-MM-DD")
    return s

def month_range(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month): raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-")); first = date(y, m, 1); last = date(y, m, calendar.monthrange(y, m)[1])
    return first.isoformat(), last.isoformat(), last

def working_days(start, end):
    n = 0; d = start
    while d <= end:
        if d.weekday() < 5: n += 1
        d += timedelta(days=1)
    return n

def base_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="attendance_emp_date")
    logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="attendance_status_date")

@app.on_event("startup")
def startup():
    base_indexes()

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date
    @model_validator(mode="after")
    def shift_diff(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v): return None if v is None else epoch_valid(v)

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v): return None if v is None else epoch_valid(v)

class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ms(cls, v): return None if v is None else epoch_valid(v)

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end: raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump(); doc["joined_on"] = body.joined_on.isoformat(); doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try: employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return clean_employee(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q); skip = (page - 1) * page_size
    items = [clean_employee(x) for x in employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "Employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    ts = utc_dt(ts).replace(microsecond=0); d = attendance_day(ts, emp)
    late = late_minutes(ts, emp["shift_start"])
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False, "history": []}
    try: logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "Already punched in for this date")
    return clean_log(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "Employee not found")
    out = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    out = utc_dt(out).replace(microsecond=0)
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": out}}, sort=[("punch_in", DESCENDING)])
    if not rec: raise HTTPException(404, "No punch-in found")
    pin = utc_dt(rec["punch_in"])
    seconds = (out - pin).total_seconds()
    if seconds <= 0 or seconds > 86400: raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late, hours, overtime, half = derive(emp, pin, out)
    updated = logs.find_one_and_update({"_id": rec["_id"], "punch_out": None}, {"$set": {"punch_out": out, "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half}}, return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409, "Already punched out")
    return clean_log(updated)

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if status is not None and status not in STATUSES: raise HTTPException(422, "Invalid status")
    if date_from and date_to and date_to < date_from: raise HTTPException(422, "date_to must be on or after date_from")
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if status: q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from.isoformat()
        if date_to: q["date"]["$lte"] = date_to.isoformat()
    total = logs.count_documents(q); skip = (page - 1) * page_size
    items = [clean_log(x) for x in logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    validate_date(date)
    emp = employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "Employee not found")
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if not rec: raise HTTPException(404, "Attendance record not found")
    old = dict(rec); old.setdefault("history", []); old.setdefault("half_day", False)
    new_status = body.status if body.status is not None else old["status"]
    pin = from_ms(body.punch_in) if body.punch_in is not None else old.get("punch_in")
    pout = from_ms(body.punch_out) if body.punch_out is not None else old.get("punch_out")
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        pin = pout = None
    elif pin is None: raise HTTPException(422, "Presence status requires punch_in")
    if pin is not None:
        if attendance_day(pin, emp) != date: raise HTTPException(422, "punch_in must belong to the record attendance date")
    if pout is not None:
        if pin is None or (utc_dt(pout)-utc_dt(pin)).total_seconds() <= 0 or (utc_dt(pout)-utc_dt(pin)).total_seconds() > 86400:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late, hours, overtime, half = derive(emp, pin, pout)
    if new_status in {"ABSENT", "LEAVE"}: late, hours, overtime, half = 0, None, 0, False
    changes = {}
    proposed = {"status": new_status, "punch_in": pin, "punch_out": pout, "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    for k, newv in proposed.items():
        oldv = old.get(k, False if k == "half_day" else 0 if k in ("late_minutes", "overtime_minutes") else None)
        if k in ("punch_in", "punch_out"): oldv = utc_dt(oldv) if oldv else None; newv = utc_dt(newv) if newv else None
        if oldv != newv: changes[k] = {"from": oldv, "to": newv}
    if not changes: raise HTTPException(422, "Request does not change the record")
    history_entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap prevents concurrent edits from overwriting one another's history.
    query = {"emp_code": emp_code, "date": date}
    if "history" in old: query["history"] = old["history"]
    else: query["history"] = {"$exists": False}
    update = {"$set": {**proposed}, "$push": {"history": history_entry}}
    updated = logs.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409, "Record changed concurrently; retry")
    return clean_log(updated)

# Analytics: all calculations are MongoDB aggregation pipelines. Dates are strings by contract.
def month_match(month):
    first, last, last_date = month_range(month)
    return {"date": {"$gte": first, "$lte": last}}, first, last, last_date

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "Employee not found")
    q, first_s, last_s, last_d = month_match(month)
    joined = max(date.fromisoformat(emp["joined_on"]), date.fromisoformat(first_s))
    working = working_days(joined, last_d) if joined <= last_d else 0
    pipeline = [{"$match": {"emp_code": emp_code, **q}}, {"$group": {"_id": None,
        "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2,3,4,5,6]]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]
    x = list(logs.aggregate(pipeline)); a = x[0] if x else {}
    present = round_half_up(a.get("present_days", 0), 2)
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": present,
            "leave_days": a.get("leave_days", 0), "late_count": a.get("late_count", 0),
            "total_late_minutes": a.get("total_late_minutes", 0), "total_overtime_minutes": a.get("total_overtime_minutes", 0),
            "attendance_pct": round_half_up(present / working * 100, 2) if working else None}

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    first, last, last_d = month_range(month)
    emp_match = {"joined_on": {"$lte": last}}
    if department is not None: emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "dept": "$department"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first]}, {"$lte": ["$date", last]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "employee_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2,3,4,5,6]]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
            "hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$set": {"headcount": {"$size": "$employee_codes"}}}, {"$match": {"headcount": {"$gt": 0}}}, {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, {"$round": [{"$divide": ["$hours_sum", "$hours_count"]}, 2]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}]
    items = list(employees.aggregate(pipeline))
    for item in items: item["present_days"] = round_half_up(item["present_days"], 2)
    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    q, first, last, _ = month_match(month)
    pipeline = [{"$match": q}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}}, {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}]
    return {"month": month, "items": list(logs.aggregate(pipeline, allowDiskUse=True))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    validate_date(from_date); validate_date(to_date)
    start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
    if end < start: raise HTTPException(422, "to must be on or after from")
    if (end-start).days + 1 > 92: raise HTTPException(422, "range cannot exceed 92 days")
    if not employees.find_one({"department": department}): raise HTTPException(404, "Department not found")
    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start, time(), UTC), datetime.combine(end, time(), UTC) + timedelta(days=1)]}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}, "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$round": ["$moving_avg_7d", 4]}]}}}, {"$sort": {"date": 1}}]
    items = list(logs.aggregate(pipeline, allowDiskUse=True))
    # Densify can emit dates as BSON dates; the pipeline converts them to strings.
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
            limit: int = Query(10, ge=1, le=50), date_from: Optional[date] = None, date_to: Optional[date] = None,
            status: Optional[str] = None, from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),
            page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed: raise HTTPException(422, "Unknown endpoint")
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status: q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from.isoformat()
            if date_to: q["date"]["$lte"] = date_to.isoformat()
        exp = logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page-1)*page_size).limit(page_size).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        q, _, _, _ = month_match(month); q["emp_code"] = emp_code
        exp = logs.database.command("explain", {"aggregate": logs.name, "pipeline": [{"$match": q}, {"$group": {"_id": "$emp_code", "total": {"$sum": 1}}}], "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        q, _, _, _ = month_match(month)
        exp = logs.database.command("explain", {"aggregate": logs.name, "pipeline": [{"$match": q}, {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}}], "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        q, _, _, _ = month_match(month)
        exp = logs.database.command("explain", {"aggregate": logs.name, "pipeline": [{"$match": q}, {"$group": {"_id": "$emp_code", "total": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}}], "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
    validate_date(from_date); validate_date(to_date)
    exp = logs.database.command("explain", {"aggregate": logs.name, "pipeline": [{"$match": {"date": {"$gte": from_date, "$lte": to_date}}}], "cursor": {}}, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
