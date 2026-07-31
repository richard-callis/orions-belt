#!/usr/bin/env python3
"""
Orion's Belt — unified installer + launcher.

The single cross-platform entry point for running from source. Detects
whether it's running on Windows or Linux/macOS, creates (or reuses) a
virtual environment, installs dependencies the right way for that platform,
downloads the AI models, and starts the app — replacing the previous
separate setup.sh/setup.bat + run.sh/run.bat pairs, which duplicated the
same install logic twice and could drift out of sync with each other.

Usage:
    python3 install.py     (Linux/macOS)
    python install.py      (Windows)

Safe to run repeatedly: an existing .venv is reused, and setup is skipped
entirely once it's already been done — every run after the first just
starts the app. run.sh/run.bat are thin OS-native wrappers around this
script, kept only because you can't reliably double-click a .py file
without OS-specific file-association setup.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
IS_WINDOWS = platform.system() == "Windows"

MIN_PYTHON = (3, 11)

# Pinned CPU-only build — the plain PyPI `torch` wheel defaults to a CUDA
# build whose native DLLs/shared libs fail to initialize on machines without
# a matching CUDA runtime (common — most users don't have one). 2.7.1+cpu is
# the known-good pin; newer builds (2.11+) have had DLL init failures on
# Windows. This one index-url serves working CPU wheels for both Windows and
# Linux, so no further OS branching is needed here.
TORCH_SPEC = "torch==2.7.1+cpu"
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"

# Becomes True for the rest of this run once the user approves bypassing SSL
# verification (corporate TLS-inspecting proxy) after a failed install.
_ssl_bypass = False


# ── venv path helpers ──────────────────────────────────────────────────────

def venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def venv_exists() -> bool:
    return venv_python().exists()


# ── pip, with the same corporate-proxy SSL-bypass prompt the old
#    setup.sh/setup.bat had ──────────────────────────────────────────────────

def _looks_like_ssl_error(text: str) -> bool:
    text = text.lower()
    return any(k in text for k in ("ssl", "certificate", "certificate_verify_failed"))


def _ask_ssl_bypass() -> bool:
    print()
    print("  " + "=" * 60)
    print("  SSL certificate error detected during pip install.")
    print("  This is common on corporate/enterprise networks that use")
    print("  a TLS-inspecting proxy.")
    print()
    print("  Bypass option adds: --trusted-host pypi.org")
    print("                      --trusted-host files.pythonhosted.org")
    print("  WARNING: bypassing SSL reduces security. Only do this on")
    print("  a network you trust.")
    print("  " + "=" * 60)
    try:
        answer = input("  Allow SSL bypass for this install session? [y/N] ").strip().lower()
    except EOFError:
        # Non-interactive (CI, piped input, etc.) — can't prompt, so decline
        # rather than hang.
        answer = ""
    if answer in ("y", "yes"):
        print("  SSL bypass enabled for this session.")
        return True
    print("  SSL bypass declined. You may need to fix your certificates, e.g.:")
    print("    pip config set global.cert /path/to/your-ca-bundle.pem")
    return False


def pip_install(args: list[str], quiet: bool = False) -> bool:
    """Run `<venv python> -m pip install <args>`. On an SSL-looking failure,
    offers the corporate-proxy bypass once per run (remembered for every
    subsequent call in this run, matching the old scripts' behavior)."""
    global _ssl_bypass
    cmd = [str(venv_python()), "-m", "pip", "install"]
    if quiet:
        cmd.append("--quiet")
    if _ssl_bypass:
        # download.pytorch.org is included because the PyTorch CPU-wheel
        # install below also goes through this same helper — the old
        # setup.bat's torch-specific retry included it too; omitting it
        # here would mean a proxy that intercepts pytorch.org specifically
        # still fails even after the user approves the bypass.
        cmd += ["--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org",
                "--trusted-host", "download.pytorch.org"]
    cmd += args

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return True

    combined = result.stdout + result.stderr
    if not _ssl_bypass and _looks_like_ssl_error(combined):
        if _ask_ssl_bypass():
            _ssl_bypass = True
            return pip_install(args, quiet=quiet)
        return False

    print(combined, file=sys.stderr)
    return False


# ── Setup steps ────────────────────────────────────────────────────────────

def find_system_python() -> str:
    """The interpreter currently running this script — run.sh/run.bat are
    responsible for invoking us with the right platform launcher name
    (python3 on Linux/macOS, python on Windows), so sys.executable IS that
    resolved interpreter."""
    if sys.version_info < MIN_PYTHON:
        sys.exit(
            f"ERROR: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required "
            f"(found {sys.version.split()[0]}). Install a newer Python and try again."
        )
    return sys.executable


def create_venv() -> None:
    # Check venv_exists() (the python binary), NOT just VENV_DIR.exists() —
    # a .venv directory can exist without a working interpreter inside it
    # (e.g. venv creation was interrupted). Checking the directory alone
    # would treat that half-built venv as "already done" and skip straight
    # to pip_install, which then fails with an unhandled FileNotFoundError
    # the first time it tries to run the (nonexistent) venv python.
    if venv_exists():
        print("  .venv already exists — reusing it.")
        return
    if VENV_DIR.exists():
        print("  .venv exists but looks incomplete — recreating it...")
    print("  Creating virtual environment...")
    system_python = find_system_python()
    subprocess.run([system_python, "-m", "venv", str(VENV_DIR)], check=True)


def install_dependencies() -> None:
    print("  Upgrading pip...")
    pip_install(["--upgrade", "pip"], quiet=True)

    print("  Installing core dependencies...")
    if not pip_install(["-r", str(ROOT / "requirements.txt")]):
        sys.exit("ERROR: core dependency install failed — see the output above.")

    print("  Installing PyTorch (CPU-only build)...")
    if not pip_install(["--force-reinstall", TORCH_SPEC, "--index-url", TORCH_INDEX], quiet=True):
        print("  WARNING: PyTorch CPU install failed. PII Guard stages 2+3 will be disabled.")
        print(f"  Retry later: {venv_python()} -m pip install --force-reinstall "
              f"\"{TORCH_SPEC}\" --index-url {TORCH_INDEX}")

    print("  Installing spaCy language model...")
    env = os.environ.copy()
    env["SSL_BYPASS"] = "1" if _ssl_bypass else "0"
    result = subprocess.run([str(venv_python()), str(ROOT / "install_spacy_model.py")], env=env)
    if result.returncode != 0:
        print("  WARNING: spaCy model download failed.")
        print(f"  Retry later: {venv_python()} -m spacy download en_core_web_sm")


def download_ai_models() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "models").mkdir(exist_ok=True)
    print("  Downloading AI models (~670MB total)...")
    print("    gliner_medium-v2.1        ~400MB   PII detection")
    print("    nli-deberta-v3-small      ~180MB   PHI judge")
    print("    all-MiniLM-L6-v2          ~90MB    Memory embeddings")
    env = os.environ.copy()
    env["SSL_BYPASS"] = "1" if _ssl_bypass else "0"
    result = subprocess.run([str(venv_python()), str(ROOT / "download_models.py")], env=env)
    if result.returncode != 0:
        print(f"  WARNING: some models failed. Retry: {venv_python()} download_models.py")


def run_setup() -> None:
    print()
    print(" * * *  Orion's Belt Setup  * * *")
    print()
    print(f" Detected platform: {'Windows' if IS_WINDOWS else platform.system()}")
    print(" Estimated total time: 10-40 min (network speed varies)")
    print()

    print("[1/3] Creating virtual environment...")
    create_venv()

    print("\n[2/3] Installing dependencies...")
    install_dependencies()

    print("\n[3/3] Downloading AI models...")
    download_ai_models()

    print()
    print(" ==========================================")
    print("  Setup complete!")
    print(" ==========================================")
    print()


def start_app() -> None:
    print("Starting Orion's Belt...\n")
    os.chdir(ROOT)
    python = str(venv_python())
    launch = str(ROOT / "launch.py")
    # os.execv replaces this process outright rather than spawning a child —
    # matches run.sh's `exec python launch.py`, so there's exactly one
    # process (no orphaned installer process sitting around the app's
    # lifetime) and Ctrl+C / window-close signals reach launch.py directly.
    os.execv(python, [python, launch])


def main() -> None:
    if not venv_exists():
        run_setup()
    start_app()


if __name__ == "__main__":
    main()
