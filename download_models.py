"""
Download all required HuggingFace models into the local models/ cache.
Run once after setup: python download_models.py

SSL bypass (corporate proxy)
  Set SSL_BYPASS=1 in the environment before running:
    set SSL_BYPASS=1 && python download_models.py       (Windows)
    SSL_BYPASS=1 python download_models.py              (Linux/macOS)
  Or pass the flag directly:
    python download_models.py --ssl-bypass

  When active, HF_HUB_DISABLE_SSL_VERIFICATION=1 is set so huggingface_hub
  skips certificate checks.  Only use this on trusted networks.

Models are saved to the project's models/ directory (set in config.py).
Subsequent runs skip already-cached models.
"""
import os
import sys
from pathlib import Path

# ── SSL bypass — must be set BEFORE any HuggingFace import ───────────────────
SSL_BYPASS = (
    os.environ.get("SSL_BYPASS", "0").strip() == "1"
    or "--ssl-bypass" in sys.argv
)
if SSL_BYPASS:
    os.environ["HF_HUB_DISABLE_SSL_VERIFICATION"] = "1"
    # huggingface_hub uses requests; this env var makes it skip cert checks.
    # REQUESTS_CA_BUNDLE="" alone is not enough — the HF flag is the right hook.
    print("  SSL bypass enabled — certificate verification disabled for HF Hub.")

# ── Cache dirs — set before any HuggingFace import ───────────────────────────
BASE_DIR = Path(__file__).parent
os.environ["HF_HOME"] = str(BASE_DIR / "models")
os.environ["TRANSFORMERS_CACHE"] = str(BASE_DIR / "models" / "hub")

# ── Model list ────────────────────────────────────────────────────────────────
MODELS = [
    {
        "id": "urchade/gliner_medium-v2.1",
        "type": "gliner",
        "size": "~400MB",
        "purpose": "Zero-shot NER — detects PII/PHI regardless of capitalization or format",
    },
    {
        "id": "cross-encoder/nli-deberta-v3-small",
        "type": "pipeline",
        "task": "zero-shot-classification",
        "size": "~180MB",
        "purpose": "PHI judge — classifies ambiguous text as PII/PHI/safe",
    },
    {
        "id": "sentence-transformers/all-MiniLM-L6-v2",
        "type": "sentence_transformer",
        "size": "~90MB",
        "purpose": "Memory embeddings — similarity recall across sessions",
    },
]

# ── Download helpers ──────────────────────────────────────────────────────────

_DLL_HINT = (
    "DLL initialization failed — the C++ runtime or onnxruntime is broken.\n"
    "  Fix 1: install Microsoft Visual C++ 2015-2022 Redistributable (x64)\n"
    "           https://aka.ms/vs/17/release/vc_redist.x64.exe\n"
    "  Fix 2: force-reinstall onnxruntime:\n"
    "           pip install onnxruntime --force-reinstall\n"
    "  Until fixed, GLiNER (Stage 2 NER) will be disabled at runtime."
)

_SSL_HINT = (
    "SSL certificate error — corporate proxy intercept detected.\n"
    "  Re-run with SSL bypass:\n"
    "    set SSL_BYPASS=1 && python download_models.py   (Windows)\n"
    "    SSL_BYPASS=1 python download_models.py          (Linux/macOS)\n"
    "  Or: python download_models.py --ssl-bypass"
)


def _is_dll_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("dll", "1114", "onnxruntime_pybind11_state", "dynamic link library"))


def _is_ssl_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("ssl", "certificate", "certificate_verify_failed"))


def download_gliner(model_id: str):
    from gliner import GLiNER
    model = GLiNER.from_pretrained(model_id)
    del model


def download_pipeline(model_id: str, task: str):
    from transformers import pipeline
    pipe = pipeline(task, model=model_id)
    del pipe


def download_sentence_transformer(model_id: str):
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(model_id)
    del model


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print()
    print("  Orion's Belt — Model Downloader")
    print("  ================================")
    print(f"  Cache: {BASE_DIR / 'models'}")
    if SSL_BYPASS:
        print("  Mode:  SSL bypass active")
    print()

    total = len(MODELS)
    failed = []
    had_ssl_error = False

    for i, m in enumerate(MODELS, 1):
        print(f"  [{i}/{total}] {m['id']}")
        print(f"         {m['purpose']}")
        print(f"         Size: {m['size']}", end=" ", flush=True)

        try:
            if m["type"] == "gliner":
                download_gliner(m["id"])
            elif m["type"] == "sentence_transformer":
                download_sentence_transformer(m["id"])
            else:
                download_pipeline(m["id"], m["task"])
            print("✓ done")

        except Exception as e:
            print(f"✗ FAILED: {e}")

            if _is_dll_error(e):
                for line in _DLL_HINT.splitlines():
                    print(f"  {line}")
            elif _is_ssl_error(e):
                had_ssl_error = True
                if not SSL_BYPASS:
                    for line in _SSL_HINT.splitlines():
                        print(f"  {line}")

            failed.append(m["id"])

        print()

    if failed:
        print(f"  WARNING: {len(failed)} model(s) failed:")
        for f in failed:
            print(f"    - {f}")
        print("  The app will attempt to download them on first use.")
        if had_ssl_error and not SSL_BYPASS:
            print("  Re-run with SSL bypass: python download_models.py --ssl-bypass")
        else:
            print("  Retry: python download_models.py")
        print()
        sys.exit(1)
    else:
        print("  All models downloaded successfully.")
        print("  Run: python launch.py")
        print()


if __name__ == "__main__":
    main()
