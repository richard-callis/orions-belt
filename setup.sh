#!/usr/bin/env bash
# Orion's Belt — Linux/Mac setup
# Run once after cloning.  Re-running is safe (venv is reused).

set -euo pipefail

echo ""
echo " * * *  Orion's Belt Setup  * * *"
echo ""

# ── SSL-aware pip helper ──────────────────────────────────────────────────────
# Usage: pip_install [extra pip flags] -- <packages>
#        pip_install -r requirements.txt
# On SSL failure, prompts the user before retrying with --trusted-host.
SSL_BYPASS=0   # set to 1 once the user approves it for this session

_ask_ssl_bypass() {
    echo ""
    echo "  ╔══════════════════════════════════════════════════════╗"
    echo "  ║  SSL certificate error detected during pip install.  ║"
    echo "  ║  This is common on corporate/enterprise networks     ║"
    echo "  ║  that use a TLS-inspecting proxy.                    ║"
    echo "  ╠══════════════════════════════════════════════════════╣"
    echo "  ║  Bypass option: --trusted-host pypi.org              ║"
    echo "  ║                 --trusted-host files.pythonhosted.org║"
    echo "  ║                                                       ║"
    echo "  ║  WARNING: bypassing SSL reduces security.  Only do   ║"
    echo "  ║  this on a trusted network.                          ║"
    echo "  ╚══════════════════════════════════════════════════════╝"
    echo ""
    printf "  Allow SSL bypass for this install session? [y/N] "
    read -r answer
    if [[ "${answer,,}" == "y" || "${answer,,}" == "yes" ]]; then
        SSL_BYPASS=1
        echo "  SSL bypass enabled for this session."
    else
        echo "  SSL bypass declined. You may need to fix your certificates:"
        echo "    export PIP_CERT=/path/to/your-ca-bundle.pem"
        echo "    pip config set global.cert /path/to/your-ca-bundle.pem"
        return 1
    fi
}

pip_install() {
    local tmp
    tmp=$(mktemp)

    # If bypass already approved for this session, go straight to trusted-host
    if [ "$SSL_BYPASS" -eq 1 ]; then
        pip install \
            --trusted-host pypi.org \
            --trusted-host files.pythonhosted.org \
            "$@"
        return $?
    fi

    # First attempt — normal install
    if pip install "$@" > >(tee "$tmp") 2>&1; then
        rm -f "$tmp"
        return 0
    fi

    local exit_code=$?

    # Check if the failure was SSL-related
    if grep -qiE "ssl|certificate|CERTIFICATE_VERIFY_FAILED" "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        if _ask_ssl_bypass; then
            pip install \
                --trusted-host pypi.org \
                --trusted-host files.pythonhosted.org \
                "$@"
            return $?
        else
            return 1
        fi
    fi

    rm -f "$tmp"
    return $exit_code
}
# ─────────────────────────────────────────────────────────────────────────────

# 1. Virtual environment
echo "[1/5] Creating virtual environment..."
if [ -d ".venv" ]; then
    echo "  .venv already exists — reusing it."
    echo "  To start fresh: rm -rf .venv && bash setup.sh"
else
    python3 -m venv .venv
fi
source .venv/bin/activate

# 2. Upgrade pip itself
echo "[2/5] Upgrading pip..."
pip_install --upgrade pip --quiet

# 3. Core dependencies
echo "[3/5] Installing core dependencies..."
pip_install -r requirements.txt

# PyTorch — CPU-only build (no CUDA runtime needed, avoids DLL issues on Windows via WSL)
echo "  Installing PyTorch CPU-only build..."
pip uninstall torch -y --quiet 2>/dev/null || true
pip_install "torch==2.7.1+cpu" --index-url https://download.pytorch.org/whl/cpu --quiet || {
    echo "  WARNING: PyTorch CPU wheel failed. PII Guard stages 2+3 will be disabled."
    echo "  Retry: source .venv/bin/activate && pip install torch --index-url https://download.pytorch.org/whl/cpu"
}

# 4. spaCy language model
echo "[4/5] Downloading spaCy model..."
if python -c "import spacy; spacy.load('en_core_web_lg')" 2>/dev/null; then
    echo "  en_core_web_lg already installed — skipping."
else
    python -m spacy download en_core_web_lg || {
        echo "  WARNING: spaCy model download failed."
        echo "  Retry later: source .venv/bin/activate && python -m spacy download en_core_web_lg"
    }
fi

# 5. Local directories
echo "[5/5] Creating local directories..."
mkdir -p logs models

echo ""
echo " =========================================="
echo "  Setup complete!"
echo " =========================================="
echo ""
echo "  To start: source .venv/bin/activate && python launch.py"
echo "  Or just:  bash run.sh  (if you add one)"
echo ""
