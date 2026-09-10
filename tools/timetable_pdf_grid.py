#!/usr/bin/env python3
"""
Deterministic extraction of an Israeli weekly timetable from the PDF the college
itself produces. No model, no API, no pixel guessing.

These PDFs are drawn, not scanned: the weekly grid is real vector geometry, so
every fact the importer needs is already in the file.

  - the hour column prints a label per 30-minute row, so a y coordinate maps to
    a clock time by interpolation - exactly, not approximately
  - each day column is a filled header rectangle, and the day name printed in it
    says which weekday that x range is - so RTL never has to be inferred
  - each class is one filled rectangle whose top and bottom edges sit ON the row
    lines, which is the start and end time, merged cells included
  - the text inside a rectangle is that class's name, lecturer, room and codes

That removes the single biggest failure of reading a timetable from an image:
binding a cell to the right day and the right hour. Here it is arithmetic.

Usage:
    python tools/timetable_pdf_grid.py FILE.pdf [FILE.pdf ...] [--json out.json]

Personal details (name, national ID, address, phone, email) sit above the grid
and are never read: extraction starts at the header row and ignores everything
above it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

import pdfplumber

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

DAY_NAMES = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]
DAY_EN = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
HOUR_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

HEBREW = re.compile(r"[֐-׿]")
LTR_RUN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.:/@_-]*")


def unmirror(text: str) -> str:
    """Turn visually-ordered Hebrew back into logical order.

    These PDFs (and the CSV export beside them) store Hebrew the way it looks on
    the page rather than the way it is typed, so 'שישי' arrives as 'ישיש'.
    Reversing the string fixes the Hebrew but would also reverse numbers and
    Latin, which were already laid out left-to-right - so those runs are flipped
    back afterwards. '1.5:ש"ש' -> 'ש"ש:1.5'.
    """
    if not HEBREW.search(text):
        return text
    flipped = text[::-1]
    return LTR_RUN.sub(lambda m: m.group(0)[::-1], flipped)

# tolerance in points when snapping a rectangle edge to an hour row line
SNAP_TOLERANCE = 3.0


@dataclass
class Meeting:
    day_index: int
    day_name: str
    start: str
    end: str
    course_name: str
    kind: str
    lecturer: str
    room: str
    course_code: str
    weekly_hours: str
    credits: str
    printed_time: str
    raw_lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        k = f" ({self.kind})" if self.kind else ""
        return f"{DAY_EN[self.day_index]} {self.start}-{self.end}  {self.course_name}{k}"


def _to_minutes(label: str) -> int | None:
    m = HOUR_RE.match(label.strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _fmt(minutes: float) -> str:
    total = int(round(minutes))
    return f"{total // 60:02d}:{total % 60:02d}"


class Grid:
    """The hour axis and the day axis, read off the page's own drawing."""

    def __init__(self, page):
        words = page.extract_words()

        # --- hour axis ---------------------------------------------------------
        # Only the hour COLUMN counts. Course blocks print their own times too,
        # and mixing those in skews the fit badly - so group every "HH:MM" by the
        # x it was printed at and keep the largest group, which is the column.
        by_x: dict[int, list[tuple[float, int]]] = {}
        for w in words:
            minutes = _to_minutes(w["text"])
            if minutes is not None:
                by_x.setdefault(round(w["x0"] / 6), []).append(
                    ((w["top"] + w["bottom"]) / 2, minutes)
                )
        if not by_x:
            raise ValueError("no HH:MM labels on the page - not a weekly grid")
        ticks = sorted(max(by_x.values(), key=len))
        if len(ticks) < 3:
            raise ValueError("could not find an hour column (need 3+ HH:MM labels)")

        # collapse labels that share a row, then fit y -> minutes
        self.ticks = ticks
        span_y = ticks[-1][0] - ticks[0][0]
        span_m = ticks[-1][1] - ticks[0][1]
        if span_y <= 0 or span_m <= 0:
            raise ValueError("degenerate hour column")
        self.minutes_per_point = span_m / span_y
        self.y0, self.m0 = ticks[0]

        # The label is printed inside its row, so the row's TOP edge - the thing a
        # block's edge actually touches - is about half a step above the label.
        # "About" is not good enough: snap that estimate to the nearest rule the
        # page really draws, so the y -> time map is exact rather than inferred.
        step_y = span_y / (len(ticks) - 1)
        self.row_height = step_y
        estimate = ticks[0][0] - step_y / 2

        rules = sorted({
            round(l["top"], 2) for l in page.lines if abs(l["y0"] - l["y1"]) < 0.6
        })
        near = [y for y in rules if abs(y - estimate) <= step_y / 2]
        self.row_top0 = min(near, key=lambda y: abs(y - estimate)) if near else estimate
        self.snapped_to_rule = bool(near)

        self.grid_top = self.row_top0
        self.grid_bottom = self.row_top0 + step_y * len(ticks)

        # --- day axis: the day names printed in the header band ---------------
        header_y = self.grid_top - step_y
        band = [w for w in words if header_y - step_y <= w["top"] <= self.grid_top]

        # Decide once, from the day names themselves, whether this document
        # stores Hebrew visually or logically - then trust it for the whole page.
        forward = sum(1 for w in band if w["text"].strip() in DAY_NAMES)
        mirrored = sum(1 for w in band if w["text"].strip()[::-1] in DAY_NAMES)
        self.visual_order = mirrored > forward

        self.columns: list[tuple[float, float, int]] = []
        for w in band:
            text = self.fix(w["text"]).strip()
            if text in DAY_NAMES:
                idx = DAY_NAMES.index(text)
                centre = (w["x0"] + w["x1"]) / 2
                self.columns.append((centre, centre, idx))
        if not self.columns:
            raise ValueError("no Hebrew day names found in the header band")

        # widen each day name into the column it labels, using the midpoints
        # between neighbouring day names as the boundaries
        self.columns.sort()
        bounds: list[tuple[float, float, int]] = []
        for i, (c, _, idx) in enumerate(self.columns):
            left = (self.columns[i - 1][0] + c) / 2 if i else c - 60
            right = (self.columns[i + 1][0] + c) / 2 if i + 1 < len(self.columns) else c + 60
            bounds.append((left, right, idx))
        self.columns = bounds

        centres = [c for c, _, _ in zip([b[0] for b in bounds], bounds, bounds)]
        self.rtl = (
            len(bounds) > 1
            and bounds[0][2] > bounds[-1][2]  # leftmost column is a LATER weekday
        )

    def fix(self, text: str) -> str:
        return unmirror(text) if self.visual_order else text

    def time_at(self, y: float) -> tuple[str, bool]:
        """Clock time at a y coordinate, and whether it landed on a row line."""
        # Snap to 5 minutes, not to a whole row. One college draws its blocks on
        # the 30-minute row lines; another puts them on half-rows, i.e. quarter
        # past and quarter to. Rounding those to the nearest row silently moves a
        # class by 15 minutes, so let the drawing say where the edge is.
        minutes = self.m0 + (y - self.row_top0) * self.minutes_per_point
        snapped = round(minutes / 5) * 5
        on_line = abs(minutes - snapped) / self.minutes_per_point <= SNAP_TOLERANCE
        return _fmt(snapped), on_line

    def day_at(self, x0: float, x1: float) -> int | None:
        centre = (x0 + x1) / 2
        for left, right, idx in self.columns:
            if left <= centre <= right:
                return idx
        return None


def _classify(lines: list[str]) -> dict[str, str]:
    """Split a block's text lines into the fields the app needs."""
    out = {
        "course_name": "", "kind": "", "lecturer": "", "room": "",
        "course_code": "", "weekly_hours": "", "credits": "", "printed_time": "",
    }
    leftovers: list[str] = []

    for line in lines:
        s = line.strip()
        if not s:
            continue
        if m := re.match(r'^ש"ש\s*:\s*([\d.]+)$', s):
            out["weekly_hours"] = m.group(1); continue
        if m := re.match(r'^(?:נ"ז|ל"ז)\s*:\s*([\d.]+)$', s):
            out["credits"] = m.group(1); continue
        if m := re.match(r"^([012]?\d:[0-5]\d)\s*-\s*([012]?\d:[0-5]\d)$", s):
            out["printed_time"] = f"{m.group(1)}-{m.group(2)}"; continue
        if re.match(r"^\d{6,}-\d+$", s):
            out["course_code"] = s; continue
        if re.match(r"^(?:ד\"ר|פרופ'|מר|גב'|הרב)\s", s):
            out["lecturer"] = s; continue
        if re.search(r"(zoom|לגסי|ספרא|בניין|חדר|מעבדה\s*\d)", s, re.I):
            out["room"] = s; continue
        leftovers.append(s)

    if leftovers:
        name = leftovers[0]
        # a trailing "-ת" / "- ת" marks a tutorial, "-מ" a lab/workshop
        if m := re.match(r"^(.*?)\s*-\s*(ת|מ|מע)$", name):
            name, suffix = m.group(1).strip(), m.group(2)
            out["kind"] = {"ת": "תרגול", "מ": "מעבדה", "מע": "מעבדה"}[suffix]
        out["course_name"] = name
        if len(leftovers) > 1 and not out["lecturer"]:
            out["lecturer"] = leftovers[1]
    return out


def extract(path: Path) -> tuple[list[Meeting], dict]:
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        grid = Grid(page)
        words = page.extract_words()

        blocks: list[Meeting] = []
        for rect in page.rects:
            top, bottom = rect["top"], rect["bottom"]
            x0, x1 = rect["x0"], rect["x1"]
            if bottom - top < grid.row_height * 1.5:
                continue                                   # single-row: an hour cell
            if top < grid.grid_top - 1 or bottom > grid.grid_bottom + 1:
                continue                                   # outside the weekly grid
            day = grid.day_at(x0, x1)
            if day is None:
                continue                                   # the hour column itself

            start, s_ok = grid.time_at(top)
            end, e_ok = grid.time_at(bottom)

            inside = [
                w for w in words
                if x0 - 1 <= w["x0"] and w["x1"] <= x1 + 1 and top - 1 <= w["top"] and w["bottom"] <= bottom + 1
            ]
            rows: dict[int, list] = {}
            for w in inside:
                rows.setdefault(round(w["top"] / 3), []).append(w)
            lines = [
                " ".join(grid.fix(w["text"]) for w in sorted(ws, key=lambda w: -w["x0"]))
                for _, ws in sorted(rows.items())
            ]

            fields = _classify(lines)
            m = Meeting(
                day_index=day, day_name=DAY_NAMES[day], start=start, end=end,
                raw_lines=lines, **fields,
            )
            if not s_ok:
                m.warnings.append("top edge does not sit on a row line")
            if not e_ok:
                m.warnings.append("bottom edge does not sit on a row line")
            if not m.course_name:
                m.warnings.append("no course name found inside the block")
            if m.printed_time and m.printed_time != f"{start}-{end}":
                m.warnings.append(
                    f"geometry says {start}-{end} but the block prints {m.printed_time}"
                )
            blocks.append(m)

    blocks.sort(key=lambda m: (m.day_index, m.start))
    meta = {
        "source": path.name,
        "direction": "rtl" if grid.rtl else "ltr",
        "day_columns": [
            {"day": DAY_NAMES[i], "x": [round(l, 1), round(r, 1)]} for l, r, i in grid.columns
        ],
        "row_height_pt": round(grid.row_height, 2),
        "minutes_per_row": round(grid.row_height * grid.minutes_per_point),
        "hour_labels": len(grid.ticks),
    }
    return blocks, meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--json", type=Path, help="write the meetings to this file")
    ap.add_argument("--raw", action="store_true", help="also print each block's raw text lines")
    args = ap.parse_args()

    everything = {}
    for path in args.pdfs:
        try:
            meetings, meta = extract(path)
        except ValueError as exc:
            print(f"\n{'=' * 76}\n{path.name}\n{'=' * 76}")
            print(f"  skipped - this is not a weekly grid ({exc})")
            print("  the college also exports a 'רשימה לסמסטר זה' list layout;")
            print("  that one needs a different reader, not this grid geometry.")
            continue
        everything[path.name] = {"meta": meta, "meetings": [asdict(m) for m in meetings]}

        print(f"\n{'=' * 76}\n{path.name}\n{'=' * 76}")
        print(f"direction {meta['direction']}   rows {meta['row_height_pt']}pt "
              f"= {meta['minutes_per_row']} min   hour labels {meta['hour_labels']}")
        print("columns L->R: " + " | ".join(c["day"] for c in meta["day_columns"]))
        print(f"\n{len(meetings)} meetings\n")
        print(f"  {'day':<5}{'time':<14}{'course':<34}{'kind':<8}{'room':<16}{'ש\"ש':<6}{'נ\"ז'}")
        for m in meetings:
            print(f"  {DAY_EN[m.day_index]:<5}{m.start + '-' + m.end:<14}"
                  f"{m.course_name[:33]:<34}{m.kind:<8}{m.room[:15]:<16}"
                  f"{m.weekly_hours:<6}{m.credits}")
            if args.raw:
                for line in m.raw_lines:
                    print(f"        | {line}")
        warned = [m for m in meetings if m.warnings]
        if warned:
            print("\n  warnings")
            for m in warned:
                print(f"    {m.describe()}")
                for w in m.warnings:
                    print(f"      - {w}")
        else:
            print("\n  every block edge landed exactly on a row line; no warnings")

    if args.json:
        args.json.write_text(json.dumps(everything, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten to {args.json}")


if __name__ == "__main__":
    main()
