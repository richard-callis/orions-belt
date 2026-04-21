"""
First-run model download page.
Shown by the launcher when required models are not yet cached.
A background thread runs snapshot_download() for each model while the browser
polls /api/first-run/status every 500 ms.
"""
import threading
from pathlib import Path

from flask import Blueprint, jsonify, render_template

bp = Blueprint("first_run", __name__)

# Shared state — written by the download thread, read by the status endpoint.
_state = {
    "started": False,
    "done": False,
    "error": None,
    "current_model": None,      # human label of model being fetched
    "current_index": 0,         # 1-based
    "total_models": 3,
    "models_done": [],          # completed model ids
}
_lock = threading.Lock()
_thread: threading.Thread | None = None

MODELS = [
    {"id": "urchade/gliner_medium-v2.1",              "label": "GLiNER medium v2.1",       "size": "~400 MB"},
    {"id": "cross-encoder/nli-deberta-v3-small",      "label": "NLI-DeBERTa v3 small",     "size": "~180 MB"},
    {"id": "sentence-transformers/all-MiniLM-L6-v2",  "label": "all-MiniLM-L6-v2",         "size": "~90 MB"},
]


def _is_cached(base_dir: Path, model_id: str) -> bool:
    folder = "models--" + model_id.replace("/", "--")
    marker = base_dir / "models" / "hub" / folder / "refs" / "main"
    return marker.exists()


def models_ready(base_dir: Path) -> bool:
    return all(_is_cached(base_dir, m["id"]) for m in MODELS)


def _download_all(base_dir: Path):
    from huggingface_hub import snapshot_download

    for i, m in enumerate(MODELS, 1):
        with _lock:
            _state["current_model"] = f"{m['label']} ({m['size']})"
            _state["current_index"] = i

        if _is_cached(base_dir, m["id"]):
            with _lock:
                _state["models_done"].append(m["id"])
            continue

        try:
            snapshot_download(m["id"])
            with _lock:
                _state["models_done"].append(m["id"])
        except Exception as exc:
            with _lock:
                _state["error"] = str(exc)
            return

    with _lock:
        _state["done"] = True
        _state["current_model"] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route("/first-run")
def page():
    return render_template("first_run.html", models=MODELS)


@bp.route("/api/first-run/start", methods=["POST"])
def start():
    global _thread
    with _lock:
        if _state["started"]:
            return jsonify({"ok": True, "already_started": True})
        _state["started"] = True

    from config import BASE_DIR
    _thread = threading.Thread(target=_download_all, args=(BASE_DIR,), daemon=True)
    _thread.start()
    return jsonify({"ok": True})


@bp.route("/api/first-run/status")
def status():
    with _lock:
        return jsonify(dict(_state))
