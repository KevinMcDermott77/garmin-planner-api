from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from app.db import validate_supabase_env

load_dotenv()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    validate_supabase_env()
    yield


app = FastAPI(title="garmin-planner API", version="0.1.0", lifespan=lifespan)

# Permissive CORS for local dev. Tighten before deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.routers import plans, race, strava  # noqa: E402

app.include_router(plans.router, prefix="/api/plans", tags=["plans"])
app.include_router(race.router, prefix="/api/race", tags=["race"])
app.include_router(strava.router, prefix="/api/strava", tags=["strava"])

if os.getenv("DEV_MODE", "").lower() == "true":
    from app.routers import dev  # noqa: E402

    app.include_router(dev.router, prefix="/api/dev", tags=["dev"])


@app.get("/")
def root():
    return {"service": "garmin-planner-api", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "healthy"}
