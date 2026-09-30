"""Blueprint 'web' — interfaz HTML (Jinja2) sobre el backend existente.

No reemplaza la API JSON (/api/v1/...): las paginas de esta capa hacen sus
propias lecturas de solo-lectura, con el MISMO criterio de tenant_id que ya
usan las rutas JSON de core/modules (siempre desde la sesion del servidor,
nunca desde el cliente). No se modifico ningun archivo de core/ ni de
modules/ para construir esta interfaz.
"""
from functools import wraps
from flask import Blueprint, render_template, redirect, url_for, request, g
from sqlalchemy import text

from ..db import saas_session

bp = Blueprint("web", __name__)
# Sin template_folder/static_folder propios: Flask(__name__) en app/__init__.py
# ya resuelve app/templates y app/static por defecto (mismo directorio).


# ---------------------------------------------------------------- helpers --

def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not getattr(g, "tenant_id", None) or not getattr(g, "user_id", None):
            return redirect(url_for("web.login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def module_required(module_key: str):
    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            with saas_session() as s:
                enabled = _enabled_modules(s, g.tenant_id)
            if module_key not in enabled:
                return render_template(
                    "errors/403.html",
                    message=f"El modulo '{module_key}' no esta habilitado para este workspace.",
                ), 403
            return view(*args, **kwargs)
        return wrapper
    return deco


def _enabled_modules(session, tenant_id: int) -> list[str]:
    rows = session.execute(text(
        "SELECT module_key FROM tenant_modules WHERE tenant_id=:t AND status='enabled' ORDER BY module_key"
    ), {"t": tenant_id})
    return [r[0] for r in rows]


def _current_tenant(session, tenant_id: int) -> dict:
    row = session.execute(text(
        "SELECT id, slug, name, status FROM tenants WHERE id=:t"
    ), {"t": tenant_id}).mappings().first()
    return dict(row) if row else {}


def _current_user(session, user_id: int, role: str) -> dict:
    row = session.execute(text(
        "SELECT id, username, email FROM users WHERE id=:u"
    ), {"u": user_id}).mappings().first()
    data = dict(row) if row else {"username": "?"}
    data["role"] = role
    return data


def _page_shell():
    """Datos comunes a toda pagina autenticada: tenant, usuario, modulos."""
    with saas_session() as s:
        tenant = _current_tenant(s, g.tenant_id)
        user = _current_user(s, g.user_id, g.role)
        modules = _enabled_modules(s, g.tenant_id)
    return tenant, user, modules


# ------------------------------------------------------------------ rutas --

@bp.get("/")
def root():
    if getattr(g, "tenant_id", None):
        return redirect(url_for("web.dashboard"))
    return redirect(url_for("web.login"))


@bp.get("/login")
def login():
    if getattr(g, "tenant_id", None):
        return redirect(url_for("web.dashboard"))
    return render_template("login.html")


@bp.get("/dashboard")
@login_required
def dashboard():
    tenant, user, modules = _page_shell()
    with saas_session() as s:
        stats = {
            "modules_enabled": len(modules),
            "voice_campaigns": s.execute(text(
                "SELECT COUNT(*) FROM voice_campaigns WHERE tenant_id=:t"), {"t": g.tenant_id}).scalar() or 0,
            "voice_calls": s.execute(text(
                "SELECT COUNT(*) FROM voice_calls WHERE tenant_id=:t"), {"t": g.tenant_id}).scalar() or 0,
            "crm_leads": s.execute(text(
                "SELECT COUNT(*) FROM crm_leads WHERE tenant_id=:t"), {"t": g.tenant_id}).scalar() or 0,
            "agents": s.execute(text(
                "SELECT COUNT(*) FROM agents WHERE tenant_id=:t"), {"t": g.tenant_id}).scalar() or 0,
        }
    return render_template(
        "dashboard.html", active_page="dashboard",
        current_tenant=tenant, current_user=user, enabled_modules=modules, stats=stats,
    )


@bp.get("/voice/campaigns")
@login_required
@module_required("voice_ai")
def voice_campaigns():
    tenant, user, modules = _page_shell()
    with saas_session() as s:
        rows = s.execute(text("""
            SELECT c.id, c.name, c.status, c.country, c.created_at,
                   a.id AS agent_id, a.name AS agent_name
            FROM voice_campaigns c
            LEFT JOIN agents a ON a.id = c.agent_id
            WHERE c.tenant_id = :t
            ORDER BY c.id DESC
        """), {"t": g.tenant_id}).mappings().all()
    return render_template(
        "voice_campaigns.html", active_page="voice_campaigns",
        current_tenant=tenant, current_user=user, enabled_modules=modules,
        campaigns=[dict(r) for r in rows],
    )


@bp.get("/crm/leads")
@login_required
@module_required("crm")
def crm_leads():
    tenant, user, modules = _page_shell()
    with saas_session() as s:
        rows = s.execute(text("""
            SELECT l.id, l.status, l.created_at,
                   c.full_name, c.phone_e164, c.email,
                   ls.label AS source_label
            FROM crm_leads l
            JOIN contacts c ON c.id = l.contact_id
            LEFT JOIN crm_lead_sources ls ON ls.id = l.source_id
            WHERE l.tenant_id = :t
            ORDER BY l.id DESC
            LIMIT 200
        """), {"t": g.tenant_id}).mappings().all()
    return render_template(
        "crm_leads.html", active_page="crm_leads",
        current_tenant=tenant, current_user=user, enabled_modules=modules,
        leads=[dict(r) for r in rows],
    )


_PLACEHOLDER_TITLES = {
    "voice_calls": "Llamadas",
    "crm_contacts": "Contactos",
    "integrations": "Integraciones",
    "agents": "Agentes IA",
    "settings": "Configuración",
}


@bp.get("/soon/<section>")
@login_required
def placeholder(section):
    tenant, user, modules = _page_shell()
    title = _PLACEHOLDER_TITLES.get(section, "Próximamente")
    return render_template(
        "placeholder.html", active_page=section,
        current_tenant=tenant, current_user=user, enabled_modules=modules,
        section_title=title,
    )
