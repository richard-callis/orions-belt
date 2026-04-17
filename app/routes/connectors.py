from flask import Blueprint, render_template

bp = Blueprint("connectors", __name__, url_prefix="/connectors")


@bp.route("/")
@bp.route("")
def index():
    return render_template("connectors.html")
