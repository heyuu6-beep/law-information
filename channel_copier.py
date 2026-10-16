#!/usr/bin/env python3
"""
Telegram channel copier
-----------------------
Copies new posts (text, photos, videos, files/documents, albums) from other
Telegram channels into your own channel after filtering/cleaning them with the
rules in filters.json. Each copy ends with "Source: @channel". Posts that are
the same or very similar to one already copied (even from another channel) are
skipped.

It logs in as YOUR Telegram account (bots cannot read channels they don't
administer). Your account must be owner/admin of the target channel.

Usage:
    python channel_copier.py                        # live mode: runs forever
    python channel_copier.py --once                 # check once, then exit (cron / GitHub)
    python channel_copier.py --once --backfill 5    # first run: also copy last 5 posts per source
    python channel_copier.py --print-session        # print a login string (for GitHub)
"""
import argparse
import asyncio
import logging
import os
import sqlite3
import time
from types import SimpleNamespace

from dotenv import load_dotenv
from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, InputPeerChannel

import dedupe
from filters import evaluate, load_config, norm_channel, rules_for

load_dotenv()

API_ID = int(os.getenv("TG_API_ID", "0") or 0)
API_HASH = os.getenv("TG_API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
SESSION_NAME = os.getenv("SESSION_NAME", "copier_session")
TARGET = os.getenv("TARGET_CHANNEL", "")
SOURCES = [c.strip() for c in os.getenv("SOURCE_CHANNELS", "").replace("\n", ",").split(",") if c.strip()]
DELAY = float(os.getenv("POST_DELAY_SECONDS", "3"))
DB_FILE = os.getenv("DB_FILE", "copier.db")
CONFIG_FILE = os.getenv("FILTERS_FILE", "filters.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("copier")

CONFIG = load_config(CONFIG_FILE)
DEDUPE = {"enabled": True, "threshold": 0.7, "days": 14}
DEDUPE.update(CONFIG.get("dedupe", {}))

db = sqlite3.connect(DB_FILE)
db.executescript("""
CREATE TABLE IF NOT EXISTS copied (chat_id INTEGER, msg_id INTEGER, PRIMARY KEY (chat_id, msg_id));
CREATE TABLE IF NOT EXISTS state (channel TEXT PRIMARY KEY, last_id INTEGER);
CREATE TABLE IF NOT EXISTS fingerprints (tokens TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS media_sigs (sig TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS peers (channel TEXT PRIMARY KEY, channel_id INTEGER, access_hash INTEGER,
                                  username TEXT, title TEXT, noforwards INTEGER);
""")
db.commit()


# ------------------------------------------------------------ bookkeeping
def already_done(chat_id: int, msg_id: int) -> bool:
    return db.execute("SELECT 1 FROM copied WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)).fetchone() is not None


def mark_done(chat_id: int, msg_id: int) -> None:
    db.execute("INSERT OR IGNORE INTO copied VALUES (?, ?)", (chat_id, msg_id))
    db.commit()


def prune_old() -> None:
    cutoff = time.time() - DEDUPE["days"] * 86400
    db.execute("DELETE FROM fingerprints WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM media_sigs WHERE ts < ?", (cutoff,))
    db.commit()


# ------------------------------------------------------------ duplicate detection
def media_signatures(m) -> list:
    """Identifiers of a message's file: the same file forwarded between channels has the same id."""
    sigs = []
    if m.photo:
        sigs.append(f"photo:{m.photo.id}")
    elif m.document:
        d = m.document
        sigs.append(f"doc:{d.id}")
        name = next((a.file_name for a in d.attributes if getattr(a, "file_name", None)), "")
        if name and getattr(d, "size", 0):
            sigs.append(f"file:{name.lower()}:{d.size}")
    return sigs


def duplicate_reason(text: str, media_groups: list) -> str:
    if not DEDUPE["enabled"]:
        return ""
    cutoff = time.time() - DEDUPE["days"] * 86400
    groups = [g for g in media_groups if g]
    if groups:
        def seen(sig):
            return db.execute("SELECT 1 FROM media_sigs WHERE sig=? AND ts>?", (sig, cutoff)).fetchone() is not None
        if all(any(seen(s) for s in g) for g in groups):
            return "same file was already posted"
    toks = dedupe.tokens(text)
    if len(toks) >= 4:
        rows = db.execute("SELECT tokens FROM fingerprints WHERE ts>? ORDER BY ts DESC LIMIT 2000", (cutoff,))
        for (stored,) in rows:
            if dedupe.is_similar(toks, stored.split(" "), DEDUPE["threshold"]):
                return "a similar post was already posted"
    return ""


def remember(text: str, media_groups: list) -> None:
    now = time.time()
    toks = dedupe.tokens(text)
    if len(toks) >= 4:
        db.execute("INSERT INTO fingerprints VALUES (?, ?)", (" ".join(toks), now))
    for g in media_groups:
        for sig in g:
            db.execute("INSERT OR REPLACE INTO media_sigs VALUES (?, ?)", (sig, now))
    db.commit()


# ------------------------------------------------------------ message helpers
def media_kind(m) -> str:
    """text | photo | video | gif | audio | document | unsupported"""
    if m.photo:
        return "photo"
    if m.video or m.video_note:
        return "video"
    if m.gif:
        return "gif"
    if m.voice or m.audio:
        return "audio"
    if m.sticker:
        return "unsupported"
    if m.document:
        return "document"
    if m.media and not m.web_preview:  # polls, contacts, locations, games...
        return "unsupported"
    return "text"


async def with_flood_wait(make_call):
    """Run a Telegram call; if Telegram says 'slow down', wait and retry once."""
    try:
        return await make_call()
    except errors.FloodWaitError as e:
        log.warning("Flood wait: sleeping %ss", e.seconds)
        await asyncio.sleep(e.seconds + 1)
        return await make_call()


# ------------------------------------------------------------ core: copy one post (or album)
async def copy_group(client, msgs: list, chat) -> None:
    first = msgs[0]
    chat_id = first.chat_id
    title = getattr(chat, "title", "") or str(chat_id)
    if already_done(chat_id, first.id):
        return
    mark_done(chat_id, first.id)  # mark first: never retry-spam after an error

    if getattr(chat, "noforwards", False):
        log.info("Skipped (channel restricts copying): %s", title)
        return

    kinds = [media_kind(m) for m in msgs]
    if "unsupported" in kinds:
        log.info("Skipped (unsupported content) %s #%s", title, first.id)
        return
    kind = next((k for k in kinds if k != "text"), "text")

    text = next((m.message for m in msgs if m.message), "")
    entities = next((m.entities for m in msgs if m.message), None)
    is_forwarded = any(m.fwd_from for m in msgs)

    username = getattr(chat, "username", None)
    rules = rules_for(CONFIG, username or str(chat_id))
    result = evaluate(text, kind, is_forwarded, rules)
    if not result.ok:
        log.info("Skipped %s #%s: %s", title, first.id, result.reason)
        return

    media_groups = [media_signatures(m) for m in msgs if m.media and not m.web_preview]
    dup = duplicate_reason(result.text, media_groups)
    if dup:
        log.info("Skipped %s #%s: %s", title, first.id, dup)
        return

    # final text = cleaned text + source credit (+ optional footer)
    final = result.text
    extra = []
    if rules["add_source_credit"]:
        if rules["credit_with_post_link"] and username:
            extra.append(f"Source: https://t.me/{username}/{first.id}")
        else:
            extra.append(f"Source: @{username}" if username else f"Source: {title}")
    if rules["footer"]:
        extra.append(rules["footer"])
    if extra:
        final = (final + "\n\n" if final else "") + "\n".join(extra)

    keep_format = bool(entities) and not result.modified
    if len(final) > 4096:
        final, keep_format = final[:4093] + "…", False
    kw = {"formatting_entities": entities} if keep_format else {"parse_mode": None}

    media = [m.media for m in msgs if m.media and not m.web_preview]
    try:
        if media:
            files = media if len(media) > 1 else media[0]
            if len(final) <= 1024:
                try:
                    await with_flood_wait(lambda: client.send_file(TARGET, files, caption=final, **kw))
                except TypeError:  # older Telethon without entity support for albums
                    await with_flood_wait(lambda: client.send_file(TARGET, files, caption=final, parse_mode=None))
            else:  # caption limit is 1024: send the files, then the long text
                await with_flood_wait(lambda: client.send_file(TARGET, files))
                await with_flood_wait(lambda: client.send_message(TARGET, final, **kw))
        else:
            await with_flood_wait(lambda: client.send_message(TARGET, final, **kw))
        remember(result.text, media_groups)
        log.info("Copied %s #%s -> %s", title, first.id, TARGET)
        await asyncio.sleep(DELAY)
    except Exception:
        log.exception("Failed to copy %s #%s", title, first.id)


# ------------------------------------------------------------ live mode
async def on_new_message(event):
    if event.message.grouped_id:  # albums are handled by on_album
        return
    await copy_group(event.client, [event.message], await event.get_chat())


async def on_album(event):
    await copy_group(event.client, list(event.messages), await event.get_chat())


# ------------------------------------------------------------ --once mode
def get_state(channel: str):
    row = db.execute("SELECT last_id FROM state WHERE channel=?", (norm_channel(channel),)).fetchone()
    return row[0] if row else None


def set_state(channel: str, last_id: int) -> None:
    db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (norm_channel(channel), last_id))
    db.commit()


async def resolve(client, channel: str):
    """Return (peer, info). Resolved channels are cached in the database, so
    repeated runs do not ask Telegram to look up the same usernames again."""
    key = norm_channel(channel)
    row = db.execute("SELECT channel_id, access_hash, username, title, noforwards FROM peers WHERE channel=?",
                     (key,)).fetchone()
    if row:
        peer = InputPeerChannel(row[0], row[1])
        return peer, SimpleNamespace(username=row[2], title=row[3], noforwards=bool(row[4]))
    entity = await client.get_entity(channel)
    if isinstance(entity, Channel):
        db.execute("INSERT OR REPLACE INTO peers VALUES (?, ?, ?, ?, ?, ?)",
                   (key, entity.id, entity.access_hash, entity.username, entity.title, int(bool(entity.noforwards))))
        db.commit()
    return entity, entity


def group_messages(msgs: list) -> list:
    groups, index = [], {}
    for m in msgs:
        if m.action:  # join/pin/service messages
            continue
        if m.grouped_id:
            if m.grouped_id in index:
                index[m.grouped_id].append(m)
                continue
            g = [m]
            index[m.grouped_id] = g
            groups.append(g)
        else:
            groups.append([m])
    return groups


async def poll_once(client, backfill: int) -> None:
    prune_old()
    for channel in SOURCES:
        try:
            peer, info = await resolve(client, channel)
            last = get_state(channel)
            if last is None:  # first time we see this channel
                latest = await client.get_messages(peer, limit=max(1, backfill))
                top = latest[0].id if latest else 0
                set_state(channel, top)
                if not backfill:
                    log.info("%s: starting point saved, only new posts will be copied", channel)
                    continue
                msgs = list(reversed(latest))
            else:
                msgs = [m async for m in client.iter_messages(peer, min_id=last, reverse=True, limit=200)]

            for group in group_messages(msgs):
                await copy_group(client, group, info)
            if msgs:
                set_state(channel, max(m.id for m in msgs))
            log.info("%s: %d new message(s) checked", channel, len(msgs))
        except errors.FloodWaitError as e:
            log.warning("Telegram asked to wait %ss at %s; stopping this run early", e.seconds, channel)
            break
        except Exception as exc:
            log.error("Cannot read %s: %s (is it a channel you can see? join it if private)", channel, exc)


# ------------------------------------------------------------ main
async def run(args) -> None:
    session = StringSession(SESSION_STRING) if SESSION_STRING else SESSION_NAME
    client = TelegramClient(session, API_ID, API_HASH)
    await client.start()  # first run on a PC asks for phone number + login code
    me = await client.get_me()
    log.info("Logged in as %s | %d source channels | target: %s", me.first_name, len(SOURCES), TARGET)

    if args.once:
        await poll_once(client, args.backfill)
        await client.disconnect()
        return

    prune_old()
    client.add_event_handler(on_new_message, events.NewMessage(chats=SOURCES))
    client.add_event_handler(on_album, events.Album(chats=SOURCES))
    log.info("Live mode: waiting for new posts (Ctrl+C to stop)")
    await client.run_until_disconnected()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--backfill", type=int, default=0, help="with --once: copy the last N posts on first run")
    parser.add_argument("--print-session", action="store_true", help="print a session string and exit")
    args = parser.parse_args()

    if not API_ID or not API_HASH:
        raise SystemExit("Set TG_API_ID and TG_API_HASH (get them at https://my.telegram.org)")

    if args.print_session:
        with TelegramClient(StringSession(), API_ID, API_HASH) as c:
            print("\nSESSION_STRING=" + c.session.save())
            print("Keep this secret: it gives full access to your Telegram account.")
        return

    if not SOURCES or not TARGET:
        raise SystemExit("Set SOURCE_CHANNELS and TARGET_CHANNEL")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
