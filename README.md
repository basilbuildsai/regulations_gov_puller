# regulations.gov puller

Standalone, server-friendly toolkit for pulling all comments and PDF attachments from a regulations.gov docket via the api.data.gov v4 API. Built for "1-Hour Rulemaking Recon" YouTube videos and any project that needs a complete, sanitized public-comment corpus ready for LLM analysis.

Three stages:

1. `puller.pull_inline` — paginates `/v4/comments` for a docket, fetches each comment in detail, writes one JSON record per line to `<data_dir>/comments.jsonl`. Resumable.
2. `puller.fetch_attachments` — downloads PDF attachments referenced in the JSONL to `<data_dir>/attachments/<comment_id>/`. Browser-headed direct CloudFront fetches (the API path returns the same blocked URL). 50 MB per-file cap, magic-byte verification.
3. `puller.extract_pdfs` — pure-Python pypdf extraction with Adobe-portfolio support; writes sanitized `.txt` sidecars next to each PDF.

A shared `puller.sanitize` module strips control / zero-width / ANSI / RTL-override characters, NFC-normalizes, caps length, and provides `wrap_for_llm()` for downstream prompt-injection-defensible LLM ingestion.

## Setup

```bash
git clone <this-private-repo>
cd regulations_gov_puller

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# Edit .env and paste your api.data.gov key (free at https://api.data.gov/signup/)
```

## Quick run (any docket)

```bash
.venv/bin/python -m puller.pull_inline --docket FAA-2025-1908 --data-dir data
.venv/bin/python -m puller.fetch_attachments              --data-dir data
.venv/bin/python -m puller.extract_pdfs                   --data-dir data
```

`<data_dir>/` will end up containing:

```
data/
├── comments.jsonl                # one full API record per comment
├── extract_log.jsonl             # per-PDF extraction log
└── attachments/
    └── <comment_id>/
        ├── attachment_1.pdf
        ├── attachment_1.txt      # sanitized extracted text
        └── ...
```

## Rate limits

- api.data.gov free tier: **1,000 requests/hr** per key
- Default sleep of `4.0` s between detail calls = ~900/hr, well under cap
- Hit a 429? The script backs off (60 s × attempt), no manual intervention needed
- Listing 3,928 comments: ~16 metadata pages (a few seconds), then ~4 hours of detail calls

## Threat model

The corpus is **public submissions plus PDF attachments authored by anyone in the world**. Treat every byte as hostile until proven otherwise.

| Layer | Defense |
|---|---|
| Network | Direct CloudFront fetch with browser headers; no shell-out; per-file 50 MB cap; magic-byte check (`%PDF`); content-type check. |
| Filesystem | Quarantine in `attachments/<commentId>/`; filenames derived only from regex-validated comment ID + numeric `docOrder`. API-supplied filenames never used as paths. |
| PDF parsing | Pure-Python pypdf — no external binary, no JS execution. Bounded at 500 pages per file, 200 K chars of output. Embedded-file walking is bounded at 50 files / 50 MB / no further recursion. |
| Text | `sanitize.py` strips C0/C1 controls, ANSI escapes, zero-width / RTL override / BOM characters; NFC-normalizes Unicode. Length capped per submission. |
| LLM ingestion | `sanitize.wrap_for_llm()` wraps text in `<untrusted_comment>` tags. `SYSTEM_PROMPT_PREAMBLE` instructs models to treat the contents as data, not instructions. |

What this toolkit deliberately does NOT do:

- No lexical filtering of "suspicious phrases" — defenses are structural, not based on string matches that bypass trivially and corrupt analysis.
- No auto-open of any attachment by default OS handlers.
- No agentic tool use over comment content. The toolkit only reads / writes its own data directory.

## Layout

```
regulations_gov_puller/
├── README.md
├── DEPLOY.md              # server setup (tmux, systemd, screen)
├── requirements.txt
├── .env.example
├── .gitignore
└── puller/
    ├── __init__.py
    ├── pull_inline.py
    ├── fetch_attachments.py
    ├── extract_pdfs.py
    └── sanitize.py
```

## See also

- `DEPLOY.md` — running the pull on a remote server so it survives laptop sleep / network drops.
