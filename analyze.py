#!/usr/bin/env python3
"""
Flowtrack - AI Pattern Analyzer  (Modules 3 & 4)

Usage:
    python3 analyze.py              # full analysis + Ollama (if running)
    python3 analyze.py --no-ai      # skip Ollama, print stats only
    python3 analyze.py --days N     # analyse last N days (default 7)

Outputs:
    ~/.focusaudit/reports/analysis_YYYY-MM-DD.txt      raw stats
    ~/.focusaudit/reports/ai_analysis_YYYY-MM-DD.txt   Ollama response (if available)
    ~/.focusaudit/reports/ai_prompt_YYYY-MM-DD.txt     prompt for manual paste
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(os.environ.get("FLOWTRACK_HOME", Path.home() / ".focusaudit")).expanduser()
LOG_DIR    = BASE_DIR / "logs"
REPORT_DIR = BASE_DIR / "reports"

def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            path.chmod(0o700)
        except OSError:
            pass


def _write_private_text(path: Path, text: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError:
            pass


for _data_dir in (BASE_DIR, LOG_DIR, REPORT_DIR):
    _ensure_private_dir(_data_dir)

log = logging.getLogger(__name__)

# ── Ollama ───────────────────────────────────────────────────────────────────────
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"
OPENAI_URL   = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
DEFAULT_MODELS = {
    "ollama": OLLAMA_MODEL,
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
    "gemini": "gemini-3.6-flash",
}

# ── Pattern constants ─────────────────────────────────────────────────────────────
RAPID_SWITCH_THRESHOLD = 5    # tab changes …
RAPID_SWITCH_WINDOW    = 120  # … within this many seconds

RABBIT_HOLE_THRESHOLD  = 20   # unique browser-tab titles in a rolling window
RABBIT_HOLE_WINDOW_MIN = 30   # rolling window size in minutes

DEEP_WORK_SECONDS      = 25 * 60   # 25 minutes
LOG_INTERVAL           = 30        # default duration fallback (seconds)
LEGACY_IDLE_GAP_SECONDS = LOG_INTERVAL * 2
SESSION_GAP_SECONDS     = 5 * 60
CONTIGUITY_TOLERANCE_SECONDS = 2

BROWSER_APPS = {
    "chrome", "chromium", "chromium-browser", "google-chrome",
    "firefox", "firefox-esr", "brave", "brave-browser",
    "opera", "vivaldi", "microsoft-edge", "epiphany", "midori", "safari",
}

APP_ALIASES = {
    "google chrome": "chrome",
    "chrome": "chrome",
    "chromium": "chromium",
    "chromium browser": "chromium-browser",
    "firefox": "firefox",
    "firefox developer edition": "firefox",
    "microsoft edge": "microsoft-edge",
    "msedge": "microsoft-edge",
    "safari": "safari",
}

SOCIAL_PATTERNS: dict[str, str] = {
    "YouTube":    r"youtube\.com|YouTube",
    "Instagram":  r"instagram\.com|Instagram",
    "Reddit":     r"reddit\.com|Reddit",
    "TikTok":     r"tiktok\.com|TikTok",
    "Twitter/X":  r"twitter\.com|x\.com|\bTwitter\b",
    "Facebook":   r"facebook\.com|Facebook",
    "LinkedIn":   r"linkedin\.com|LinkedIn",
    "Reels":      r"\bReels?\b",
}

# ════════════════════════════════════════════════════════════════════════════════
#  Data loading
# ════════════════════════════════════════════════════════════════════════════════

def normalize_app_name(value: str) -> str:
    """Normalize OS-specific process names for cross-platform classification."""
    name = value.strip().casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    return APP_ALIASES.get(name, name)


def _coerce_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    timestamp = value.get("ts")
    if not isinstance(timestamp, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    # Logs are local wall-clock observations. Convert offset-aware imports to
    # the current local zone, then remove tzinfo so mixed files sort safely.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)

    title = value.get("title", "")
    app = value.get("app", "unknown")
    if not isinstance(title, str) or not isinstance(app, str):
        return None
    try:
        duration = float(value.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration < 0:
        duration = 0.0
    try:
        schema_version = int(value.get("schema_version", 1))
    except (TypeError, ValueError):
        schema_version = 1

    entry = dict(value)
    entry.update({
        "dt": parsed,
        "title": title,
        "app": app,
        "duration": duration,
        "event": str(value.get("event", "interval")),
        "schema_version": schema_version,
    })
    return entry


def _identity(entry: dict, app_only: bool = False) -> tuple[str, ...]:
    app = normalize_app_name(str(entry.get("app", "")))
    return (app,) if app_only else (app, str(entry.get("title", "")))


def _gap_seconds(previous: dict, current: dict) -> float:
    return (current["dt"] - previous["dt"]).total_seconds()


def _same_session(previous: dict, current: dict) -> bool:
    gap = _gap_seconds(previous, current)
    return 0 <= gap <= SESSION_GAP_SECONDS


def normalize_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate entries and repair legacy cumulative-duration records.

    Version-1 rows were snapshots whose stored duration was cumulative and was
    attached to the newly focused window. Their reliable signal is the gap
    between adjacent timestamps, which belongs to the earlier snapshot.
    """
    prepared = []
    for value in entries:
        entry = value if isinstance(value, dict) and isinstance(value.get("dt"), datetime.datetime) else _coerce_entry(value)
        if entry is not None:
            prepared.append(dict(entry))
    prepared.sort(key=lambda entry: entry["dt"])

    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(prepared):
        item = dict(entry)
        if int(item.get("schema_version", 1)) >= 2:
            try:
                duration = float(item.get("duration", 0))
            except (TypeError, ValueError):
                duration = 0.0
            item["duration"] = duration if math.isfinite(duration) and duration >= 0 else 0.0
            normalized.append(item)
            continue

        following = prepared[index + 1] if index + 1 < len(prepared) else None
        duration = 0.0
        changed = False
        if following is not None:
            gap = _gap_seconds(item, following)
            if gap >= 0:
                duration = min(gap, LEGACY_IDLE_GAP_SECONDS)
                changed = gap <= SESSION_GAP_SECONDS and _identity(item) != _identity(following)
        item["duration"] = duration
        item["event"] = "change" if changed else "interval"
        item["legacy_normalized"] = True
        normalized.append(item)
    return normalized


def load_entries(days: int = 7) -> list[dict[str, Any]]:
    """Load, validate, sort, and normalize recent JSONL activity records."""
    today = datetime.date.today()
    entries: list[dict[str, Any]] = []
    skipped = 0
    for delta in range(max(0, days)):
        date = today - datetime.timedelta(days=delta)
        log_file = LOG_DIR / f"{date.isoformat()}.jsonl"
        if not log_file.exists() or log_file.is_symlink():
            continue
        try:
            with open(log_file, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        decoded = json.loads(raw)
                    except json.JSONDecodeError:
                        skipped += 1
                        continue
                    entry = _coerce_entry(decoded)
                    if entry is None:
                        skipped += 1
                    else:
                        entries.append(entry)
        except OSError as exc:
            log.warning("Could not read %s: %s", log_file, exc)
    if skipped:
        log.warning("Skipped %d malformed Flowtrack log row(s).", skipped)
    return normalize_entries(entries)


# ════════════════════════════════════════════════════════════════════════════════
#  Module 3 — Rapid Switching detector
# ════════════════════════════════════════════════════════════════════════════════

def _change_records(entries: list[dict], *, app_only: bool = False) -> list[dict]:
    changes = []
    for previous, current in zip(entries, entries[1:]):
        if not _same_session(previous, current):
            continue
        # In schema v2 the row describes the segment that just ended. Only a
        # segment closed because focus changed can introduce a transition;
        # interval/flush rows must not connect separate tracker sessions.
        if str(previous.get("event", "interval")).casefold() != "change":
            continue
        before = _identity(previous, app_only=app_only)
        after = _identity(current, app_only=app_only)
        if before == after:
            continue
        changes.append({
            "dt": current["dt"],
            "ts": current["ts"],
            "from": before,
            "to": after,
            "apps": {before[0] or "unknown", after[0] or "unknown"},
        })
    return changes


def detect_rapid_switching(entries: list[dict]) -> list[dict]:
    """
    Return periods where more than RAPID_SWITCH_THRESHOLD window changes
    occurred within RAPID_SWITCH_WINDOW seconds.
    """
    changes = _change_records(entries)
    events = []
    n = len(changes)
    i = 0
    while i < n:
        window_end = changes[i]["dt"] + datetime.timedelta(seconds=RAPID_SWITCH_WINDOW)
        j = i
        while j < n and changes[j]["dt"] <= window_end:
            j += 1
        count = j - i
        if count > RAPID_SWITCH_THRESHOLD:
            apps = sorted(set().union(*(change["apps"] for change in changes[i:j])))
            events.append({
                "start":   changes[i]["ts"],
                "end":     changes[j - 1]["ts"],
                "switches": count,
                "apps":    apps,
            })
            i = j   # jump past the burst to avoid overlap
        else:
            i += 1
    return events


# ════════════════════════════════════════════════════════════════════════════════
#  Module 4 — Pattern Finder
# ════════════════════════════════════════════════════════════════════════════════

# ── "The Rabbit Hole" ─────────────────────────────────────────────────────────

def detect_rabbit_holes(entries: list[dict]) -> list[dict]:
    """
    Detect browser sessions where many unique tab titles appear in a short window
    (one search / curiosity chain leading to many unrelated tabs).
    """
    browser_entries = [
        e for e in entries
        if normalize_app_name(str(e.get("app", ""))) in BROWSER_APPS
    ]
    holes = []
    n = len(browser_entries)
    i = 0
    while i < n:
        window_end   = browser_entries[i]["dt"] + datetime.timedelta(minutes=RABBIT_HOLE_WINDOW_MIN)
        j            = i
        while j < n and browser_entries[j]["dt"] <= window_end:
            if j > i and not _same_session(browser_entries[j - 1], browser_entries[j]):
                break
            j += 1
        unique_titles = len({e["title"] for e in browser_entries[i:j]})
        if unique_titles >= RABBIT_HOLE_THRESHOLD:
            tab_changes = sum(
                browser_entries[k - 1]["title"] != browser_entries[k]["title"]
                for k in range(i + 1, j)
            )
            holes.append({
                "start":        browser_entries[i]["ts"],
                "tab_changes":  tab_changes,
                "unique_tabs":  unique_titles,
                "trigger_title": browser_entries[i]["title"][:120],
            })
            i = j
        else:
            i += 1
    return holes


# ── "The Fatigue Pattern" ─────────────────────────────────────────────────────

def detect_fatigue_pattern(entries: list[dict]) -> dict[str, Any]:
    """
    Actual window-switch rate per observed active hour by time-of-day slot.
    Fatigue is flagged when the afternoon rate is ≥50 % higher than morning.
    """
    active_seconds = {"morning": 0.0, "afternoon": 0.0, "evening": 0.0}

    def bucket(hour: int) -> str | None:
        if 6 <= hour < 12:
            return "morning"
        if 12 <= hour < 18:
            return "afternoon"
        if 18 <= hour < 22:
            return "evening"
        return None

    for e in entries:
        label = bucket(e["dt"].hour)
        if label:
            active_seconds[label] += max(0.0, float(e.get("duration", 0)))

    switch_counts = Counter()
    for change in _change_records(entries):
        label = bucket(change["dt"].hour)
        if label:
            switch_counts[label] += 1

    def rate(label: str) -> float:
        seconds = active_seconds[label]
        return round(switch_counts[label] / (seconds / 3600), 1) if seconds > 0 else 0.0

    m_rate = rate("morning")
    a_rate = rate("afternoon")
    e_rate = rate("evening")
    # percent change from morning to afternoon; handle zero-morning safely
    if m_rate <= 0:
        pm_vs_am_pct = 100.0 if a_rate > 0 else 0.0
    else:
        pm_vs_am_pct = round(((a_rate - m_rate) / m_rate) * 100, 1)

    return {
        "morning_rate":     m_rate,
        "afternoon_rate":   a_rate,
        "evening_rate":     e_rate,
        "fatigue_detected": active_seconds["morning"] > 0 and a_rate > m_rate * 1.5,
        "pm_vs_am_pct":     pm_vs_am_pct,
    }


# ── "The Visual Bait" ─────────────────────────────────────────────────────────

def detect_visual_bait(entries: list[dict]) -> dict[str, dict]:
    """
    For each social/entertainment platform, compute visits, total minutes,
    and the maximum number of context-switches triggered in the 30 min after
    first landing on that site.
    """
    results: dict[str, dict] = {}
    changes = _change_records(entries)
    for site, pattern in SOCIAL_PATTERNS.items():
        rx       = re.compile(pattern, re.IGNORECASE)
        matches = [bool(rx.search(str(e.get("title", "")))) for e in entries]
        hit_idx = [i for i, matched in enumerate(matches) if matched]
        if not hit_idx:
            continue
        total_sec = sum(entries[i].get("duration", 0) for i in hit_idx)
        trigger_idx = [
            index for index in hit_idx
            if index == 0
            or not matches[index - 1]
            or not _same_session(entries[index - 1], entries[index])
        ]
        max_post  = 0
        for idx in trigger_idx:
            cutoff   = entries[idx]["dt"] + datetime.timedelta(minutes=30)
            post_cnt = sum(
                1 for change in changes
                if entries[idx]["dt"] <= change["dt"] <= cutoff
            )
            max_post = max(max_post, post_cnt)
        results[site] = {
            "visits":                     len(trigger_idx),
            "total_minutes":              round(total_sec / 60, 1),
            "max_post_trigger_switches":  max_post,
        }
    return results


# ── Context-switching between apps ────────────────────────────────────────────

def analyze_context_switches(entries: list[dict]) -> dict[str, Any]:
    """
    Track explicit app-to-app transitions (Chrome→VSCode, etc.).
    This is the core "Focus Score" signal.
    """
    switches = [
        {"from": change["from"][0], "to": change["to"][0], "at": change["ts"]}
        for change in _change_records(entries, app_only=True)
    ]

    pair_counts  = Counter((s["from"], s["to"]) for s in switches)
    top_pairs    = [
        {"from": p[0], "to": p[1], "count": c}
        for p, c in pair_counts.most_common(10)
    ]
    total_hours = sum(max(0.0, float(e.get("duration", 0))) for e in entries) / 3600
    return {
        "total_app_switches":   len(switches),
        "switches_per_hour":    round(len(switches) / total_hours, 1) if total_hours > 0 else 0.0,
        "top_app_pairs":        top_pairs,
    }


# ── Focus Score ───────────────────────────────────────────────────────────────

def merge_activity_blocks(entries: list[dict]) -> list[dict]:
    """Merge contiguous 30-second segments belonging to the same window."""
    blocks: list[dict] = []
    for entry in entries:
        duration = max(0.0, float(entry.get("duration", 0)))
        if blocks:
            previous = blocks[-1]
            expected_start = previous["dt"] + datetime.timedelta(seconds=previous["duration"])
            continuity_gap = (entry["dt"] - expected_start).total_seconds()
            if (
                _identity(previous) == _identity(entry)
                and abs(continuity_gap) <= CONTIGUITY_TOLERANCE_SECONDS
            ):
                previous["duration"] += duration
                continue
        block = dict(entry)
        block["duration"] = duration
        blocks.append(block)
    return blocks


def _hour_slices(start: datetime.datetime, duration: float) -> list[tuple[str, float]]:
    slices = []
    cursor = start
    remaining = max(0.0, duration)
    while remaining > 0:
        hour_start = cursor.replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + datetime.timedelta(hours=1)
        seconds = min(remaining, max(0.0, (hour_end - cursor).total_seconds()))
        if seconds <= 0:
            break
        slices.append((hour_start.strftime("%Y-%m-%d %H:00"), seconds))
        remaining -= seconds
        cursor += datetime.timedelta(seconds=seconds)
    return slices


def calculate_focus_score(entries: list[dict]) -> dict[str, Any]:
    """
    Per-hour and daily focus score (0–100).

    Score components (per hour):
      40 % — average time per window   (5 min avg = 100)
      40 % — switch-rate score         (0 switches = 100, 67/hr = 0)
      20 % — deep-work ratio           (% of time in ≥25 min uninterrupted blocks)
    """
    if not entries:
        return {"daily": 0, "hourly": {}}

    active_seconds: dict[str, float] = defaultdict(float)
    deep_seconds: dict[str, float] = defaultdict(float)
    block_durations: dict[str, list[float]] = defaultdict(list)
    blocks = merge_activity_blocks(entries)
    for block in blocks:
        duration = block["duration"]
        for hour_key, seconds in _hour_slices(block["dt"], duration):
            active_seconds[hour_key] += seconds
            block_durations[hour_key].append(duration)
            if duration >= DEEP_WORK_SECONDS:
                deep_seconds[hour_key] += seconds

    switch_counts = Counter(
        change["dt"].strftime("%Y-%m-%d %H:00")
        for change in _change_records(entries)
    )

    hourly_scores: dict[str, float] = {}
    for hour_key in sorted(active_seconds):
        durations = block_durations[hour_key]
        avg_dur = sum(durations) / len(durations) if durations else 0.0
        time_score = min(100.0, max(0.0, (avg_dur / 300) * 100))
        rate_score = max(0.0, 100.0 - switch_counts[hour_key] * 1.5)
        deep_score = deep_seconds[hour_key] / max(active_seconds[hour_key], 1) * 100
        score = 0.4 * time_score + 0.4 * rate_score + 0.2 * deep_score
        hourly_scores[hour_key] = round(min(100.0, max(0.0, score)), 1)

    total_active = sum(active_seconds.values())
    daily = (
        round(sum(hourly_scores[key] * active_seconds[key] for key in hourly_scores) / total_active, 1)
        if total_active > 0 else 0.0
    )
    return {"daily": daily, "hourly": hourly_scores}


# ════════════════════════════════════════════════════════════════════════════════
#  Reporting
# ════════════════════════════════════════════════════════════════════════════════

def _top_apps(entries: list[dict], n: int = 10) -> list[tuple[str, float]]:
    """Return (app_name, minutes) for the top-n apps by time."""
    totals: dict[str, float] = defaultdict(float)
    for e in entries:
        totals[e.get("app", "unknown")] += e.get("duration", 0) / 60
    return sorted(totals.items(), key=lambda x: x[1], reverse=True)[:n]


def generate_text_report(
    entries:        list[dict],
    rapid:          list[dict],
    holes:          list[dict],
    fatigue:        dict,
    visual_bait:    dict,
    ctx_switches:   dict,
    focus:          dict,
    days:           int = 7,
) -> str:
    today = datetime.date.today().isoformat()
    lines = [
        "══════════════════════════════════════════════════════════════",
        f"  Flowtrack Analysis  —  generated {today}",
        "══════════════════════════════════════════════════════════════",
        "",
        f"TOTAL EVENTS (last {days} day{'s' if days != 1 else ''}): {len(entries)}",
        "",
        "┌─ FOCUS SCORE ──────────────────────────────────────────────",
        f"│  Daily average : {focus['daily']} / 100",
        "│  (40% avg window time · 40% switch rate · 20% deep work)",
        "│",
        "│  Worst hours:",
    ]
    worst = sorted(focus["hourly"].items(), key=lambda x: x[1])[:5]
    for h, s in worst:
        lines.append(f"│    {h}  →  {s}/100")
    lines.append("└────────────────────────────────────────────────────────────")

    lines += [
        "",
        "┌─ CONTEXT SWITCHING (app-to-app) ───────────────────────────",
        f"│  Total app switches   : {ctx_switches['total_app_switches']}",
        f"│  Rate                 : {ctx_switches['switches_per_hour']} switches/hr",
        "│  Top transitions:",
    ]
    for pair in ctx_switches["top_app_pairs"][:6]:
        lines.append(f"│    {pair['from']:20s} → {pair['to']:20s}  ({pair['count']}×)")
    lines.append("└────────────────────────────────────────────────────────────")

    lines += [
        "",
        "┌─ TOP APPLICATIONS BY TIME ─────────────────────────────────",
    ]
    top_apps = _top_apps(entries)
    if not top_apps:
        lines.append("│  No application time data available.")
    else:
        max_mins = max(1.0, top_apps[0][1])
        for app, mins in top_apps:
            bar = "█" * int((mins / max_mins) * 30)
            lines.append(f"│  {app:22s} {mins:7.1f} min  {bar}")
    lines.append("└────────────────────────────────────────────────────────────")

    lines += [
        "",
        f"┌─ RAPID SWITCHING  ({len(rapid)} events) ──────────────────────────",
    ]
    for ev in rapid[:8]:
        lines.append(
            f"│  {ev['start']}  {ev['switches']} switches  "
            f"apps: {', '.join(ev['apps'][:4])}"
        )
    lines.append("└────────────────────────────────────────────────────────────")

    lines += [
        "",
        f"┌─ THE RABBIT HOLE  ({len(holes)} sessions) ─────────────────────────",
    ]
    for h in holes[:5]:
        lines.append(f"│  {h['start']}  {h['tab_changes']} changes · {h['unique_tabs']} unique tabs")
        lines.append(f"│    triggered by: {h['trigger_title'][:80]}")
    lines.append("└────────────────────────────────────────────────────────────")

    lines += [
        "",
        "┌─ THE FATIGUE PATTERN ───────────────────────────────────────",
        f"│  Morning   (06-12) : {fatigue['morning_rate']} switches/hr",
        f"│  Afternoon (12-18) : {fatigue['afternoon_rate']} switches/hr",
        f"│  Evening   (18-22) : {fatigue['evening_rate']} switches/hr",
        f"│  PM vs AM increase : {fatigue['pm_vs_am_pct']}%",
        f"│  Fatigue detected  : {'⚠ YES' if fatigue['fatigue_detected'] else 'NO'}",
        "└────────────────────────────────────────────────────────────",
        "",
        "┌─ THE VISUAL BAIT ──────────────────────────────────────────",
    ]
    if visual_bait:
        for site, data in sorted(visual_bait.items(), key=lambda x: x[1]["total_minutes"], reverse=True):
            lines.append(
                f"│  {site:15s}  {data['visits']:3d} visits  "
                f"{data['total_minutes']:6.1f} min  "
                f"{data['max_post_trigger_switches']} post-trigger switches"
            )
    else:
        lines.append("│  No social / entertainment activity detected.")
    lines.append("└────────────────────────────────────────────────────────────")
    return "\n".join(lines)


def build_ai_prompt(report: str, days: int = 7) -> str:
    return f"""You are a productivity analyst specialising in attention and deep work.

Below is a structured activity report from a user's desktop for the past {days} day{'s' if days != 1 else ''}.
Analyse it and respond with:

1. OVERALL ASSESSMENT — two sentences on their current focus pattern.
2. TOP 3 PROBLEMS — the three most harmful patterns, with the specific numbers from the data.
3. FOCUS TRAPS ACTIVE — which of the following apply and how severely:
   • The Rabbit Hole (one search → 20+ unrelated tabs)
   • The Fatigue Pattern (PM switching rate > AM)
   • The Visual Bait (social/video triggers extended distraction)
4. FOCUS SCORE BREAKDOWN — explain what is dragging their score down most.
5. 3-STEP IMPROVEMENT PLAN — concrete, implementable actions tied to the data.

Be direct, data-driven, and specific. Reference actual numbers.

─── REPORT ───────────────────────────────────────────────────────────────────
{report}
──────────────────────────────────────────────────────────────────────────────
"""


# ════════════════════════════════════════════════════════════════════════════════
#  Ollama integration
# ════════════════════════════════════════════════════════════════════════════════

def query_llm(
    prompt: str,
    provider: str,
    model: str,
    api_key: str,
    base_url: str = "",
) -> str | None:
    provider = provider.lower().strip()

    try:
        if provider == "ollama":
            payload = json.dumps({
                "model":   model,
                "prompt":  prompt,
                "stream":  False,
                "options": {"num_ctx": 4096},
            }).encode("utf-8")
            req = urllib.request.Request(
                base_url or OLLAMA_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read()).get("response", "").strip()

        if provider == "openai":
            if not api_key:
                return None
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            }).encode("utf-8")
            req = urllib.request.Request(
                base_url or OPENAI_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"].strip()

        if provider == "anthropic":
            if not api_key:
                return None
            payload = json.dumps({
                "model": model,
                "max_tokens": 1200,
                "messages": [{"role": "user", "content": prompt}],
            }).encode("utf-8")
            req = urllib.request.Request(
                base_url or ANTHROPIC_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                parts = data.get("content", [])
                if parts and isinstance(parts, list):
                    return parts[0].get("text", "").strip()
                return None

        if provider == "gemini":
            if not api_key:
                return None
            url = (base_url or GEMINI_URL_TMPL).format(model=model, key=api_key)
            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    except (urllib.error.URLError, json.JSONDecodeError, OSError, KeyError, IndexError, TypeError):
        return None

    return None


# ════════════════════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════════════════════

def _days_value(raw: str) -> int:
    value = int(raw)
    if not 1 <= value <= 365:
        raise argparse.ArgumentTypeError("days must be between 1 and 365")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Flowtrack AI Pattern Analyzer")
    parser.add_argument("--days",   type=_days_value, default=7, help="Days of data to analyse (default: 7)")
    parser.add_argument("--no-ai",  action="store_true", help="Skip Ollama query")
    parser.add_argument("--provider", default="ollama", choices=["ollama", "openai", "anthropic", "gemini"], help="LLM provider")
    parser.add_argument("--model",  default="", help="Model name (defaults by provider)")
    parser.add_argument("--api-key", default="", help="API key for cloud providers")
    parser.add_argument("--base-url", default="", help="Optional custom API base URL")
    args = parser.parse_args()

    print("Flowtrack — AI Pattern Analyzer")
    print("=" * 60)

    entries = load_entries(args.days)
    if not entries:
        print(f"No log data found in {LOG_DIR}")
        print(f"Ensure tracker.py is running and check {BASE_DIR / 'tracker.log'} for errors.")
        sys.exit(0)

    print(f"Loaded {len(entries)} events from the last {args.days} day(s).\n")

    rapid       = detect_rapid_switching(entries)
    holes       = detect_rabbit_holes(entries)
    fatigue     = detect_fatigue_pattern(entries)
    visual_bait = detect_visual_bait(entries)
    ctx         = analyze_context_switches(entries)
    focus       = calculate_focus_score(entries)

    report = generate_text_report(entries, rapid, holes, fatigue, visual_bait, ctx, focus, days=args.days)
    print(report)

    today = datetime.date.today().isoformat()

    # Save raw report
    report_path = REPORT_DIR / f"analysis_{today}.txt"
    _write_private_text(report_path, report)
    print(f"\nReport saved → {report_path}")

    if args.no_ai:
        return

    # Build prompt
    ai_prompt    = build_ai_prompt(report, days=args.days)
    prompt_path  = REPORT_DIR / f"ai_prompt_{today}.txt"
    _write_private_text(prompt_path, ai_prompt)

    key = args.api_key.strip()
    if not key:
        env_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
        }
        if args.provider in env_map:
            key = os.environ.get(env_map[args.provider], "")

    model = args.model.strip() or DEFAULT_MODELS[args.provider]
    print(f"\nQuerying {args.provider} ({model}) with timeout 120 s …")
    ai_response = query_llm(
        ai_prompt,
        provider=args.provider,
        model=model,
        api_key=key,
        base_url=args.base_url,
    )

    if ai_response:
        print("\n" + "=" * 60)
        print("AI ANALYSIS")
        print("=" * 60)
        print(ai_response)
        ai_path = REPORT_DIR / f"ai_analysis_{today}.txt"
        _write_private_text(ai_path, ai_response)
        print(f"\nAI report saved → {ai_path}")
    else:
        print("\nLLM unavailable or request failed.")
        print("Paste the prompt below into any web AI (ChatGPT, Claude, Gemini, etc.)")
        print(f"\nPrompt also saved → {prompt_path}\n")
        print("─" * 60)
        print(ai_prompt)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
