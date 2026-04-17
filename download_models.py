"""
Download all required HuggingFace models into the local models/ cache.
Run once after setup: python download_models.py

Models are saved to the project's models/ directory (set in config.py).
Subsequent runs skip already-cached models.
"""
import sys
import os
from pathlib import Path

# Set cache dirs before importing anything HuggingFace-related
BASE_DIR = Path(__file__).parent
os.environ["HF_HOME"] = str(BASE_DIR / "models")
os.environ["TRANSFORMERS_CACHE"] = str(BASE_DIR / "models" / "hub")

MODELS = [
    {
        "id": "dslim/bert-base-NER",
        "type": "pipeline",
        "task": "ner",
        "size": "~400MB",
        "purpose": "Contextual PII detection (names, orgs, locations)",
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


def bar(label: str, size: str):
    print(f"\n  [{size}] {label}")
    print(f"  {'─' * 50}")


def download_pipeline(model_id: str, task: str):
    from transformers import pipeline
    pipe = pipeline(task, model=model_id)
    del pipe


def download_sentence_transformer(model_id: str):
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(model_id)
    del model


def main():
    print()
    print("  Orion's Belt — Model Downloader")
    print("  ================================")
    print(f"  Cache: {BASE_DIR / 'models'}")
    print()

    total = len(MODELS)
    failed = []

    for i, m in enumerate(MODELS, 1):
        print(f"  [{i}/{total}] {m['id']}")
        print(f"         {m['purpose']}")
        print(f"         Size: {m['size']}", end=" ", flush=True)

        try:
            if m["type"] == "sentence_transformer":
                download_sentence_transformer(m["id"])
            else:
                download_pipeline(m["id"], m["task"])
            print("✓ done")
        except Exception as e:
            print(f"✗ FAILED: {e}")
            failed.append(m["id"])

    print()
    if failed:
        print(f"  WARNING: {len(failed)} model(s) failed to download:")
        for f in failed:
            print(f"    - {f}")
        print("  The app will attempt to download them on first use.")
        print()
        sys.exit(1)
    else:
        print("  All models downloaded successfully.")
        print("  Orion's Belt is ready — run: python launch.py")
        print()


if __name__ == "__main__":
    main()
