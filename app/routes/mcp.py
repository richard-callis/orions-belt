import asyncio
from flask import Blueprint, jsonify, render_template, request
from app import db
from app.models.mcp_tool import MCPTool
from app.models.connector import AuthorizedDirectory
from app.services.mcp.tools import execute_tool

bp = Blueprint("mcp", __name__, url_prefix="/mcp")


@bp.route("/")
@bp.route("")
def index():
    return render_template("mcp.html")


@bp.route("/api/tools", methods=["GET"])
def list_tools():
    """Return all MCP tools from the database."""
    tools = MCPTool.query.order_by(MCPTool.tier, MCPTool.name).all()
    return jsonify([
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "tier": t.tier,
            "enabled": t.enabled,
            "source": t.source,
        }
        for t in tools
    ])


@bp.route("/api/run", methods=["POST"])
def run_tool():
    """Manually invoke an MCP tool and return its result.

    Request body: { "tool": "read_file", "args": {"path": "..."} }
    """
    body = request.get_json() or {}
    tool_name = body.get("tool", "").strip()
    args = body.get("args", {})

    if not tool_name:
        return jsonify({"error": "tool name is required"}), 400
    if not isinstance(args, dict):
        return jsonify({"error": "args must be a JSON object"}), 400

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(execute_tool(tool_name, args))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        loop.close()

    return jsonify({"result": result})


@bp.route("/api/directories", methods=["GET"])
def list_directories():
    """Return all authorized directories."""
    dirs = AuthorizedDirectory.query.all()
    return jsonify([
        {
            "id": d.id,
            "path": d.path,
            "alias": d.alias,
            "recursive": d.recursive,
            "read_only": d.read_only,
            "max_tier": d.max_tier,
            "enabled": d.enabled,
        }
        for d in dirs
    ])


@bp.route("/api/directories", methods=["POST"])
def add_directory():
    """Add an authorized directory."""
    body = request.get_json() or {}
    path = body.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400

    d = AuthorizedDirectory(
        path=path,
        alias=body.get("alias", path),
        recursive=body.get("recursive", True),
        read_only=body.get("read_only", False),
        max_tier=body.get("max_tier", 3),
    )
    db.session.add(d)
    db.session.commit()
    return jsonify({"success": True, "id": d.id}), 201


@bp.route("/api/directories/<dir_id>", methods=["PATCH"])
def toggle_directory(dir_id):
    """Enable or disable an authorized directory."""
    d = AuthorizedDirectory.query.get(dir_id)
    if not d:
        return jsonify({"error": "not found"}), 404
    body = request.get_json() or {}
    if "enabled" in body:
        d.enabled = bool(body["enabled"])
    db.session.commit()
    return jsonify({"success": True, "enabled": d.enabled})


@bp.route("/api/directories/<dir_id>", methods=["DELETE"])
def delete_directory(dir_id):
    """Remove an authorized directory."""
    d = AuthorizedDirectory.query.get(dir_id)
    if not d:
        return jsonify({"error": "not found"}), 404
    db.session.delete(d)
    db.session.commit()
    return "", 204
