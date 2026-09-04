#!/usr/bin/env python3
"""
One-time (well, re-runnable) backfill: pulls your FULL LeetCode Accepted
history -- not just the last 20 sync.py sees via recentAcSubmissionList --
and writes a note for every solved problem that doesn't already have one,
foldered under YYYY/MM by the date you first solved it.

Reuses sync.py's fetch_question/fetch_submission_detail/build_note so the
notes are byte-for-byte the same format as the incremental daily sync.
Makes no git calls, same as sync.py -- leetcode-push picks these up on its
next poll.

Usage: python3 backfill.py [--dry-run]
"""
import argparse
import os
import re
import sys
import time

import sync

# stdout/stderr default to ASCII under launchd/non-UTF-8 locales; problem
# titles routinely contain non-ASCII characters (e.g. U+00D7 "x"), which
# would otherwise crash a bare print().
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SLEEP_BETWEEN_CALLS = 0.5

SUBMISSION_LIST_QUERY = """
query submissionList($offset: Int!, $limit: Int!, $lastKey: String) {
  submissionList(offset: $offset, limit: $limit, lastKey: $lastKey) {
    lastKey
    hasNext
    submissions {
      id
      title
      titleSlug
      timestamp
      statusDisplay
    }
  }
}"""


def fetch_all_accepted(cfg):
    """Paginate LeetCode's submission history end to end, keeping only the
    earliest Accepted submission per problem (that's the actual solve
    date -- later re-submits of the same problem don't count as new)."""
    earliest = {}
    offset, last_key, page = 0, "", 0
    while True:
        page += 1
        data = sync.graphql(SUBMISSION_LIST_QUERY,
                             {"offset": offset, "limit": 20, "lastKey": last_key},
                             cfg, auth=True)
        block = data.get("submissionList")
        if block is None:
            sync.die("Could not read submissionList -- cookie may be invalid.")
        subs = block.get("submissions") or []
        if not subs:
            break
        for s in subs:
            if s.get("statusDisplay") != "Accepted":
                continue
            slug = s["titleSlug"]
            if slug not in earliest or int(s["timestamp"]) < int(earliest[slug]["timestamp"]):
                earliest[slug] = s
        print(f"  page {page}: {len(subs)} submissions, "
              f"{len(earliest)} unique accepted so far", file=sys.stderr)
        if not block.get("hasNext"):
            break
        last_key = block.get("lastKey") or ""
        offset += len(subs)
        time.sleep(SLEEP_BETWEEN_CALLS)
    return sorted(earliest.values(), key=lambda s: int(s["timestamp"]))


def existing_note_titles(vault_dir):
    """Cheap pre-filter so we don't burn a fetch_question call on every
    already-noted problem -- match by title straight out of the filename
    (format is '{frontend_id}. {title}.md') before we know frontend_id."""
    titles = set()
    for root, dirs, files in os.walk(vault_dir):
        dirs[:] = [d for d in dirs if d not in sync.SKIP_DIRS]
        for fname in files:
            if fname.endswith(".md"):
                m = re.match(r"^\d+\.\s+(.+)\.md$", fname)
                if m:
                    titles.add(m.group(1))
    return titles


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                         help="report counts only, write nothing")
    args = parser.parse_args()

    cfg = sync.load_config()

    print("Fetching full submission history...", file=sys.stderr)
    accepted = fetch_all_accepted(cfg)
    print(f"{len(accepted)} unique solved problems found on LeetCode.", file=sys.stderr)

    known_titles = existing_note_titles(sync.VAULT_DIR)
    candidates = [s for s in accepted if s["title"] not in known_titles]
    print(f"{len(candidates)} without a matching note title "
          f"({len(accepted) - len(candidates)} already noted).", file=sys.stderr)

    if args.dry_run:
        for s in candidates:
            print(f"  would fetch: {s['title']}")
        return

    written = skipped = 0
    for i, sub in enumerate(candidates, 1):
        question = sync.fetch_question(sub["titleSlug"], cfg)
        time.sleep(SLEEP_BETWEEN_CALLS)
        frontend_id = question["questionFrontendId"]

        existing = sync.existing_note_for(frontend_id, sync.VAULT_DIR)
        if existing:
            print(f"[{i}/{len(candidates)}] Skipping {frontend_id} "
                  f"({sub['title']}): note already exists ({existing}).")
            skipped += 1
            continue

        detail = sync.fetch_submission_detail(sub["id"], cfg)
        time.sleep(SLEEP_BETWEEN_CALLS)

        filename, body = sync.build_note(question, detail, sub, cfg, analysis=None)
        path = os.path.join(sync.VAULT_DIR, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)
        print(f"[{i}/{len(candidates)}] Wrote {filename}")
        written += 1

    print(f"\nDone. {written} new notes written, {skipped} skipped "
          f"(already existed).")


if __name__ == "__main__":
    main()
