"""
ChromaDB local vector store.

Three collections per the design doc:
- optimization_playbooks  (semantic chunks from playbooks KB)
- episodic_memory         (past optimization runs and outcomes)
- semantic_memory         (distilled learnings)

Uses Mistral embeddings via a custom EmbeddingFunction adapter.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

import chromadb
from chromadb.api.types import EmbeddingFunction, Embeddings, Documents

from app.config import settings
from app.services.mistral_service import mistral_service

logger = logging.getLogger(__name__)


class MistralEmbeddingFunction(EmbeddingFunction):
    """Adapter so Chroma can call Mistral embeddings."""

    def __call__(self, input: Documents) -> Embeddings:
        # Chroma's signature requires 'input' arg name
        if not input:
            return []
        try:
            return mistral_service.embed(list(input))
        except Exception as e:
            logger.error(f"Mistral embedding failed, falling back to local: {e}")
            # Fallback to sentence-transformers if Mistral fails
            try:
                from sentence_transformers import SentenceTransformer
                model = SentenceTransformer("all-MiniLM-L6-v2")
                return model.encode(list(input)).tolist()
            except Exception as e2:
                logger.exception(f"Fallback embedding also failed: {e2}")
                raise


class VectorStore:
    """Wrapper over ChromaDB's persistent client."""

    def __init__(self, persist_dir: Optional[str] = None):
        self.persist_dir = persist_dir or settings.CHROMA_PERSIST_DIR
        self.client = chromadb.PersistentClient(path=self.persist_dir)
        self.embed_fn = MistralEmbeddingFunction()

        self.playbooks = self._get_or_create(settings.CHROMA_COLLECTION_PLAYBOOKS)
        self.episodic = self._get_or_create(settings.CHROMA_COLLECTION_EPISODIC)
        self.semantic = self._get_or_create(settings.CHROMA_COLLECTION_SEMANTIC)

    def _get_or_create(self, name: str):
        return self.client.get_or_create_collection(
            name=name,
            embedding_function=self.embed_fn,
            metadata={"hnsw:space": "cosine"},
        )

    # ---------- write ----------
    def add_playbook_chunks(
        self, chunks: List[str], metadatas: List[Dict[str, Any]]
    ) -> List[str]:
        ids = [str(uuid.uuid4()) for _ in chunks]
        self.playbooks.add(ids=ids, documents=chunks, metadatas=metadatas)
        logger.info(f"Added {len(chunks)} playbook chunks")
        return ids

    def add_playbook(self, text: str, metadata: Dict[str, Any]) -> str:
        """Add a single playbook/rule and return its id."""
        id_ = str(uuid.uuid4())
        self.playbooks.add(ids=[id_], documents=[text], metadatas=[metadata])
        logger.info(f"Added playbook rule {id_}")
        return id_

    def list_playbooks(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Return every playbook/rule with id, content and metadata."""
        if self.playbooks.count() == 0:
            return []
        res = self.playbooks.get(limit=limit, include=["documents", "metadatas"])
        ids = res.get("ids", []) or []
        docs = res.get("documents", []) or []
        metas = res.get("metadatas", []) or []
        out: List[Dict[str, Any]] = []
        for i, _id in enumerate(ids):
            out.append({
                "id": _id,
                "content": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
            })
        return out

    def get_playbook(self, playbook_id: str) -> Optional[Dict[str, Any]]:
        res = self.playbooks.get(ids=[playbook_id], include=["documents", "metadatas"])
        ids = res.get("ids", []) or []
        if not ids:
            return None
        docs = res.get("documents", []) or []
        metas = res.get("metadatas", []) or []
        return {
            "id": ids[0],
            "content": docs[0] if docs else "",
            "metadata": metas[0] if metas else {},
        }

    def update_playbook(
        self,
        playbook_id: str,
        text: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        existing = self.get_playbook(playbook_id)
        if not existing:
            return False
        kwargs: Dict[str, Any] = {"ids": [playbook_id]}
        if text is not None:
            kwargs["documents"] = [text]
        if metadata is not None:
            merged = {**(existing.get("metadata") or {}), **metadata}
            kwargs["metadatas"] = [merged]
        self.playbooks.update(**kwargs)
        logger.info(f"Updated playbook rule {playbook_id}")
        return True

    def delete_playbook(self, playbook_id: str) -> bool:
        if not self.get_playbook(playbook_id):
            return False
        self.playbooks.delete(ids=[playbook_id])
        logger.info(f"Deleted playbook rule {playbook_id}")
        return True

    def add_episodic_memory(
        self, text: str, metadata: Dict[str, Any]
    ) -> str:
        id_ = str(uuid.uuid4())
        self.episodic.add(ids=[id_], documents=[text], metadatas=[metadata])
        return id_

    def add_semantic_memory(
        self, text: str, metadata: Dict[str, Any]
    ) -> str:
        id_ = str(uuid.uuid4())
        self.semantic.add(ids=[id_], documents=[text], metadatas=[metadata])
        return id_

    # ---------- read ----------
    def query_playbooks(
        self, query: str, n_results: int = 4, where: Optional[Dict] = None
    ) -> List[Dict[str, Any]]:
        return self._query(self.playbooks, query, n_results, where)

    def query_episodic(
        self, query: str, n_results: int = 4, where: Optional[Dict] = None
    ) -> List[Dict[str, Any]]:
        return self._query(self.episodic, query, n_results, where)

    def query_semantic(
        self, query: str, n_results: int = 4, where: Optional[Dict] = None
    ) -> List[Dict[str, Any]]:
        return self._query(self.semantic, query, n_results, where)

    def hybrid_query(
        self, query: str, n_results: int = 6
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Query all three collections; return categorized hits."""
        return {
            "playbooks": self.query_playbooks(query, n_results=n_results // 2 + 1),
            "episodic": self.query_episodic(query, n_results=n_results // 3 + 1),
            "semantic": self.query_semantic(query, n_results=n_results // 3 + 1),
        }

    @staticmethod
    def _query(collection, query: str, n: int, where: Optional[Dict]) -> List[Dict[str, Any]]:
        if collection.count() == 0:
            return []
        kwargs = {"query_texts": [query], "n_results": min(n, collection.count())}
        if where:
            kwargs["where"] = where
        res = collection.query(**kwargs)
        out: List[Dict[str, Any]] = []
        if not res.get("documents"):
            return out
        docs = res["documents"][0]
        metas = res["metadatas"][0] if res.get("metadatas") else [{}] * len(docs)
        dists = res["distances"][0] if res.get("distances") else [None] * len(docs)
        ids = res["ids"][0] if res.get("ids") else [""] * len(docs)
        for i, doc in enumerate(docs):
            out.append({
                "id": ids[i] if i < len(ids) else "",
                "text": doc,
                "metadata": metas[i] if i < len(metas) else {},
                "distance": dists[i] if i < len(dists) else None,
            })
        return out

    def counts(self) -> Dict[str, int]:
        return {
            "playbooks": self.playbooks.count(),
            "episodic": self.episodic.count(),
            "semantic": self.semantic.count(),
        }


# Singleton (lazy)
_vector_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store
