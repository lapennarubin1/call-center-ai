#!/usr/bin/env python3
"""
Panel de administracion ITSP + Facturacion.
Flask + SQLAlchemy sobre la misma BD MariaDB de Asterisk.
"""
import os
import subprocess
import secrets
import datetime
from functools import wraps
from decimal import Decimal

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, Response)
from sqlalchemy import func, text

from models import (db, Client, Connection, Provider, ClientDID, RatePlan,
                    SellRate, BuyRate, BillingRecord, Invoice, CDR,
                    FraudEvent, AdminUser)

# --------------------------- CONFIG ---------------------------------
DB_USER = os.environ.get("ITSP_DB_USER", "asterisk_rw")
DB_PASS = os.environ.get("ITSP_DB_PASS", "CAMBIA_ESTO_PASS_BD_RW")
DB_HOST = os.environ.get("ITSP_DB_HOST", "127.0.0.1")
DB_NAME = os.environ.get("ITSP_DB_NAME", "asterisk")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.secret_key = os.environ.get("ITSP_SECRET", secrets.token_hex(32))
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "ITSP_DB_URI",
    f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)


# --------------------------- AUTH -----------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("user"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = AdminUser.query.filter_by(username=request.form["username"]).first()
        if u and u.check_password(request.form["password"]):
            session["user"] = u.username
            return redirect(url_for("dashboard"))
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------- DASHBOARD ------------------------------
@app.route("/")
@login_required
def dashboard():
    today = datetime.date.today()
    stats = {
        "clients": Client.query.count(),
        "clients_active": Client.query.filter_by(active=True).count(),
        "providers": Provider.query.filter_by(active=True).count(),
        "connections": Connection.query.filter_by(active=True).count(),
        "dids": ClientDID.query.count(),
    }
    # Gasto de hoy (suma de amount facturado hoy)
    spend = db.session.query(func.coalesce(func.sum(BillingRecord.amount), 0)).filter(
        func.date(BillingRecord.created_at) == today).scalar()
    calls_today = db.session.query(func.count(CDR.id)).filter(
        func.date(CDR.calldate) == today).scalar()
    fraud = FraudEvent.query.order_by(FraudEvent.id.desc()).limit(8).all()
    return render_template("dashboard.html", stats=stats, spend=spend,
                           calls_today=calls_today, fraud=fraud)


# --------------------------- CLIENTES -------------------------------
@app.route("/clients")
@login_required
def clients():
    rows = Client.query.order_by(Client.client).all()
    plans = {p.id: p.name for p in RatePlan.query.all()}
    return render_template("clients.html", clients=rows, plans=plans)


@app.route("/clients/new", methods=["GET", "POST"])
@app.route("/clients/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def client_form(cid=None):
    c = Client.query.get(cid) if cid else None
    if request.method == "POST":
        f = request.form
        if not c:
            c = Client(client=f["client"])
            db.session.add(c)
        c.name = f["name"]
        c.company = f.get("company")
        c.email = f.get("email")
        c.max_channels = int(f.get("max_channels") or 10)
        c.billing_type = f.get("billing_type", "postpaid")
        c.balance = Decimal(f.get("balance") or 0)
        c.credit_limit = Decimal(f.get("credit_limit") or 0)
        c.currency = f.get("currency", "USD")
        c.rate_plan_id = int(f["rate_plan_id"]) if f.get("rate_plan_id") else None
        c.active = bool(f.get("active"))
        db.session.commit()
        flash("Cliente guardado.", "ok")
        return redirect(url_for("client_form", cid=c.id))
    plans = RatePlan.query.filter_by(active=True).all()
    conns = Connection.query.filter_by(client=c.client).all() if c else []
    dids = ClientDID.query.filter_by(client=c.client).all() if c else []
    return render_template("client_form.html", c=c, plans=plans,
                           conns=conns, dids=dids)


@app.route("/clients/<int:cid>/toggle")
@login_required
def client_toggle(cid):
    c = Client.query.get_or_404(cid)
    c.active = not c.active
    db.session.commit()
    flash(f"Cliente {'activado' if c.active else 'desactivado'}.", "ok")
    return redirect(url_for("clients"))


# --------------------------- CONEXIONES -----------------------------
@app.route("/clients/<int:cid>/connections/add", methods=["POST"])
@login_required
def connection_add(cid):
    c = Client.query.get_or_404(cid)
    f = request.form
    conn = Connection(
        client=c.client, label=f.get("label"),
        username=f["username"],
        password=f.get("password") or secrets.token_urlsafe(16),
        transport=f.get("transport", "udp"),
        max_channels=int(f.get("max_channels") or 5),
        allowed_ip=f.get("allowed_ip") or None,
        provider_order=f.get("provider_order") or None,
        active=True)
    db.session.add(conn)
    db.session.commit()
    flash("Conexión añadida.", "ok")
    return redirect(url_for("client_form", cid=cid))


@app.route("/connections/<int:conid>/delete", methods=["POST"])
@login_required
def connection_delete(conid):
    conn = Connection.query.get_or_404(conid)
    cid = Client.query.filter_by(client=conn.client).first().id
    db.session.delete(conn)
    db.session.commit()
    flash("Conexión eliminada.", "ok")
    return redirect(url_for("client_form", cid=cid))


# --------------------------- DIDs -----------------------------------
@app.route("/clients/<int:cid>/dids/add", methods=["POST"])
@login_required
def did_add(cid):
    c = Client.query.get_or_404(cid)
    for num in request.form["numbers"].replace(",", "\n").splitlines():
        num = num.strip()
        if num and not ClientDID.query.filter_by(number=num).first():
            db.session.add(ClientDID(client=c.client, number=num,
                                     provider=request.form.get("provider")))
    db.session.commit()
    flash("DID(s) añadidos.", "ok")
    return redirect(url_for("client_form", cid=cid))


@app.route("/dids/<int:did>/delete", methods=["POST"])
@login_required
def did_delete(did):
    d = ClientDID.query.get_or_404(did)
    cid = Client.query.filter_by(client=d.client).first().id
    db.session.delete(d)
    db.session.commit()
    return redirect(url_for("client_form", cid=cid))


# --------------------------- PROVEEDORES ----------------------------
@app.route("/providers")
@login_required
def providers():
    return render_template("providers.html",
                           providers=Provider.query.order_by(Provider.priority).all())


@app.route("/providers/new", methods=["GET", "POST"])
@app.route("/providers/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def provider_form(pid=None):
    p = Provider.query.get(pid) if pid else None
    if request.method == "POST":
        f = request.form
        if not p:
            p = Provider(name=f["name"])
            db.session.add(p)
        for field in ("host", "username", "password", "from_domain",
                      "codecs", "ip_cidrs"):
            setattr(p, field, f.get(field) or None)
        p.port = int(f.get("port") or 5060)
        p.auth_type = f.get("auth_type", "userpass")
        p.transport = f.get("transport", "udp")
        p.priority = int(f.get("priority") or 100)
        p.active = bool(f.get("active"))
        db.session.commit()
        flash("Proveedor guardado.", "ok")
        return redirect(url_for("providers"))
    return render_template("provider_form.html", p=p)


# --------------------------- PLANES / TARIFAS -----------------------
@app.route("/plans")
@login_required
def plans():
    return render_template("rate_plans.html",
                           plans=RatePlan.query.all())


@app.route("/plans/new", methods=["GET", "POST"])
@app.route("/plans/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def plan_form(pid=None):
    p = RatePlan.query.get(pid) if pid else None
    if request.method == "POST":
        f = request.form
        if not p:
            p = RatePlan(name=f["name"])
            db.session.add(p)
        p.name = f["name"]
        p.currency = f.get("currency", "USD")
        p.description = f.get("description")
        p.active = bool(f.get("active"))
        db.session.commit()
        flash("Plan guardado.", "ok")
        return redirect(url_for("plan_rates", pid=p.id))
    return render_template("rate_plan_form.html", p=p)


@app.route("/plans/<int:pid>/rates", methods=["GET"])
@login_required
def plan_rates(pid):
    p = RatePlan.query.get_or_404(pid)
    rates = SellRate.query.filter_by(plan_id=pid).order_by(SellRate.prefix).all()
    return render_template("rates.html", p=p, rates=rates)


@app.route("/plans/<int:pid>/rates/add", methods=["POST"])
@login_required
def rate_add(pid):
    RatePlan.query.get_or_404(pid)
    f = request.form
    # Soporta pegar CSV: prefix,description,rate,connect_fee,min,increment
    bulk = f.get("bulk", "").strip()
    if bulk:
        for line in bulk.splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < 2 or not parts[0]:
                continue
            db.session.add(SellRate(
                plan_id=pid, prefix=parts[0],
                description=parts[1] if len(parts) > 1 else None,
                rate_per_min=Decimal(parts[2]) if len(parts) > 2 else 0,
                connect_fee=Decimal(parts[3]) if len(parts) > 3 else 0,
                min_seconds=int(parts[4]) if len(parts) > 4 else 0,
                increment_seconds=int(parts[5]) if len(parts) > 5 else 60))
    else:
        db.session.add(SellRate(
            plan_id=pid, prefix=f["prefix"], description=f.get("description"),
            rate_per_min=Decimal(f["rate_per_min"]),
            connect_fee=Decimal(f.get("connect_fee") or 0),
            min_seconds=int(f.get("min_seconds") or 0),
            increment_seconds=int(f.get("increment_seconds") or 60)))
    db.session.commit()
    flash("Tarifa(s) añadidas.", "ok")
    return redirect(url_for("plan_rates", pid=pid))


@app.route("/rates/<int:rid>/delete", methods=["POST"])
@login_required
def rate_delete(rid):
    r = SellRate.query.get_or_404(rid)
    pid = r.plan_id
    db.session.delete(r)
    db.session.commit()
    return redirect(url_for("plan_rates", pid=pid))


# --------------------------- FACTURACION ----------------------------
@app.route("/billing/run", methods=["POST"])
@login_required
def billing_run():
    """Tarifica los CDR pendientes ejecutando rating.py."""
    script = os.path.join(BASE_DIR, "..", "billing", "rating.py")
    try:
        out = subprocess.run(["python3", script], capture_output=True,
                             text=True, timeout=120)
        flash(f"Rating: {out.stdout.strip() or out.stderr.strip()}", "ok")
    except Exception as e:
        flash(f"Error al tarificar: {e}", "error")
    return redirect(url_for("invoices"))


@app.route("/invoices")
@login_required
def invoices():
    rows = Invoice.query.order_by(Invoice.id.desc()).limit(200).all()
    clients = Client.query.order_by(Client.client).all()
    return render_template("invoices.html", invoices=rows, clients=clients)


@app.route("/invoices/generate", methods=["POST"])
@login_required
def invoice_generate():
    f = request.form
    start = datetime.date.fromisoformat(f["period_start"])
    end = datetime.date.fromisoformat(f["period_end"])
    targets = ([f["client"]] if f.get("client")
               else [c.client for c in Client.query.all()])
    created = 0
    for cl in targets:
        recs = BillingRecord.query.filter(
            BillingRecord.client == cl,
            BillingRecord.invoice_id.is_(None),
            func.date(BillingRecord.created_at) >= start,
            func.date(BillingRecord.created_at) <= end).all()
        if not recs:
            continue
        total = sum(Decimal(r.amount) for r in recs)
        cost = sum(Decimal(r.cost) for r in recs if r.cost is not None)
        minutes = sum(r.billed_seconds for r in recs) / 60.0
        inv = Invoice(client=cl, period_start=start, period_end=end,
                      currency=recs[0].currency, total_calls=len(recs),
                      total_minutes=round(minutes, 2), total_amount=total,
                      total_cost=cost or None, status="draft")
        db.session.add(inv)
        db.session.flush()
        for r in recs:
            r.invoice_id = inv.id
        created += 1
    db.session.commit()
    flash(f"{created} factura(s) generada(s).", "ok")
    return redirect(url_for("invoices"))


@app.route("/invoices/<int:iid>")
@login_required
def invoice_detail(iid):
    inv = Invoice.query.get_or_404(iid)
    # Resumen por prefijo/destino
    summary = db.session.query(
        BillingRecord.prefix_matched,
        func.count(BillingRecord.id),
        func.sum(BillingRecord.billed_seconds),
        func.sum(BillingRecord.amount)
    ).filter(BillingRecord.invoice_id == iid).group_by(
        BillingRecord.prefix_matched).all()
    return render_template("invoice_detail.html", inv=inv, summary=summary)


@app.route("/invoices/<int:iid>/status/<status>", methods=["POST"])
@login_required
def invoice_status(iid, status):
    inv = Invoice.query.get_or_404(iid)
    if status in ("draft", "issued", "paid"):
        inv.status = status
        db.session.commit()
        flash(f"Factura marcada como {status}.", "ok")
    return redirect(url_for("invoice_detail", iid=iid))


# --------------------------- FRAUDE / CDR ---------------------------
@app.route("/fraud")
@login_required
def fraud():
    rows = FraudEvent.query.order_by(FraudEvent.id.desc()).limit(200).all()
    return render_template("fraud.html", events=rows)


@app.route("/cdr")
@login_required
def cdr():
    client = request.args.get("client", "")
    query = CDR.query
    if client:
        query = query.filter_by(accountcode=client)
    rows = query.order_by(CDR.id.desc()).limit(200).all()
    clients = Client.query.order_by(Client.client).all()
    return render_template("cdr.html", rows=rows, clients=clients, sel=client)


# --------------------------- APLICAR CONFIG -------------------------
@app.route("/apply-config", methods=["POST"])
@login_required
def apply_config():
    """Regenera pjsip/extensions desde la BD y recarga Asterisk."""
    script = os.path.join(BASE_DIR, "config_generator.py")
    try:
        out = subprocess.run(["sudo", "python3", script], capture_output=True,
                             text=True, timeout=60)
        msg = out.stdout.strip() or out.stderr.strip()
        flash(f"Config aplicada. {msg}", "ok")
    except Exception as e:
        flash(f"Error al aplicar config: {e}", "error")
    return redirect(request.referrer or url_for("dashboard"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=False)
