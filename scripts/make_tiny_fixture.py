"""Write fixtures/tiny_feed.jsonl: three obvious topics over 50 minutes plus 1-minute bars
(with a 15-minute pre-roll so realized volatility is defined when the first story lands).

Used by the replay tests. Prices are synthetic: SOL drops while the outage story breaks, ETH rises
with the ETF story, BTC drifts. Headlines are written for the test, not copied from any outlet.
"""

import json
import math
import random
from pathlib import Path

START = 1_789_995_600_000  # 2026-08-27T13:00:00Z
MIN = 60_000
OUT = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_feed.jsonl"

NEWS = [
    # (minute, source, title, summary)
    (
        3,
        "decrypt",
        "Solana validators report stalled block production",
        "Several validators say the network stopped producing blocks after a client bug.",
    ),
    (
        5,
        "cointelegraph",
        "Solana network halts as validators coordinate restart",
        "Core developers are coordinating a restart of the Solana network.",
    ),
    (
        7,
        "coindesk",
        "Solana outage: engineers trace halt to consensus client bug",
        "The halt began shortly after 13:00 UTC, according to the status page.",
    ),
    (
        9,
        "theblock",
        "SOL slides as Solana outage stretches past an hour",
        "Traders sold SOL while the chain was down.",
    ),
    (
        12,
        "cryptoslate",
        "Solana validators push patched client ahead of restart",
        "A patched validator client was released to operators.",
    ),
    (
        15,
        "bitcoinmagazine",
        "Bitcoin holds steady while Solana network stays offline",
        "BTC traded in a narrow range during the Solana outage.",
    ),
    (
        18,
        "decrypt",
        "Solana restart underway after patched client ships",
        "Validators are upgrading and restarting in a coordinated sequence.",
    ),
    (
        22,
        "coindesk",
        "Solana resumes block production after outage",
        "Blocks are being produced again after the network halt.",
    ),
    (
        26,
        "cointelegraph",
        "What caused the Solana outage, and what validators changed",
        "Developers published a short post-mortem of the consensus bug.",
    ),
    (
        30,
        "theblock",
        "Solana outage post-mortem points to consensus client bug",
        "The post-mortem says a rare edge case in the client triggered the halt.",
    ),
    (
        40,
        "cryptonews",
        "SOL recovers part of its outage losses as blocks resume",
        "SOL bounced once block production resumed.",
    ),
    (
        11,
        "coindesk",
        "SEC approves spot ether ETF staking for three issuers",
        "The approval lets issuers stake part of the ether they hold.",
    ),
    (
        13,
        "theblock",
        "Spot ether ETF staking approved by SEC",
        "Three issuers can now stake ETH held by their funds.",
    ),
    (
        16,
        "decrypt",
        "Ether jumps after SEC signs off on ETF staking",
        "ETH rose after the decision was published.",
    ),
    (
        19,
        "cointelegraph",
        "ETH rallies as spot ether ETFs win staking approval",
        "Analysts expect inflows into the staking-enabled funds.",
    ),
    (
        21,
        "cryptoslate",
        "BlackRock files to add staking to its spot ether ETF",
        "The filing follows the SEC decision on staking.",
    ),
    (
        24,
        "coindesk",
        "Ether ETF issuers race to launch staking after SEC approval",
        "Issuers said staking would begin within weeks.",
    ),
    (
        28,
        "cryptonews",
        "Spot ether ETF inflows jump on staking approval",
        "Inflows rose sharply on the day of the decision.",
    ),
    (
        33,
        "theblock",
        "Ether ETF staking: what the SEC approval means for holders",
        "The funds will pass staking rewards through to shareholders.",
    ),
    (
        37,
        "decrypt",
        "Ethereum staking demand rises after ETF approval",
        "Validator queue length grew after the decision.",
    ),
    (
        45,
        "cointelegraph",
        "ETH holds gains as ether ETF staking approval sinks in",
        "ETH kept most of its gains into the afternoon.",
    ),
    (
        4,
        "cnbc",
        "Nvidia earnings beat estimates on data center demand",
        "Nvidia reported revenue above estimates, led by data center chips.",
    ),
    (
        6,
        "marketwatch",
        "Nvidia shares rise after earnings beat and strong guidance",
        "The company guided revenue above consensus for next quarter.",
    ),
    (
        8,
        "ft_markets",
        "Nvidia results beat expectations as data center sales climb",
        "Data center revenue again made up most of sales.",
    ),
    (
        14,
        "benzinga",
        "Nvidia guidance tops estimates on Blackwell chip demand",
        "Management said Blackwell demand remains ahead of supply.",
    ),
    (
        20,
        "cnbc",
        "Nvidia earnings: data center revenue beats, gaming misses",
        "Gaming revenue came in below estimates.",
    ),
    (
        27,
        "marketwatch",
        "Analysts raise Nvidia price targets after earnings beat",
        "Several brokers raised targets after the results.",
    ),
    (
        35,
        "ft_markets",
        "Nvidia earnings lift chip stocks in early trading",
        "Other chipmakers rose in sympathy.",
    ),
    (
        42,
        "benzinga",
        "Nvidia data center demand keeps earnings beat streak alive",
        "It was the latest in a run of quarterly beats.",
    ),
    (
        48,
        "cnbc",
        "Nvidia shares hold gains after earnings as chip stocks rally",
        "The broader chip index also closed higher.",
    ),
    # duplicates the ETL should drop
    (17, "decrypt", "Ether jumps after SEC signs off on ETF staking", "Same story, tracking link."),
    (
        23,
        "cryptonews",
        "Solana resumes block production after outage - CryptoNews",
        "Syndicated copy.",
    ),
]


def slug(title):
    return "-".join("".join(c for c in w.lower() if c.isalnum()) for w in title.split())[:80]


def main():
    rng = random.Random(7)
    rows = []
    for i, (minute, source, title, summary) in enumerate(NEWS):
        ts = START + minute * MIN + rng.randrange(0, 50_000)
        url = f"https://{source}.example/news/{slug(title)}"
        if i == len(NEWS) - 2:
            url = f"https://decrypt.example/news/{slug(title)}?utm_source=rss&utm_medium=feed"
        rows.append(
            {
                "type": "news",
                "ts_ms": ts,
                "source": source,
                "title": title,
                "url": url,
                "summary": summary,
                "published_ms": ts,
            }
        )
    prices = {"BTC": 64000.0, "ETH": 2600.0, "SOL": 150.0}
    for m in range(-15, 50):
        for sym in ("BTC", "ETH", "SOL"):
            drift = 0.0
            if sym == "SOL" and 3 <= m < 22:
                drift = -0.004
            elif sym == "SOL" and 22 <= m < 45:
                drift = 0.0015
            elif sym == "ETH" and 11 <= m < 30:
                drift = 0.003
            ret = drift + rng.gauss(0, 0.0006)
            o = prices[sym]
            c = o * math.exp(ret)
            prices[sym] = c
            hi, lo = (
                max(o, c) * (1 + abs(rng.gauss(0, 0.0002))),
                min(o, c) * (1 - abs(rng.gauss(0, 0.0002))),
            )
            start = START + m * MIN
            rows.append(
                {
                    "type": "bar",
                    "ts_ms": start + MIN,
                    "symbol": sym,
                    "start_ms": start,
                    "o": round(o, 4),
                    "h": round(hi, 4),
                    "l": round(lo, 4),
                    "c": round(c, 4),
                    "v": round(rng.uniform(5, 50), 3),
                    "venue": "synthetic",
                }
            )
    rows.sort(key=lambda r: (r["ts_ms"], r["type"], r.get("symbol", ""), r.get("url", "")))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    print(f"wrote {len(rows)} rows to {OUT}")


if __name__ == "__main__":
    main()
