# garmin-planner API

FastAPI backend for the Garmin Planner SaaS. This project exposes the AI plan
generation engine over HTTP. The Next.js frontend lives separately.

## Setup

```powershell
cd C:\Dev\garmin-planner-api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .[dev]
Copy-Item .env.example .env
```

Edit `.env` and set these variables:

```powershell
ANTHROPIC_API_KEY
SUPABASE_URL
SUPABASE_SERVICE_KEY
DATABASE_URL
```

Before first run, open the Supabase SQL editor and run:

```powershell
Get-Content .\migrations\001_initial_schema.sql
```

Paste the SQL into Supabase and execute it. The app does not run migrations
programmatically.

## Run

```powershell
uvicorn app.main:app --reload --port 8000
```

Health check:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

OpenAPI docs:

```powershell
Start-Process http://localhost:8000/docs
```

## Plan Endpoints

Generate and persist a plan:

```powershell
curl.exe -X POST "http://localhost:8000/api/plans/generate" `
  -H "Content-Type: application/json" `
  -d "{\"profile\":{\"name\":\"Joe\",\"weekly_km_recent\":35,\"longest_run_km_recent\":18,\"easy_pace_min_per_km\":6.0,\"days_per_week\":5},\"goal\":\"sub-4 marathon\",\"weeks\":16,\"race_date\":\"2026-10-25\",\"history_summary\":null,\"notes\":null}"
```

Retrieve one saved plan:

```powershell
curl.exe "http://localhost:8000/api/plans/YOUR_PLAN_ID"
```

List saved plans:

```powershell
curl.exe "http://localhost:8000/api/plans"
```

List saved plans for one profile:

```powershell
curl.exe "http://localhost:8000/api/plans?profile_id=YOUR_PROFILE_ID"
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m compileall app tests
.\.venv\Scripts\python.exe -m pytest tests
```
