#!/usr/bin/env python3
"""
Refreshes LEETCODE_SESSION / csrftoken in config.json using a persistent
Playwright browser profile, instead of copy-pasting cookies out of DevTools
by hand.

One-time setup (interactive, opens a real browser window):
    python3 refresh_cookies.py --login

After that, sync.py calls refresh() headlessly before every real sync
attempt: it launches WebKit (Safari's engine -- Chromium gets flagged by
LeetCode's Cloudflare bot check, WebKit doesn't) against the same saved
profile, visits leetcode.com (which silently renews LEETCODE_SESSION off
LeetCode's own longer-lived remember-me cookie), and writes the current
cookie values into config.json. No OS Keychain access involved -- the
profile is a plain directory Playwright owns outright.

Re-run --login only once LeetCode's remember-me cookie itself expires
(months, not days) and the site stops silently renewing the session.
"""
import argparse
import json
import os
import sys

from playwright.sync_api import sync_playwright

SYNC_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SYNC_DIR, "config.json")
PROFILE_DIR = os.path.join(SYNC_DIR, ".browser-profile")


def _load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def _extract(cookies):
    by_name = {c["name"]: c["value"] for c in cookies}
    return by_name.get("LEETCODE_SESSION"), by_name.get("csrftoken")


def login():
    """Interactive one-time setup: opens a real window, waits for you to
    log into LeetCode by hand, then saves the session into PROFILE_DIR."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        context = p.webkit.launch_persistent_context(PROFILE_DIR, headless=False)
        page = context.new_page()
        page.goto("https://leetcode.com/accounts/login/")
        print("Log into LeetCode in the opened browser window, then come back "
              "here and press Enter...")
        input()
        session, csrf = _extract(context.cookies("https://leetcode.com"))
        context.close()
    if not session or not csrf:
        sys.exit("Didn't see LEETCODE_SESSION/csrftoken after login -- were you "
                  "actually signed in? Try --login again.")
    cfg = _load_config()
    cfg["leetcode_session"] = session
    cfg["leetcode_csrftoken"] = csrf
    _save_config(cfg)
    print("Saved fresh cookies to config.json. Profile stored for future "
          "headless refreshes.")


def refresh(headless=True):
    """Headless refresh used by sync.py. Returns True if cookies were
    updated, False if the saved profile isn't logged in (needs --login)."""
    if not os.path.isdir(PROFILE_DIR):
        print("No saved browser profile yet -- run "
              "'python3 refresh_cookies.py --login' once first.", file=sys.stderr)
        return False
    with sync_playwright() as p:
        context = p.webkit.launch_persistent_context(PROFILE_DIR, headless=headless)
        page = context.new_page()
        page.goto("https://leetcode.com", wait_until="networkidle")
        session, csrf = _extract(context.cookies("https://leetcode.com"))
        context.close()
    if not session or not csrf:
        print("Saved profile is logged out -- run "
              "'python3 refresh_cookies.py --login' again.", file=sys.stderr)
        return False
    cfg = _load_config()
    cfg["leetcode_session"] = session
    cfg["leetcode_csrftoken"] = csrf
    _save_config(cfg)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true",
                         help="one-time interactive login")
    args = parser.parse_args()
    if args.login:
        login()
    else:
        ok = refresh(headless=True)
        print("Refreshed." if ok else "Refresh failed.")
        sys.exit(0 if ok else 1)
