# Telegram channel copier

Copies new posts from other Telegram channels into yours, after filtering and
cleaning them. Every copy ends with a "Source: @channel" credit.

## Why it uses your account
Telegram bots cannot read channels they are not admins of. This program logs in
as your own Telegram account (via the official API, using Telethon).

## Setup
1. Get `api_id` and `api_hash`: https://my.telegram.org -> API development tools.
2. `cp .env.example .env` and fill in TG_API_ID, TG_API_HASH, SOURCE_CHANNELS, TARGET_CHANNEL.
3. Your account must be owner/admin of the target channel; join private source channels first.
4. Install: `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
5. First run (asks for your phone number and the login code): `python channel_copier.py`

## Modes
- `python channel_copier.py` : live, copies as soon as a post appears (keep it running).
- `python channel_copier.py --once` : checks once and exits (for cron). The first run only
  saves a starting point; add `--backfill 5` to also copy the last 5 posts of each source.

## Filtering (filters.json)
`defaults` apply to every source; `channels` overrides them per source (name without @).

| Setting | Meaning |
|---|---|
| include_any | copy only if the text has ANY of these words |
| exclude_any | skip if the text has ANY of these words |
| exclude_regex | skip if the text matches a regular expression |
| allowed_types | text, photo, video, audio, document, gif |
| min_length / max_length | length limits after cleaning (0 = off) |
| skip_forwarded | skip posts forwarded from another channel |
| skip_without_text | skip photos/videos without a caption |
| strip_lines_containing | delete whole lines that contain these phrases (ads, "join us") |
| replace | {"old": "new"} text replacements |
| remove_links / remove_usernames / remove_hashtags | clean the text |
| add_source_credit, credit_with_post_link | credit line style |
| footer | extra text added to every post |

Note: when the text is changed by cleaning, bold/italic formatting is dropped (plain text is posted).
Channels with "restrict saving content" turned on are skipped automatically.

## Duplicate detection
Before copying, the program compares each post with everything copied in the last 14 days:
- the same file (same Telegram file id, or same file name + size) is never posted twice;
- text is compared word by word after removing links and @names, so the same news posted by
  different channels (even with small edits or extra lines) is copied only once.
Tune it in filters.json: "dedupe": {"enabled": true, "threshold": 0.7, "days": 14}
(a lower threshold skips more posts, a higher one skips fewer).
