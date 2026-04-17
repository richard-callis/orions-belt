"""
install_spacy_model.py — SSL-aware spaCy model installer.

Called by setup.bat / setup.sh.  Handles corporate proxy environments where
python -m spacy download fails because the compatibility-check HTTP request
(requests.get → urllib3) cannot be intercepted by pip's --trusted-host flag.

Strategy
--------
1. Skip if the model already loads successfully.
2. Try the normal `python -m spacy download` (works on unconstrained networks).
3. If that fails with an SSL error:
   a. Derive the model wheel URL from the installed spaCy version.
   b. Run `pip install <url> --trusted-host github.com ...` directly,
      bypassing the compatibility-check request entirely.
4. Exit 0 on success, 1 on failure (setup scripts check $? / %ERRORLEVEL%).
"""

import os
import subprocess
import sys

MODEL = "en_core_web_sm"

# Set by setup.bat/setup.sh when the user has approved the SSL bypass for this session
SSL_BYPASS = os.environ.get("SSL_BYPASS", "0").strip() == "1"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _model_ok() -> bool:
    """Return True if the model is already installed and loadable."""
    try:
        import spacy
        spacy.load(MODEL)
        return True
    except Exception:
        return False


def _spacy_version() -> str:
    import spacy
    return spacy.__version__


def _candidate_urls(ver: str) -> list[str]:
    """Build a list of wheel URLs to try, from most to least specific."""
    parts = ver.split(".")
    candidates = []
    # Exact version first (e.g. 3.8.3), then major.minor.0 (e.g. 3.8.0)
    for v in dict.fromkeys([ver, f"{parts[0]}.{parts[1]}.0"]):
        pkg = f"{MODEL}-{v}"
        candidates.append(
            f"https://github.com/explosion/spacy-models/releases/download/{pkg}/{pkg}-py3-none-any.whl"
        )
    return candidates


def _pip_install_direct(url: str) -> bool:
    """Install a wheel URL with trusted-host flags (SSL bypass)."""
    result = subprocess.run([
        sys.executable, "-m", "pip", "install", url,
        "--trusted-host", "github.com",
        "--trusted-host", "objects.githubusercontent.com",
        "--quiet",
    ])
    return result.returncode == 0


def _spacy_download_normal() -> bool:
    """Run python -m spacy download and return True on success."""
    result = subprocess.run(
        [sys.executable, "-m", "spacy", "download", MODEL],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True
    # Surface the error so the caller can inspect it
    combined = (result.stdout + result.stderr).lower()
    if any(kw in combined for kw in ("ssl", "certificate", "certificate_verify_failed")):
        print(f"  SSL error detected during spaCy download.", flush=True)
    else:
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", flush=True)
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    if _model_ok():
        print(f"  {MODEL} already installed — skipping.", flush=True)
        return 0

    print(f"  Downloading {MODEL}...", flush=True)

    # Step 1: try the normal path (works without a corporate proxy)
    if _spacy_download_normal():
        print(f"  {MODEL} installed successfully.", flush=True)
        return 0

    # Step 2: if SSL bypass is approved, go direct via pip
    if not SSL_BYPASS:
        print(
            f"  WARNING: {MODEL} download failed.\n"
            f"  If you are on a corporate network, re-run setup and approve the\n"
            f"  SSL bypass when prompted — it will be remembered for the session.",
            flush=True,
        )
        return 1

    print(
        "  Normal download failed — SSL bypass approved, trying direct pip install...",
        flush=True,
    )

    ver = _spacy_version()
    for url in _candidate_urls(ver):
        print(f"    → {url}", flush=True)
        if _pip_install_direct(url):
            if _model_ok():
                print(f"  {MODEL} installed successfully (via direct pip).", flush=True)
                return 0
            print("  Wheel installed but model failed to load — trying next candidate.", flush=True)

    print(
        f"  WARNING: {MODEL} could not be installed.\n"
        f"  Retry manually once the network is available:\n"
        f"    .venv\\Scripts\\activate && python -m spacy download {MODEL}",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
