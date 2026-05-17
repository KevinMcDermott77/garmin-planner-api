from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import validate_supabase_env

router = APIRouter()


class DevTokenRequest(BaseModel):
    email: str
    password: str


@router.post("/token")
def create_dev_token(request: DevTokenRequest) -> dict[str, str]:
    try:
        response = _create_dev_auth_client().auth.sign_in_with_password(
            {"email": request.email, "password": request.password}
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Could not sign in test user: {exc}") from exc

    session = getattr(response, "session", None)
    token = getattr(session, "access_token", None)
    if not token:
        raise HTTPException(status_code=401, detail="Supabase did not return an access token.")
    return {"access_token": token}


def _create_dev_auth_client():
    """Create an isolated client so sign-in never mutates the DB service-role singleton."""
    validate_supabase_env()
    import os

    from supabase import create_client

    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
