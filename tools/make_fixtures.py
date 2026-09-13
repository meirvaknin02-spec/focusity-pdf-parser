"""Build the anonymised test fixtures from real exported PDFs.

The fixtures in tests/fixtures/ are real institutional exports with every
personal detail removed: student names, ID numbers, addresses, phone
numbers and e-mail addresses are redacted (the text is deleted from the PDF,
not covered), and on the grade sheet every grade and average is replaced by
a made-up number. Table structure, header wording, fonts and glyph order are
untouched -- those quirks are exactly what the tests exist to guard.

This repository is public. Never commit an un-redacted export; run this
script on the originals locally and commit only its output.

    pip install pymupdf        # only needed to run this script
    python tools/make_fixtures.py <dir-with-originals>

The originals are never read from or written to the repository.
"""
import re
import sys
from pathlib import Path

import pdfplumber
import pymupdf

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Whole words as pdfplumber extracts them (glyph order included) -> redacted.
PII = {
    "sapir_timetable.pdf": {"325327773", "איהיל", "ןהד", "8484965", "91", "בגנה", "תלצבח",
                            "c2532777@mail.sapir.ac.il", "0523617141"},
    "sce_exam_schedule.pdf": {":315057026", "םהרבא", "ריאמ", "ןינקוו"},
    "sce_grade_sheet.pdf": {"315057026", "םהרבא", "ריאמ", "ןינקוו", "8725555", "12/10", "ינוקרי",
                            "סומע", "Meirva@ac.sce.ac.il", "0558874076"},
}
SOURCES = {
    "sapir_timetable.pdf": "schedule.pdf",
    "sce_exam_schedule.pdf": "exams.pdf",
    "sce_grade_sheet.pdf": "grades.pdf",
}
# Personal details only ever sit in the header block. Matching whole words
# inside that band keeps the street number "91" from taking room "9102" with it.
HEADER_BAND_BOTTOM = 200

GRADE_X = (110, 126)      # the grade column on the SCE grade sheet
AVERAGE_X = (276, 300)    # the value column of the summary boxes


def fake_grade(g: int) -> str:
    return str(55 + (g * 37) % 41)


def build(src_dir: Path):
    OUT.mkdir(parents=True, exist_ok=True)
    for name, src_name in SOURCES.items():
        src = src_dir / src_name
        doc = pymupdf.open(src)
        with pdfplumber.open(src) as plumber:
            for pno, ppage in enumerate(plumber.pages):
                page = doc[pno]
                for w in ppage.extract_words():
                    t = w["text"]
                    rect = pymupdf.Rect(w["x0"] - 0.5, w["top"] - 0.5, w["x1"] + 0.5, w["bottom"] + 0.5)
                    if t in PII[name] and w["top"] < HEADER_BAND_BOTTOM:
                        page.add_redact_annot(rect)
                    elif name == "sce_grade_sheet.pdf" and GRADE_X[0] <= w["x0"] <= GRADE_X[1] and re.fullmatch(r"\d{2,3}", t):
                        page.add_redact_annot(rect, text=fake_grade(int(t)), fontname="helv", fontsize=7, align=pymupdf.TEXT_ALIGN_RIGHT)
                    elif name == "sce_grade_sheet.pdf" and AVERAGE_X[0] <= w["x0"] <= AVERAGE_X[1] and re.fullmatch(r"\d{2}\.\d{2}", t) and float(t) > 50:
                        page.add_redact_annot(rect, text="70.00", fontname="helv", fontsize=7)
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
        doc.set_metadata({})
        doc.save(OUT / name, garbage=4, deflate=True, clean=True)
        doc.close()
        print("wrote", OUT / name)


if __name__ == "__main__":
    build(Path(sys.argv[1]))
