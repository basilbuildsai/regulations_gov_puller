"""
Download PDF attachments referenced in <data_dir>/comments.jsonl.

Security posture (untrusted public input — treat accordingly):
  * Pure HTTP fetch; never invoke a shell, never auto-open the file.
  * Hard cap on file size (default 50 MB) to avoid decompression / cost bombs.
  * Verify the response actually starts with %PDF magic bytes; reject otherwise.
  * Filenames are derived only from the comment ID (regex-validated) +
    numeric docOrder. The API-provided filename is never trusted as a path.
  * Files land under <data_dir>/attachments/<comment_id>/ — that directory
    is quarantine: never in PATH, never opened by default tools.

Usage:
    python -m puller.fetch_attachments --data-dir raw
    python -m puller.fetch_attachments --data-dir raw --max 25
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

MAX_BYTES = 50 * 1024 * 1024  # 50 MB hard cap per file
PDF_MAGIC = b"%PDF"

# Headers that pass CloudFront's bot filter on downloads.regulations.gov.
# Determined empirically; without these, all requests return HTTP 403.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/121.0.0.0 Safari/537.36",
    "Accept": "application/pdf,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.regulations.gov/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
}

# Accept dockets like FAA-2025-1908-0024 (agency-year-docket-comment).
SAFE_ID_RX = re.compile(r"^[A-Z]{2,8}-\d{4}-\d+-\d+$")


def safe_comment_dir(attach_root: Path, comment_id: str) -> Path:
    if not SAFE_ID_RX.match(comment_id):
        raise ValueError(f"refusing to use untrusted comment id as path: {comment_id!r}")
    return attach_root / comment_id


def iter_attachment_jobs(comments_path: Path):
    """Yield (comment_id, doc_order, file_url, declared_size) tuples."""
    with comments_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = rec["data"]["id"]
            for inc in rec.get("included", []) or []:
                if inc.get("type") != "attachments":
                    continue
                attrs = inc.get("attributes", {}) or {}
                doc_order = int(attrs.get("docOrder") or 1)
                for ff in attrs.get("fileFormats") or []:
                    if ff.get("format", "").lower() != "pdf":
                        continue
                    url = ff.get("fileUrl")
                    size = int(ff.get("size") or 0)
                    if not url:
                        continue
                    yield cid, doc_order, url, size


def download_one(url: str, target: Path, declared_size: int) -> tuple[bool, str]:
    if declared_size and declared_size > MAX_BYTES:
        return False, f"declared size {declared_size} exceeds cap"

    try:
        with requests.get(url, headers=BROWSER_HEADERS, stream=True, timeout=60) as resp:
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            ctype = resp.headers.get("Content-Type", "")
            if "pdf" not in ctype.lower() and "octet-stream" not in ctype.lower():
                return False, f"unexpected content-type {ctype}"
            total = 0
            tmp = target.with_suffix(target.suffix + ".part")
            magic_checked = False
            with tmp.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if not magic_checked:
                        if not chunk.startswith(PDF_MAGIC):
                            tmp.unlink(missing_ok=True)
                            return False, "magic-bytes mismatch (not a PDF)"
                        magic_checked = True
                    total += len(chunk)
                    if total > MAX_BYTES:
                        tmp.unlink(missing_ok=True)
                        return False, f"exceeded {MAX_BYTES}-byte cap mid-stream"
                    f.write(chunk)
            if not magic_checked:
                tmp.unlink(missing_ok=True)
                return False, "empty response"
            tmp.rename(target)
            return True, f"{total} bytes"
    except requests.RequestException as e:
        return False, f"network error: {e}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="raw", help="dir containing comments.jsonl (default ./raw)")
    p.add_argument("--max", type=int, default=0, help="cap downloads (0 = all)")
    p.add_argument("--sleep", type=float, default=0.2, help="seconds between downloads")
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    comments_path = data_dir / "comments.jsonl"
    attach_root = data_dir / "attachments"

    if not comments_path.exists():
        sys.exit(f"error: {comments_path} not found — run pull_inline.py first")

    attach_root.mkdir(parents=True, exist_ok=True)

    jobs = list(iter_attachment_jobs(comments_path))
    print(f"Total PDF attachments referenced: {len(jobs)}")

    todo = []
    for cid, idx, url, size in jobs:
        target = safe_comment_dir(attach_root, cid) / f"attachment_{idx}.pdf"
        if target.exists():
            continue
        todo.append((cid, idx, url, size, target))
    if args.max:
        todo = todo[: args.max]
    print(f"To download: {len(todo)} (skipping {len(jobs) - len(todo)} already on disk or capped)")

    ok = 0
    fail = 0
    t0 = time.time()
    for i, (cid, idx, url, size, target) in enumerate(todo, 1):
        target.parent.mkdir(parents=True, exist_ok=True)
        success, note = download_one(url, target, size)
        if success:
            ok += 1
            tag = "ok"
        else:
            fail += 1
            tag = f"FAIL: {note}"
        if i % 25 == 0 or i == len(todo) or not success:
            elapsed = time.time() - t0
            rate = i / max(elapsed, 0.001)
            print(f"  [{i}/{len(todo)}] {cid} #{idx}  {tag}  ({rate:.1f}/s)")
        time.sleep(args.sleep)

    print(f"\nDone. {ok} ok, {fail} failed.")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
