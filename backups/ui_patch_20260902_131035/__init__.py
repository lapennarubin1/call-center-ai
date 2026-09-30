"""Application factory. core/ se registra siempre; modules/ se registran por
manifiesto (core/registry). Habilitar un modulo para un tenant es una fila
en tenant_modules, no un deploy."""
import logging
from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException
from .config import Config
from . import db
from .core.registry import register_module
from .core.tenants.context import load_tenant_context
from .core import routes as core_routes
from .core.auth import routes as auth_routes
from .modules import voice_ai, crm
from .cli import register_cli


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    logging.basicConfig(level=app.config["LOG_LEVEL"], format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db.init_db(app)
    app.before_request(load_tenant_context)

    app.register_blueprint(core_routes.bp)
    app.register_blueprint(auth_routes.bp)
    for mod in (voice_ai, crm):
        register_module(mod.MANIFEST)
        app.register_blueprint(mod.MANIFEST["blueprint"])

    register_cli(app)

    @app.errorhandler(HTTPException)
    def http_error(e):
        return jsonify(error=e.description, code=e.code), e.code

    @app.get("/")
    def root():
        return jsonify(service="saas_platform", api="/api/v1", health="/api/v1/health")

    return app
