from flask import Blueprint, render_template

bp = Blueprint("memory", __name__, url_prefix="/memory")


@bp.route("/")
def index():
    return render_template("memory.html")
