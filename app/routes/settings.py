from flask import Blueprint, render_template, jsonify

bp = Blueprint("settings", __name__)


@bp.route("/settings")
def index():
    return render_template("settings.html")


@bp.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": "Orion's Belt"})
