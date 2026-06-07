from flask import Flask, Response, g, jsonify, request
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
            chat, work, agent, connector, mcp_tool, memory, logs, pii, settings, nova,
            chat_room, chat_room_goal
        )

    # Register blueprints
    from app.routes.auth import bp as auth_bp
    from app.routes.chat import bp as chat_bp
    from app.routes.work import bp as work_bp
    from app.routes.agents import bp as agents_bp
    from app.routes.connectors import bp as connectors_bp
    from app.routes.mcp import bp as mcp_bp
    from app.routes.memory import bp as memory_bp
    from app.routes.logs import bp as logs_bp
    from app.routes.settings import bp as settings_bp
    from app.routes.first_run import bp as first_run_bp
    from app.routes.nova import bp as nova_bp
    from app.routes.chat_rooms import bp as chat_rooms_bp
    from app.routes.system import bp as system_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(work_bp)
    app.register_blueprint(agents_bp)
    app.register_blueprint(connectors_bp)
    app.register_blueprint(mcp_bp)
    app.register_blueprint(memory_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(first_run_bp)
    app.register_blueprint(nova_bp)
    app.register_blueprint(chat_rooms_bp)
    app.register_blueprint(system_bp)

    # ── Plugin system — load extensions at startup ─────────────────────────────
    try:
        from app.services.plugins import get_plugin_manager
        pm = get_plugin_manager()
        results = pm.load_all()
        with app.app_context():
            for r in results:
                if r["status"] == "loaded":
                    app.logger.info(f"Plugin loaded: {r['name']}")
                elif r["status"] == "error":
                    app.logger.warning(f"Plugin failed: {r['name']} — {r.get('error', 'unknown')}")
    except Exception as e:
        app.logger.warning(f"Plugin system error: {e}")

    # ── Authentication middleware (global before_request) ────────────────────
    @app.before_request
    def auth_check():
        """Enforce authentication on all protected routes.

        API routes (URL contains /api/) return 401 for unauthenticated requests.
        HTML page routes allow through so checkAuth() in base.html can show the overlay.
        """
        # Skip for public routes
        public_paths = (
            "/api/auth/login",
            "/api/auth/logout",
            "/api/health",
            "/first-run",
            "/api/first-run/",
            "/static/",
        )
        if request.path == "/":
            return None  # root redirect is public
        if any(request.path.startswith(p) for p in public_paths):
            return None

        from app.auth import check_auth
        user, authenticated = check_auth()
        if not authenticated:
            # API routes return 401 immediately
            if "/api/" in request.path:
                return jsonify({"error": "Unauthorized", "detail": "Authentication required"}), 401
            # HTML page routes: allow through so checkAuth() JS shows the overlay
            g.current_user = None
            return None

        g.current_user = user

    # ── 401 error handler ──────────────────────────────────────────────────
    @app.errorhandler(401)
    def unauthorized(e):
        return jsonify({"error": "Unauthorized", "detail": "Authentication required"}), 401

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
