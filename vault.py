"""
Semantic search of Obsidian vault using ChromaDB + fastembed.
Returns markdown snippet for triage context injection.
"""

import os
import re
from pathlib import Path
from typing import List

import chromadb
from chromadb.utils.embedding_functions import EmbeddingFunction
from fastembed import TextEmbedding

VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/vault"))
CHROMA_PATH = os.environ.get("CHROMA_PATH", "/chroma_data")
MAX_NOTE_CHARS = 800
MAX_NOTES = 3
BASE_NOTE = "TheLab/jarvis-triage-base.md"
BASE_NOTE_MAX_CHARS = 3000
SEARCH_DIRS = ["TheLab", "Areas/Security", "Areas/Infrastructure"]

_embed_model: TextEmbedding | None = None


def _get_embed_model() -> TextEmbedding:
    global _embed_model
    if _embed_model is None:
        _embed_model = TextEmbedding("BAAI/bge-small-en-v1.5")
    return _embed_model


class _FastEmbedFn(EmbeddingFunction):
    def __call__(self, input: List[str]) -> List[List[float]]:
        model = _get_embed_model()
        return [v for v in model.embed(input)]


def _clean(text: str) -> str:
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_base() -> str:
    base_path = VAULT_PATH / BASE_NOTE
    if not base_path.exists():
        return ""
    try:
        return _clean(base_path.read_text(errors="ignore"))[:BASE_NOTE_MAX_CHARS]
    except OSError:
        return ""


def _build_collection() -> chromadb.Collection | None:
    if not VAULT_PATH.exists():
        return None

    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    ef = _FastEmbedFn()

    try:
        client.delete_collection("vault")
    except Exception:
        pass

    collection = client.create_collection("vault", embedding_function=ef)

    base_path = VAULT_PATH / BASE_NOTE
    docs, ids, metas = [], [], []

    for dir_name in SEARCH_DIRS:
        search_dir = VAULT_PATH / dir_name
        if not search_dir.exists():
            continue
        for md in search_dir.rglob("*.md"):
            if md == base_path:
                continue
            try:
                text = md.read_text(errors="ignore")
            except OSError:
                continue
            rel = str(md.relative_to(VAULT_PATH))
            doc_id = rel.replace("/", "_").replace("\\", "_").replace(" ", "-")
            docs.append(_clean(text)[:2000])
            ids.append(doc_id)
            metas.append({"path": rel})

    if docs:
        collection.add(documents=docs, ids=ids, metadatas=metas)

    return collection


_collection = _build_collection()


def search(alert_text: str) -> str:
    if not VAULT_PATH.exists():
        return ""

    parts = []
    base = _load_base()
    if base:
        parts.append(f"### Homelab topology ({BASE_NOTE})\n{base}")

    if _collection is not None and _collection.count() > 0:
        n = min(MAX_NOTES, _collection.count())
        results = _collection.query(query_texts=[alert_text], n_results=n)
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i]
            parts.append(f"### {meta['path']}\n{doc[:MAX_NOTE_CHARS]}")

    if not parts:
        return ""

    return "Homelab notes from vault:\n\n" + "\n\n".join(parts)
