"""Typed, environment-driven configuration. All env vars are prefixed HGE_."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDERS = {"change-me", "change-me-too", ""}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HGE_", env_file=".env", extra="ignore")

    env: Literal["dev", "staging", "prod"] = "dev"
    mode: Literal["mock", "live"] = "mock"
    company_name: str = "Summit HVAC"
    timezone: str = "America/New_York"
    database_path: str = "data/engine.db"
    log_level: str = "INFO"

    # security
    webhook_secret: SecretStr = SecretStr("change-me")
    admin_api_key: SecretStr = SecretStr("change-me-too")
    webhook_max_skew_seconds: int = 300
    # Optional: GHL's native webhook action can only send STATIC headers (no per-request HMAC).
    # When set, requests carrying this value in X-Webhook-Token are accepted (no replay protection).
    webhook_static_token: SecretStr | None = None

    # compliance
    quiet_hours_start: int = 21   # no marketing sends from 21:00 local ...
    quiet_hours_end: int = 8      # ... until 08:00 local
    marketing_cap_per_week: int = 4
    # treat a missed-call text-back as a reply to the caller's inquiry (confirm with counsel)
    missed_call_implied_consent: bool = True
    review_link: str = ""

    # calendar-fill campaign
    fill_window_start_days: int = 7
    fill_window_end_days: int = 14
    fill_max_slow_day_fraction: float = 0.75  # if more of the workdays look slow than this, assume bad data
    fill_open_threshold: float = 0.5   # day counts as "slow" if >= 50% of capacity is open
    fill_max_targets: int = 40              # hard safety cap per run (also the rollout dial)
    fill_expected_booking_rate: float = 0.08  # ASSUMPTION: bookings per message; calibrate with real data
    fill_target_fill_fraction: float = 0.5    # aim to fill about half of the open slots
    fill_offer: str = "$40 off"
    fill_min_months_since_service: int = 6

    # live integrations (only required when mode == "live")
    ghl_base_url: str = "https://services.leadconnectorhq.com"
    ghl_token: SecretStr | None = None
    ghl_location_id: str | None = None
    ghl_api_version: str = "2021-07-28"      # VERIFY against current GHL docs (a docs page showed "v3")
    ghl_pipeline_id: str | None = None
    ghl_stage_ids: dict[str, str] = {}        # JSON env: {"New Lead":"<stageId>", "Booked":"<stageId>", ...}
    fieldpulse_base_url: str = "https://ywe3crmpll.execute-api.us-east-2.amazonaws.com/stage"
    fieldpulse_api_key: SecretStr | None = None
    fieldpulse_webhook_token: SecretStr = SecretStr("change-me")
    # FieldPulse exposes no availability endpoint: capacity is configured, bookings come from /jobs
    fp_tech_count: int = 3
    fp_slots_per_tech_per_day: int = 4
    fp_workdays: str = "0,1,2,3,4,5"          # Mon=0 ... Sun=6
    fp_snapshot_ttl_seconds: int = 60

    campaigns_live: bool = False              # scheduled campaigns only plan (dry-run) until this is true

    # in-process scheduler (use an external cron hitting /admin/jobs/* if you prefer)
    scheduler_enabled: bool = False
    tick_seconds: int = 60

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        if self.env == "prod":
            for name in ("webhook_secret", "admin_api_key", "fieldpulse_webhook_token"):
                if getattr(self, name).get_secret_value() in _PLACEHOLDERS:
                    raise ValueError(f"HGE_{name.upper()} must be set to a real secret in prod")
        if self.mode == "live":
            missing = [n for n in ("ghl_token", "ghl_location_id", "fieldpulse_api_key")
                       if not getattr(self, n)]
            if missing:
                raise ValueError(f"live mode requires: {', '.join('HGE_' + m.upper() for m in missing)}")
        if not 0 <= self.quiet_hours_start <= 23 or not 0 <= self.quiet_hours_end <= 23:
            raise ValueError("quiet hours must be 0-23")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
