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

Edit `.env` and set `ANTHROPIC_API_KEY`.

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

## Preview Endpoint

Live curl example:

```powershell
curl.exe -X POST "http://localhost:8000/api/plans/preview" `
  -H "Content-Type: application/json" `
  -d "{\"profile\":{\"name\":\"Joe\",\"weekly_km_recent\":35,\"longest_run_km_recent\":18,\"easy_pace_min_per_km\":6.0,\"days_per_week\":5},\"goal\":\"sub-4 marathon\",\"weeks\":16,\"race_date\":\"2026-10-25\",\"history_summary\":null,\"notes\":null}"
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m compileall app tests
.\.venv\Scripts\python.exe -m pytest tests
```
