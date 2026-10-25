# Focusity PDF parser

Small rule-based FastAPI service (`main.py`) that turns Israeli university PDFs (Hebrew and
English) into JSON for the Focusity web app's import screens. No AI calls: `pdfplumber` tables,
Hebrew keyword header matching, geometry for weekly timetables, bidi fixes.

- GitHub: `meirvaknin02-spec/focusity-pdf-parser` -- **PUBLIC**. Never commit real student files,
  names, ID numbers, phones or emails.
- Live: https://focusity-pdf-parser.onrender.com (Render free tier, `render.yaml`).
  Render auto-deploys `main`. A cold start takes ~20 s, so a first request may time out.

## Endpoints
`POST /parse-pdf` (exam schedule), `POST /parse-grade-sheet`, `POST /parse-class-schedule-pdf`
(weekly timetable), `GET /health`.

## Run
```
python -m venv .venv
.venv\Scripts\activate            # Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn main:app --reload --port 8000
pytest
```
In Git Bash use a Windows path for curl uploads: `curl -F "file=@C:/path/file.pdf" ...`
(an MSYS `/c/...` path silently fails with HTTP 000).

## Tests
`tests/test_real_forms.py` parses redacted real PDFs and compares every row with
`tests/expected/*.json`. Fixtures are redacted with `tools/make_fixtures.py`; a test fails if a
fixture contains an ID number, mobile number or email. Never regenerate an expected file just to
make a test pass -- check the real document (or its geometry) first. CI runs pytest on every push.

## Current state (2026-10-21)
`main`: SCE weekly print timetable, Sapir quarter-hour blocks, missing-ToUnicode Hebrew
fonts and dotted hour ranges are handled. Since 2026-10-25 also registration prints with one page
per semester (semester named in the page heading), a "שעות" column holding the time range, and
several course-ish headers (the most specific keyword wins, not the leftmost column). Known gap: University of Haifa elective-list PDFs are
still rejected (no strategy for that table shape).
