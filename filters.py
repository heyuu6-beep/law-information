"""
Filtering and cleaning rules for the channel copier.
Pure Python (no Telegram code) so it can be tested on its own.
"""
import json
import re
from typing import NamedTuple

DEFAULT_RULES = {
    "include_any": [],            # post only if text contains ANY of these (empty = no restriction)
    "exclude_any": [],            # skip if text contains ANY of these
    "exclude_regex": [],          # skip if text matches ANY of these regular expressions
    "allowed_types": ["text", "photo", "video", "audio", "document", "gif"],
    "min_length": 0,              # minimum characters of text after cleaning (0 = off)
    "max_length": 0,              # maximum characters of text after cleaning (0 = off)
    "skip_forwarded": False,      # skip posts that are themselves forwarded from elsewhere
    "skip_without_text": False,   # skip photos/videos that have no caption
    "strip_lines_containing": [], # delete whole lines containing these phrases (e.g. "join us")
    "replace": {},                # {"old text": "new text"}
    "remove_links": False,
    "remove_usernames": False,    # removes @handles
    "remove_hashtags": False,
    "add_source_credit": True,    # append "Source: @channel"
    "credit_with_post_link": False,  # use the link of the original post instead of @name
    "footer": "",                 # extra text added at the end of every post
}

URL_RE = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)
USER_RE = re.compile(r"(?<![\w/])@\w{3,}")
TAG_RE = re.compile(r"(?<!\w)#\w+")


class Result(NamedTuple):
    ok: bool          # copy this post?
    text: str         # cleaned text
    reason: str       # why it was skipped (or "")
    modified: bool    # text differs from the original (formatting must then be dropped)


def norm_channel(name: str) -> str:
    return (name or "").strip().lstrip("@").lower()


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def rules_for(config: dict, channel: str) -> dict:
    """defaults < filters.json 'defaults' < filters.json 'channels'[channel]"""
    rules = dict(DEFAULT_RULES)
    rules.update(config.get("defaults", {}))
    per = {norm_channel(k): v for k, v in config.get("channels", {}).items()}
    rules.update(per.get(norm_channel(channel), {}))
    return rules


def _contains_any(text: str, words: list) -> str | None:
    low = text.lower()
    for w in words:
        if w and w.lower() in low:
            return w
    return None


def clean_text(text: str, rules: dict) -> str:
    out = text or ""

    phrases = [p.lower() for p in rules["strip_lines_containing"] if p]
    if phrases:
        out = "\n".join(
            line for line in out.split("\n")
            if not any(p in line.lower() for p in phrases)
        )
    for old, new in rules["replace"].items():
        out = out.replace(old, new)
    if rules["remove_links"]:
        out = URL_RE.sub("", out)
    if rules["remove_usernames"]:
        out = USER_RE.sub("", out)
    if rules["remove_hashtags"]:
        out = TAG_RE.sub("", out)

    out = re.sub(r"[ \t]+\n", "\n", out)      # trailing spaces
    out = re.sub(r"[ \t]{2,}", " ", out)      # double spaces left by removals
    out = re.sub(r"\n{3,}", "\n\n", out)      # too many blank lines
    return out.strip()


def evaluate(text: str, kind: str, is_forwarded: bool, rules: dict) -> Result:
    """Decide whether to copy a post, and return its cleaned text."""
    text = text or ""

    if kind not in rules["allowed_types"]:
        return Result(False, "", f"type '{kind}' not allowed", False)
    if rules["skip_forwarded"] and is_forwarded:
        return Result(False, "", "forwarded post", False)

    hit = _contains_any(text, rules["exclude_any"])
    if hit:
        return Result(False, "", f"contains excluded word '{hit}'", False)
    for pattern in rules["exclude_regex"]:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return Result(False, "", f"matches excluded pattern '{pattern}'", False)
        except re.error:
            pass  # ignore a broken pattern instead of crashing
    if rules["include_any"] and not _contains_any(text, rules["include_any"]):
        return Result(False, "", "no required keyword found", False)

    cleaned = clean_text(text, rules)

    if kind == "text" and not cleaned:
        return Result(False, "", "empty after cleaning", False)
    if kind != "text" and rules["skip_without_text"] and not cleaned:
        return Result(False, "", "media without caption", False)
    if rules["min_length"] and len(cleaned) < rules["min_length"]:
        return Result(False, "", "text too short", False)
    if rules["max_length"] and len(cleaned) > rules["max_length"]:
        return Result(False, "", "text too long", False)

    has_link_entities = bool(rules["remove_links"])  # hidden hyperlinks need formatting dropped too
    return Result(True, cleaned, "", cleaned != text.strip() or has_link_entities)
