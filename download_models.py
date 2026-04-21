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
import time
from pathlib import Path

# ── SSL bypass — must be set BEFORE any HuggingFace import ───────────────────
SSL_BYPASS = (
    os.environ.get("SSL_BYPASS", "0").strip() == "1"
    or "--ssl-bypass" in sys.argv
)
if SSL_BYPASS:
    os.environ["HF_HUB_DISABLE_SSL_VERIFICATION"] = "1"
    print("  SSL bypass enabled — certificate verification disabled for HF Hub.")

# ── Cache dirs — set before any HuggingFace import ───────────────────────────
BASE_DIR = Path(__file__).parent
os.environ["HF_HOME"] = str(BASE_DIR / "models")
os.environ["TRANSFORMERS_CACHE"] = str(BASE_DIR / "models" / "hub")

# ── Model list ────────────────────────────────────────────────────────────────
MODELS = [
    {
        "id": "urchade/gliner_medium-v2.1",
        "size_mb": 400,
        "size": "~400MB",
        "purpose": "Zero-shot NER — detects PII/PHI regardless of capitalization or format",
    },
    {
        "id": "cross-encoder/nli-deberta-v3-small",
        "size_mb": 180,
        "size": "~180MB",
        "purpose": "PHI judge — classifies ambiguous text as PII/PHI/safe",
    },
    {
        "id": "sentence-transformers/all-MiniLM-L6-v2",
        "size_mb": 90,
        "size": "~90MB",
        "purpose": "Memory embeddings — similarity recall across sessions",
    },
]

TOTAL_MB = sum(m["size_mb"] for m in MODELS)

# ── Error hint templates ──────────────────────────────────────────────────────
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


# ── Cache check ───────────────────────────────────────────────────────────────

def _is_cached(model_id: str) -> bool:
    """Return True if the model snapshot is already in the local HF cache."""
    folder = "models--" + model_id.replace("/", "--")
    # A completed snapshot has a refs/main pointer written by snapshot_download
    marker = BASE_DIR / "models" / "hub" / folder / "refs" / "main"
    return marker.exists()


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def _fmt_eta(elapsed_s: float, done_mb: float, remaining_mb: float) -> str:
    if elapsed_s <= 0 or done_mb <= 0:
        return "—"
    speed = done_mb / elapsed_s          # MB/s
    eta_s = remaining_mb / speed
    return f"~{_fmt_duration(eta_s)}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    from huggingface_hub import snapshot_download

    print()
    print("  Orion's Belt — Model Downloader")
    print("  ================================")
    print(f"  Cache : {BASE_DIR / 'models'}")
    print(f"  Total : {TOTAL_MB} MB across {len(MODELS)} models")
    if SSL_BYPASS:
        print("  Mode  : SSL bypass active")
    print()

    total = len(MODELS)
    failed = []
    had_ssl_error = False
    session_start = time.monotonic()
    completed_mb = 0.0

    for i, m in enumerate(MODELS, 1):
        print(f"  ─── [{i}/{total}] {m['id']} ({m['size']}) ───")
        print(f"       {m['purpose']}")

        if _is_cached(m["id"]):
            print("       ✓  Already cached — skipping\n")
            completed_mb += m["size_mb"]
            continue

        model_start = time.monotonic()
        print("       Downloading...\n", flush=True)

        try:
            snapshot_download(m["id"])   # tqdm per-file bars shown here

            elapsed = time.monotonic() - model_start
            completed_mb += m["size_mb"]

            remaining_mb = sum(m2["size_mb"] for m2 in MODELS[i:])
            total_elapsed = time.monotonic() - session_start
            eta = _fmt_eta(total_elapsed, completed_mb, remaining_mb)

            status = f"  ✓  Done in {_fmt_duration(elapsed)}"
            if remaining_mb > 0:
                status += f"   |   remaining: {eta}"
            print(f"\n{status}\n")

        except Exception as e:
            print(f"\n  ✗  FAILED: {e}\n")

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

    total_elapsed = time.monotonic() - session_start
    print(f"  {'─' * 46}")

    if failed:
        print(f"  WARNING: {len(failed)} model(s) failed to download:")
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
        cached_count = sum(1 for m in MODELS if _is_cached(m["id"]))
        print(f"  All {total} models ready  ({_fmt_duration(total_elapsed)} total)")
        print("  Run: python launch.py")
        print()


if __name__ == "__main__":
    main()
