from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="garmin-planner API", version="0.1.0")

# Permissive CORS for local dev. Tighten before deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.routers import plans  # noqa: E402

app.include_router(plans.router, prefix="/api/plans", tags=["plans"])


@app.get("/")
def root():
    return {"service": "garmin-planner-api", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "healthy"}
