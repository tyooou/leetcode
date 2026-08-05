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

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

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
    api_key = cfg.get("anthropic_api_key", "")
    if not api_key or api_key.startswith("PASTE_"):
        print("NOTE: no anthropic_api_key set -- Reasoning/Complexity will be "
              "left blank for you to fill in by hand.")
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
        hints
        topicTags { name }
      }
    }"""
    data = graphql(query, {"titleSlug": title_slug}, cfg)
    q = data.get("question")
    if q is None:
        die(f"Could not fetch question data for '{title_slug}'.")
    return q


def generate_analysis(question, code, fence_lang, cfg):
    """Ask Claude for a short reasoning summary + time/space complexity.
    Returns None (leaving the note sections blank) if no API key is
    configured or the call/parse fails for any reason -- this is a nice-to-
    have and must never break the sync."""
    api_key = cfg.get("anthropic_api_key")
    if not api_key or api_key.startswith("PASTE_"):
        return None

    summary = html_to_markdown(question.get("content", ""))[:1500]
    prompt = f"""You are annotating a LeetCode solution for a personal study log.
Given the problem and the accepted solution code below, respond with ONLY a
JSON object (no markdown code fences, no commentary) with exactly these keys:
- "reasoning_steps": an array of 2-5 short plain-English strings, each one
  step of the approach used in the code. This becomes a numbered list, so
  keep each step self-contained and specific to this code (not generic).
- "key_insight": one punchy sentence (max ~20 words) naming the single core
  trick or observation that makes this approach work -- the thing worth
  remembering if you saw this problem again in six months.
- "time_complexity": a short string like "O(n)"
- "space_complexity": a short string like "O(1)"

Problem: {question.get('title')} ({question.get('difficulty')})
Problem summary:
{summary}

Submitted solution ({fence_lang}):
{code}
"""
    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    req = urllib.request.Request(ANTHROPIC_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        print(f"WARNING: AI analysis failed: HTTP {e.code}: {detail}\n"
              f"Leaving Reasoning/Complexity blank for you to fill in.", file=sys.stderr)
        return None
    except Exception as e:
        print(f"WARNING: AI analysis request failed ({e}); leaving "
              f"Reasoning/Complexity blank for you to fill in.", file=sys.stderr)
        return None

    try:
        text = data["content"][0]["text"].strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        parsed = json.loads(text)
        if not parsed.get("reasoning_steps"):
            return None
        return parsed
    except Exception as e:
        print(f"WARNING: AI response unparseable ({e}); raw response: "
              f"{json.dumps(data)[:500]}\nLeaving Reasoning/Complexity blank.", file=sys.stderr)
        return None


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


SECTION_HEADING_RE = re.compile(
    r'<p>\s*<strong[^>]*>\s*(Example\s+\d+|Constraints|Follow[\s-]?[Uu]p)\s*:?\s*</strong>\s*</p>',
    re.IGNORECASE)


def split_problem_sections(html):
    """Split LeetCode's question content HTML into intro text + a list of
    (label, raw_html) sections for each 'Example N:' / 'Constraints:' /
    'Follow up:' heading LeetCode renders as its own block. Section bodies
    are left as raw HTML -- the caller decides how to render each one."""
    if not html:
        return "", []
    matches = list(SECTION_HEADING_RE.finditer(html))
    if not matches:
        return html_to_markdown(html), []
    intro = html_to_markdown(html[:matches[0].start()])
    sections = []
    for i, m in enumerate(matches):
        label = re.sub(r"\s+", " ", m.group(1)).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        sections.append((label, html[start:end]))
    return intro, sections


EXAMPLE_LABEL_RE = re.compile(
    r'(?is)<strong>\s*(Input|Output|Explanation|Note)\s*:?\s*</strong>\s*')


def format_example(raw_html, fence_lang):
    """Render an Example block as separate Input / Output / Explanation
    code fences, matching LeetCode's own layout, instead of one flat
    paragraph."""
    pre_match = re.search(r"(?is)<pre>(.*?)</pre>", raw_html)
    if not pre_match:
        return html_to_markdown(raw_html)
    pre_content = pre_match.group(1)
    matches = list(EXAMPLE_LABEL_RE.finditer(pre_content))
    if not matches:
        return html_to_markdown(raw_html)
    blocks = []
    for i, m in enumerate(matches):
        label = m.group(1).strip().capitalize()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(pre_content)
        value = html_to_markdown(pre_content[start:end]).strip()
        blocks.append(f"**{label}:**\n\n```{fence_lang}\n{value}\n```")
    return "\n\n".join(blocks)


def safe_title(title):
    # Strip characters illegal in filenames on macOS/most filesystems.
    return re.sub(r'[\\/:*?"<>|]', "", title).strip()


def kebab(text):
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")


def to_callout(text, kind, title, fold=None):
    """Wrap text in an Obsidian callout block. fold: None (not foldable),
    '-' (collapsed), '+' (expanded/foldable)."""
    marker = f"[!{kind}]{fold or ''} {title}".rstrip()
    lines = text.split("\n") if text else [""]
    quoted = "\n".join(f"> {line}".rstrip() if line else ">" for line in lines)
    return f"> {marker}\n{quoted}"


SECTION_CALLOUT = {
    "constraints": ("warning", "Constraints"),
    "follow up": ("info", "Follow up"),
    "followup": ("info", "Follow up"),
}


def build_note(question, detail, submission, cfg, analysis=None):
    frontend_id = question["questionFrontendId"]
    title = safe_title(question["title"])
    difficulty = question["difficulty"]
    tag_names = [t["name"] for t in question.get("topicTags", [])]
    tag_slugs = [kebab(t) for t in tag_names]
    tags_yaml = ", ".join(tag_slugs)
    slug = submission["titleSlug"]
    link = f"https://leetcode.com/problems/{slug}/description/"
    iso_date = datetime.fromtimestamp(int(submission["timestamp"])).strftime("%Y-%m-%d")
    intro, sections = split_problem_sections(question.get("content", ""))
    hints = question.get("hints") or []
    lang_name = (detail.get("lang") or {}).get("name", "").lower()
    fence_lang = LANG_EXT.get(lang_name, lang_name or "text")
    code = detail.get("code", "")
    runtime = detail.get("runtimeDisplay") or "—"
    memory = detail.get("memoryDisplay") or "—"

    if analysis and analysis.get("reasoning_steps"):
        reasoning = "\n".join(f"{i + 1}. {step}" for i, step in
                               enumerate(analysis["reasoning_steps"]))
        if analysis.get("key_insight"):
            reasoning += f"\n\n**Key insight:** {analysis['key_insight']}"
    else:
        reasoning = "*(fill in)*"

    time_c = (analysis or {}).get("time_complexity") or "*(fill in)*"
    space_c = (analysis or {}).get("space_complexity") or "*(fill in)*"

    stats_table = (
        "| Runtime | Memory | Time | Space |\n"
        "|---|---|---|---|\n"
        f"| `{runtime}` | `{memory}` | `{time_c}` | `{space_c}` |"
    )
    submission_block = f"{stats_table}\n\n```{fence_lang}\n{code}\n```"

    parts = [
        "---",
        f"link: {link}",
        f"difficulty: {difficulty}",
        f"date: {iso_date}",
        f"tags: [{tags_yaml}]",
        f'runtime: "{runtime}"',
        f'memory: "{memory}"',
        "---",
        "",
        "### Problem Statement:",
        "",
        intro,
        "",
    ]

    for label, raw_html in sections:
        norm = label.lower()
        if norm.startswith("example"):
            kind, heading = "example", label
            body_text = format_example(raw_html, fence_lang)
        else:
            kind, heading = SECTION_CALLOUT.get(norm, ("note", label))
            body_text = html_to_markdown(raw_html)
        parts.append(to_callout(body_text, kind, heading, fold="-"))
        parts.append("")

    if hints:
        parts += ["---", "", "### Hints:", ""]
        for i, hint in enumerate(hints, 1):
            parts.append(to_callout(html_to_markdown(hint), "success", f"Hint {i}", fold="-"))
            parts.append("")

    parts += [
        "---",
        "",
        "### Submission:",
        "",
        to_callout(submission_block, "example", "Submission", fold="+"),
        "",
        to_callout(reasoning, "success", "Reasoning", fold="-"),
        "",
        "---",
        "",
        "### Notes:",
        "",
        "*(fill in)*",
        "",
    ]
    body = "\n".join(parts)
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
        lang_name = (detail.get("lang") or {}).get("name", "").lower()
        fence_lang = LANG_EXT.get(lang_name, lang_name or "text")
        analysis = generate_analysis(question, detail.get("code", ""), fence_lang, cfg)
        filename, body = build_note(question, detail, sub, cfg, analysis)
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
