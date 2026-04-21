from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy()
migrate = Migrate()


def create_app(config_object="config.Config"):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(config_object)

    # Extensions
    db.init_app(app)
    migrate.init_app(app, db)

    # Register models (ensures they're known to SQLAlchemy)
    with app.app_context():
        from app.models import (  # noqa: F401
            chat, work, agent, connector, mcp_tool, memory, logs, pii, settings
        )

    # Register blueprints
    from app.routes.chat import bp as chat_bp
    from app.routes.work import bp as work_bp
    from app.routes.agents import bp as agents_bp
    from app.routes.connectors import bp as connectors_bp
    from app.routes.mcp import bp as mcp_bp
    from app.routes.memory import bp as memory_bp
    from app.routes.logs import bp as logs_bp
    from app.routes.settings import bp as settings_bp
    from app.routes.first_run import bp as first_run_bp

    app.register_blueprint(chat_bp)
    app.register_blueprint(work_bp)
    app.register_blueprint(agents_bp)
    app.register_blueprint(connectors_bp)
    app.register_blueprint(mcp_bp)
    app.register_blueprint(memory_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(first_run_bp)

    # Root redirect
    from flask import redirect, url_for

    @app.route("/")
    def index():
        return redirect(url_for("chat.index"))

    # ── Error handlers ─────────────────────────────────────────────────────────
    # SECURITY NOTE: No CSRF middleware is applied. This is intentional for a
    # localhost-only application. The server binds exclusively to 127.0.0.1
    # (see launch.py). All mutating API routes use JSON Content-Type, which
    # triggers a CORS preflight from cross-origin pages, preventing CSRF.
    # If this app is ever exposed beyond localhost, add Flask-WTF CSRF protection.

    from flask import jsonify as _jsonify
    import logging as _logging
    _err_log = _logging.getLogger("orions-belt.errors")

    @app.errorhandler(400)
    def bad_request(e):
        return _jsonify({"error": "Bad request", "detail": str(e)}), 400

    @app.errorhandler(404)
    def not_found(e):
        return _jsonify({"error": "Not found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return _jsonify({"error": "Method not allowed"}), 405

    @app.errorhandler(500)
    def internal_error(e):
        # SECURITY: never expose exception details to the client
        _err_log.exception("Internal server error")
        return _jsonify({"error": "Internal server error"}), 500

    return app
