#!/usr/bin/env bash
# Orion's Belt — Linux/Mac setup (dev use)
set -e

echo ""
echo " * * *  Orion's Belt Setup  * * *"
echo ""

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip --quiet
pip install -r requirements.txt
python -m spacy download en_core_web_lg

mkdir -p logs models

echo ""
echo " Setup complete!"
echo " To start: source .venv/bin/activate && python launch.py"
echo ""
