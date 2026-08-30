"""Reject LLM briefs that cite items that don't exist or state numbers that aren't in the input."""

from __future__ import annotations

import re

_CITE = re.compile(r"\[(\d+)\]")
# Clock times count as one token so "13:09" in the input doesn't license a stray "9".
_NUMBER = re.compile(r"\d{1,2}:\d{2}|\d+(?:[.,]\d+)*")
_ABBREV = re.compile(
    r"\b(U\.S|U\.K|Inc|Corp|Co|Ltd|vs|Mr|Ms|Dr|St|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\."
)


def normalize_number(raw: str) -> str:
    if ":" in raw:
        return raw.lstrip("0") or "0"
    s = raw.replace(",", "")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    s = s.lstrip("0") or "0"
    return "0" + s if s.startswith(".") else s


_PERCENT = re.compile(r"([+\-−]?)(\d+(?:\.\d+)?)\s?%")


def percents_in(text: str) -> set[tuple[str, str]]:
    """(sign, magnitude) for each percentage; sign is "+", "-" or "" when unsigned."""
    return {
        ("-" if sign in "-−" and sign else sign, normalize_number(mag))
        for sign, mag in _PERCENT.findall(_CITE.sub(" ", text))
    }


def numbers_in(text: str) -> set[str]:
    return {normalize_number(m) for m in _NUMBER.findall(_CITE.sub(" ", text))}


def split_sentences(body: str) -> list[str]:
    protected = _ABBREV.sub(lambda m: m.group(0).replace(".", "․"), body.strip())
    # Split after sentence punctuation plus any trailing citations.
    parts = re.split(r"(?<=[.!?])((?:\s*\[\d+\])*)\s+(?=[A-Z\"'(])", protected)
    sentences, i = [], 0
    while i < len(parts):
        text = parts[i]
        cites = parts[i + 1] if i + 1 < len(parts) else ""
        sentences.append((text + cites).replace("․", ".").strip())
        i += 2
    return [s for s in sentences if s]


def validate_brief(text: str, n_items: int, source_text: str) -> list[str]:
    """Return a list of problems; an empty list means the brief is acceptable."""
    problems: list[str] = []
    if not text.strip():
        return ["empty"]
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    why = [ln for ln in lines if ln.lower().startswith("why it matters")]
    body = " ".join(ln for ln in lines if not ln.lower().startswith("why it matters"))
    sentences = split_sentences(body)
    if not sentences:
        problems.append("no sentences")
    for s in sentences:
        cites = [int(c) for c in _CITE.findall(s)]
        if not cites:
            problems.append(f"uncited sentence: {s[:60]!r}")
    for c in _CITE.findall(text):
        if not 1 <= int(c) <= n_items:
            problems.append(f"invalid citation [{c}]")
    # Citation indices are stripped before this check, so "[3]" never licenses a bare "3".
    allowed = numbers_in(source_text)
    for num in sorted(numbers_in(text) - allowed):
        problems.append(f"number not in input: {num}")
    # A signed percentage must keep its sign: "+2.1%" is invented if the input only has "-2.1%".
    source_pcts = percents_in(source_text)
    for sign, mag in sorted(percents_in(text)):
        if sign and (sign, mag) not in source_pcts and ("", mag) not in source_pcts:
            problems.append(f"percentage sign not in input: {sign}{mag}%")
    if len(why) > 1:
        problems.append("more than one 'Why it matters' line")
    return problems
