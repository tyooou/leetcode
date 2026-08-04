#!/usr/bin/env python3
"""
LeetCode -> Obsidian -> GitHub daily sync.

Stdlib-only (no pip installs required) so it runs in any fresh sandbox.

What it does:
  1. Reads config.json (LeetCode username + session cookies).
  2. Asks LeetCode's GraphQL API for your recent Accepted submissions.
  3. For any submission not yet synced (tracked in state.json), fetches the
     full problem statement + your submitted code.
  4. Writes a markdown note into the vault root, matching the existing note
     format ("Problem link / Difficulty / Date / Tags" header, Problem
     Summary, Submission, then empty Reasoning/Notes/Complexity sections for
     you to fill in by hand).
  5. Skips any problem that already has a note file (never overwrites your
     existing manual notes).
  6. git add / commit / push (uses whatever git remote + stored credentials
     are already configured in this repo -- this script never handles or
     stores your GitHub token).

Exit codes: 0 = success (including "nothing new"), 1 = config/auth error.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime
from html import unescape

VAULT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC_DIR = os.path.join(VAULT_DIR, ".leetcode-sync")
CONFIG_PATH = os.path.join(SYNC_DIR, "config.json")
STATE_PATH = os.path.join(SYNC_DIR, "state.json")

GRAPHQL_URL = "https://leetcode.com/graphql"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

LANG_EXT = {
    "python": "python", "python3": "python", "java": "java", "c": "c",
    "cpp": "cpp", "c++": "cpp", "csharp": "csharp", "javascript": "javascript",
    "typescript": "typescript", "php": "php", "swift": "swift",
    "kotlin": "kotlin", "dart": "dart", "golang": "go", "go": "go",
    "ruby": "ruby", "scala": "scala", "rust": "rust", "racket": "racket",
    "erlang": "erlang", "elixir": "elixir", "mysql": "sql", "mssql": "sql",
    "oraclesql": "sql", "postgresql": "sql", "pythondata": "python",
}


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        die(f"Missing config file at {CONFIG_PATH}. Copy config.example.json "
            f"to config.json and fill in your values.")
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    missing = [k for k in ("leetcode_username", "leetcode_session", "leetcode_csrftoken")
               if not cfg.get(k) or cfg[k].startswith("PASTE_")]
    if missing:
        die(f"config.json is missing/unfilled values for: {', '.join(missing)}")
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"synced_ids": []}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def graphql(query, variables, cfg, auth=False):
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Referer": "https://leetcode.com",
        "Origin": "https://leetcode.com",
    }
    if auth:
        headers["Cookie"] = (f"LEETCODE_SESSION={cfg['leetcode_session']}; "
                              f"csrftoken={cfg['leetcode_csrftoken']}")
        headers["x-csrftoken"] = cfg["leetcode_csrftoken"]
    req = urllib.request.Request(GRAPHQL_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        die(f"LeetCode API HTTP {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        die(f"LeetCode API unreachable: {e}")
    if "errors" in data and data["errors"]:
        die(f"LeetCode API error: {data['errors']}")
    return data.get("data", {})


def fetch_recent_ac(cfg):
    query = """
    query recentAcSubmissions($username: String!, $limit: Int!) {
      recentAcSubmissionList(username: $username, limit: $limit) {
        id
        title
        titleSlug
        timestamp
      }
    }"""
    data = graphql(query, {"username": cfg["leetcode_username"], "limit": 20}, cfg)
    subs = data.get("recentAcSubmissionList")
    if subs is None:
        die("Could not read recentAcSubmissionList -- check leetcode_username "
            "and that your LeetCode profile submissions aren't private.")
    return subs


def fetch_submission_detail(sub_id, cfg):
    query = """
    query submissionDetails($submissionId: Int!) {
      submissionDetails(submissionId: $submissionId) {
        code
        lang { name }
        runtimeDisplay
        memoryDisplay
      }
    }"""
    data = graphql(query, {"submissionId": int(sub_id)}, cfg, auth=True)
    detail = data.get("submissionDetails")
    if detail is None:
        die("Could not read submission details -- your LEETCODE_SESSION / "
            "csrftoken cookie is likely expired. Refresh it in config.json.")
    return detail


def fetch_question(title_slug, cfg):
    query = """
    query questionData($titleSlug: String!) {
      question(titleSlug: $titleSlug) {
        questionFrontendId
        title
        difficulty
        content
        topicTags { name }
      }
    }"""
    data = graphql(query, {"titleSlug": title_slug}, cfg)
    q = data.get("question")
    if q is None:
        die(f"Could not fetch question data for '{title_slug}'.")
    return q


def html_to_markdown(html):
    if not html:
        return ""
    text = html
    text = re.sub(r"(?is)<sup>(.*?)</sup>", r"^\1", text)
    text = re.sub(r"(?is)<sub>(.*?)</sub>", r"_\1", text)
    text = re.sub(r"(?is)<(strong|b)>(.*?)</\1>", r"**\2**", text)
    text = re.sub(r"(?is)<(em|i)>(.*?)</\1>", r"_\2_", text)
    text = re.sub(r"(?is)<code>(.*?)</code>", r"`\1`", text)
    text = re.sub(r"(?is)<li>(.*?)</li>", r"- \1\n", text)
    text = re.sub(r"(?is)</?(ul|ol)>", "\n", text)
    text = re.sub(r"(?is)<pre>(.*?)</pre>", r"\n\1\n", text)
    text = re.sub(r"(?is)</p>\s*<p>", "\n\n", text)
    text = re.sub(r"(?is)</?p>", "\n", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)  # strip any remaining tags
    text = unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def safe_title(title):
    # Strip characters illegal in filenames on macOS/most filesystems.
    return re.sub(r'[\\/:*?"<>|]', "", title).strip()


def build_note(question, detail, submission, cfg):
    frontend_id = question["questionFrontendId"]
    title = safe_title(question["title"])
    difficulty = question["difficulty"]
    tags = ", ".join(t["name"] for t in question.get("topicTags", []))
    slug = submission["titleSlug"]
    date_str = datetime.fromtimestamp(int(submission["timestamp"])).strftime("%d/%m/%Y")
    summary = html_to_markdown(question.get("content", ""))
    lang_name = (detail.get("lang") or {}).get("name", "").lower()
    fence_lang = LANG_EXT.get(lang_name, lang_name or "text")
    code = detail.get("code", "")

    body = f"""Problem link: https://leetcode.com/problems/{slug}/description/
Difficulty: {difficulty}
Date: {date_str}
Tags: {tags}

## Problem Summary
{summary}

## Submission

```{fence_lang}
{code}
```

## Reasoning

*(fill in)*

## Notes

*(fill in)*

## Complexity
**Time:**
**Space:**
"""
    filename = f"{frontend_id}. {title}.md"
    return filename, body


def existing_note_for(frontend_id, vault_dir):
    prefix = f"{frontend_id}. "
    for fname in os.listdir(vault_dir):
        if fname.startswith(prefix) and fname.endswith(".md"):
            return fname
    return None


def git_sync(new_files, vault_dir):
    if not new_files:
        return
    try:
        subprocess.run(["git", "-C", vault_dir, "add"] + new_files, check=True)
        subprocess.run(["git", "-C", vault_dir, "commit", "-m",
                         f"LeetCode sync: {len(new_files)} new submission(s) "
                         f"({datetime.now().strftime('%Y-%m-%d')})"], check=True)
        subprocess.run(["git", "-C", vault_dir, "push"], check=True)
    except subprocess.CalledProcessError as e:
        die(f"git step failed: {e}. New note files were written locally but "
            f"NOT pushed -- push manually once fixed.")


def main():
    cfg = load_config()
    state = load_state()
    synced_ids = set(state.get("synced_ids", []))

    recent = fetch_recent_ac(cfg)
    new_subs = [s for s in recent if s["id"] not in synced_ids]

    if not new_subs:
        print("No new accepted submissions since last sync.")
        return

    new_files = []
    for sub in new_subs:
        question = fetch_question(sub["titleSlug"], cfg)
        frontend_id = question["questionFrontendId"]

        existing = existing_note_for(frontend_id, VAULT_DIR)
        if existing:
            print(f"Skipping {frontend_id} ({sub['title']}): note already exists ({existing}).")
            synced_ids.add(sub["id"])
            continue

        detail = fetch_submission_detail(sub["id"], cfg)
        filename, body = build_note(question, detail, sub, cfg)
        path = os.path.join(VAULT_DIR, filename)
        with open(path, "w") as f:
            f.write(body)
        print(f"Wrote {filename}")
        new_files.append(filename)
        synced_ids.add(sub["id"])

    state["synced_ids"] = sorted(synced_ids)
    save_state(state)

    git_sync(new_files, VAULT_DIR)
    if new_files:
        print(f"Synced {len(new_files)} new note(s) and pushed to GitHub.")


if __name__ == "__main__":
    main()
