"""URL canonicalization, a Bloom filter for exact ids, and SimHash for near-duplicate titles.

All hashing uses blake2b/sha256 rather than Python's hash(), which is salted per process and would
make dedup decisions differ between runs.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections import deque
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_", "mc_", "ga_")
_TRACKING_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "ref", "ref_src", "cmpid", "taid", "ncid", "src",
    "cid", "mod", "yptr", "guccounter", "guce_referrer", "guce_referrer_sig", "sr_share",
}  # fmt: skip


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_KEYS
    ]
    return urlunsplit((scheme, host, path, urlencode(sorted(query)), ""))


def article_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()


class BloomFilter:
    def __init__(self, capacity: int = 1_000_000, error_rate: float = 0.001) -> None:
        self.bits = max(8, int(-capacity * math.log(error_rate) / math.log(2) ** 2))
        self.hashes = max(1, round(self.bits / capacity * math.log(2)))
        self._array = bytearray((self.bits + 7) // 8)

    def _positions(self, key: str) -> list[int]:
        digest = hashlib.blake2b(key.encode(), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:], "big") | 1
        return [(h1 + i * h2) % self.bits for i in range(self.hashes)]

    def add(self, key: str) -> None:
        for p in self._positions(key):
            self._array[p >> 3] |= 1 << (p & 7)

    def __contains__(self, key: str) -> bool:
        return all(self._array[p >> 3] & (1 << (p & 7)) for p in self._positions(key))


_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_TITLE_SUFFIX = re.compile(r"\s+[-|–—]\s+[A-Z][\w .&]{1,30}$")


def normalize_title(title: str) -> str:
    return _TITLE_SUFFIX.sub("", title).lower()


def simhash64(text: str) -> int:
    words = _WORD.findall(normalize_title(text))
    features = words + [f"{a} {b}" for a, b in itertools.pairwise(words)]
    if not features:
        return 0
    acc = [0] * 64
    for feat in features:
        h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            acc[bit] += 1 if (h >> bit) & 1 else -1
    return sum(1 << bit for bit in range(64) if acc[bit] > 0)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


class NearDuplicateIndex:
    """Recent SimHashes; a new title within `max_distance` bits of one of them is a duplicate."""

    def __init__(self, max_items: int = 5000, max_distance: int = 3) -> None:
        self.max_distance = max_distance
        self._items: deque[tuple[int, str]] = deque(maxlen=max_items)

    def find(self, h: int) -> str | None:
        if h == 0:
            return None
        for other, item_id in self._items:
            if hamming(h, other) <= self.max_distance:
                return item_id
        return None

    def add(self, h: int, item_id: str) -> None:
        if h:
            self._items.append((h, item_id))
