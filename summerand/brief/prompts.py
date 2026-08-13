SYSTEM = """You write short market news briefs for traders. Use only the numbered items and the
computed facts you are given. Rules:
- 2 or 3 sentences, then one line starting with "Why it matters:".
- End every sentence with the citation(s) it relies on, like [1] or [2][3].
- Do not state any number (price, percent, count, date) that does not appear in the input.
- No speculation, no advice, no hype words."""

MARKET_SYSTEM = """You write a market brief covering the top stories right now. Use only the
numbered stories and the computed market moves you are given. Rules:
- 3 or 4 sentences, then one line starting with "Why it matters:".
- End every sentence with the citation(s) it relies on, like [1] or [2][3].
- Do not state any number that does not appear in the input. No advice."""


def render_items(items: list[dict], facts: list[str]) -> str:
    lines = ["Items:"]
    for it in items:
        summary = f" {it['summary'][:300]}" if it.get("summary") else ""
        lines.append(f"[{it['n']}] {it['source']}, {it['time']}: {it['title']}.{summary}")
    lines.append("")
    lines.append("Computed facts (from price data, not from the articles):")
    lines.extend(f"- {f}" for f in facts or ["none"])
    return "\n".join(lines)
