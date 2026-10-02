"""GoHighLevel (LeadConnector API v2) adapter.

Endpoints used (from the official docs; re-verify header Version and bodies on day 1 with
scripts/ghl_probe.py): POST /contacts/upsert, POST /conversations/messages, POST /contacts/{id}/tags,
PUT /contacts/{id}, POST /opportunities/upsert.

Channels without a public send API (ringless voicemail, Sendblue/iMessage) are triggered by adding a
tag; a GHL workflow listening for the tag performs the send (webhook to Sendblue / RVM action).
"""
from __future__ import annotations

import logging
from collections import OrderedDict

import httpx

from ..config import Settings
from ..domain import Channel
from .http import request_with_retry

logger = logging.getLogger(__name__)
TAG_TRIGGERED = {Channel.RVM: "trigger:rvm", Channel.IMESSAGE: "trigger:imessage"}


class GHLClient:
    def __init__(self, token: str, location_id: str, base_url: str, version: str,
                 pipeline_id: str | None = None, stage_ids: dict[str, str] | None = None,
                 http: httpx.Client | None = None):
        self.location_id = location_id
        self.pipeline_id, self.stage_ids = pipeline_id, stage_ids or {}
        self.http = http or httpx.Client(
            base_url=base_url, timeout=15.0,
            headers={"Authorization": f"Bearer {token}", "Version": version, "Accept": "application/json"})
        self._recent: OrderedDict[str, None] = OrderedDict()   # at-least-once -> best-effort dedupe

    @classmethod
    def from_settings(cls, s: Settings) -> GHLClient:
        assert s.ghl_token and s.ghl_location_id
        return cls(s.ghl_token.get_secret_value(), s.ghl_location_id, s.ghl_base_url, s.ghl_api_version,
                   s.ghl_pipeline_id, s.ghl_stage_ids)

    def _call(self, method: str, path: str, **kw) -> dict:
        resp = request_with_retry(self.http, "ghl", method, path, **kw)
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------ port
    def upsert_contact(self, name, phone, email, tags):
        body = {"locationId": self.location_id, "name": name, "tags": sorted(tags)}
        if phone:
            body["phone"] = phone
        if email:
            body["email"] = email
        data = self._call("POST", "/contacts/upsert", json=body)
        return data["contact"]["id"], bool(data.get("new"))

    def send(self, contact_id, channel: Channel, text, idempotency_key):
        if idempotency_key in self._recent:
            return True
        if channel in TAG_TRIGGERED:
            self.add_tags(contact_id, {TAG_TRIGGERED[channel]})
        elif channel == Channel.SMS:
            self._call("POST", "/conversations/messages",
                       json={"type": "SMS", "contactId": contact_id, "message": text})
        elif channel == Channel.EMAIL:
            self._call("POST", "/conversations/messages",
                       json={"type": "Email", "contactId": contact_id, "subject": text.split(".")[0][:80],
                             "html": f"<p>{text}</p>", "message": text})
        else:
            return False
        self._recent[idempotency_key] = None
        while len(self._recent) > 5000:
            self._recent.popitem(last=False)
        return True

    def set_stage(self, contact_id, stage):
        stage_id = self.stage_ids.get(stage)
        if self.pipeline_id and stage_id:
            self._call("POST", "/opportunities/upsert", json={
                "pipelineId": self.pipeline_id, "locationId": self.location_id, "contactId": contact_id,
                "pipelineStageId": stage_id, "name": stage, "status": "open"})
        else:  # no pipeline mapping configured: expose the stage as a tag workflows can react to
            self.add_tags(contact_id, {f"stage:{stage}"})

    def add_tags(self, contact_id, tags):
        self._call("POST", f"/contacts/{contact_id}/tags", json={"tags": sorted(tags)})

    def set_dnd(self, contact_id, dnd):
        self._call("PUT", f"/contacts/{contact_id}", json={"dnd": dnd})
