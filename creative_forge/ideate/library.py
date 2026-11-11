"""Retrieval over a small creative library: past lessons and concepts, embedded once and searched by cosine.

The index is a JSON file of vectors keyed by a hash of (embedder, text), stored next to the generation cache. It is
a few dozen vectors searched with numpy, not a vector database service: at this size a database adds a moving
part and no recall. Re-embedding happens only for entries whose text changed.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Protocol

import numpy as np
import yaml


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str  # "lesson" (production defect), "perf_lesson" (from imported ad data) or "past_concept"
    title: str
    text: str
    synthetic: bool = False

    @property
    def document(self) -> str:
        return f"{self.title}. {self.text}"


def _entries(text: str, lesson_kind: str = "lesson") -> list[Entry]:
    d = yaml.safe_load(text) or {}
    return [
        Entry(e["id"], kind, e["title"], " ".join(e["text"].split()), bool(e.get("synthetic", False)))
        for key, kind in (("lessons", lesson_kind), ("past_concepts", "past_concept"))
        for e in d.get(key, [])
    ]


def load_library(path: Path | None = None, *, perf_lessons: bool = True) -> list[Entry]:
    """The bundled library (or ``path``), plus the lessons ``forge perf report --write-lessons`` derived from real ad
    performance (kind ``perf_lesson``), when that file exists."""
    if path is None:
        text = resources.files("creative_forge.data").joinpath("library.yaml").read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    entries = _entries(text)
    if perf_lessons:
        from creative_forge.perf.lessons import lessons_path

        extra = lessons_path()
        if extra.is_file():
            entries += _entries(extra.read_text(encoding="utf-8"), "perf_lesson")
    return entries


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str], *, query: bool) -> np.ndarray: ...


_STOP = frozenset({"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "its"})
_STOP |= {"of", "on", "or", "the", "to", "with", "this", "that"}


class HashingEmbedder:
    """Deterministic, offline: unigrams and bigrams hashed into ``dim`` signed buckets, log-scaled counts, L2-normed.
    Good enough to rank a few dozen notes by shared vocabulary; it has no notion of synonyms."""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.name = f"hash-{dim}"

    def _features(self, text: str) -> Counter[str]:
        words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1]
        return Counter(words + [f"{a}_{b}" for a, b in itertools.pairwise(words)])

    def embed(self, texts: list[str], *, query: bool) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), np.float32)
        for i, t in enumerate(texts):
            for feat, n in self._features(t).items():
                h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
                out[i, h % self.dim] += (1 if h >> 63 else -1) * (1 + math.log(n))
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)


class VertexTextEmbedder:
    """``text-embedding-005`` on Vertex AI (``:predict``), with the retrieval task types for documents and queries."""

    def __init__(self, client, location: str = "us-central1", model: str = "text-embedding-005") -> None:
        self.client = client
        self.location = location
        self.model = model
        self.name = f"vertex-{model}"
        self.inputs = 0  # how many texts were sent (the unit Vertex bills embeddings by)

    def embed(self, texts: list[str], *, query: bool) -> np.ndarray:
        task = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
        vectors: list[list[float]] = []
        for i in range(0, len(texts), 64):
            batch = texts[i : i + 64]
            d = self.client.post(
                self.client.model_url(self.location, self.model, "predict"),
                {"instances": [{"content": t, "task_type": task} for t in batch]},
            )
            vectors += [p["embeddings"]["values"] for p in d["predictions"]]
            self.inputs += len(batch)
        return np.asarray(vectors, np.float32)


class LibraryIndex:
    def __init__(self, entries: list[Entry], embedder: Embedder, index_file: Path | None = None) -> None:
        self.entries = entries
        self.embedder = embedder
        self.index_file = index_file
        self._matrix: np.ndarray | None = None

    def _key(self, e: Entry) -> str:
        return hashlib.sha256(f"{self.embedder.name}\n{e.document}".encode()).hexdigest()[:20]

    def build(self) -> int:
        """Embed entries that are not in the index file yet; returns how many were embedded."""
        stored: dict[str, list[float]] = {}
        if self.index_file and self.index_file.exists():
            data = json.loads(self.index_file.read_text(encoding="utf-8"))
            if data.get("embedder") == self.embedder.name:
                stored = data["vectors"]
        missing = [e for e in self.entries if self._key(e) not in stored]
        if missing:
            for e, v in zip(missing, self.embedder.embed([e.document for e in missing], query=False), strict=True):
                stored[self._key(e)] = [round(float(x), 6) for x in v]
            if self.index_file:
                self.index_file.parent.mkdir(parents=True, exist_ok=True)
                keep = {self._key(e) for e in self.entries}
                payload = {"embedder": self.embedder.name, "vectors": {k: stored[k] for k in sorted(keep)}}
                self.index_file.write_text(json.dumps(payload), encoding="utf-8")
        m = np.asarray([stored[self._key(e)] for e in self.entries], np.float32)
        self._matrix = m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
        return len(missing)

    def search(self, query: str, k: int = 4) -> list[tuple[float, Entry]]:
        if self._matrix is None:
            self.build()
        assert self._matrix is not None
        q = self.embedder.embed([query], query=True)[0]
        q = q / max(float(np.linalg.norm(q)), 1e-9)
        scores = self._matrix @ q
        order = np.argsort(-scores)[: max(1, min(k, len(self.entries)))]
        return [(float(scores[i]), self.entries[i]) for i in order]
