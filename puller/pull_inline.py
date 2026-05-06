"""
Pull all comment metadata + inline text for a regulations.gov docket.

Resumable: writes <data_dir>/comments.jsonl, one JSON record per line. If the
file already contains records, IDs already present are skipped.

Rate limiting: api.data.gov free key allows 1000 req/hr. Default 4 s sleep
keeps us under that ceiling with margin and survives transient 429s.

Usage:
    python -m puller.pull_inline --docket FAA-2025-1908
    python -m puller.pull_inline --docket FAA-2025-1908 --max 50
    python -m puller.pull_inline --docket FAA-2025-1908 --data-dir /var/data/part108
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = "https://api.regulations.gov/v4"


def load_key() -> str:
    """Read DATA_GOV_KEY from .env (cwd) or environment."""
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("DATA_GOV_KEY="):
                return line.split("=", 1)[1].strip()
    key = os.environ.get("DATA_GOV_KEY")
    if not key:
        sys.exit("error: DATA_GOV_KEY not set in .env or environment")
    return key


def api_get(path: str, params: dict, key: str, retries: int = 5) -> dict:
    url = f"{BASE}{path}?{urlencode(params)}"
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"X-Api-Key": key, "Accept": "application/json"})
            with urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            last_exc = e
            if e.code == 429 and attempt < retries - 1:
                wait = 60 * (attempt + 1)
                print(f"  [429] backing off {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if 500 <= e.code < 600 and attempt < retries - 1:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_exc = e
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"  [retry] {type(e).__name__}: {e} (sleep {wait}s)", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("unreachable")


def existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids = set()
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["data"]["id"])
            except (json.JSONDecodeError, KeyError):
                pass
    return ids


def list_all_comment_ids(docket: str, key: str) -> list[tuple[str, str]]:
    """Returns (comment_id, posted_date) across all pages of a docket."""
    page_size = 250
    all_items: list[tuple[str, str]] = []
    page = 1
    while True:
        data = api_get(
            "/comments",
            {
                "filter[docketId]": docket,
                "page[size]": page_size,
                "page[number]": page,
                "sort": "lastModifiedDate",
            },
            key,
        )
        for item in data["data"]:
            all_items.append((item["id"], item["attributes"].get("postedDate", "")))
        meta = data.get("meta", {})
        total = meta.get("totalElements", "?")
        print(f"  list page {page}: {len(data['data'])} items (cumulative {len(all_items)} / {total})")
        if not meta.get("hasNextPage"):
            break
        page += 1
        time.sleep(0.4)
    return all_items


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--docket", required=True, help="docket id, e.g. FAA-2025-1908")
    p.add_argument("--data-dir", default="raw", help="output directory (default ./raw)")
    p.add_argument("--max", type=int, default=0, help="cap total fetches (0 = no cap)")
    p.add_argument("--sleep", type=float, default=4.0,
                   help="seconds between detail calls (default 4.0 = ~900/hr, well under 1000/hr cap)")
    args = p.parse_args()

    key = load_key()
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / "comments.jsonl"

    have = existing_ids(out_path)
    print(f"Existing records on disk: {len(have)}")

    print(f"\nListing all comment IDs in docket {args.docket} ...")
    all_ids = list_all_comment_ids(args.docket, key)
    print(f"Total comment IDs: {len(all_ids)}")

    todo = [cid for cid, _ in all_ids if cid not in have]
    if args.max:
        todo = todo[: args.max]
    print(f"To fetch: {len(todo)}")

    out = out_path.open("a")
    fetched = 0
    t0 = time.time()
    try:
        for i, cid in enumerate(todo, 1):
            try:
                detail = api_get(f"/comments/{cid}", {"include": "attachments"}, key)
            except HTTPError as e:
                print(f"  [{i}/{len(todo)}] {cid} HTTP {e.code} — skipping")
                time.sleep(2.0)
                continue
            out.write(json.dumps(detail) + "\n")
            out.flush()
            fetched += 1
            if i % 25 == 0 or i == len(todo):
                elapsed = time.time() - t0
                rate = fetched / max(elapsed, 0.001)
                eta_min = (len(todo) - i) / max(rate, 0.01) / 60
                print(f"  [{i}/{len(todo)}] {cid}  {rate:.2f} req/s  ETA {eta_min:.1f} min")
            time.sleep(args.sleep)
    except KeyboardInterrupt:
        print("\nInterrupted; partial results saved.")
    finally:
        out.close()

    print(f"\nDone. Fetched {fetched} records this run.")
    print(f"File: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
