# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Orion's Belt.

Produces a single OrionsBelt.exe that bundles Python + all dependencies.
Models (~670 MB) are NOT embedded — they download on first run via the
/first-run page and are cached next to the exe in models/.

Build:
    pip install pyinstaller
    pyinstaller orions_belt.spec

Output: dist/OrionsBelt.exe
"""
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [
    ("app/templates", "app/templates"),
    ("app/static",    "app/static"),
]
binaries    = []
hiddenimports = [
    "app",
    "app.models.chat",
    "app.models.work",
    "app.models.agent",
    "app.models.connector",
    "app.models.mcp_tool",
    "app.models.memory",
    "app.models.logs",
    "app.models.pii",
    "app.models.settings",
    "config",
    "sqlalchemy.dialects.sqlite",
    "flask_migrate",
    "flask_sqlalchemy",
]

# Heavy packages — collect everything (data files, binaries, hidden imports)
for pkg in [
    "torch",
    "transformers",
    "tokenizers",
    "huggingface_hub",
    "gliner",
    "sentence_transformers",
    "spacy",
    "en_core_web_sm",
    "presidio_analyzer",
    "presidio_anonymizer",
    "webview",
    "pystray",
    "PIL",
    "jinja2",
]:
    try:
        d, b, h = collect_all(pkg)
        datas         += d
        binaries      += b
        hiddenimports += h
    except Exception:
        pass  # package not installed — skip silently

a = Analysis(
    ["launch.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "notebook", "IPython", "pytest", "setuptools"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="OrionsBelt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=["vcruntime140.dll", "python3*.dll"],
    runtime_tmpdir=None,
    console=False,   # no terminal window — app opens its own webview
    icon=None,
    onefile=True,
)
