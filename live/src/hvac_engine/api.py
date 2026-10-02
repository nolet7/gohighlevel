"""HTTP surface: signed webhooks in, admin job triggers, health and metrics."""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .container import Engine, build_engine
from .domain import LeadIn
from .logging import configure_logging
from .scheduler import Scheduler
from .security import constant_time_equal, verify

logger = logging.getLogger(__name__)
MAX_BODY = 64 * 1024
SOURCES = {"website": "website", "meta": "meta_lead_ad", "lsa": "google_lsa"}


class LeadPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    issue: str = Field(default="HVAC issue", max_length=200)
    sms_consent: bool = False


class InboundPayload(BaseModel):
    contact_id: str = Field(max_length=64)
    message: str = Field(max_length=1600)


class MissedCallPayload(BaseModel):
    phone: str = Field(max_length=32)
    name: str = Field(default="there", max_length=120)


def create_app(engine: Engine | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    eng = engine or build_engine(settings)
    scheduler = Scheduler(eng, settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.scheduler_enabled:
            scheduler.start()
        yield
        await scheduler.stop()

    app = FastAPI(title="HVAC Growth Engine", version="1.0.0", lifespan=lifespan)
    app.state.engine = eng

    # ----------------------------------------------------------- auth deps
    async def signed_body(request: Request,
                          x_timestamp: str | None = Header(default=None),
                          x_signature: str | None = Header(default=None),
                          x_webhook_token: str | None = Header(default=None)) -> bytes:
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "payload too large")
        static = settings.webhook_static_token
        if static and x_webhook_token and constant_time_equal(x_webhook_token, static.get_secret_value()):
            eng.metrics.inc("webhook_auth_total", mode="static_token")
            return body
        now = eng.deps.clock.now().timestamp()
        if not verify(settings.webhook_secret.get_secret_value(), x_timestamp, x_signature, body,
                      now, settings.webhook_max_skew_seconds):
            eng.metrics.inc("webhook_rejected_total", reason="bad_signature")
            raise HTTPException(401, "invalid signature")
        return body

    def admin(authorization: str | None = Header(default=None)) -> None:
        token = (authorization or "").removeprefix("Bearer ").strip()
        if not constant_time_equal(token, settings.admin_api_key.get_secret_value()):
            raise HTTPException(401, "unauthorized")

    def parse(model, body: bytes):
        try:
            return model.model_validate_json(body)
        except Exception as e:
            raise HTTPException(422, f"invalid payload: {str(e)[:200]}") from e

    # ------------------------------------------------------------ webhooks
    @app.post("/webhooks/leads/{source}")
    async def lead_webhook(source: str, body: bytes = Depends(signed_body),
                           x_event_id: str | None = Header(default=None)):
        if source not in SOURCES:
            raise HTTPException(404, "unknown source")
        event_id = x_event_id or hashlib.sha256(body).hexdigest()
        if not eng.store.first_time(f"lead:{source}:{event_id}", eng.deps.clock.now()):
            eng.metrics.inc("webhook_duplicate_total", kind="lead")
            return {"status": "duplicate"}
        p = parse(LeadPayload, body)
        try:
            cid = eng.intake.ingest(LeadIn(SOURCES[source], p.name, p.phone, p.email, p.issue, p.sms_consent))
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {"status": "ok", "contact_id": cid}

    @app.post("/webhooks/ghl/inbound")
    async def inbound_webhook(body: bytes = Depends(signed_body)):
        p = parse(InboundPayload, body)
        try:
            outcome = eng.conversation.handle_inbound(p.contact_id, p.message)
        except KeyError as e:
            raise HTTPException(404, "unknown contact") from e
        return {"status": "ok", "outcome": outcome}

    @app.post("/webhooks/ghl/missed-call")
    async def missed_call_webhook(body: bytes = Depends(signed_body)):
        p = parse(MissedCallPayload, body)
        try:
            return {"status": "ok", "contact_id": eng.conversation.missed_call(p.phone, p.name)}
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/webhooks/fieldpulse/{token}")
    async def fieldpulse_webhook(token: str, request: Request):
        """FieldPulse cannot sign requests, so the payload is treated as a HINT only: we re-read the
        truth from the FieldPulse API. A forged call can at worst trigger a reconcile."""
        if not constant_time_equal(token, settings.fieldpulse_webhook_token.get_secret_value()):
            raise HTTPException(404, "not found")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "payload too large")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:     # JSONDecodeError and UnicodeDecodeError are both ValueErrors
            payload = {}
        cust = None
        if isinstance(payload, dict):
            data = payload.get("data")
            cust = payload.get("customer_id") or (data.get("customer_id") if isinstance(data, dict) else None)
        if hasattr(eng.deps.fs, "snapshot"):
            eng.deps.fs.snapshot(force=True)      # bypass cache: webhook means something changed
        changed = eng.sync.sync_by_fp_id(str(cust)) if cust else bool(eng.sync.sync_all())
        eng.metrics.inc("fieldpulse_webhooks_total")
        return {"status": "ok", "changed": changed}

    # --------------------------------------------------------------- admin
    @app.post("/admin/jobs/{name}", dependencies=[Depends(admin)])
    def run_job(name: str, dry_run: bool = Query(default=False), force: bool = Query(default=False)):
        jobs = {
            "tick": lambda: eng.tick(),
            "sync": lambda: {"fp_changes": eng.sync.sync_all()},
            "sequences": lambda: {"messages_sent": eng.runner.run_due()},
            "fill-calendar": lambda: eng.campaigns.fill_calendar(dry_run, force).as_dict(),
            "estimates": lambda: eng.campaigns.reactivate_estimates(dry_run).as_dict(),
            "database": lambda: eng.campaigns.reactivate_database(dry_run).as_dict(),
        }
        if name not in jobs:
            raise HTTPException(404, f"unknown job; choose from {sorted(jobs)}")
        eng.store.audit(eng.deps.clock.now(), "admin", f"job:{name}", None, dry_run=dry_run, force=force)
        return jobs[name]()

    @app.get("/admin/audit", dependencies=[Depends(admin)])
    def audit(limit: int = Query(default=50, le=500)):
        rows = eng.store.audit_rows()[-limit:]
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ health
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            ok = eng.store.ping()
        except Exception:
            ok = False
        if not ok:
            raise HTTPException(503, "database unavailable")
        return {"status": "ready", "mode": settings.mode}

    @app.get("/metrics")
    def metrics():
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(eng.metrics.render())

    return app
