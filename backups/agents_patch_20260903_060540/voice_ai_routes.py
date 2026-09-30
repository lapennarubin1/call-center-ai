"""Modulo voice_ai — Sprint 1: lectura de campanas y llamadas del tenant.
No dispara llamadas: eso sigue en el sistema actual (Asterisk + n8n) hasta
Fase B. Solo depende del core.

Bloque "Campañas real" (post Sprint 1): administracion de campanas dentro
de saas_platform unicamente. Ninguna ruta de este archivo toca Asterisk,
dispara llamadas ni activa n8n — crear/editar una campana es una operacion
100% de base de datos."""
from flask import Blueprint, jsonify, g, request, abort
from sqlalchemy import text
from ...db import saas_session
from ...core.auth.rbac import requires_role, requires_module
from ...core.events.outbox import publish
from . import countries

bp = Blueprint("voice_ai", __name__, url_prefix="/api/v1/voice")

# Estados permitidos por operacion. 'active' y 'finished' quedan fuera de
# este bloque a proposito: activar una campana implica futura integracion
# con n8n/Asterisk, explicitamente fuera de alcance ahora.
CREATE_ALLOWED_STATUS = {"draft"}
EDIT_ALLOWED_STATUS = {"draft", "paused", "archived"}


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


# ------------------------------------------------------- helpers privados --

def _agent_belongs_to_tenant(session, agent_id: int, tenant_id: int) -> bool:
    return session.execute(text(
        "SELECT 1 FROM agents WHERE id=:a AND tenant_id=:t"
    ), {"a": agent_id, "t": tenant_id}).first() is not None


def _campaign_row(session, campaign_id: int, tenant_id: int):
    """SIEMPRE filtrado por tenant_id. Una campana de otro tenant no existe
    para esta consulta — nunca se distingue "no existe" de "es de otro
    tenant" en la respuesta, para no filtrar informacion."""
    return session.execute(text("""
        SELECT c.id, c.tenant_id, c.name, c.description, c.status, c.country, c.timezone,
               c.daily_limit, c.created_at, c.updated_at,
               a.id AS agent_id, a.name AS agent_name
        FROM voice_campaigns c
        LEFT JOIN agents a ON a.id = c.agent_id
        WHERE c.id = :id AND c.tenant_id = :t
    """), {"id": campaign_id, "t": tenant_id}).mappings().first()


def _campaign_or_404(session, campaign_id: int, tenant_id: int):
    row = _campaign_row(session, campaign_id, tenant_id)
    if not row:
        abort(404, description="Campaña no encontrada")
    return row


# --------------------------------------------------------------- endpoints --

@bp.get("/campaigns/<int:campaign_id>")
@requires_module("voice_ai")
@requires_role("viewer", "operator", "admin", "owner")
def get_campaign(campaign_id):
    with saas_session() as s:
        row = _campaign_or_404(s, campaign_id, g.tenant_id)
    return jsonify(dict(row))


@bp.post("/campaigns")
@requires_module("voice_ai")
@requires_role("operator", "admin", "owner")
def create_campaign():
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    country = (d.get("country") or "").strip().upper()
    description = (d.get("description") or "").strip() or None
    agent_id = d.get("agent_id")
    status = (d.get("status") or "draft").strip().lower()

    if not name:
        return jsonify(error="name es obligatorio"), 400
    if not countries.is_valid_country(country):
        return jsonify(error=f"country invalido. Valores permitidos: {', '.join(c for c, _ in countries.country_choices())}"), 400
    if status not in CREATE_ALLOWED_STATUS:
        return jsonify(error=f"status invalido para creacion. Permitido: {', '.join(sorted(CREATE_ALLOWED_STATUS))}"), 400
    if not agent_id:
        return jsonify(error="agent_id es obligatorio (el esquema actual no permite campañas sin agente)"), 400

    with saas_session() as s:
        if not _agent_belongs_to_tenant(s, agent_id, g.tenant_id):
            return jsonify(error="agent_id no pertenece a este tenant"), 400

        timezone = countries.default_timezone(country)
        res = s.execute(text("""
            INSERT INTO voice_campaigns (tenant_id, agent_id, name, description, country, timezone, status)
            VALUES (:t, :a, :n, :d, :c, :tz, :st)
        """), {"t": g.tenant_id, "a": agent_id, "n": name, "d": description,
               "c": country, "tz": timezone, "st": status})
        campaign_id = res.lastrowid

        publish(s, g.tenant_id, "voice_ai", "voice.campaign.created", "voice_campaign", campaign_id, {
            "name": name, "country": country, "agent_id": agent_id, "status": status,
        })
        row = _campaign_row(s, campaign_id, g.tenant_id)
    return jsonify(dict(row)), 201


@bp.patch("/campaigns/<int:campaign_id>")
@requires_module("voice_ai")
@requires_role("operator", "admin", "owner")
def update_campaign(campaign_id):
    d = request.get_json(silent=True) or {}

    with saas_session() as s:
        current = _campaign_or_404(s, campaign_id, g.tenant_id)

        fields, params = [], {"id": campaign_id, "t": g.tenant_id}
        changed = {}

        if "name" in d:
            name = (d.get("name") or "").strip()
            if not name:
                return jsonify(error="name no puede quedar vacio"), 400
            fields.append("name=:name"); params["name"] = name; changed["name"] = name

        if "description" in d:
            description = (d.get("description") or "").strip() or None
            fields.append("description=:description"); params["description"] = description
            changed["description"] = description

        if "country" in d:
            country = (d.get("country") or "").strip().upper()
            if not countries.is_valid_country(country):
                return jsonify(error=f"country invalido. Valores permitidos: {', '.join(c for c, _ in countries.country_choices())}"), 400
            fields.append("country=:country"); params["country"] = country
            fields.append("timezone=:timezone"); params["timezone"] = countries.default_timezone(country)
            changed["country"] = country

        if "agent_id" in d:
            agent_id = d.get("agent_id")
            if not agent_id or not _agent_belongs_to_tenant(s, agent_id, g.tenant_id):
                return jsonify(error="agent_id invalido o no pertenece a este tenant"), 400
            fields.append("agent_id=:agent_id"); params["agent_id"] = agent_id; changed["agent_id"] = agent_id

        if "status" in d:
            status = (d.get("status") or "").strip().lower()
            if status not in EDIT_ALLOWED_STATUS:
                return jsonify(error=f"status invalido. Permitido en este bloque: {', '.join(sorted(EDIT_ALLOWED_STATUS))}"), 400
            fields.append("status=:status"); params["status"] = status; changed["status"] = status

        if not fields:
            return jsonify(error="no se envio ningun campo para actualizar"), 400

        s.execute(text(f"UPDATE voice_campaigns SET {', '.join(fields)} WHERE id=:id AND tenant_id=:t"), params)
        publish(s, g.tenant_id, "voice_ai", "voice.campaign.updated", "voice_campaign", campaign_id, changed)
        row = _campaign_row(s, campaign_id, g.tenant_id)
    return jsonify(dict(row))

