#!/usr/bin/env python
"""Builds tests/fixtures/partial_tounicode_timetable.pdf.

    pip install pymupdf fonttools     # only needed to run this script
    python tools/make_partial_tounicode_fixture.py

Reproduces the Levinsky-Wingate registration-system timetable without any of
its content: a ruled row-per-class table (יום | משעה | עד שעה | קוד קורס |
שם קורס | ש"ש | מרצה | חדר), Hebrew drawn in visual order the way Word
exports it, and the two font traps that made the real file unreadable:

  - The font is Type0 / Identity-H and its /ToUnicode CMap covers digits,
    punctuation and Latin only. Every Hebrew letter extracts as "(cid:N)"
    unless the parser falls back to the embedded font's own cmap.
  - That cmap gives the nun glyph a second, private-use code point (U+F8FF),
    as Windows' David does. Taking the wrong one drops every נ.

Courses, lecturers and rooms are made up. The font is David Libre (SIL Open
Font License), not the proprietary David the real file embeds.
"""
import io
import re
import sys
from pathlib import Path

import pymupdf
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from main import fix_bidi_line  # noqa: E402  (visual order = the parser's own inverse)

OUT = ROOT / "tests" / "fixtures" / "partial_tounicode_timetable.pdf"
FONT = Path(r"C:/Windows/Fonts/DavidLibre-Regular.ttf")

HEADER = ["יום", "משעה", "עד שעה", "קוד קורס", "שם קורס", 'ש"ש', "מרצה", "חדר"]
ROWS = [
    ["א'", "09:30", "12:00", "11110100", "מבוא לתנועה ונשימה", "2", "ד\"ר כהן נוי", "M.P.E / מעבדה 101"],
    ["א'", "12:15", "14:00", "11120000", "יסודות בכדורסל", "1", "לוין נעמה", "מכון / אולם ב'"],
    ["ג'", "09:30", "11:00", "12130000", "מבוא לסטטיסטיקה", "1", "ד\"ר נחום ניר", "קמפוס צפון / חדר 12"],
    ["ג'", "13:30", "15:00", "12140100", "חינוך ונוער", "1", "ברנע ענבל", "קמפוס צפון / חדר 14"],
    ["ד'", "11:30", "13:00", "13150000", "מבוא לפסיכולוגיה", "1", "ד\"ר אלון ניצן", "קמפוס צפון / חדר 15"],
]
# Right-to-left columns, as printed: יום is the rightmost.
WIDTHS = [30, 42, 42, 70, 170, 28, 90, 120]
LEFT, TOP, ROW_H = 20, 120, 26


def _font_with_private_use_nun() -> bytes:
    font = TTFont(str(FONT))
    nun = font.getBestCmap()[0x05E0]
    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap[0xF8FF] = nun
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


def _visual(text: str) -> str:
    return fix_bidi_line(text) if re.search("[֐-׿]", text) else text


def _strip_hebrew_from_tounicode(doc: pymupdf.Document) -> int:
    """Keep only the CMap entries below U+0590, like the real export's."""
    stripped = 0
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Subtype")[1] != "/Type0":
            continue
        kind, ref = doc.xref_get_key(xref, "ToUnicode")
        if kind != "xref":
            continue
        cmap_xref = int(ref.split()[0])
        src = doc.xref_stream(cmap_xref).decode("latin1")

        def keep(block_match):
            body = block_match.group(2)
            if block_match.group(1) == "bfchar":
                pairs = re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", body)
                pairs = [p for p in pairs if int(p[1][:4], 16) < 0x0590]
                lines = "\n".join(f"<{a}> <{b}>" for a, b in pairs)
                return f"{len(pairs)} beginbfchar\n{lines}\nendbfchar"
            ranges = re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", body)
            ranges = [r for r in ranges if int(r[2][:4], 16) < 0x0590]
            lines = "\n".join(f"<{a}> <{b}> <{c}>" for a, b, c in ranges)
            return f"{len(ranges)} beginbfrange\n{lines}\nendbfrange"

        out = re.sub(r"\d+\s+begin(bfchar|bfrange)(.*?)end\1", keep, src, flags=re.S)
        doc.update_stream(cmap_xref, out.encode("latin1"))
        stripped += 1
    return stripped


def build() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="dl", fontbuffer=_font_with_private_use_nun())

    right = LEFT + sum(WIDTHS)
    page.insert_text((230, 80), _visual("מערכת שעות לדוגמה"), fontname="dl", fontsize=14)

    for r, cells in enumerate([HEADER] + ROWS):
        y = TOP + r * ROW_H
        x = right
        for w, text in zip(WIDTHS, cells):
            x -= w
            page.draw_rect(pymupdf.Rect(x, y, x + w, y + ROW_H), color=(0, 0, 0), width=0.6)
            page.insert_text((x + 3, y + 17), _visual(text), fontname="dl", fontsize=9)

    # No doc.subset_fonts(): MuPDF's subsetter drops the font's cmap table,
    # and the cmap is exactly what the real export still carries.
    if not _strip_hebrew_from_tounicode(doc):
        sys.exit("no Type0 font with a /ToUnicode was written -- the fixture would not reproduce the bug")
    doc.set_metadata({})
    doc.save(OUT, garbage=4, deflate=True)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    build()
