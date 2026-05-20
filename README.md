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
DEV_MODE
STRAVA_CLIENT_ID
STRAVA_CLIENT_SECRET
STRAVA_REDIRECT_URI
STRAVA_TOKEN_ENCRYPTION_KEY
```

Optional legacy auth variable:

```powershell
SUPABASE_JWT_SECRET
```

JWT verification supports both Supabase signing modes:

- New Supabase projects use asymmetric signing keys. The API verifies these
  tokens automatically from `SUPABASE_URL/auth/v1/.well-known/jwks.json`.
- Legacy HS256 projects can also set `SUPABASE_JWT_SECRET`. Find it in
  Supabase under `Settings -> API -> JWT Settings -> JWT Secret`.

Leave `SUPABASE_JWT_SECRET` empty unless your project still uses legacy HS256
tokens.

Generate the Strava token encryption key before using Strava features:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set `STRAVA_TOKEN_ENCRYPTION_KEY` in `.env` to that generated value. For local
Strava OAuth, `STRAVA_REDIRECT_URI` must exactly match the callback URL
registered in your Strava API application, for example:

```powershell
STRAVA_REDIRECT_URI=http://localhost:8000/api/strava/callback
```

Before first run, open the Supabase SQL editor and run:

```powershell
Get-Content .\migrations\001_initial_schema.sql
```

Then run the auth ownership migration:

```powershell
Get-Content .\migrations\002_add_user_id.sql
```

Paste each SQL file into Supabase and execute it. The app does not run
migrations programmatically.

Before using Strava features, also run:

```powershell
Get-Content .\migrations\005_strava_tokens.sql
```

Paste the SQL into Supabase and execute it manually.

`migrations/003_rls_placeholder.sql` is comments only. It documents the
production RLS policies to apply before real users, but it is not meant to be
run in the current development posture.

## Database Security Posture

The backend currently uses a service-role Supabase client for database reads and
writes. This intentionally bypasses RLS during development. The API validates
the user's JWT itself, then sets and filters `user_id` in application code for
every profile and plan operation.

Do not inject a user's access token into the shared Supabase database client.
Doing so changes the `Authorization` header away from the service-role key,
re-enables RLS evaluation, and causes inserts to fail until production RLS
policies are configured.

Before production, use `migrations/003_rls_placeholder.sql` as the hardening
checklist: enable RLS, add user-scoped policies, then test switching database
access to user-scoped clients for defence in depth.

For local curl testing before the frontend exists, set this in `.env`:

```powershell
DEV_MODE=true
```

Restart the API after changing `DEV_MODE`. The `/api/dev/token` helper is only
registered when `DEV_MODE=true`; keep it disabled or remove it before production.

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

Get a dev access token:

```powershell
$tokenResponse = Invoke-RestMethod -Method Post "http://localhost:8000/api/dev/token" `
  -ContentType "application/json" `
  -Body '{"email":"runner@example.com","password":"your-test-password"}'

$token = $tokenResponse.access_token
```

Generate and persist a plan:

```powershell
$generateBody = @{
  profile = @{
    name = "Joe"
    weekly_km_recent = 35
    longest_run_km_recent = 18
    easy_pace_min_per_km = 6.0
    days_per_week = 5
  }
  goal = "sub-4 marathon"
  weeks = 16
  race_date = "2026-10-25"
  history_summary = $null
  notes = $null
} | ConvertTo-Json -Depth 20

$generated = Invoke-RestMethod -Method Post "http://localhost:8000/api/plans/generate" `
  -Headers @{ Authorization = "Bearer $token" } `
  -ContentType "application/json" `
  -Body $generateBody

$generated.plan_id
```

Retrieve one saved plan:

```powershell
Invoke-RestMethod "http://localhost:8000/api/plans/$($generated.plan_id)" `
  -Headers @{ Authorization = "Bearer $token" }
```

List your saved plans:

```powershell
Invoke-RestMethod "http://localhost:8000/api/plans" `
  -Headers @{ Authorization = "Bearer $token" }
```

Import a CLI-generated plan for the logged-in user:

`/api/dev/import-plan` is a personal development helper and is only registered
when `DEV_MODE=true`. It requires the same bearer token as the normal plan
endpoints. The imported plan is saved as `active` and uses the plan JSON from
disk as the source of truth.

```powershell
$planPath = "C:\Dev\garmin-planner\plans\plan_20260515_205043.json"
$planJson = Get-Content -Raw $planPath | ConvertFrom-Json
$importBody = @{ plan_json = $planJson } | ConvertTo-Json -Depth 100

$imported = Invoke-RestMethod -Method Post "http://localhost:8000/api/dev/import-plan" `
  -Headers @{ Authorization = "Bearer $token" } `
  -ContentType "application/json" `
  -Body $importBody

$imported.plan_id
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m compileall app tests
.\.venv\Scripts\python.exe -m pytest tests
```
