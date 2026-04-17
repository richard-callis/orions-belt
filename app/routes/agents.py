from flask import Blueprint, render_template

bp = Blueprint("agents", __name__, url_prefix="/agents")


@bp.route("/")
@bp.route("")
def index():
    return render_template("agents.html")
