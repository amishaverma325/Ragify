from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ollama import Client
from sentence_transformers import SentenceTransformer


@dataclass
class RetrievedChunk:
    source: str
    chunk_text: str
    score: float


class RagEngine:
    def __init__(
        self,
        knowledge_dir: str,
        db_path: str,
        embedding_model: str,
        ollama_host: str,
        ollama_model: str,
        top_k: int = 4,
        max_context_chars: int = 3500,
    ) -> None:
        self.knowledge_dir = Path(knowledge_dir)
        self.db_path = Path(db_path)
        self.embedding_model_name = embedding_model
        self.ollama_model = ollama_model
        self.top_k = top_k
        self.max_context_chars = max_context_chars
        self.embedder = SentenceTransformer(embedding_model)
        self.llm_client = Client(host=ollama_host)

        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    doc_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS indexed_documents (
                    source TEXT PRIMARY KEY,
                    doc_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_cache (
                    query_hash TEXT PRIMARY KEY,
                    query_text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)"
            )

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _iter_documents(self) -> Iterable[Path]:
        if not self.knowledge_dir.exists():
            return []

        docs = []
        for extension in ("*.md", "*.txt"):
            docs.extend(sorted(self.knowledge_dir.glob(extension)))
        return docs

    @staticmethod
    def _split_into_chunks(text: str, max_chunk_chars: int = 700, overlap: int = 120) -> list[str]:
        cleaned = "\n".join(line.rstrip() for line in text.splitlines()).strip()
        if not cleaned:
            return []

        paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
        chunks: list[str] = []
        current = ""

        for para in paragraphs:
            candidate = f"{current}\n\n{para}".strip() if current else para
            if len(candidate) <= max_chunk_chars:
                current = candidate
                continue

            if current:
                chunks.append(current)

            if len(para) <= max_chunk_chars:
                current = para
                continue

            start = 0
            while start < len(para):
                piece = para[start : start + max_chunk_chars]
                chunks.append(piece)
                if start + max_chunk_chars >= len(para):
                    break
                start += max_chunk_chars - overlap
            current = ""

        if current:
            chunks.append(current)

        return chunks

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self.embedder.encode(texts, normalize_embeddings=True)
        return [vector.tolist() for vector in vectors]

    def _is_document_unchanged(self, source: str, doc_hash: str, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT doc_hash FROM indexed_documents WHERE source = ?",
            (source,),
        ).fetchone()
        return bool(row and row["doc_hash"] == doc_hash)

    def _delete_document_chunks(self, source: str, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM chunks WHERE source = ?", (source,))

    def index_knowledge_base(self) -> tuple[int, int]:
        documents = list(self._iter_documents())
        if not documents:
            return (0, 0)

        indexed_files = 0
        indexed_chunks = 0

        with self._connect() as conn:
            for path in documents:
                raw_text = path.read_text(encoding="utf-8")
                source = path.name
                doc_hash = self._sha256(raw_text)

                if self._is_document_unchanged(source, doc_hash, conn):
                    continue

                chunks = self._split_into_chunks(raw_text)
                if not chunks:
                    continue

                embeddings = self._embed(chunks)
                self._delete_document_chunks(source, conn)

                now = self._utc_now()
                conn.executemany(
                    """
                    INSERT INTO chunks (source, chunk_index, chunk_text, embedding_json, doc_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            source,
                            idx,
                            chunk,
                            json.dumps(embedding),
                            doc_hash,
                            now,
                        )
                        for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings))
                    ],
                )

                conn.execute(
                    """
                    INSERT INTO indexed_documents (source, doc_hash, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(source) DO UPDATE SET
                        doc_hash = excluded.doc_hash,
                        updated_at = excluded.updated_at
                    """,
                    (source, doc_hash, now),
                )

                indexed_files += 1
                indexed_chunks += len(chunks)

            indexed_names = {path.name for path in documents}
            stored_rows = conn.execute("SELECT source FROM indexed_documents").fetchall()
            for row in stored_rows:
                source = row["source"]
                if source not in indexed_names:
                    self._delete_document_chunks(source, conn)
                    conn.execute("DELETE FROM indexed_documents WHERE source = ?", (source,))

        return indexed_files, indexed_chunks

    @staticmethod
    def _cosine_similarity_from_normalized(vec_a: list[float], vec_b: list[float]) -> float:
        return sum(x * y for x, y in zip(vec_a, vec_b))

    def _query_embedding(self, query: str, conn: sqlite3.Connection) -> list[float]:
        query_hash = self._sha256(query.strip().lower())
        cached = conn.execute(
            "SELECT embedding_json FROM query_cache WHERE query_hash = ?",
            (query_hash,),
        ).fetchone()

        if cached:
            return json.loads(cached["embedding_json"])

        embedding = self._embed([query])[0]
        conn.execute(
            """
            INSERT INTO query_cache (query_hash, query_text, embedding_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(query_hash) DO UPDATE SET
                query_text = excluded.query_text,
                embedding_json = excluded.embedding_json,
                created_at = excluded.created_at
            """,
            (query_hash, query, json.dumps(embedding), self._utc_now()),
        )
        return embedding

    def retrieve(self, query: str, top_k: int | None = None) -> list[RetrievedChunk]:
        k = top_k or self.top_k

        with self._connect() as conn:
            query_vec = self._query_embedding(query, conn)
            rows = conn.execute(
                "SELECT source, chunk_text, embedding_json FROM chunks"
            ).fetchall()

        scored: list[RetrievedChunk] = []
        for row in rows:
            chunk_vec = json.loads(row["embedding_json"])
            score = self._cosine_similarity_from_normalized(query_vec, chunk_vec)
            scored.append(
                RetrievedChunk(
                    source=row["source"],
                    chunk_text=row["chunk_text"],
                    score=score,
                )
            )

        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:k]

    def _build_context(self, chunks: list[RetrievedChunk]) -> str:
        if not chunks:
            return ""

        parts: list[str] = []
        total = 0

        for idx, chunk in enumerate(chunks, start=1):
            block = f"[{idx}] Source: {chunk.source}\n{chunk.chunk_text}\n"
            if total + len(block) > self.max_context_chars:
                break
            parts.append(block)
            total += len(block)

        return "\n".join(parts)

    def _chat(self, system_prompt: str, user_prompt: str) -> str:
        response = self.llm_client.chat(
            model=self.ollama_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.2},
        )
        return response["message"]["content"].strip()

    def answer(self, query: str) -> tuple[str, list[RetrievedChunk]]:
        chunks = self.retrieve(query)
        context = self._build_context(chunks)

        if not context:
            return (
                "I could not find any indexed knowledge. Add docs to the knowledge base and re-index.",
                [],
            )

        system_prompt = (
            "You are a helpful assistant for policy and FAQ questions. "
            "Answer only from the provided context. "
            "If the answer is not in context, clearly say it is not available."
        )
        user_prompt = (
            f"Question:\n{query}\n\n"
            f"Context:\n{context}\n\n"
            "Give a concise answer in plain text with key points."
        )

        try:
            answer_text = self._chat(system_prompt, user_prompt)
        except Exception as exc:  # pragma: no cover
            answer_text = (
                "I found relevant context, but the local LLM call failed. "
                "Make sure Ollama is running and the selected model is pulled. "
                f"Error: {exc}"
            )

        return answer_text, chunks

    def summarize_text(self, text: str) -> str:
        system_prompt = "Summarize text into 3 short bullet points."
        user_prompt = f"Text to summarize:\n{text}"

        try:
            return self._chat(system_prompt, user_prompt)
        except Exception as exc:  # pragma: no cover
            return f"Unable to summarize right now. Error: {exc}"
