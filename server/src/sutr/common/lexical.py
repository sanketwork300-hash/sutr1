"""BM25 over any keyed text. One implementation, two services.

The documentation service ranks chunks; discovery ranks tools. Both want the
same thing — Okapi BM25 with an IDF floor — and two copies of a scoring
function are two copies that will disagree the first time somebody tunes one.

So this operates on `(key, text)` pairs and knows nothing about either service's
tables. It is a shared-library primitive in the sense LLD §2.6 means: no state,
no I/O, and both planes may use it.

The constants are the usual defaults. `k1` damps term-frequency saturation so a
document that says "refund" nine times does not outrank one that says it three
times and means it; `b` normalises for length so a long document is not favoured
merely for containing more words.
"""

import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

K1 = 1.5
B = 0.75

# The constant from the original Reciprocal Rank Fusion paper. It damps the
# influence of any single ranker's top result, which is the point: fusion is
# for when no one ranker is trusted alone.
RRF_K = 60

_WORD = re.compile(r"[a-z0-9][a-z0-9'_-]*")

# Words that match nearly everything and therefore rank nothing.
STOPWORDS = frozenset(
    """a an and are as at be by for from has have how in is it its of on or that the
    this to was were what when where which who will with""".split()
)


def fold(word: str) -> str:
    """Fold an English plural onto its singular.

    Not a stemmer. A full Porter stemmer conflates words that mean different
    things ("universal" and "university" share a stem), and the failure it
    would fix here is narrower than that: BM25 treats `refund` and `refunds` as
    unrelated terms, so a search for "refund a payment" misses a tool whose
    every mention is plural. That is the single biggest source of wrong
    rankings in a corpus of short, noun-heavy tool descriptions.

    So this handles plurals and nothing else, conservatively — short words are
    left alone, because `is`, `gas` and `bus` are not plurals.
    """
    if len(word) <= 3 or word.endswith("ss"):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("es") and word[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    return [fold(word) for word in _WORD.findall((text or "").lower()) if word not in STOPWORDS]


def bm25(query: str, documents: Sequence[tuple[Any, str]]) -> list[tuple[Any, float, list[str]]]:
    """Rank `(key, text)` pairs against `query`. Non-zero scores only, best first.

    Returns the matched terms alongside each score, because a ranking nobody can
    explain is a ranking nobody can debug — and in discovery it is what lets a
    caller see *why* a tool came back.
    """
    terms = tokenize(query)
    if not terms or not documents:
        return []

    tokenized = [(key, tokenize(text)) for key, text in documents]
    lengths = [len(tokens) for _, tokens in tokenized]
    average = (sum(lengths) / len(lengths)) if lengths else 0.0
    total = len(tokenized)

    frequencies = [Counter(tokens) for _, tokens in tokenized]
    document_frequency: Counter = Counter()
    for counts in frequencies:
        for term in set(terms):
            if counts[term]:
                document_frequency[term] += 1

    scored: list[tuple[Any, float, list[str]]] = []
    for (key, tokens), counts in zip(tokenized, frequencies, strict=True):
        score = 0.0
        matched: list[str] = []
        length = len(tokens) or 1
        for term in terms:
            frequency = counts[term]
            if not frequency:
                continue
            matched.append(term)
            # The BM25+ IDF floor: never let a common term push a score down.
            idf = math.log(
                1 + (total - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5)
            )
            denominator = frequency + K1 * (1 - B + B * length / (average or 1))
            score += idf * (frequency * (K1 + 1)) / denominator
        if score > 0:
            scored.append((key, score, sorted(set(matched))))

    scored.sort(key=lambda entry: entry[1], reverse=True)
    return scored


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[Any]], *, k: int = RRF_K
) -> dict[Any, float]:
    """Fuse several ranked lists of keys into one score per key.

    RRF uses only the *rank* a key achieved in each list, never the score, which
    is what makes it safe to combine rankers whose scores are on unrelated
    scales — a BM25 score and a cosine similarity have no common unit.
    """
    fused: dict[Any, float] = {}
    for ranking in rankings:
        for position, key in enumerate(ranking, start=1):
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + position)
    return fused
