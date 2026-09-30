"""Modulo voice_ai — Sprint 1: lectura de campanas y llamadas del tenant.
No dispara llamadas: eso sigue en el sistema actual (Asterisk + n8n) hasta
Fase B. Solo depende del core."""
from flask import Blueprint, jsonify, g
from sqlalchemy import text
from ...db import saas_session
from ...core.auth.rbac import requires_role, requires_module

bp = Blueprint("voice_ai", __name__, url_prefix="/api/v1/voice")


@bp.get("/campaigns")
@requires_module("voice_ai")
@requires_role("viewer", "operator", "admin", "owner")
def list_campaigns():
    with saas_session() as s:
        rows = s.execute(text("""
            SELECT id, name, country, timezone, status, daily_limit, created_at
            FROM voice_campaigns WHERE tenant_id=:t ORDER BY id DESC
        """), {"t": g.tenant_id}).mappings().all()
    return jsonify(items=[dict(r) for r in rows])


@bp.get("/calls")
@requires_module("voice_ai")
@requires_role("viewer", "operator", "admin", "owner")
def list_calls():
    with saas_session() as s:
        rows = s.execute(text("""
            SELECT id, campaign_id, contact_id, direction, phone_e164, started_at, billsec, status, outcome
            FROM voice_calls WHERE tenant_id=:t ORDER BY id DESC LIMIT 100
        """), {"t": g.tenant_id}).mappings().all()
    return jsonify(items=[dict(r) for r in rows])
