"""
Centralized configuration from environment variables.
All settings have sane defaults — app works without any .env file.
"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # ── database (optional) ───────────────────────────
    database_url: Optional[str] = None

    # ── redis (optional) ──────────────────────────────
    redis_url: Optional[str] = None

    # ── app ───────────────────────────────────────────
    default_operator: str = "db"
    log_level: str = "info"

    # ── cache TTLs (seconds) ──────────────────────────
    cache_ttl_departures: int = 30
    cache_ttl_stations: int = 3600
    cache_ttl_trips: int = 300
    cache_ttl_geometry: int = 86400

    # ── websocket ─────────────────────────────────────
    ws_broadcast_interval: int = 15

    # ── overpass ──────────────────────────────────────
    overpass_rate_limit: int = 2

    # ── delay tracker ─────────────────────────────────
    delay_retention_hours: int = 24

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
