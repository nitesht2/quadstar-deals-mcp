"""source_tracker.py — Deal source performance tracking.

Tracks which deal sources (Slickdeals, DealNews, Camelcamelcamel) produce
the best-performing deals. Over time the pipeline scrapes more from sources
that consistently produce posted, engaging deals.

Metrics per source:
  scraped    — total deals found
  posted     — deals that were approved and posted
  engagement — cumulative likes + retweets + replies
  post_rate  — posted / scraped  (quality signal)
  avg_eng    — engagement / posted (virality signal)

Weight formula (used to scale scraping effort):
  weight = 0.6 * norm(post_rate) + 0.4 * norm(avg_eng)
  Normalised across all active sources so they sum to 1.0.
  New sources (< 5 deals) get a neutral weight of 1.0 until we have data.

Storage: data/source_performance.json
"""
from __future__ import annotations

import json
import os
from datetime import datetime

try:
    from config.settings import DATA_DIR
except ImportError:
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

_PERF_FILE = os.path.join(DATA_DIR, "source_performance.json")
_MIN_DEALS_FOR_WEIGHT = 5  # Don't weight until we have enough data


def _load() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(_PERF_FILE):
        return {}
    try:
        with open(_PERF_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = f"{_PERF_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, _PERF_FILE)


def _entry(data: dict, source: str) -> dict:
    if source not in data:
        data[source] = {
            "scraped": 0,
            "posted": 0,
            "engagement": 0,
            "last_scraped": None,
        }
    return data[source]


def record_scraped(source: str, count: int = 1):
    """Record that `count` deals were found from this source."""
    try:
        data = _load()
        e = _entry(data, source)
        e["scraped"] += count
        e["last_scraped"] = datetime.now().isoformat()
        _save(data)
    except Exception as exc:
        print(f"  [source_tracker] record_scraped failed: {exc}")


def record_posted(source: str):
    """Record that a deal from this source was approved and posted."""
    try:
        data = _load()
        e = _entry(data, source)
        e["posted"] += 1
        _save(data)
    except Exception as exc:
        print(f"  [source_tracker] record_posted failed: {exc}")


def record_engagement(source: str, engagement: int):
    """Add engagement (likes+retweets+replies) to this source's running total."""
    try:
        if engagement <= 0:
            return
        data = _load()
        e = _entry(data, source)
        e["engagement"] = e.get("engagement", 0) + engagement
        _save(data)
    except Exception as exc:
        print(f"  [source_tracker] record_engagement failed: {exc}")


def get_source_weights() -> dict[str, float]:
    """Return {source_name: weight} for scraping prioritisation.

    Sources with < MIN_DEALS_FOR_WEIGHT scraped deals get weight 1.0.
    Otherwise weight is computed from post_rate + avg_engagement,
    normalised so the highest-performing source gets weight 2.0 and
    the lowest gets 0.5 (avoids starving any source completely).
    """
    try:
        data = _load()
        if not data:
            return {}

        scores = {}
        for source, e in data.items():
            scraped = e.get("scraped", 0)
            posted = e.get("posted", 0)
            engagement = e.get("engagement", 0)
            if scraped < _MIN_DEALS_FOR_WEIGHT:
                scores[source] = None  # Not enough data
                continue
            post_rate = posted / scraped if scraped else 0
            avg_eng = engagement / posted if posted else 0
            scores[source] = (post_rate, avg_eng)

        # Normalise known scores
        known = {s: v for s, v in scores.items() if v is not None}
        if not known:
            return {s: 1.0 for s in scores}

        max_post_rate = max(v[0] for v in known.values()) or 1
        max_avg_eng = max(v[1] for v in known.values()) or 1

        weights = {}
        for source, v in scores.items():
            if v is None:
                weights[source] = 1.0
            else:
                norm_pr = v[0] / max_post_rate
                norm_eng = v[1] / max_avg_eng
                raw = 0.6 * norm_pr + 0.4 * norm_eng
                # Scale to [0.5, 2.0] so no source is completely ignored
                weights[source] = round(0.5 + 1.5 * raw, 2)

        return weights

    except Exception as exc:
        print(f"  [source_tracker] get_source_weights failed: {exc}")
        return {}


def get_report() -> str:
    """Return a human-readable performance report for all sources."""
    try:
        data = _load()
        if not data:
            return "No source performance data yet."

        weights = get_source_weights()
        lines = ["Source Performance Report", "─" * 50]
        for source, e in sorted(data.items()):
            scraped = e.get("scraped", 0)
            posted = e.get("posted", 0)
            engagement = e.get("engagement", 0)
            post_rate = f"{posted/scraped*100:.1f}%" if scraped else "n/a"
            avg_eng = f"{engagement/posted:.1f}" if posted else "n/a"
            weight = weights.get(source, 1.0)
            lines.append(
                f"{source:<25} scraped={scraped:>4}  posted={posted:>3}  "
                f"post_rate={post_rate:>6}  avg_eng={avg_eng:>6}  weight={weight:.2f}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Source report failed: {exc}"
