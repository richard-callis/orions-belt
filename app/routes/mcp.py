from flask import Blueprint, render_template

bp = Blueprint("mcp", __name__, url_prefix="/mcp")


@bp.route("/")
def index():
    return render_template("mcp.html")
