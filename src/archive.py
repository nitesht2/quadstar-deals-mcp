"""
archive.py — permanent SQLite history (write-alongside the JSON pipeline).

The live pipeline still runs on the rolling JSON store (deals expire 24h, posted
purge 7d). This module ALSO records every saved deal and every posted deal into
data/quadstar.db, kept forever — for analytics, audit, and learning. SQLite is
stdlib (`sqlite3`), free, serverless, one file.

All writes are fire-and-forget: any failure is swallowed so the archive can
NEVER break scraping/posting. Read it anytime:
    sqlite3 data/quadstar.db 'select count(*) from deals_archive;'
"""
import os
import sqlite3
from datetime import datetime

try:
    from config.settings import DATA_DIR
except ImportError:
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

DB_PATH = os.path.join(DATA_DIR, "quadstar.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deals_archive (
    asin           TEXT PRIMARY KEY,
    title          TEXT,
    deal_price     REAL,
    original_price REAL,
    discount_pct   REAL,
    category       TEXT,
    source         TEXT,
    source_url     TEXT,
    affiliate_url  TEXT,
    star_rating    REAL,
    review_count   INTEGER,
    first_seen     TEXT,
    last_seen      TEXT,
    times_seen     INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS posts_archive (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asin          TEXT,
    title         TEXT,
    deal_price    REAL,
    discount_pct  REAL,
    category      TEXT,
    affiliate_url TEXT,
    tweet_1       TEXT,
    copy_source   TEXT,
    posted_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_asin ON posts_archive(asin);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")   # concurrent readers/writers across processes
    c.executescript(_SCHEMA)
    return c


def archive_deal(deal: dict) -> None:
    """Permanently record a saved deal (upsert by ASIN; bumps last_seen/times_seen)."""
    asin = (deal.get("asin") or "").strip()
    if not asin:
        return
    now = datetime.now().isoformat()
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO deals_archive
                   (asin,title,deal_price,original_price,discount_pct,category,source,
                    source_url,affiliate_url,star_rating,review_count,first_seen,last_seen,times_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(asin) DO UPDATE SET
                     last_seen=excluded.last_seen,
                     deal_price=excluded.deal_price,
                     discount_pct=excluded.discount_pct,
                     times_seen=deals_archive.times_seen+1""",
                (asin, deal.get("title"), deal.get("deal_price"), deal.get("original_price"),
                 deal.get("discount_pct"), deal.get("category"), deal.get("source"),
                 deal.get("source_url"), deal.get("affiliate_url"), deal.get("star_rating"),
                 deal.get("review_count"), now, now),
            )
    except Exception as exc:
        print(f"  [archive] deal archive skipped ({exc})")


def archive_post(deal: dict) -> None:
    """Permanently record a posted deal (one row per post)."""
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO posts_archive
                   (asin,title,deal_price,discount_pct,category,affiliate_url,tweet_1,copy_source,posted_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (deal.get("asin"), deal.get("title"), deal.get("deal_price"), deal.get("discount_pct"),
                 deal.get("category"), deal.get("affiliate_url"),
                 deal.get("hermes_tweet_1"), deal.get("copy_source"),
                 deal.get("posted_at") or datetime.now().isoformat()),
            )
    except Exception as exc:
        print(f"  [archive] post archive skipped ({exc})")


def stats() -> dict:
    """Quick counts for a status check."""
    try:
        with _conn() as c:
            d = c.execute("SELECT COUNT(*) FROM deals_archive").fetchone()[0]
            p = c.execute("SELECT COUNT(*) FROM posts_archive").fetchone()[0]
            return {"deals_archived": d, "posts_archived": p, "db": DB_PATH}
    except Exception as exc:
        return {"error": str(exc)}
