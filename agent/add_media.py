#!/usr/bin/env python3
"""
add_media.py
============
Add a media coverage entry to data/media.json and push to GitHub.

Entries can be standalone (single outlet) or grouped (multiple outlets
covering the same publication). Use --group to create or append to a
grouped entry identified by its headline.

Usage — new grouped entry (first outlet):
    python add_media.py --date 2025-06-01 \
        --headline "Study reveals new epigenetic mechanism" \
        --outlet "Nature News" \
        --url "https://www.nature.com/articles/..."

Usage — append outlet to existing grouped entry:
    python add_media.py --date 2025-06-01 \
        --headline "Study reveals new epigenetic mechanism" \
        --outlet "ScienceDaily" \
        --url "https://www.sciencedaily.com/..." \
        --group

Usage (from Python / Cowork agent):
    from add_media import add_media_item, append_outlet_to_group

    # Create new grouped entry
    add_media_item("2025-06-01", "Study reveals...",
                   outlet="Nature News", url="https://...", push=True)

    # Append another outlet to an existing grouped entry
    append_outlet_to_group("Study reveals...",
                           outlet="ScienceDaily", url="https://...", push=True)
"""

import json
import os
import base64
import argparse
from pathlib import Path
from dotenv import load_dotenv
import requests

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

with open(SCRIPT_DIR / "config.json") as f:
    CONFIG = json.load(f)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GH_OWNER     = CONFIG["github"]["owner"]
GH_REPO      = CONFIG["github"]["repo"]
GH_BRANCH    = CONFIG["github"]["branch"]
MEDIA_PATH   = CONFIG["paths"]["media_data"]   # relative in repo
REPO_ROOT    = SCRIPT_DIR.parent

# ── GitHub helpers ─────────────────────────────────────────────────────────────

def gh_get_file(path: str) -> tuple[str, str]:
    url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{path}"
    r = requests.get(url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
                     params={"ref": GH_BRANCH})
    r.raise_for_status()
    data = r.json()
    return base64.b64decode(data["content"]).decode(), data["sha"]


def gh_put_file(path: str, content: str, sha: str, message: str):
    url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha":     sha,
        "branch":  GH_BRANCH,
        "committer": {
            "name":  CONFIG["github"]["committer_name"],
            "email": CONFIG["github"]["committer_email"]
        }
    }
    r = requests.put(url, json=payload,
                     headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                               "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()

# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_media() -> list:
    local_path = REPO_ROOT / MEDIA_PATH
    if local_path.exists():
        with open(local_path) as f:
            return json.load(f)
    return []


def _save_and_push(media: list, commit_msg: str, push: bool):
    local_path = REPO_ROOT / MEDIA_PATH
    new_content = json.dumps(media, indent=2, ensure_ascii=False)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with open(local_path, "w") as f:
        f.write(new_content)
    print(f"[add_media] Written to {local_path}")
    if push:
        if not GITHUB_TOKEN:
            print("[add_media] WARNING: GITHUB_TOKEN not set — skipping push.")
            return
        try:
            _, sha = gh_get_file(MEDIA_PATH)
            gh_put_file(MEDIA_PATH, new_content, sha, commit_msg)
            print("[add_media] ✅ Pushed to GitHub")
        except Exception as e:
            print(f"[add_media] ERROR pushing to GitHub: {e}")

# ── Core functions ─────────────────────────────────────────────────────────────

def add_media_item(date: str, headline: str, outlet: str,
                   url: str = "", push: bool = True) -> dict:
    """
    Prepend a new grouped media entry (headline + first outlet).

    If an entry with the same headline already exists, appends the outlet
    to it instead of creating a duplicate.

    Args:
        date:     ISO date string e.g. "2025-06-01"
        headline: The shared headline / story title for this publication
        outlet:   Name of the first news outlet, e.g. "Nature News"
        url:      URL to the outlet's article
        push:     Whether to push to GitHub

    Returns:
        The full media entry dict.
    """
    if not headline:
        raise ValueError("headline cannot be empty")
    if not outlet:
        raise ValueError("outlet cannot be empty")

    media = _load_media()

    # Check if a grouped entry with this headline already exists
    for entry in media:
        if entry.get("headline", "").strip().lower() == headline.strip().lower():
            if "outlets" not in entry:
                entry["outlets"] = []
            outlet_obj = {"name": outlet}
            if url:
                outlet_obj["url"] = url
            entry["outlets"].append(outlet_obj)
            print(f"[add_media] Appended outlet '{outlet}' to existing entry.")
            _save_and_push(media,
                           f"Add outlet to media: {outlet} — {headline[:50]}…",
                           push)
            return entry

    # Create new grouped entry
    outlet_obj = {"name": outlet}
    if url:
        outlet_obj["url"] = url
    item = {"date": date, "headline": headline, "outlets": [outlet_obj]}
    media.insert(0, item)
    _save_and_push(media, f"Add media item: {date} — {headline[:60]}…", push)
    return item


def append_outlet_to_group(headline: str, outlet: str,
                            url: str = "", push: bool = True) -> dict:
    """
    Append an additional outlet to an existing grouped entry.

    Args:
        headline: Exact (case-insensitive) headline of the existing entry
        outlet:   Name of the outlet to add
        url:      URL to the outlet's article
        push:     Whether to push to GitHub

    Returns:
        The updated media entry dict.

    Raises:
        ValueError: if no entry with the given headline is found.
    """
    if not headline:
        raise ValueError("headline cannot be empty")
    if not outlet:
        raise ValueError("outlet cannot be empty")

    media = _load_media()
    for entry in media:
        if entry.get("headline", "").strip().lower() == headline.strip().lower():
            if "outlets" not in entry:
                entry["outlets"] = []
            outlet_obj = {"name": outlet}
            if url:
                outlet_obj["url"] = url
            entry["outlets"].append(outlet_obj)
            _save_and_push(media,
                           f"Add outlet to media: {outlet} — {headline[:50]}…",
                           push)
            return entry

    raise ValueError(f"No media entry found with headline: {headline!r}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Add or update a media coverage entry on the website.")
    parser.add_argument("--date",     default="",
                        help="Date in ISO format, e.g. 2025-06-01 (required for new entries)")
    parser.add_argument("--headline", required=True,
                        help="Shared story headline (used to group outlets)")
    parser.add_argument("--outlet",   required=True,
                        help="Name of the news outlet or publication")
    parser.add_argument("--url",      default="",
                        help="URL to the outlet's article")
    parser.add_argument("--group",    action="store_true",
                        help="Append outlet to an existing grouped entry instead of creating new")
    parser.add_argument("--no-push",  action="store_true",
                        help="Write locally only, don't push to GitHub")
    args = parser.parse_args()

    if args.group:
        append_outlet_to_group(args.headline, args.outlet,
                                args.url, push=not args.no_push)
    else:
        if not args.date:
            parser.error("--date is required when creating a new entry (omit --group)")
        add_media_item(args.date, args.headline, args.outlet,
                       args.url, push=not args.no_push)


if __name__ == "__main__":
    main()
