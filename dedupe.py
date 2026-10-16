"""
Detect posts that are the same (or nearly the same) as ones already copied.
Pure Python so it can be tested on its own.
"""
import re

_URL = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)
_USER = re.compile(r"@\w+")
_WORD = re.compile(r"\w+", re.UNICODE)   # also matches Amharic / Afaan Oromoo letters


def tokens(text: str) -> list[str]:
    """Lowercase words of a text, without links, @handles and punctuation."""
    t = _URL.sub(" ", (text or "").lower())
    t = _USER.sub(" ", t)
    return _WORD.findall(t)


def _shingles(words: list[str], n: int = 3) -> set[str]:
    return {" ".join(words[i:i + n]) for i in range(max(1, len(words) - n + 1))}


def is_similar(a: list[str], b: list[str], threshold: float = 0.7) -> bool:
    if not a or not b:
        return False
    if len(a) < 4 or len(b) < 4:          # very short texts: only exact matches count
        return a == b
    sa, sb = _shingles(a), _shingles(b)
    inter = len(sa & sb)
    jaccard = inter / len(sa | sb)
    contained = inter / min(len(sa), len(sb))
    # same text, or one post is the other plus a few extra lines
    return jaccard >= threshold or (contained >= 0.9 and min(len(a), len(b)) >= 8)
