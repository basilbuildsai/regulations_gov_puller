"""
Extract text from quarantined PDFs into sanitized .txt sidecars.

Pure-Python pypdf — no external binary, no shell, no JS execution.

Adobe-portfolio support: when a PDF is just a wrapper with embedded files
(common for industry submissions), we extract text from the embedded PDFs.

Usage:
    python -m puller.extract_pdfs --data-dir raw
    python -m puller.extract_pdfs --data-dir raw --force
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError, PdfStreamError

from .sanitize import sanitize

MAX_PAGES_PER_PDF = 500
MAX_OUTPUT_CHARS = 200_000

PORTFOLIO_TRIGGER_MAX_CHARS = 500
MAX_EMBEDDED_FILES = 50
MAX_EMBEDDED_BYTES = 50 * 1024 * 1024


def _extract_text_from_reader(reader: PdfReader, info: dict) -> tuple[str, int]:
    parts: list[str] = []
    pages_done = 0
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001 — untrusted input
            info.setdefault("page_errors", []).append(f"{type(e).__name__}: {e}")
            t = ""
        if t:
            parts.append(t)
        pages_done += 1
        if sum(len(p) for p in parts) > MAX_OUTPUT_CHARS * 2:
            break
    return "\n\n".join(parts), pages_done


def _extract_embedded_pdfs(reader: PdfReader, info: dict) -> str:
    try:
        attachments = reader.attachments
    except Exception as e:  # noqa: BLE001
        info["embedded_error"] = f"{type(e).__name__}: {e}"
        return ""

    if not attachments:
        return ""

    embedded_parts: list[str] = []
    bytes_used = 0
    files_done = 0
    embedded_log: list[dict] = []

    for name, blobs in attachments.items():
        if files_done >= MAX_EMBEDDED_FILES:
            break
        if not isinstance(blobs, list):
            blobs = [blobs]
        for data in blobs:
            if files_done >= MAX_EMBEDDED_FILES:
                break
            if not isinstance(data, (bytes, bytearray)):
                continue
            if bytes_used + len(data) > MAX_EMBEDDED_BYTES:
                embedded_log.append({"name": str(name)[:120], "skipped": "byte cap"})
                continue
            if not data.startswith(b"%PDF"):
                embedded_log.append({"name": str(name)[:120], "skipped": "not a PDF"})
                continue
            bytes_used += len(data)
            files_done += 1
            try:
                inner = PdfReader(io.BytesIO(data), strict=False)
            except Exception as e:  # noqa: BLE001
                embedded_log.append({"name": str(name)[:120], "open_failed": str(e)[:200]})
                continue
            sub_info: dict = {}
            text, pages = _extract_text_from_reader(inner, sub_info)
            embedded_log.append({
                "name": str(name)[:120],
                "pages": pages,
                "chars": len(text),
                "page_errors": sub_info.get("page_errors", [])[:5],
            })
            if text:
                embedded_parts.append(f"[embedded file: {str(name)[:120]}]\n{text}")

    info["embedded_files_processed"] = files_done
    info["embedded_log"] = embedded_log
    return "\n\n".join(embedded_parts)


def extract_one(pdf_path: Path, root: Path) -> dict:
    info: dict = {"path": str(pdf_path.relative_to(root)), "ts": time.time()}
    try:
        reader = PdfReader(str(pdf_path), strict=False)
    except (PdfReadError, PdfStreamError, OSError, Exception) as e:
        info["status"] = "open_failed"
        info["error"] = f"{type(e).__name__}: {e}"
        return info

    n_pages = len(reader.pages)
    info["pages"] = n_pages
    if n_pages > MAX_PAGES_PER_PDF:
        info["status"] = "too_many_pages"
        info["pages_processed"] = 0
        return info

    raw_text, pages_done = _extract_text_from_reader(reader, info)

    if len(raw_text) < PORTFOLIO_TRIGGER_MAX_CHARS:
        embedded_text = _extract_embedded_pdfs(reader, info)
        if embedded_text:
            raw_text = embedded_text
            info["used_embedded"] = True

    clean, san_info = sanitize(raw_text, max_chars=MAX_OUTPUT_CHARS)

    out_path = pdf_path.with_suffix(".txt")
    out_path.write_text(clean, encoding="utf-8")
    info["status"] = "ok"
    info["pages_processed"] = pages_done
    info["chars_out"] = len(clean)
    info["sanitize"] = san_info
    return info


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="raw", help="dir containing attachments/ (default ./raw)")
    p.add_argument("--force", action="store_true", help="re-extract even if .txt exists")
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    attach_root = data_dir / "attachments"
    log_path = data_dir / "extract_log.jsonl"

    if not attach_root.exists():
        sys.exit(f"error: {attach_root} not found — run fetch_attachments.py first")

    pdfs = sorted(attach_root.rglob("*.pdf"))
    print(f"Found {len(pdfs)} PDFs")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a")

    ok = 0
    skipped = 0
    failed = 0
    try:
        for i, pdf in enumerate(pdfs, 1):
            txt = pdf.with_suffix(".txt")
            if txt.exists() and not args.force:
                skipped += 1
                continue
            info = extract_one(pdf, data_dir)
            log.write(json.dumps(info) + "\n")
            log.flush()
            if info["status"] == "ok":
                ok += 1
            else:
                failed += 1
                print(f"  [{i}/{len(pdfs)}] FAIL {pdf.name}: {info.get('error') or info['status']}")
            if i % 50 == 0:
                print(f"  [{i}/{len(pdfs)}] ok={ok} fail={failed} skip={skipped}")
    finally:
        log.close()

    print(f"\nDone. ok={ok} fail={failed} skip={skipped}")
    print(f"Log: {log_path}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
