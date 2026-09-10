#!/usr/bin/env python3
"""
Benchmark vision extraction of Israeli (Hebrew, RTL) university timetables.

Answers one question with a number instead of an opinion:
  does a vision model read a real timetable screenshot correctly,
  and is the cheaper model as accurate as the expensive one?

For every (image, model) it runs:
  pass A - orientation probe: read ONLY the header row, left-to-right, and say
           whether the table is RTL. Establishing direction separately is what
           stops the whole week from being mirrored silently.
  pass B - full extraction, repeated N times, each meeting carrying the day and
           time labels *as the model claims it saw them* so Python can check the
           model against itself.

Then it reports:
  - validation failures (bad day name, unparseable time, label/index mismatch...)
  - self-consistency  : run 1 vs run 2 of the same model
  - cross-model diff  : model A vs model B, slot by slot
  - accuracy vs gold  : precision / recall, if you pass --gold
  - token cost per import, per model

Usage
-----
    pip install anthropic
    export ANTHROPIC_API_KEY=...          # or: ant auth login

    # smoke test on your own screenshot
    python tools/timetable_vision_bench.py mysched.png

    # the actual comparison
    python tools/timetable_vision_bench.py mysched.png --repeats 2

    # turn the best run into a gold file, hand-fix it, then score against it
    python tools/timetable_vision_bench.py mysched.png --write-gold gold.json
    python tools/timetable_vision_bench.py mysched.png --gold gold.json

Nothing here touches the FastAPI service or its requirements.txt.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import anthropic

# Hebrew course names would crash a cp1252 Windows console.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - older interpreters
    pass


# --------------------------------------------------------------------------
# pricing - USD per 1M tokens, first-party Anthropic API rates
# --------------------------------------------------------------------------

PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

DEFAULT_MODELS = ["claude-opus-5", "claude-sonnet-5"]


# --------------------------------------------------------------------------
# Hebrew day handling
# --------------------------------------------------------------------------

DAY_NAMES = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]
DAY_LETTERS = ["א", "ב", "ג", "ד", "ה", "ו", "ש"]
DAY_EN = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

_NIKUD = re.compile(r"[֑-ׇ]")
_PUNCT = re.compile(r"[\"'׳״.,\-–—()\[\]]")


def _strip_heb(s: str) -> str:
    s = _NIKUD.sub("", s or "")
    s = _PUNCT.sub(" ", s)
    return " ".join(s.split())


def day_to_index(label: str) -> int | None:
    """'יום ג׳' / 'שלישי' / 'ג' / 'Tuesday' -> 2. None if unrecognisable."""
    if not label:
        return None
    s = _strip_heb(label)
    s = re.sub(r"^יום\s+", "", s).strip()

    for i, name in enumerate(DAY_NAMES):
        if name in s:
            return i
    for i, en in enumerate(DAY_EN):
        if s.lower().startswith(en.lower()):
            return i
    if s in DAY_LETTERS:
        return DAY_LETTERS.index(s)
    return None


_TIME = re.compile(r"^(\d{1,2})\s*[:.״]?\s*(\d{2})?$")


def time_to_minutes(label: str) -> int | None:
    """'08:00' / '8.00' / '8' -> minutes since midnight. None if unparseable."""
    if not label:
        return None
    s = _strip_heb(str(label)).replace(" ", "")
    m = _TIME.match(s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh * 60 + mm


def hhmm(minutes: int | None) -> str:
    if minutes is None:
        return "??:??"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def normalize_course(name: str) -> str:
    return _strip_heb(name).lower()


# --------------------------------------------------------------------------
# response schemas - hand-written so nothing depends on Pydantic -> JSON Schema
# --------------------------------------------------------------------------

HEADER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "columns_left_to_right": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every column header, in the order they appear on screen from LEFT to RIGHT.",
        },
        "direction": {
            "type": "string",
            "enum": ["rtl", "ltr", "unknown"],
            "description": "rtl if the first day of the week is the RIGHTMOST column.",
        },
        "time_axis": {
            "type": "string",
            "enum": ["rows", "columns", "unknown"],
            "description": "Where the hours live.",
        },
        "notes": {"type": "string"},
    },
    "required": ["columns_left_to_right", "direction", "time_axis", "notes"],
    "additionalProperties": False,
}

TIMETABLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "meetings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "course_name": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "description": "הרצאה / תרגול / מעבדה / סמינר, or empty if not shown.",
                    },
                    "day_label_as_seen": {
                        "type": "string",
                        "description": "The column header text EXACTLY as printed. Do not translate or normalise.",
                    },
                    "time_label_as_seen": {
                        "type": "string",
                        "description": (
                            "The hour labels this block spans, as printed in the hour column, "
                            "as 'FIRST-LAST' e.g. '08:30-10:45'. Empty if the block carries no "
                            "readable time text of its own."
                        ),
                    },
                    "weekly_hours_as_seen": {
                        "type": "string",
                        "description": (
                            "The ש\"ש value printed in the cell, digits only, e.g. '1.5'. "
                            "Empty string if the cell does not show one. Never compute it."
                        ),
                    },
                    "day_index": {
                        "type": "integer",
                        "description": "0=Sunday .. 6=Saturday.",
                    },
                    "start": {"type": "string", "description": "HH:MM, 24h."},
                    "end": {"type": "string", "description": "HH:MM, 24h."},
                    "location": {"type": "string"},
                    "instructor": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
                "required": [
                    "course_name",
                    "kind",
                    "day_label_as_seen",
                    "time_label_as_seen",
                    "weekly_hours_as_seen",
                    "day_index",
                    "start",
                    "end",
                    "location",
                    "instructor",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "unreadable_regions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Parts of the image you could NOT read. Never invent a meeting to fill a gap.",
        },
    },
    "required": ["meetings", "unreadable_regions"],
    "additionalProperties": False,
}


HEADER_PROMPT = """This image is a university class timetable from an Israeli institution.

Do NOT extract any classes yet. Read ONLY the header row.

List every column header in the order they physically appear on screen, scanning
from the LEFT edge to the RIGHT edge. Report the text exactly as printed.

Then decide the direction:
- "rtl"  if the week starts on the RIGHT (the rightmost day column is יום ראשון)
- "ltr"  if the week starts on the LEFT
Hebrew tables are usually rtl, but decide from what you actually see, not from the language.

Also say whether the hours run down the rows or across the columns."""


def extraction_prompt(header: dict[str, Any]) -> str:
    cols = " | ".join(header.get("columns_left_to_right") or []) or "(unknown)"
    return f"""Extract every class meeting from this Israeli university timetable.

Established already, by reading the header row of this same image:
  columns, left to right : {cols}
  table direction        : {header.get('direction', 'unknown')}
  hours run along        : {header.get('time_axis', 'unknown')}

Use that. If direction is "rtl", the RIGHTMOST day column is the first day of
the week - do not assume the leftmost column is Sunday. In these documents the
hour column is often on the RIGHT edge, not the left; read the hours from
wherever the header probe found them.

PRIVACY - read this first:
These documents usually open with a personal block holding the student's name,
national ID, home address, phone and email. Do NOT extract, transcribe, echo or
summarise any of it, in any field. Skip that block entirely and start at the
weekly grid.

Rules:
1. For every meeting, copy `day_label_as_seen`, `time_label_as_seen` and
   `weekly_hours_as_seen` verbatim from the image. These are your evidence; they
   are checked in code against your day_index and start/end. Never adjust the
   evidence to agree with your answer - if they disagree, report both as seen.
2. A block spanning several hour-rows is ONE meeting, not several one-hour ones.
   Read its start and end from the GRID LINES the block's top and bottom edges
   touch, not from where the text happens to sit inside it - these blocks carry
   a lot of internal blank space, and the text is usually not flush with either
   edge.
3. Empty day columns are normal and common - many students have classes on only
   two or three days. If a column has no blocks, it has no meetings. Never fill
   a quiet day with a plausible class.
4. Israeli week: 0=ראשון, 1=שני, 2=שלישי, 3=רביעי, 4=חמישי, 5=שישי, 6=שבת.
5. `course_name` is the course title only. Delivery-mode notes in parentheses
   (קמפוס, מקוון, לסירוגין, היברידי ...) are NOT part of the name - drop them.
   A trailing שיעור / תרגול / מעבדה / סמינר goes in `kind`, and the base
   `course_name` must stay byte-identical to the lecture's, so the two can be
   merged into one course later.
6. Leave a field as an empty string when the image does not show it. Never guess
   a room, an instructor, a time or a ש"ש value.
7. If part of the image is unreadable, list it in unreadable_regions and omit
   those meetings. An honest gap beats an invented class.
8. Ignore legends, footnotes, totals and any table that is not the weekly grid."""


# --------------------------------------------------------------------------
# API calls
# --------------------------------------------------------------------------


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )

    def cost(self, model: str) -> float:
        rate_in, rate_out = PRICES.get(model, (0.0, 0.0))
        return (self.input_tokens * rate_in + self.output_tokens * rate_out) / 1_000_000


def load_image(path: Path) -> dict[str, Any]:
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    if media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        raise SystemExit(f"unsupported image type for {path.name}: {media_type}")
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def call_json(
    client: anthropic.Anthropic,
    model: str,
    image_block: dict[str, Any],
    prompt: str,
    schema: dict[str, Any],
    effort: str,
) -> tuple[dict[str, Any], Usage]:
    """One vision request constrained to `schema`. Returns (parsed, usage)."""
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 16000,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        "messages": [
            {"role": "user", "content": [image_block, {"type": "text", "text": prompt}]}
        ],
    }

    try:
        response = client.messages.create(**kwargs)
    except anthropic.NotFoundError:
        raise SystemExit(f"model not available to this account: {model}")
    except anthropic.RateLimitError:
        raise SystemExit("rate limited - wait a moment and re-run")
    except anthropic.APIStatusError as exc:
        raise SystemExit(f"API error {exc.status_code} on {model}: {exc.message}")
    except anthropic.APIConnectionError as exc:
        raise SystemExit(f"network error talking to the API: {exc}")

    usage = Usage(response.usage.input_tokens, response.usage.output_tokens)

    if response.stop_reason == "refusal":
        raise SystemExit(f"{model} declined the request (stop_reason=refusal)")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise SystemExit(f"{model} returned no text block (stop_reason={response.stop_reason})")
    return json.loads(text), usage


# --------------------------------------------------------------------------
# validation - pure Python, no API
# --------------------------------------------------------------------------


@dataclass
class Meeting:
    course: str
    kind: str
    day_index: int | None
    start: int | None
    end: int | None
    day_label: str
    time_label: str
    weekly_hours: str
    location: str
    confidence: str
    issues: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def slot(self) -> tuple[int | None, int | None, int | None]:
        return (self.day_index, self.start, self.end)

    def describe(self) -> str:
        day = DAY_EN[self.day_index] if self.day_index is not None and 0 <= self.day_index <= 6 else "???"
        return f"{day} {hhmm(self.start)}-{hhmm(self.end)}  {self.course or '(no name)'}"


def to_meetings(payload: dict[str, Any], header: dict[str, Any]) -> list[Meeting]:
    header_cols = {_strip_heb(c) for c in (header.get("columns_left_to_right") or [])}
    out: list[Meeting] = []

    for raw in payload.get("meetings", []):
        day_label = raw.get("day_label_as_seen", "")
        start = time_to_minutes(raw.get("start", ""))
        end = time_to_minutes(raw.get("end", ""))
        claimed = raw.get("day_index")
        m = Meeting(
            course=raw.get("course_name", "").strip(),
            kind=raw.get("kind", "").strip(),
            day_index=claimed if isinstance(claimed, int) else None,
            start=start,
            end=end,
            day_label=day_label,
            time_label=raw.get("time_label_as_seen", ""),
            weekly_hours=str(raw.get("weekly_hours_as_seen", "")).strip(),
            location=raw.get("location", "").strip(),
            confidence=raw.get("confidence", ""),
        )

        # the checks that catch the silent failures
        derived = day_to_index(day_label)
        if derived is None:
            m.issues.append(f"day label not recognised: {day_label!r}")
        elif m.day_index is None:
            m.issues.append("no day_index returned")
        elif derived != m.day_index:
            m.issues.append(
                f"day mismatch: label {day_label!r} means {DAY_EN[derived]} "
                f"but day_index={m.day_index} ({DAY_EN[m.day_index] if 0 <= m.day_index <= 6 else '?'})"
            )

        if header_cols and derived is not None and _strip_heb(day_label) not in header_cols:
            m.issues.append(f"day label {day_label!r} was not among the header columns")

        if start is None:
            m.issues.append(f"unparseable start: {raw.get('start')!r}")
        if end is None:
            m.issues.append(f"unparseable end: {raw.get('end')!r}")
        if start is not None and end is not None:
            if end <= start:
                m.issues.append(f"end {hhmm(end)} not after start {hhmm(start)}")
            elif not (30 <= end - start <= 8 * 60):
                m.issues.append(f"implausible duration: {(end - start) / 60:.1f}h")
        if start is not None and not (6 * 60 <= start <= 23 * 60):
            m.issues.append(f"start outside teaching hours: {hhmm(start)}")
        if not m.course:
            m.issues.append("empty course name")

        # ש"ש printed in the cell is an independent check on the block geometry:
        # one academic hour is 45 minutes, and a slot adds breaks on top.
        if m.weekly_hours and start is not None and end is not None:
            try:
                ss = float(m.weekly_hours)
            except ValueError:
                m.soft.append(f'unparseable ש"ש: {m.weekly_hours!r}')
            else:
                duration = end - start
                low, high = ss * 40, ss * 45 + 60
                if not (low <= duration <= high):
                    m.soft.append(
                        f'ש"ש={ss:g} implies roughly {low / 60:.1f}-{high / 60:.1f}h '
                        f"but the block was read as {duration / 60:.1f}h"
                    )

        out.append(m)

    # same slot twice = the grid was misread
    seen: dict[tuple, str] = {}
    for m in out:
        key = (m.day_index, m.start)
        if key in seen and seen[key] != normalize_course(m.course):
            m.issues.append(f"slot collision with {seen[key]!r}")
        seen[key] = normalize_course(m.course)

    return out


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------


@dataclass
class Diff:
    same: list[tuple[Meeting, Meeting]] = field(default_factory=list)
    name_differs: list[tuple[Meeting, Meeting]] = field(default_factory=list)
    only_a: list[Meeting] = field(default_factory=list)
    only_b: list[Meeting] = field(default_factory=list)

    @property
    def agreement(self) -> float:
        total = len(self.same) + len(self.name_differs) + len(self.only_a) + len(self.only_b)
        return len(self.same) / total if total else 1.0


def diff_meetings(a: list[Meeting], b: list[Meeting], name_threshold: float = 0.85) -> Diff:
    d = Diff()
    unmatched_b = list(b)

    for ma in a:
        match = next((mb for mb in unmatched_b if mb.slot() == ma.slot()), None)
        if match is None:
            d.only_a.append(ma)
            continue
        unmatched_b.remove(match)
        ratio = SequenceMatcher(
            None, normalize_course(ma.course), normalize_course(match.course)
        ).ratio()
        (d.same if ratio >= name_threshold else d.name_differs).append((ma, match))

    d.only_b.extend(unmatched_b)
    return d


def score_against_gold(got: list[Meeting], gold: list[Meeting]) -> dict[str, float]:
    d = diff_meetings(gold, got)
    tp = len(d.same)
    fn = len(d.only_a) + len(d.name_differs)
    fp = len(d.only_b) + len(d.name_differs)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


@dataclass
class Run:
    model: str
    index: int
    header: dict[str, Any]
    payload: dict[str, Any]
    meetings: list[Meeting]
    usage: Usage


def run_model(
    client: anthropic.Anthropic,
    model: str,
    image_block: dict[str, Any],
    repeats: int,
    effort: str,
) -> list[Run]:
    header, header_usage = call_json(
        client, model, image_block, HEADER_PROMPT, HEADER_SCHEMA, effort
    )
    prompt = extraction_prompt(header)

    runs: list[Run] = []
    for i in range(repeats):
        payload, usage = call_json(
            client, model, image_block, prompt, TIMETABLE_SCHEMA, effort
        )
        total = usage + (header_usage if i == 0 else Usage())
        runs.append(Run(model, i + 1, header, payload, to_meetings(payload, header), total))
    return runs


def meetings_from_gold(path: Path) -> list[Meeting]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return to_meetings(data, data.get("header", {}))


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def report(image: Path, runs_by_model: dict[str, list[Run]], gold: list[Meeting] | None) -> None:
    print(f"\n{'=' * 78}\nimage: {image.name}\n{'=' * 78}")

    print("\norientation probe")
    for model, runs in runs_by_model.items():
        h = runs[0].header
        cols = " | ".join(h.get("columns_left_to_right") or [])
        print(f"  {model:<20} direction={h.get('direction','?'):<8} hours along {h.get('time_axis','?')}")
        print(f"  {'':<20} columns L->R: {cols}")
    directions = {tuple([m, runs[0].header.get("direction")]) for m, runs in runs_by_model.items()}
    if len({d[1] for d in directions}) > 1:
        print("  !! models disagree on table direction - the whole week may be mirrored in one of them")

    print(f"\n{'model':<20}{'run':<6}{'mtgs':<7}{'valid':<8}{'issues':<9}"
          f"{'in':<9}{'out':<9}{'cost':>9}")
    for model, runs in runs_by_model.items():
        for r in runs:
            bad = sum(1 for m in r.meetings if not m.ok)
            print(
                f"{model:<20}{r.index:<6}{len(r.meetings):<7}"
                f"{len(r.meetings) - bad:<8}{bad:<9}"
                f"{r.usage.input_tokens:<9}{r.usage.output_tokens:<9}"
                f"${r.usage.cost(model):>8.4f}"
            )

    for model, runs in runs_by_model.items():
        problems = [(r, m) for r in runs for m in r.meetings if not m.ok]
        if problems:
            print(f"\nvalidation failures - {model}")
            for r, m in problems:
                print(f"  run{r.index}  {m.describe()}")
                for issue in m.issues:
                    print(f"          - {issue}")
        soft = [(r, m) for r in runs for m in r.meetings if m.soft]
        if soft:
            print(f"\ngeometry warnings (ש\"ש vs block height) - {model}")
            for r, m in soft:
                print(f"  run{r.index}  {m.describe()}")
                for note in m.soft:
                    print(f"          - {note}")

        unread = [u for r in runs for u in r.payload.get("unreadable_regions", [])]
        if unread:
            print(f"\nreported as unreadable - {model}")
            for u in dict.fromkeys(unread):
                print(f"  - {u}")

    for model, runs in runs_by_model.items():
        if len(runs) < 2:
            continue
        d = diff_meetings(runs[0].meetings, runs[1].meetings)
        print(f"\nself-consistency - {model}  run1 vs run2: "
              f"{d.agreement:.0%} ({len(d.same)} identical, "
              f"{len(d.name_differs)} same slot different name, "
              f"{len(d.only_a)} only in run1, {len(d.only_b)} only in run2)")
        for m in d.only_a:
            print(f"    only run1: {m.describe()}")
        for m in d.only_b:
            print(f"    only run2: {m.describe()}")

    models = list(runs_by_model)
    for i, a in enumerate(models):
        for b in models[i + 1:]:
            ma, mb = runs_by_model[a][0].meetings, runs_by_model[b][0].meetings
            d = diff_meetings(ma, mb)
            print(f"\ncross-model - {a} vs {b}: {d.agreement:.0%} agreement "
                  f"({len(ma)} vs {len(mb)} meetings)")
            for x, y in d.name_differs:
                print(f"    same slot, different name: {x.describe()}  ||  {y.course}")
            for m in d.only_a:
                print(f"    only {a}: {m.describe()}")
            for m in d.only_b:
                print(f"    only {b}: {m.describe()}")

    if gold:
        print(f"\naccuracy vs gold ({len(gold)} meetings)")
        print(f"  {'model':<20}{'precision':<12}{'recall':<10}{'f1':<8}{'tp/fp/fn'}")
        for model, runs in runs_by_model.items():
            s = score_against_gold(runs[0].meetings, gold)
            print(f"  {model:<20}{s['precision']:<12.0%}{s['recall']:<10.0%}"
                  f"{s['f1']:<8.2f}{s['tp']:.0f}/{s['fp']:.0f}/{s['fn']:.0f}")

    print("\ncost per import (all passes, one run)")
    for model, runs in runs_by_model.items():
        print(f"  {model:<20}${runs[0].usage.cost(model):.4f}"
              f"   x2 for the two-pass diff = ${runs[0].usage.cost(model) * 2:.4f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", type=Path, help="timetable screenshots (png/jpg/webp)")
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS,
                    help=f"default: {' '.join(DEFAULT_MODELS)}")
    ap.add_argument("--repeats", type=int, default=2,
                    help="extraction passes per model, for self-consistency (default 2)")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--gold", type=Path, help="hand-corrected JSON to score against")
    ap.add_argument("--write-gold", type=Path,
                    help="dump the first model's first run here as a starting point for a gold file")
    ap.add_argument("--out", type=Path, help="directory for the raw JSON of every run")
    args = ap.parse_args()

    for m in args.models:
        if m not in PRICES:
            print(f"note: no price on file for {m}, cost will show as $0", file=sys.stderr)

    client = anthropic.Anthropic()
    gold = meetings_from_gold(args.gold) if args.gold else None

    for image in args.images:
        if not image.exists():
            raise SystemExit(f"no such file: {image}")
        image_block = load_image(image)

        runs_by_model: dict[str, list[Run]] = {}
        for model in args.models:
            print(f"running {model} on {image.name} ...", file=sys.stderr)
            runs_by_model[model] = run_model(
                client, model, image_block, args.repeats, args.effort
            )

        report(image, runs_by_model, gold)

        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            for model, runs in runs_by_model.items():
                for r in runs:
                    slug = f"{image.stem}.{model}.run{r.index}.json"
                    (args.out / slug).write_text(
                        json.dumps({"header": r.header, **r.payload}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            print(f"\nraw JSON written to {args.out}")

        if args.write_gold:
            first = runs_by_model[args.models[0]][0]
            args.write_gold.write_text(
                json.dumps({"header": first.header, **first.payload}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"gold starting point written to {args.write_gold} - correct it by hand, "
                  f"then re-run with --gold {args.write_gold}")


if __name__ == "__main__":
    main()
