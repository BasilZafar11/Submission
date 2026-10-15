# Employee Attendance and Analytics API

Requires Python 3.11+ and MongoDB 6.0+ (local or Atlas). Run from this repository root:

```text
python -m pip install -r requirements.txt
```

Set `MONGO_URI` to your MongoDB connection string and `MONGO_DB` to the database name.

PowerShell example for local MongoDB:

```powershell
$env:MONGO_URI = 'mongodb://127.0.0.1:27017'
$env:MONGO_DB = 'attendance_db'
uvicorn app.main:app --port 8000
```

On macOS/Linux, set variables with `export MONGO_URI=...` and `export MONGO_DB=...`, then use the same Uvicorn command.

Open http://127.0.0.1:8000/docs to try the API. `/health` returns 200 when MongoDB responds and indexes are ready, otherwise 503.

All application code is in `app/main.py`. All required endpoints i have implemented completely. 
