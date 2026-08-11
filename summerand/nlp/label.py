"""Cluster labels with class-based TF-IDF: each cluster's titles form one document, and terms are
scored by how much more frequent they are in that cluster than across all clusters."""

from __future__ import annotations

import numpy as np
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, CountVectorizer

_DOMAIN_STOP = {
    "says", "said", "say", "new", "news", "report", "reports", "update", "today", "week", "weekly",
    "just", "amid", "could", "would", "may", "might", "big", "top", "set", "sets", "gets", "make",
    "makes", "year", "years", "day", "days", "time", "watch", "live", "latest", "breaking", "here",
    "what", "why", "how", "know", "need", "things", "look", "like", "want", "first", "next", "per",
    "cent", "percent", "price", "prices", "market", "markets", "stock", "stocks", "crypto",
}  # fmt: skip
STOP_WORDS = sorted(ENGLISH_STOP_WORDS | _DOMAIN_STOP)
_TOKEN = r"(?u)\b[A-Za-z][A-Za-z0-9&'\-]+\b"


def ctfidf_terms(docs_by_cluster: dict[str, list[str]], top_n: int = 3) -> dict[str, list[str]]:
    ids = sorted(docs_by_cluster)
    if not ids:
        return {}
    class_docs = [" ".join(docs_by_cluster[c]) for c in ids]
    cv = CountVectorizer(ngram_range=(1, 2), stop_words=STOP_WORDS, token_pattern=_TOKEN)
    try:
        counts = cv.fit_transform(class_docs).astype(np.float64)
    except ValueError:  # every token was a stop word
        return {c: [] for c in ids}
    vocab = cv.get_feature_names_out()
    row_sums = np.asarray(counts.sum(axis=1)).ravel()
    row_sums[row_sums == 0] = 1.0
    tf = counts.multiply(1.0 / row_sums[:, None]).tocsr()
    avg_words = counts.sum() / len(ids)
    term_freq = np.asarray(counts.sum(axis=0)).ravel()
    idf = np.log1p(avg_words / term_freq)
    scores = tf.multiply(idf).tocsr()
    out: dict[str, list[str]] = {}
    for row, cid in enumerate(ids):
        r = scores.getrow(row)
        order = sorted(zip(r.data, r.indices, strict=True), key=lambda x: (-x[0], vocab[x[1]]))
        chosen: list[str] = []
        for _, j in order:
            term = vocab[j]
            words = set(term.split())
            if any(words <= set(c.split()) for c in chosen):
                continue
            chosen = [c for c in chosen if not set(c.split()) <= words]
            chosen.append(term)
            if len(chosen) >= top_n:
                break
        out[cid] = chosen
    return out


def headline_index(X: np.ndarray, centroid: np.ndarray) -> int:
    return int(np.argmax(X @ centroid))
