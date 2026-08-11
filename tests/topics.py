"""Synthetic headlines for clustering tests: each topic draws from its own vocabulary."""

import random

TOPICS = {
    "solana": "solana validators outage network restart blocks halted downtime engineers patch client",
    "nvidia": "nvidia earnings revenue datacenter chips guidance beats quarter gpu demand blackwell",
    "fed": "federal reserve powell rate cut inflation fomc minutes treasury yields policy decision",
    "etf": "spot ether etf approval sec filings issuers blackrock inflows staking listing",
    "tesla": "tesla deliveries robotaxi musk factory shanghai margins cybertruck recall vehicles",
    "hack": "exploit hack drained bridge attacker stolen funds protocol audit vulnerability wallet",
}


def headlines(topic: str, n: int, seed: int = 0) -> list[str]:
    rng = random.Random(f"{topic}-{seed}")
    words = TOPICS[topic].split()
    return [" ".join(rng.sample(words, 6)) + f" {topic}{i}" for i in range(n)]
