"""
Modelos de datos del panel ITSP (SQLAlchemy).
Mapean a las tablas creadas por sql/schema.sql + monitoring + billing.
"""
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class Client(db.Model):
    __tablename__ = "clients"
    id = db.Column(db.Integer, primary_key=True)
    client = db.Column(db.String(64), unique=True, nullable=False)   # id logico
    name = db.Column(db.String(128), nullable=False)
    company = db.Column(db.String(128))
    email = db.Column(db.String(128))
    max_channels = db.Column(db.Integer, default=10)
    active = db.Column(db.Boolean, default=True)
    billing_type = db.Column(db.String(16), default="postpaid")      # prepaid|postpaid
    balance = db.Column(db.Numeric(12, 4), default=0)
    credit_limit = db.Column(db.Numeric(12, 4), default=0)
    rate_plan_id = db.Column(db.Integer)
    currency = db.Column(db.String(3), default="USD")


class Connection(db.Model):
    __tablename__ = "connections"
    id = db.Column(db.Integer, primary_key=True)
    client = db.Column(db.String(64), nullable=False)
    label = db.Column(db.String(64))
    username = db.Column(db.String(64), unique=True, nullable=False)
    password = db.Column(db.String(128), nullable=False)
    transport = db.Column(db.String(8), default="udp")
    max_channels = db.Column(db.Integer, default=5)
    allowed_ip = db.Column(db.String(64))
    provider_order = db.Column(db.String(255))    # override LCR, coma-separado
    active = db.Column(db.Boolean, default=True)


class Provider(db.Model):
    __tablename__ = "providers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    host = db.Column(db.String(128), nullable=False)
    port = db.Column(db.Integer, default=5060)
    auth_type = db.Column(db.String(16), default="userpass")   # userpass|ip
    username = db.Column(db.String(128))
    password = db.Column(db.String(128))
    transport = db.Column(db.String(8), default="udp")
    from_domain = db.Column(db.String(128))
    codecs = db.Column(db.String(128), default="ulaw,alaw")
    ip_cidrs = db.Column(db.String(255))
    priority = db.Column(db.Integer, default=100)
    active = db.Column(db.Boolean, default=True)


class ClientDID(db.Model):
    __tablename__ = "client_dids"
    id = db.Column(db.Integer, primary_key=True)
    client = db.Column(db.String(64), nullable=False)
    number = db.Column(db.String(32), unique=True, nullable=False)
    provider = db.Column(db.String(32))
    active = db.Column(db.Boolean, default=True)


class RatePlan(db.Model):
    __tablename__ = "rate_plans"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    currency = db.Column(db.String(3), default="USD")
    description = db.Column(db.String(255))
    active = db.Column(db.Boolean, default=True)


class SellRate(db.Model):
    __tablename__ = "sell_rates"
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, nullable=False)
    prefix = db.Column(db.String(24), nullable=False)
    description = db.Column(db.String(128))
    rate_per_min = db.Column(db.Numeric(10, 5), nullable=False)
    connect_fee = db.Column(db.Numeric(10, 5), default=0)
    min_seconds = db.Column(db.Integer, default=0)
    increment_seconds = db.Column(db.Integer, default=60)


class BuyRate(db.Model):
    __tablename__ = "buy_rates"
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(64), nullable=False)
    prefix = db.Column(db.String(24), nullable=False)
    description = db.Column(db.String(128))
    rate_per_min = db.Column(db.Numeric(10, 5), nullable=False)
    increment_seconds = db.Column(db.Integer, default=60)


class BillingRecord(db.Model):
    __tablename__ = "billing_records"
    id = db.Column(db.BigInteger, primary_key=True)
    cdr_id = db.Column(db.BigInteger, unique=True, nullable=False)
    client = db.Column(db.String(64), nullable=False)
    dst = db.Column(db.String(80))
    prefix_matched = db.Column(db.String(24))
    plan_id = db.Column(db.Integer)
    billsec = db.Column(db.Integer, default=0)
    billed_seconds = db.Column(db.Integer, default=0)
    rate_per_min = db.Column(db.Numeric(10, 5), default=0)
    amount = db.Column(db.Numeric(12, 5), default=0)
    cost = db.Column(db.Numeric(12, 5))
    currency = db.Column(db.String(3), default="USD")
    invoice_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime)


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    client = db.Column(db.String(64), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    currency = db.Column(db.String(3), default="USD")
    total_calls = db.Column(db.Integer, default=0)
    total_minutes = db.Column(db.Numeric(12, 2), default=0)
    total_amount = db.Column(db.Numeric(12, 4), default=0)
    total_cost = db.Column(db.Numeric(12, 4))
    status = db.Column(db.String(16), default="draft")
    created_at = db.Column(db.DateTime)


class CDR(db.Model):
    __tablename__ = "cdr"
    id = db.Column(db.BigInteger, primary_key=True)
    calldate = db.Column(db.DateTime)
    src = db.Column(db.String(80))
    dst = db.Column(db.String(80))
    dcontext = db.Column(db.String(80))
    duration = db.Column(db.Integer)
    billsec = db.Column(db.Integer)
    disposition = db.Column(db.String(45))
    accountcode = db.Column(db.String(80))
    uniqueid = db.Column(db.String(150))


class FraudEvent(db.Model):
    __tablename__ = "fraud_events"
    id = db.Column(db.BigInteger, primary_key=True)
    ts = db.Column(db.DateTime)
    client = db.Column(db.String(64))
    rule = db.Column(db.String(64))
    detail = db.Column(db.String(255))
    action = db.Column(db.String(64))


class AdminUser(db.Model):
    __tablename__ = "admin_users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)
