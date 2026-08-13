from summerand.brief.generate import Briefer, extractive, mmr_select
from summerand.brief.prompts import render_items
from summerand.brief.validate import numbers_in, split_sentences, validate_brief

ITEMS = [
    {
        "n": 1,
        "source": "coindesk",
        "time": "13:05 UTC",
        "title": "Solana halts after client bug",
        "summary": "Validators said blocks stopped at 13:02 UTC. A restart is being coordinated.",
        "url": "u1",
    },
    {
        "n": 2,
        "source": "decrypt",
        "time": "13:09 UTC",
        "title": "SOL falls 4% during outage",
        "summary": "Traders sold SOL as the chain stayed down.",
        "url": "u2",
    },
]
FACTS = ["SOL -4.2% since 13:03 UTC, 12.9σ", "2 reports from 2 sources since 13:05 UTC"]
SRC = render_items(ITEMS, FACTS)


class FakeLLM:
    name = "fake"

    def __init__(self, text):
        self.text = text

    async def complete(self, system, user):
        return self.text


def test_valid_brief_passes():
    text = (
        "Solana stopped producing blocks after a client bug [1]. SOL fell 4.2% as the outage "
        "continued [2].\nWhy it matters: the move was 12.9σ [2]."
    )
    assert validate_brief(text, 2, SRC) == []


def test_invented_number_is_rejected():
    text = "Solana halted for 3 hours [1]. SOL fell 9% [2]."
    problems = validate_brief(text, 2, SRC)
    assert "number not in input: 9" in problems and "number not in input: 3" in problems


def test_bad_or_missing_citations_are_rejected():
    assert "invalid citation [3]" in validate_brief("Solana halted [3].", 2, SRC)
    assert any(
        p.startswith("uncited") for p in validate_brief("Solana halted. SOL fell [2].", 2, SRC)
    )


def test_numbers_are_normalized():
    assert numbers_in("64,000.50 and 4.20% at 09:05 [7]") == {"64000.5", "4.2", "9:05"}


def test_sentence_split_keeps_abbreviations_and_citations():
    assert split_sentences("The U.S. SEC acted [1]. Ether rose [2][3]. Done.") == [
        "The U.S. SEC acted [1].",
        "Ether rose [2][3].",
        "Done.",
    ]


def test_mmr_trades_relevance_for_diversity():
    texts = [
        "solana outage halts blocks",
        "solana outage halts blocks",
        "solana price falls during outage",
    ]
    assert mmr_select(texts, k=2, lam=1.0) == [0, 1]  # pure relevance takes the duplicate
    assert mmr_select(texts, k=2, lam=0.5) == [0, 2]


def test_extractive_brief_is_cited_and_valid():
    text = extractive(ITEMS, FACTS)
    assert validate_brief(text, 2, SRC) == []
    assert text.splitlines()[-1].startswith("Why it matters: SOL -4.2%")


async def test_briefer_falls_back_when_llm_invents_numbers():
    b = Briefer(FakeLLM("Solana was down for 7 hours [1]."))
    res = await b.cluster_brief("c1", ["a", "b"], ITEMS, FACTS)
    assert res.method == "extractive" and "number not in input: 7" in res.problems
    assert b.llm_rejected == 1


async def test_briefer_keeps_valid_llm_output_and_caches_by_membership():
    llm = FakeLLM(
        "Solana halted after a client bug [1]. SOL fell 4.2% [2].\nWhy it matters: 12.9σ move [2]."
    )
    b = Briefer(llm)
    r1 = await b.cluster_brief("c1", ["a", "b"], ITEMS, FACTS)
    r2 = await b.cluster_brief("c1", ["b", "a"], ITEMS, FACTS)
    assert r1.method == "llm" and r1 is r2 and b.llm_calls == 1


async def test_llm_errors_fall_back():
    class Broken:
        name = "broken"

        async def complete(self, system, user):
            raise ConnectionError

    res = await Briefer(Broken()).market_brief(ITEMS, FACTS)
    assert res.method == "extractive" and res.problems == ["llm error: ConnectionError"]
