"""Cluster and market briefs. An LLM writes them when one is configured; every LLM draft goes
through validate_brief, and anything rejected (or no LLM at all) falls back to an extractive
brief built with maximal marginal relevance over the items' titles and summary sentences."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from summerand.brief.prompts import MARKET_SYSTEM, SYSTEM, render_items
from summerand.brief.validate import split_sentences, validate_brief

log = logging.getLogger(__name__)

MMR_LAMBDA = 0.7
MAX_LLM_ERRORS = 3


class LLM(Protocol):
    name: str

    async def complete(self, system: str, user: str) -> str: ...


class OpenAIChat:
    def __init__(self, api_key: str, model: str) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, max_retries=2, timeout=30)
        self.model = model
        self.name = f"openai/{model}"

    async def complete(self, system: str, user: str) -> str:
        resp = await self._client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            max_tokens=320,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return (resp.choices[0].message.content or "").strip()


def membership_hash(ids: list[str]) -> str:
    return hashlib.sha256("|".join(sorted(ids)).encode()).hexdigest()[:16]


def _clean_sentence(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip().rstrip("…").strip()
    if s and s[-1] not in ".!?":
        s += "."
    return s


def candidates(items: list[dict[str, Any]]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for it in items:
        title = _clean_sentence(it["title"])
        out.append((it["n"], title))
        for s in split_sentences(it.get("summary") or ""):
            s = _clean_sentence(s)
            if 25 <= len(s) <= 260 and s.lower() != title.lower() and not s.endswith("…."):
                out.append((it["n"], s))
    return out


def mmr_select(texts: list[str], k: int = 3, lam: float = MMR_LAMBDA) -> list[int]:
    if not texts:
        return []
    try:
        X = TfidfVectorizer(stop_words="english").fit_transform(texts)
    except ValueError:
        return list(range(min(k, len(texts))))
    X = X.toarray()
    query = X.mean(axis=0)
    qn = np.linalg.norm(query) or 1.0
    norms = np.linalg.norm(X, axis=1)
    norms[norms == 0] = 1.0
    rel = (X @ query) / (norms * qn)
    sim = (X @ X.T) / np.outer(norms, norms)
    chosen: list[int] = []
    while len(chosen) < min(k, len(texts)):
        best, best_score = None, -np.inf
        for i in range(len(texts)):
            if i in chosen:
                continue
            div = max((sim[i, j] for j in chosen), default=0.0)
            score = lam * rel[i] - (1 - lam) * div
            if score > best_score + 1e-12:
                best, best_score = i, score
        assert best is not None
        chosen.append(best)
    return chosen


def extractive(items: list[dict[str, Any]], facts: list[str], k: int = 3) -> str:
    cands = candidates(items)
    picks = mmr_select([t for _, t in cands], k=k)
    # A picked title can itself be two sentences ("Want to retire early? Here's how."), so each
    # piece gets the citation.
    body = " ".join(
        f"{part} [{cands[i][0]}]"
        for i in sorted(picks, key=lambda i: (cands[i][0], i))
        for part in split_sentences(cands[i][1])
    )
    why = "Why it matters: " + "; ".join(facts) + "." if facts else ""
    return body + ("\n" + why if why else "")


@dataclass
class BriefResult:
    text: str
    method: str
    problems: list[str]


class Briefer:
    def __init__(self, llm: LLM | None = None) -> None:
        self.llm = llm
        self._cache: dict[tuple[str, str, bool], BriefResult] = {}
        self.llm_calls = 0
        self.llm_rejected = 0
        self.llm_errors = 0
        self.disabled: str | None = None

    @property
    def state(self) -> str:
        if self.llm is None:
            return "off"
        if self.disabled:
            return f"disabled ({self.disabled})"
        return self.llm.name

    def _note_error(self, exc: Exception) -> None:
        # Latch the LLM off for the rest of the process: a quota error won't fix itself, and
        # repeated failures would add a timeout to every brief.
        code = getattr(exc, "code", None) or (getattr(exc, "body", None) or {}).get("code")
        self.llm_errors += 1
        if code in ("insufficient_quota", "credit_balance_exhausted"):
            self.disabled = "quota"
        elif self.llm_errors >= MAX_LLM_ERRORS:
            self.disabled = f"{self.llm_errors} consecutive errors"
        if self.disabled:
            log.warning("LLM briefs disabled: %s; using extractive briefs", self.disabled)

    async def _write(
        self, system: str, items: list[dict], facts: list[str], k: int, use_llm: bool = True
    ) -> BriefResult:
        source_text = render_items(items, facts)
        problems: list[str] = []
        if self.llm is not None and use_llm and not self.disabled:
            self.llm_calls += 1
            try:
                draft = await self.llm.complete(system, source_text)
            except Exception as exc:  # network, quota, anything: fall back
                problems = [f"llm error: {type(exc).__name__}"]
                self._note_error(exc)
            else:
                self.llm_errors = 0
                problems = validate_brief(draft, len(items), source_text)
                if not problems:
                    return BriefResult(draft, "llm", [])
                self.llm_rejected += 1
                log.info("llm brief rejected: %s", "; ".join(problems[:3]))
        text = extractive(items, facts, k=k)
        leftover = validate_brief(text, len(items), source_text)
        if leftover:  # should not happen; keep it visible if it does
            log.warning("extractive brief failed validation: %s", leftover)
        return BriefResult(text, "extractive", problems)

    async def cluster_brief(
        self,
        cluster_id: str,
        member_ids: list[str],
        items: list[dict],
        facts: list[str],
        use_llm: bool = True,
    ) -> BriefResult:
        # Keyed on whether the LLM was allowed, so an extractive brief written while catching up
        # doesn't stick once the pipeline is live.
        key = (cluster_id, membership_hash(member_ids), use_llm and self.llm is not None)
        if key not in self._cache:
            self._cache[key] = await self._write(SYSTEM, items, facts, k=3, use_llm=use_llm)
        return self._cache[key]

    async def market_brief(
        self, items: list[dict], facts: list[str], use_llm: bool = True
    ) -> BriefResult:
        return await self._write(MARKET_SYSTEM, items, facts, k=4, use_llm=use_llm)
