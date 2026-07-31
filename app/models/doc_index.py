"""
Indexed document chunks for local semantic search (search_documents tool).
Deliberately a SEPARATE table from Memory — see app/services/doc_index.py's
module docstring for why indexed documents must never be auto-injected
into agent prompts the way recalled memories are.
"""
import uuid
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class DocumentChunk(db.Model):
    __tablename__ = "document_chunks"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    authorized_directory_id = db.Column(
        db.String(36), db.ForeignKey("authorized_directories.id"), nullable=False, index=True)
    file_path = db.Column(db.String(2048), nullable=False, index=True)
    chunk_index = db.Column(db.Integer, nullable=False)
    content = db.Column(db.Text, nullable=False)
    # float32 embedding bytes — same storage shape as Memory.embedding, read
    # back via a NumPy cosine scan (see app/services/doc_index.py).
    embedding = db.Column(db.LargeBinary, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
