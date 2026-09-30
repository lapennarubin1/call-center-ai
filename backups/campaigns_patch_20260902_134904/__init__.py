"""Application factory. core/ se registra siempre; modules/ se registran por
manifiesto (core/registry). Habilitar un modulo para un tenant es una fila
en tenant_modules, no un deploy."""
import logging
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from .config import Config
from . import db
from .core.registry import register_module
from .core.tenants.context import load_tenant_context
from .core import routes as core_routes
from .core.auth import routes as auth_routes
from .modules import voice_ai, crm
from .web import routes as web_routes
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
    # UI web (Jinja2). "/" pasa a resolver login/dashboard en vez del JSON
    # informativo anterior; la API sigue intacta en /api/v1/*.
    app.register_blueprint(web_routes.bp)

    register_cli(app)

    @app.errorhandler(HTTPException)
    def http_error(e):
        if e.code == 404:
            return render_template("errors/404.html"), 404
        if request.path.startswith("/api/"):
            return jsonify(error=e.description, code=e.code), e.code
        if e.code == 403:
            return render_template("errors/403.html", message=e.description), 403
        return jsonify(error=e.description, code=e.code), e.code

    return app
