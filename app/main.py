import calendar
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal
from bson import Decimal128, ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

logger = logging.getLogger(__name__)

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ['PRESENT', 'WFH', 'ON_DUTY']
Status = Literal['PRESENT', 'ABSENT', 'LEAVE', 'WFH', 'ON_DUTY']
PresenceStatus = Literal['PRESENT', 'WFH', 'ON_DUTY']
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]
client = MongoClient(os.getenv('MONGO_URI', 'mongodb://localhost:27017'), tz_aware=True, connect=False,serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=15000)
db = client[os.getenv('MONGO_DB', 'attendance_db')]


def create_indexes():
    db.employees.create_indexes([
        IndexModel([('emp_code', 1)], unique=True, name='employee_code'),
        IndexModel([('department', 1), ('emp_code', 1)], name='employee_department_code'),
        IndexModel([('joined_on', 1), ('department', 1)], name='employee_joined_department'),
        IndexModel([('department', 1), ('joined_on', 1)], name='employee_department_joined'),
    ])
    db.attendance_logs.create_indexes([
        IndexModel([('emp_code', 1), ('date', -1)], unique=True, name='attendance_employee_date'),
        IndexModel([('date', -1), ('emp_code', 1)], name='attendance_date_employee'),
        IndexModel([('status', 1), ('date', -1), ('emp_code', 1)], name='attendance_status_date_employee'),
        IndexModel([('emp_code', 1), ('punch_in', -1)], name='attendance_employee_punch'),
    ])


@asynccontextmanager
async def lifespan(app):
    try:
        create_indexes()
        app.state.indexes_ready = True
    except PyMongoError as exc:
        logger.error("MongoDB index initialization failed (%s); check connectivity, permissions and existing indexes", type(exc).__name__)
        app.state.indexes_ready = False
    yield
    client.close()


app = FastAPI(title='Employee Attendance & Analytics API', version='2.0.0', lifespan=lifespan)
app.state.indexes_ready = False


@app.exception_handler(PyMongoError)
async def database_error(request, exc):
    return JSONResponse(status_code=503, content={'detail': 'Database unavailable'})


@app.middleware('http')
async def require_indexes(request, call_next):
    if request.url.path not in ('/health', '/docs', '/redoc', '/openapi.json') and not app.state.indexes_ready:
        return JSONResponse(status_code=503, content={'detail': 'Database is not ready; check /health'})
    return await call_next(request)


def valid_date(value: str) -> str:
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('Expected YYYY-MM-DD')
    date.fromisoformat(value)
    return value


def valid_month(value: str) -> str:
    if not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', value):
        raise ValueError('Expected YYYY-MM')
    date.fromisoformat(value + '-01')
    return value


DateString = Annotated[str, AfterValidator(valid_date)]
MonthString = Annotated[str, AfterValidator(valid_month)]
ShiftTime = Annotated[str, Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r'^EMP[0-9]{4,6}$')
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = '09:30'
    shift_end: ShiftTime = '18:30'
    joined_on: DateString

    @model_validator(mode='after')
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError('shift_start must differ from shift_end')
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = Field(default_factory=lambda: epoch_ms(now()))


class PunchInIn(PunchOutIn):
    status: PresenceStatus = 'PRESENT'


class RegularizeIn(BaseModel):
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode='after')
    def no_explicit_nulls(self):
        for key in ('status', 'punch_in', 'punch_out'):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f'{key} cannot be null; omit it to preserve its value')
        return self


def invalid(message):
    raise HTTPException(422, [{'type': 'value_error', 'loc': ['request'], 'msg': message}])


def now():
    return datetime.now(UTC).replace(microsecond=0)


def from_epoch_ms(milliseconds):
    return EPOCH + timedelta(seconds=milliseconds // 1000)


def as_utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def epoch_ms(value):
    delta = as_utc(value) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def json_value(value, *, preserve_id=False):
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {k: json_value(v, preserve_id=preserve_id)
                for k, v in value.items() if preserve_id or k != '_id'}
    if isinstance(value, list):
        return [json_value(v, preserve_id=preserve_id) for v in value]
    return value


DEFAULTS = {'punch_in': None, 'punch_out': None, 'work_hours': None,
            'late_minutes': 0, 'overtime_minutes': 0, 'half_day': False, 'history': []}
MUTABLE = ('status', 'punch_in', 'punch_out', 'work_hours', 'late_minutes', 'overtime_minutes', 'half_day')


def attendance_json(doc):
    return json_value({**DEFAULTS, **doc})


def get_employee(emp_code):
    employee = db.employees.find_one({'emp_code': emp_code})
    if employee is None:
        raise HTTPException(404, 'Employee not found')
    return employee


def attendance_date(punch_in, employee):
    local = as_utc(punch_in).astimezone(IST).replace(microsecond=0)
    day = local.date()
    if employee['shift_end'] <= employee['shift_start'] and local.time() < time.fromisoformat(employee['shift_end']):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(day, employee):
    start = datetime.combine(date.fromisoformat(day), time.fromisoformat(employee['shift_start']), IST)
    end = datetime.combine(date.fromisoformat(day), time.fromisoformat(employee['shift_end']), IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def compute_work_hours(punch_in, punch_out):
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return float((Decimal(seconds) / 3600).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def compute_attendance_fields(doc, employee):
    if doc['status'] not in PRESENCE:
        return {'punch_in': None, 'punch_out': None, 'work_hours': None,
                'late_minutes': 0, 'overtime_minutes': 0, 'half_day': False}
    punch_in, punch_out = doc.get('punch_in'), doc.get('punch_out')
    if punch_in is None:
        invalid('A presence status requires punch_in')
    punch_in = as_utc(punch_in).replace(microsecond=0)
    if attendance_date(punch_in, employee) != doc['date']:
        invalid('punch_in must remain on the attendance date')
    start, end = shift_bounds(doc['date'], employee)
    late_seconds = int((punch_in - start).total_seconds())
    result = {'punch_in': punch_in, 'punch_out': None, 'work_hours': None,
              'late_minutes': late_seconds // 60 if late_seconds > 600 else 0,
              'overtime_minutes': 0, 'half_day': False}
    if punch_out is not None:
        punch_out = as_utc(punch_out).replace(microsecond=0)
        elapsed = (punch_out - punch_in).total_seconds()
        if not 0 < elapsed <= 86400:
            invalid('punch_out must be after punch_in and within 24 hours')
        hours = compute_work_hours(punch_in, punch_out)
        overtime = int((punch_out - end).total_seconds() // 60)
        result.update(punch_out=punch_out, work_hours=hours, half_day=hours < 4.50,
                      overtime_minutes=overtime if overtime >= 30 else 0)
    return result


def snapshot_filter(doc):
    return {'_id': doc['_id'], **{
        key: {'$eq': doc[key], '$exists': True} if key in doc else {'$exists': False}
        for key in (*MUTABLE, 'history')
    }}


@app.get('/health')
def health():
    try:
        db.command('ping')
        if not app.state.indexes_ready:
            create_indexes()
            app.state.indexes_ready = True
    except PyMongoError as exc:
        logger.warning('MongoDB readiness check failed (%s)', type(exc).__name__)
        raise HTTPException(503, 'Database unavailable') from None
    return {'status': 'ok'}


@app.post('/employees', status_code=201)
def create_employee(body: EmployeeIn):
    doc = {**body.model_dump(), 'created_at': now()}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, 'emp_code already exists') from None
    return json_value(doc)


@app.get('/employees')
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {'department': department}
    hint = 'employee_code' if department is None else 'employee_department_code'
    items = list(db.employees.find(query, {'_id': 0}).hint(hint).sort('emp_code', 1)
                 .skip((page - 1) * page_size).limit(page_size))
    return {'items': json_value(items), 'total': db.employees.count_documents(query, hint=hint),
            'page': page, 'page_size': page_size}


@app.post('/attendance/punch-in', status_code=201)
def punch_in(body: PunchInIn):
    employee = get_employee(body.emp_code)
    ts = from_epoch_ms(body.punched_at)
    doc = {'emp_code': body.emp_code, 'date': attendance_date(ts, employee),
           'status': body.status, 'punch_in': ts, 'history': []}
    doc.update(compute_attendance_fields(doc, employee))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, 'Already recorded for this attendance date') from None
    return attendance_json(doc)


@app.post('/attendance/punch-out')
def punch_out(body: PunchOutIn):
    employee = get_employee(body.emp_code)
    ts = from_epoch_ms(body.punched_at)
    doc = db.attendance_logs.find_one(
        {'emp_code': body.emp_code, 'punch_in': {'$type': 'date', '$lte': ts}},
        sort=[('punch_in', -1)], hint='attendance_employee_punch')
    if doc is None:
        raise HTTPException(404, 'No punch-in found at or before this time')
    if doc.get('punch_out') is not None:
        raise HTTPException(409, 'Already punched out')
    computed = compute_attendance_fields({**doc, 'punch_out': ts}, employee)
    updates = {key: computed[key] for key in ('punch_out', 'work_hours', 'overtime_minutes', 'half_day')}
    updated = db.attendance_logs.find_one_and_update(snapshot_filter(doc), {'$set': updates},return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, 'Record changed concurrently; reload before retrying')
    return attendance_json(updated)


@app.patch('/attendance/{emp_code}/{date}')
def regularize(emp_code: str, date: DateString, body: RegularizeIn):
    employee = get_employee(emp_code)
    original = db.attendance_logs.find_one({'emp_code': emp_code, 'date': date})
    if original is None:
        raise HTTPException(404, 'Attendance record not found')
    final = {**DEFAULTS, **original}
    fields = body.model_fields_set
    if 'status' in fields:
        final['status'] = body.status
    if final['status'] not in PRESENCE and fields & {'punch_in', 'punch_out'}:
        invalid('ABSENT and LEAVE cannot have punch times')
    for key in ('punch_in', 'punch_out'):
        if key in fields:
            final[key] = from_epoch_ms(getattr(body, key))
    final.update(compute_attendance_fields(final, employee))
    changes = {key: {'from': original.get(key, DEFAULTS.get(key)), 'to': final[key]}
               for key in MUTABLE if original.get(key, DEFAULTS.get(key)) != final[key]}
    if not changes:
        invalid('Correction changes nothing')
    entry = {'at': now(), 'by': body.regularized_by, 'reason': body.reason, 'changes': changes}
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(original),
        {'$set': {key: final[key] for key in MUTABLE}, '$push': {'history': entry}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, 'Record changed concurrently; reload before retrying')
    return attendance_json(updated)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None, page=1, page_size=20):
    if date_from is not None and date_to is not None and date_from > date_to:
        invalid('date_from must be on or before date_to')
    query = {}
    if emp_code is not None:
        query['emp_code'] = emp_code
    if date_from is not None or date_to is not None:
        query['date'] = {}
        if date_from is not None:
            query['date']['$gte'] = date_from
        if date_to is not None:
            query['date']['$lte'] = date_to
    if status is not None:
        query['status'] = status
    hint = ('attendance_employee_date' if emp_code is not None else
            'attendance_status_date_employee' if status is not None else 'attendance_date_employee')
    return {'find': 'attendance_logs', 'filter': query, 'projection': {'_id': 0},
            'sort': {'date': -1, 'emp_code': 1}, 'skip': (page - 1) * page_size,
            'limit': page_size, 'hint': hint}


@app.get('/attendance')
def list_attendance(emp_code: str | None = None, date_from: DateString | None = None,
                    date_to: DateString | None = None, status: Status | None = None,
                    page: Page = 1, page_size: PageSize = 20):
    spec = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    rows = db.attendance_logs.find(spec['filter'], spec['projection']).hint(spec['hint'])
    rows = rows.sort(list(spec['sort'].items())).skip(spec['skip']).limit(spec['limit'])
    return {'items': [attendance_json(row) for row in rows],
            'total': db.attendance_logs.count_documents(spec['filter'], hint=spec['hint']),
            'page': page, 'page_size': page_size}


def month_bounds(month):
    first = date.fromisoformat(month + '-01')
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first.isoformat(), last.isoformat()


def mongo_date(value):
    return {'$dateFromString': {'dateString': value, 'format': '%Y-%m-%d', 'timezone': 'UTC'}}


def weekday(value):
    return {'$in': [{'$isoDayOfWeek': mongo_date(value)}, [1, 2, 3, 4, 5]]}


def round_half_up(value, places=2):
    scale = 10 ** places
    scaled = {'$multiply': [{'$toDecimal': value}, scale]}
    rounded = {'$floor': {'$add': [scaled, Decimal128('0.5')]}}
    result = {'$toDouble': {'$divide': [rounded, scale]}}
    return {'$cond': [{'$eq': [value, None]}, None, result]}


def present_weight(weekdays_only=False):
    condition = {'$in': ['$status', PRESENCE]}
    if weekdays_only:
        condition = {'$and': [condition, weekday('$date')]}
    return {'$cond': [condition, {'$cond': [{'$ifNull': ['$half_day', False]}, 0.5, 1]}, 0]}


def record_totals(*, department=False):
    has_hours = {'$and': [{'$in': ['$status', PRESENCE]}, {'$ne': [{'$ifNull': ['$work_hours', None]}, None]}]}
    totals = {
        '_id': None, 'present_days': {'$sum': present_weight(True)},
        'leave_days': {'$sum': {'$cond': [{'$eq': ['$status', 'LEAVE']}, 1, 0]}},
        'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}},
        'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}},
    }
    if department:
        totals.update({
            'on_duty_count': {'$sum': {'$cond': [{'$eq': ['$status', 'ON_DUTY']}, 1, 0]}},
            'hours_sum': {'$sum': {'$cond': [has_hours, {'$toDecimal': '$work_hours'}, 0]}},
            'hours_count': {'$sum': {'$cond': [has_hours, 1, 0]}},
        })
    else:
        totals['total_overtime_minutes'] = {'$sum': {'$ifNull': ['$overtime_minutes', 0]}}
    return {'$group': totals}


def logs_lookup(first, last, *, department=False):
    return {'$lookup': {
        'from': 'attendance_logs',
        'localField': 'emp_code',
        'foreignField': 'emp_code',
        'pipeline': [
            {'$match': {'date': {'$gte': first, '$lte': last}}},
            record_totals(department=department),
        ],
        'as': 'totals',
    }}



def total_value(key):
    return {'$ifNull': [{'$arrayElemAt': ['$totals.' + key, 0]}, 0]}


def working_days(first, last):
    start = mongo_date({'$max': [first, '$joined_on']})
    elapsed_days = {'$dateDiff': {
        'startDate': start, 'endDate': mongo_date(last), 'unit': 'day',
    }}
    day_count = {'$max': [0, {'$add': [1, elapsed_days]}]}
    current_day = {'$dateAdd': {
        'startDate': start, 'unit': 'day', 'amount': '$$offset',
    }}
    weekdays = {'$filter': {
        'input': {'$range': [0, day_count]},
        'as': 'offset',
        'cond': {'$lte': [{'$isoDayOfWeek': current_day}, 5]},
    }}
    return {'$size': weekdays}


def monthly_pipeline(emp_code, month):
    first, last = month_bounds(month)
    metrics = ('present_days', 'leave_days', 'late_count',
               'total_late_minutes', 'total_overtime_minutes')
    attendance_pct = {'$cond': [
        {'$gt': ['$working_days', 0]},
        round_half_up({'$divide': [{'$multiply': ['$present_days', 100]}, '$working_days']}),
        None,
    ]}
    pipeline = [
        {'$match': {'emp_code': emp_code}},
        logs_lookup(first, last),
        {'$set': {
            'working_days': working_days(first, last),
            **{key: total_value(key) for key in metrics},
        }},
        {'$project': {
            '_id': 0, 'emp_code': 1, 'month': {'$literal': month},
            'working_days': 1, 'attendance_pct': attendance_pct,
            **{key: 1 for key in metrics},
        }},
    ]
    return 'employees', pipeline, 'employee_code'


def summary_pipeline(month, department=None):
    first, last = month_bounds(month)
    match = {'joined_on': {'$lte': last}}
    if department is not None:
        match['department'] = department
    group = {'_id': '$department', 'headcount': {'$sum': 1}}
    for key in ('present_days', 'late_count', 'total_late_minutes', 'leave_days', 'on_duty_count', 'hours_sum', 'hours_count'):
        group[key] = {'$sum': total_value(key)}
    avg_work_hours = {'$cond': [
        {'$gt': ['$hours_count', 0]},
        round_half_up({'$divide': ['$hours_sum', '$hours_count']}),
        None,
    ]}
    pipeline = [
        {'$match': match},
        logs_lookup(first, last, department=True),
        {'$group': group},
        {'$project': {
            '_id': 0, 'department': '$_id', 'headcount': 1, 'present_days': 1,
            'late_count': 1, 'total_late_minutes': 1, 'leave_count': '$leave_days',
            'on_duty_count': 1, 'avg_work_hours': avg_work_hours,
        }},
        {'$sort': {'department': 1}},
    ]
    return 'employees', pipeline, 'employee_department_joined' if department is not None else 'employee_joined_department'


def leaderboard_pipeline(month, department=None, limit=10):
    first, last = month_bounds(month)
    pipeline = [
        {'$match': {'date': {'$gte': first, '$lte': last}}},
        {'$group': {'_id': '$emp_code', 'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}},
                    'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}}},
        {'$match': {'total_late_minutes': {'$gt': 0}}},
        {'$lookup': {'from': 'employees', 'localField': '_id', 'foreignField': 'emp_code', 'as': 'employee'}},
        {'$unwind': '$employee'},
    ]
    if department is not None:
        pipeline.append({'$match': {'employee.department': department}})
    pipeline.extend([
        {'$setWindowFields': {'sortBy': {'total_late_minutes': -1}, 'output': {'rank': {'$rank': {}}}}},
        {'$match': {'rank': {'$lte': limit}}},
        {'$project': {'_id': 0, 'emp_code': '$_id', 'name': '$employee.name', 'department': '$employee.department',
                      'rank': 1, 'total_late_minutes': 1, 'late_count': 1}},
        {'$sort': {'total_late_minutes': -1, 'emp_code': 1}},
    ])
    return 'attendance_logs', pipeline, 'attendance_date_employee'


def trend_pipeline(department, first, last):
    days = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
    if not 1 <= days <= 92:
        invalid('Trend range must contain 1 to 92 calendar days')
    calendar_day = {'$dateToString': {
        'format': '%Y-%m-%d',
        'date': {'$dateAdd': {
            'startDate': mongo_date(first), 'unit': 'day', 'amount': '$offset',
        }},
    }}
    eligible_employees = {'$filter': {
        'input': '$joins', 'as': 'joined', 'cond': {'$lte': ['$$joined', '$date']},
    }}
    daily_logs = [
        {'$match': {'$expr': {'$and': [
            {'$eq': ['$date', '$$day']},
            {'$in': ['$emp_code', '$$codes']},
        ]}}},
        {'$group': {
            '_id': None,
            'present_count': {'$sum': present_weight()},
            'late_count': {'$sum': {'$cond': [
                {'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0,
            ]}},
        }},
    ]
    attendance_rate = {'$cond': [
        {'$and': ['$is_working_day', {'$gt': ['$headcount', 0]}]},
        round_half_up({'$divide': ['$present_count', '$headcount']}, 4),
        None,
    ]}
    pipeline = [
        {'$match': {'department': department}},
        {'$group': {'_id': None, 'codes': {'$push': '$emp_code'}, 'joins': {'$push': '$joined_on'}}},
        # Generate the complete calendar in MongoDB even when there are no logs.
        {'$set': {'offset': {'$range': [0, days]}}}, {'$unwind': '$offset'},
        {'$set': {'date': calendar_day}},
        {'$set': {
            'headcount': {'$size': eligible_employees},
            'is_working_day': weekday('$date'),
        }},
        {'$lookup': {
            'from': 'attendance_logs',
            'let': {'day': '$date', 'codes': '$codes'},
            'pipeline': daily_logs,
            'as': 'totals',
        }},
        {'$project': {'_id': 0, 'date': 1, 'headcount': 1, 'is_working_day': 1,
                      'present_count': total_value('present_count'), 'late_count': total_value('late_count')}},
        {'$set': {'attendance_rate': attendance_rate}},
        {'$setWindowFields': {'sortBy': {'date': 1}, 'output': {'moving_avg_7d': {
            '$avg': {'$toDecimal': '$attendance_rate'}, 'window': {'documents': [-6, 0]},
        }}}},
        {'$set': {'moving_avg_7d': round_half_up('$moving_avg_7d', 4)}},
        {'$sort': {'date': 1}},
    ]
    return 'employees', pipeline, 'employee_department_joined'


def aggregate(spec):
    collection, pipeline, hint = spec
    return json_value(list(db[collection].aggregate(pipeline, hint=hint)))


@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code: str, month: MonthString):
    items = aggregate(monthly_pipeline(emp_code, month))
    if not items:
        raise HTTPException(404, 'Employee not found')
    return items[0]


@app.get('/analytics/departments/summary')
def department_summary(month: MonthString, department: str | None = None):
    return {'month': month, 'items': aggregate(summary_pipeline(month, department))}


@app.get('/analytics/leaderboard/late')
def late_leaderboard(month: MonthString, department: str | None = None, limit: Limit = 10):
    return {'month': month, 'items': aggregate(leaderboard_pipeline(month, department, limit))}


@app.get('/analytics/departments/{department}/trend')
def department_trend(department: str, from_: Annotated[DateString, Query(alias='from')], to: DateString):
    items = aggregate(trend_pipeline(department, from_, to))
    if not items:
        raise HTTPException(404, 'Department not found')
    return {'department': department, 'items': items}


ExplainEndpoint = Literal['attendance_list', 'employee_monthly', 'department_summary', 'late_leaderboard', 'department_trend']


@app.get('/admin/explain/{endpoint}')
def explain_endpoint(endpoint: ExplainEndpoint, emp_code: str | None = None,
                     month: MonthString | None = None, department: str | None = None,
                     limit: Limit = 10, date_from: DateString | None = None,
                     date_to: DateString | None = None, status: Status | None = None,
                     from_: Annotated[DateString | None, Query(alias='from')] = None,
                     to: DateString | None = None, page: Page = 1, page_size: PageSize = 20):
    if endpoint == 'attendance_list':
        command = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = 'attendance_logs'
    else:
        required = {'employee_monthly': {'emp_code': emp_code, 'month': month},
                    'department_summary': {'month': month}, 'late_leaderboard': {'month': month},
                    'department_trend': {'department': department, 'from': from_, 'to': to}}[endpoint]
        missing = [key for key, value in required.items() if value is None]
        if missing:
            invalid('Missing required parameters: ' + ', '.join(missing))
        if endpoint == 'employee_monthly':
            spec = monthly_pipeline(emp_code, month)
        elif endpoint == 'department_summary':
            spec = summary_pipeline(month, department)
        elif endpoint == 'late_leaderboard':
            spec = leaderboard_pipeline(month, department, limit)
        else:
            spec = trend_pipeline(department, from_, to)
        collection, pipeline, hint = spec
        command = {'aggregate': collection, 'pipeline': pipeline, 'cursor': {}, 'hint': hint}
    result = db.command('explain', command, verbosity='executionStats')
    return {'endpoint': endpoint, 'collection': collection, 'explain': json_value(result, preserve_id=True)}
