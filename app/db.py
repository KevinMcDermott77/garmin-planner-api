"""Supabase client setup."""

from __future__ import annotations

import os
from typing import Any

_supabase_client: Any | None = None


class SupabaseConfigError(RuntimeError):
    """Raised when Supabase environment configuration is incomplete."""


def validate_supabase_env() -> None:
    """Validate required Supabase env vars without opening a network connection."""
    missing = [name for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY") if not os.getenv(name)]
    if missing:
        joined = ", ".join(missing)
        raise SupabaseConfigError(f"Missing required Supabase environment variable(s): {joined}")


def get_supabase_client() -> Any:
    """Return a lazily-created singleton Supabase client."""
    global _supabase_client
    if _supabase_client is None:
        validate_supabase_env()
        from supabase import create_client

        url = os.environ["SUPABASE_URL"]
        service_key = os.environ["SUPABASE_SERVICE_KEY"]
        # Service-role client. Never inject a user JWT into this client's headers
        # -- doing so re-enables RLS and breaks inserts. RLS policies are NOT yet
        # configured (see migration notes); security is enforced in application
        # code by always scoping queries by user_id.
        _supabase_client = create_client(url, service_key)
    return _supabase_client
