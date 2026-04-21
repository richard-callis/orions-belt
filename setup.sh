#!/usr/bin/env bash
# Orion's Belt — Linux/Mac setup
# Run once after cloning.  Re-running is safe (venv is reused).

set -euo pipefail

echo ""
echo " * * *  Orion's Belt Setup  * * *"
echo ""
echo " Estimated total time: 10-40 min  (network speed varies)"
echo ""
echo " Steps:"
echo "   [1/6] Create virtual environment      ~10s"
echo "   [2/6] Upgrade pip                     ~15s"
echo "   [3/6] Install core dependencies       ~1-3 min"
echo "   [4/6] Install NLP stack               ~5-15 min"
echo "   [5/6] Download spaCy model            ~30s"
echo "   [6/6] Download AI models  ~670MB      ~5-30 min"
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
echo "[1/6] Creating virtual environment..."
if [ -d ".venv" ]; then
    echo "  .venv already exists — reusing it."
    echo "  To start fresh: rm -rf .venv && bash setup.sh"
else
    python3 -m venv .venv
fi
source .venv/bin/activate

# 2. Upgrade pip itself
echo "[2/6] Upgrading pip..."
pip_install --upgrade pip --quiet

# 3. Core dependencies
echo "[3/6] Installing core dependencies..."
pip_install -r requirements.txt

# PyTorch — CPU-only build (no CUDA runtime needed, avoids DLL issues on Windows via WSL)
echo "  Installing PyTorch CPU-only build..."
pip uninstall torch -y --quiet 2>/dev/null || true
pip_install "torch==2.7.1+cpu" --index-url https://download.pytorch.org/whl/cpu --quiet || {
    echo "  WARNING: PyTorch CPU wheel failed. PII Guard stages 2+3 will be disabled."
    echo "  Retry: source .venv/bin/activate && pip install torch --index-url https://download.pytorch.org/whl/cpu"
}

# GLiNER — zero-shot NER model (stage 2 PII detection, handles any casing)
echo "  Installing GLiNER..."
pip_install gliner --quiet || {
    echo "  WARNING: GLiNER install failed. Stage 2 NER will be disabled."
    echo "  Retry: source .venv/bin/activate && pip install gliner"
}

# protobuf — required by transformers tokenizers (DeBERTa PHI judge)
pip_install protobuf --quiet || true

# 4. spaCy language model
echo "[5/6] Downloading spaCy model..."
export SSL_BYPASS
python install_spacy_model.py || {
    echo "  WARNING: spaCy model download failed."
    echo "  Retry later: source .venv/bin/activate && python -m spacy download en_core_web_sm"
}

# 5. Local directories (no echo — part of step 5 flow)
mkdir -p logs models

# 6. Download AI models
echo "[6/6] Downloading AI models (~670MB)..."
echo "    gliner_medium-v2.1        ~400MB   PII detection"
echo "    nli-deberta-v3-small      ~180MB   PHI judge"
echo "    all-MiniLM-L6-v2          ~90MB    Memory embeddings"
echo ""
export SSL_BYPASS
python download_models.py
if [ $? -ne 0 ]; then
    echo "  WARNING: Some models failed. Retry: python download_models.py"
fi

echo ""
echo " =========================================="
echo "  Setup complete!"
echo " =========================================="
echo ""
echo "  To start: source .venv/bin/activate && python launch.py"
echo "  Or just:  bash run.sh  (if you add one)"
echo ""
