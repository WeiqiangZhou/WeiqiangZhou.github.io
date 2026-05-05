#!/usr/bin/env python3
"""
update_website.py
=================
Master update agent for weiqiangzhou.com.

What it does:
  1. Fetches all publications from Google Scholar (full bibliographic data + citations)
  2. Merges new papers into data/publications.json, preserving hand-curated fields
  3. Flags selected publications (highlight=true) — papers where W. Zhou is
     first / co-first / senior (last) author, ranked by citation count
  4. Auto-generates news items for papers published in the current/recent year
  5. Detects software tools mentioned in paper titles and updates data/software.json
  6. Pushes all changed files to GitHub via the Contents API

Usage:
    python update_website.py              # full run + push to GitHub
    python update_website.py --dry-run   # fetch + compute, print changes only
    python update_website.py --local     # write local files only, no push

Requirements:
    pip install scholarly requests python-dotenv
"""

# ── Bootstrap: install missing dependencies before anything else ──────────────
import subprocess, sys as _sys

def _ensure_deps():
    import importlib
    needed = []
    for pkg, mod in [("scholarly", "scholarly"), ("requests", "requests"), ("python-dotenv", "dotenv")]:
        try:
            importlib.import_module(mod)
        except ImportError:
            needed.append(pkg)
    if needed:
        print(f"[bootstrap] Installing: {', '.join(needed)}", flush=True)
        subprocess.check_call([
            _sys.executable, "-m", "pip", "install", "--quiet",
            "--break-system-packages", *needed
        ])
        print("[bootstrap] Done.", flush=True)

_ensure_deps()
# ─────────────────────────────────────────────────────────────────────────────

import json, os, sys, re, base64, time, hashlib, argparse
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv

# ── Config ───────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

with open(SCRIPT_DIR / "config.json") as f:
    CONFIG = json.load(f)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GH_OWNER     = CONFIG["github"]["owner"]
GH_REPO      = CONFIG["github"]["repo"]
GH_BRANCH    = CONFIG["github"]["branch"]
COMMITTER    = {"name": CONFIG["github"]["committer_name"],
                "email": CONFIG["github"]["committer_email"]}
SCHOLAR_ID   = CONFIG["scholar"]["author_id"]
REPO_ROOT    = SCRIPT_DIR.parent

PUBS_PATH    = CONFIG["paths"]["publications_data"]   # data/publications.json
NEWS_PATH    = CONFIG["paths"]["news_data"]           # data/news.json
SW_PATH      = "data/software.json"

# How many highlighted papers to show (first/senior author, ranked by citations)
HIGHLIGHT_TOP_N = 6
# News items: include papers from this many recent years
NEWS_RECENT_YEARS = 2
# Minimum citations to appear in highlighted set (avoids brand-new un-cited papers)
MIN_CITATIONS_FOR_HIGHLIGHT = 5

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ── Author utilities ──────────────────────────────────────────────────────────

def parse_authors(authors_str: str) -> list[str]:
    """Split an author string into a list of individual author names."""
    if not authors_str:
        return []
    # Normalize " and " separators, then split on commas
    # Handle: "W Zhou, Z Ji, H Ji"  or  "Zhou, W and Ji, Z and Ji, H"
    # Scholarly often uses format: "A Author, B Author, C Author"
    norm = re.sub(r'\s+and\s+', ', ', authors_str, flags=re.IGNORECASE)
    parts = [p.strip() for p in norm.split(',') if p.strip()]
    return parts


def is_zhou_first_or_senior(authors_str: str) -> tuple[bool, str]:
    """
    Returns (True, role) if Zhou W. is first, co-first, or senior (last) author.
    role is one of: "first", "co-first", "senior", or "" (not in special role).
    """
    if not authors_str:
        return False, ""

    parts = parse_authors(authors_str)
    if not parts:
        return False, ""

    def has_zhou(name: str) -> bool:
        n = name.lower()
        return 'zhou' in n and ('w' in n or 'weiqiang' in n or len(n.split()) <= 2)

    # First author
    if has_zhou(parts[0]):
        return True, "first"

    # Co-first (second author, when paper has co-first notation)
    # Heuristic: second author is Zhou and paper may have a '†' or '*' somewhere
    if len(parts) > 1 and has_zhou(parts[1]):
        # Mark as co-first if first two authors could be joint
        return True, "co-first"

    # Senior / last author
    if len(parts) > 2 and has_zhou(parts[-1]):
        return True, "senior"

    return False, ""


# ── Scholar utilities ─────────────────────────────────────────────────────────

def scholar_to_record(pub: dict, fill_extra: bool = True) -> dict:
    """Convert scholarly pub object → our JSON schema."""
    bib = pub.get("bib", {})
    title      = bib.get("title", "").strip()
    year       = int(bib.get("pub_year") or bib.get("year") or 0)
    venue      = bib.get("venue") or bib.get("journal") or bib.get("conference") or ""
    authors    = bib.get("author", "")
    volume     = bib.get("volume", "")
    pages      = bib.get("pages", "")
    abstract   = bib.get("abstract", "")
    citations  = pub.get("num_citations", 0) or 0
    pub_url    = pub.get("pub_url", "") or ""
    eprint_url = pub.get("eprint_url", "") or ""
    url        = pub_url or eprint_url

    slug = hashlib.md5(f"{title}{year}".encode()).hexdigest()[:8]

    return {
        "id":           slug,
        "year":         year,
        "title":        title,
        "authors":      authors,
        "journal":      venue,
        "volume":       volume,
        "pages":        pages,
        "doi":          "",          # scholarly rarely returns DOI; can be filled manually
        "url":          url,
        "abstract":     abstract[:300] if abstract else "",
        "num_citations": citations,
        "highlight":    False,
        "tags":         [],
        "_source":      "scholar"
    }


def fetch_from_scholar(scholar_id: str) -> list[dict]:
    """Fetch all publications for an author from Google Scholar."""
    from scholarly import scholarly as _scholarly

    log(f"  Connecting to Google Scholar (ID: {scholar_id})…")
    try:
        author = _scholarly.search_author_id(scholar_id)
        author = _scholarly.fill(author, sections=["publications"])
    except Exception as e:
        log(f"  ERROR fetching author: {e}")
        return []

    raw_pubs = author.get("publications", [])
    log(f"  Found {len(raw_pubs)} publications on Scholar")

    results = []
    for i, pub in enumerate(raw_pubs):
        try:
            # Fill individual pub for complete data (title, authors, venue, citations)
            pub_filled = _scholarly.fill(pub)
            record = scholar_to_record(pub_filled)
            if record["title"]:
                results.append(record)
            if (i + 1) % 10 == 0:
                log(f"  Processed {i+1}/{len(raw_pubs)} papers…")
            time.sleep(0.5)  # be polite to Google Scholar
        except Exception as e:
            # Fall back to minimal data from unfilled pub
            record = scholar_to_record(pub, fill_extra=False)
            if record["title"]:
                results.append(record)

    log(f"  Successfully processed {len(results)} publications")
    return results


# ── Merge logic ───────────────────────────────────────────────────────────────

def merge_publications(existing: list, fetched: list) -> tuple[list, int]:
    """
    Merge freshly-fetched publications into the existing list.
    - Hand-curated fields (doi, highlight, tags, note) in existing records are preserved.
    - Citation counts and author data are updated from Scholar.
    - New papers are appended.
    Returns (merged_list, num_newly_added).
    """
    # Index existing by lower-cased title
    existing_by_title = {p["title"].lower().strip(): p for p in existing}
    added = 0

    for f in fetched:
        key = f["title"].lower().strip()
        if not key:
            continue

        if key in existing_by_title:
            # Update mutable fields from Scholar (citations, url if empty)
            ex = existing_by_title[key]
            ex["num_citations"] = f.get("num_citations", ex.get("num_citations", 0))
            if not ex.get("authors") and f.get("authors"):
                ex["authors"] = f["authors"]
            if not ex.get("journal") and f.get("journal"):
                ex["journal"] = f["journal"]
            if not ex.get("url") and f.get("url"):
                ex["url"] = f["url"]
            if not ex.get("volume") and f.get("volume"):
                ex["volume"] = f["volume"]
            if not ex.get("pages") and f.get("pages"):
                ex["pages"] = f["pages"]
            if not ex.get("abstract") and f.get("abstract"):
                ex["abstract"] = f["abstract"]
        else:
            existing.append(f)
            existing_by_title[key] = f
            added += 1

    # Sort by year descending, then citations descending
    existing.sort(key=lambda p: (p.get("year", 0), p.get("num_citations", 0)), reverse=True)
    return existing, added


# ── Highlight selection ───────────────────────────────────────────────────────

def select_highlights(pubs: list, top_n: int = HIGHLIGHT_TOP_N) -> list:
    """
    Set highlight=True on the top_n papers where W. Zhou is first/co-first/senior
    author, ranked by citation count. Resets all existing highlight flags first.
    """
    # Reset
    for p in pubs:
        p["highlight"] = False
        p.pop("author_role", None)

    # Filter eligible
    eligible = []
    for p in pubs:
        is_key, role = is_zhou_first_or_senior(p.get("authors", ""))
        citations = p.get("num_citations", 0) or 0
        if is_key and citations >= MIN_CITATIONS_FOR_HIGHLIGHT:
            p["author_role"] = role
            eligible.append(p)

    # Rank by citations
    eligible.sort(key=lambda p: p.get("num_citations", 0), reverse=True)

    for p in eligible[:top_n]:
        p["highlight"] = True

    roles = [(p["title"][:50], p.get("num_citations", 0), p.get("author_role", ""))
             for p in eligible[:top_n]]
    log(f"  Selected {len(roles)} highlighted publications:")
    for title, cit, role in roles:
        log(f"    [{role:8s}] {cit:>4d} cit  {title}…")

    return pubs


# ── News from publications ────────────────────────────────────────────────────

def generate_news_from_pubs(pubs: list, existing_news: list) -> tuple[list, int]:
    """
    Add news items for papers published in the recent years that aren't already
    represented in existing_news.
    Returns (updated_news_list, num_added).
    """
    current_year = date.today().year
    cutoff_year  = current_year - NEWS_RECENT_YEARS

    # Titles already mentioned in news
    mentioned = set()
    for item in existing_news:
        text = item.get("text", "").lower()
        for p in pubs:
            if len(p.get("title", "")) > 10 and p["title"][:40].lower() in text:
                mentioned.add(p["title"])

    new_items = []
    for p in sorted(pubs, key=lambda x: x.get("year", 0), reverse=True):
        year = p.get("year", 0)
        if year < cutoff_year:
            break
        if not p.get("title") or p["title"] in mentioned:
            continue
        if not p.get("journal") and not p.get("url"):
            continue  # skip papers with no venue info yet

        # Build news text — use url, fall back to doi-based url
        link = p.get("url") or (f'https://doi.org/{p["doi"]}' if p.get("doi") else "")
        title_part = (f'<a href="{link}" target="_blank">{p["title"]}</a>'
                      if link else p["title"])
        venue_part = f' in <em>{p["journal"]}</em>' if p.get("journal") else ""
        # Infer author role for context
        _, role = is_zhou_first_or_senior(p.get("authors", ""))
        role_note = ""
        if role == "senior":
            role_note = " (senior author)"
        elif role in ("first", "co-first"):
            role_note = ""

        text = f'New publication{role_note}: {title_part}{venue_part}.'
        date_str = f"{year}-07-01"  # approximate mid-year date

        item = {"date": date_str, "text": text}
        if link:
            item["url"] = link  # also store as separate field for front-end use
        new_items.append(item)
        mentioned.add(p["title"])

    if new_items:
        all_news = new_items + existing_news
        # Sort newest first
        all_news.sort(key=lambda x: x.get("date", ""), reverse=True)
        return all_news, len(new_items)

    return existing_news, 0


# ── Software detection ────────────────────────────────────────────────────────

# Known tool → details mapping. Add new tools here when they're published.
KNOWN_TOOLS = {
    "scate": {
        "name": "SCATE",
        "badge": "R Package",
        "tagline": "Single-Cell ATAC-seq signal Extraction and Enhancement",
        "desc": "Extracts and enhances signal from sparse single-cell ATAC-seq data by leveraging information across cells and genomic loci. Enables accurate characterization of regulatory activity and cell-type heterogeneity.",
        "github": "https://github.com/zji90/SCATE",
        "paper_url": "https://genomebiology.biomedcentral.com/articles/10.1186/s13059-020-02122-3",
        "paper_label": "Paper (Genome Bio.)"
    },
    "bird": {
        "name": "BIRD",
        "badge": "R / Command Line",
        "tagline": "Big Data Regression for DNase I Hypersensitivity",
        "desc": "Predicts genome-wide chromatin accessibility (DNase-seq signal) from gene expression data. Generates predictions across ~1 million genomic loci using a pre-built regression model.",
        "github": "https://github.com/WeiqiangZhou/BIRD",
        "paper_url": "https://www.nature.com/articles/s41467-017-01188-x",
        "paper_label": "Paper (Nat. Comm.)"
    },
    "pddb": {
        "name": "PDDB",
        "badge": "Web Database",
        "tagline": "Predicted DNase I Hypersensitivity Database",
        "desc": "A comprehensive database of predicted DNase I hypersensitivity profiles for thousands of biological samples, generated using BIRD from gene expression data.",
        "github": "",
        "web_url": "http://jilab.biostat.jhsph.edu/~bsherwo2/bird/index.php",
        "web_label": "Web App"
    },
    "scrat": {
        "name": "SCRAT",
        "badge": "R / Shiny",
        "tagline": "Single-Cell Regulome Analysis Toolbox",
        "desc": "An interactive Shiny toolbox for analyzing single-cell regulome data (scATAC-seq, scChIP-seq). Provides clustering, visualization, and regulatory feature analysis with no coding required.",
        "github": "https://github.com/zji90/SCRAT",
        "paper_url": "https://doi.org/10.1093/bioinformatics/btx315",
        "paper_label": "Paper (Bioinformatics)"
    },
    "funcode": {
        "name": "FUNCODE",
        "badge": "R Package",
        "tagline": "Functional Conservation of Regulatory Elements",
        "desc": "Quantifies functional conservation of human and mouse regulatory elements, enabling comparative analysis of cis-regulatory activity across species.",
        "github": "https://github.com/WeiqiangZhou/FUNCODE",
        "paper_url": "",
        "paper_label": ""
    },
    "hi-plex cut&tag": {
        "name": "Hi-Plex CUT&Tag",
        "badge": "Method / Protocol",
        "tagline": "Combinatorial Chromatin Regulatory Mapping",
        "desc": "A high-throughput multiplexed CUT&Tag approach for simultaneous profiling of multiple chromatin regulatory factors, enabling global mapping of combinatorial epigenetic states.",
        "github": "",
        "paper_url": "",
        "paper_label": ""
    },
    "scmbert": {
        "name": "scMBERT",
        "badge": "Python / Deep Learning",
        "tagline": "Single-Cell Multiomic Representation Learning",
        "desc": "A pre-trained BERT-based deep learning model for single-cell multiomic data representation and prediction, enabling cross-modal inference and cell-type annotation.",
        "github": "",
        "paper_url": "",
        "paper_label": ""
    },
}


def detect_software_from_pubs(pubs: list, existing_software: list) -> tuple[list, int]:
    """
    Scan publication titles for known tool keywords and add any newly detected
    tools to the software list.
    Returns (updated_software_list, num_added).
    """
    existing_names = {s["name"].lower() for s in existing_software}
    added = 0

    for key, tool in KNOWN_TOOLS.items():
        if tool["name"].lower() in existing_names:
            continue  # already present

        # Check if any publication title mentions this tool
        for p in pubs:
            if key in p.get("title", "").lower():
                existing_software.append(tool)
                existing_names.add(tool["name"].lower())
                added += 1
                log(f"  Detected new tool from publications: {tool['name']}")
                break

    return existing_software, added


def load_software() -> list:
    local = REPO_ROOT / SW_PATH
    if local.exists():
        with open(local) as f:
            return json.load(f)
    # Default: the four original tools
    return [
        KNOWN_TOOLS["scate"],
        KNOWN_TOOLS["bird"],
        KNOWN_TOOLS["pddb"],
        KNOWN_TOOLS["scrat"],
    ]


def generate_software_html(software: list) -> str:
    """Re-generate software.html from the software list."""
    cards = ""
    for sw in software:
        github_btn = (f'<a class="sw-link sw-link-primary" href="{sw["github"]}" '
                      f'target="_blank">💻 GitHub</a>'
                      if sw.get("github") else "")
        paper_btn  = (f'<a class="sw-link sw-link-secondary" href="{sw["paper_url"]}" '
                      f'target="_blank">📄 {sw["paper_label"]}</a>'
                      if sw.get("paper_url") else "")
        web_btn    = (f'<a class="sw-link sw-link-primary" href="{sw["web_url"]}" '
                      f'target="_blank">🌐 {sw.get("web_label","Web App")}</a>'
                      if sw.get("web_url") else "")
        links = "\n            ".join(filter(None, [github_btn, paper_btn, web_btn]))
        cards += f"""
        <!-- {sw['name']} -->
        <div class="sw-card">
          <span class="sw-badge">{sw['badge']}</span>
          <p class="sw-name">{sw['name']}</p>
          <p class="sw-tagline">{sw['tagline']}</p>
          <p class="sw-desc">{sw['desc']}</p>
          <div class="sw-links">
            {links}
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Software — Weiqiang Zhou</title>
  <meta name="description"
        content="Open-source computational tools for genomics and single-cell analysis developed by Weiqiang Zhou.">
  <link rel="canonical" href="https://www.weiqiangzhou.com/software.html">
  <link rel="stylesheet" href="./assets/css/style.css">
  <script async src="https://www.googletagmanager.com/gtag/js?id=UA-72222972-2"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{dataLayer.push(arguments);}}
    gtag('js', new Date()); gtag('config', 'UA-72222972-2');
  </script>
</head>
<body>

<nav class="nav">
  <div class="nav-inner">
    <a class="nav-brand" href="/">Weiqiang Zhou</a>
    <ul class="nav-links">
      <li><a href="/">Home</a></li>
      <li><a href="./publications.html">Publications</a></li>
      <li><a href="./software.html" class="active">Software</a></li>
      <li><a href="https://github.com/WeiqiangZhou" target="_blank">GitHub</a></li>
      <li><a href="https://scholar.google.com/citations?user=BDB3l1oAAAAJ&hl=en" target="_blank">Google Scholar</a></li>
    </ul>
  </div>
</nav>

<main class="main">
  <div class="page-header">
    <div class="container">
      <h1>Software &amp; Tools</h1>
      <p>Open-source computational tools for genomics and single-cell analysis</p>
    </div>
  </div>

  <section class="section">
    <div class="container">
      <div class="software-grid">
        {cards}
      </div>
      <p style="margin-top:28px;font-size:0.87rem;color:var(--text-muted);">
        All projects and source code are available at
        <a href="https://github.com/WeiqiangZhou" target="_blank">github.com/WeiqiangZhou</a>.
      </p>
    </div>
  </section>
</main>

<footer class="footer">
  <div class="container">
    <div class="footer-links">
      <a href="https://scholar.google.com/citations?user=BDB3l1oAAAAJ&hl=en" target="_blank">Google Scholar</a>
      <a href="https://github.com/WeiqiangZhou" target="_blank">GitHub</a>
      <a href="https://twitter.com/kenzhou86" target="_blank">Twitter / X</a>
      <a href="mailto:kenzhou86@gmail.com">Email</a>
    </div>
    <p class="footer-copy">
      &copy; <span id="year"></span> Weiqiang Zhou &middot;
      Johns Hopkins Bloomberg School of Public Health
    </p>
  </div>
</footer>
<script>document.getElementById('year').textContent = new Date().getFullYear();</script>
</body>
</html>"""


# ── GitHub API ───────────────────────────────────────────────────────────────

import requests as _req

def gh_get_sha(path: str) -> str | None:
    r = _req.get(f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{path}",
                 headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
                 params={"ref": GH_BRANCH})
    return r.json().get("sha") if r.status_code == 200 else None


def gh_put(path: str, content: str | bytes, sha: str | None, message: str):
    if isinstance(content, str):
        content = content.encode()
    payload = {
        "message":   message,
        "content":   base64.b64encode(content).decode(),
        "branch":    GH_BRANCH,
        "committer": COMMITTER,
    }
    if sha:
        payload["sha"] = sha
    r = _req.put(f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{path}",
                 json=payload,
                 headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                           "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()


def push_file(rel_path: str, content: str, label: str, dry_run: bool):
    if dry_run:
        log(f"  [dry-run] Would push: {rel_path}")
        return
    sha = gh_get_sha(rel_path)
    gh_put(rel_path, content, sha, label)
    log(f"  ✅ Pushed: {rel_path}")


# ── Persistence helpers ───────────────────────────────────────────────────────

def load_json(rel_path: str, default) -> any:
    local = REPO_ROOT / rel_path
    if local.exists():
        with open(local) as f:
            return json.load(f)
    return default


def save_json(rel_path: str, data: any):
    local = REPO_ROOT / rel_path
    local.parent.mkdir(parents=True, exist_ok=True)
    with open(local, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def save_text(rel_path: str, text: str):
    local = REPO_ROOT / rel_path
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(text, encoding="utf-8")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Update weiqiangzhou.com website data.")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Compute changes but don't push to GitHub or write files")
    parser.add_argument("--local",        action="store_true",
                        help="Write local files but don't push to GitHub")
    parser.add_argument("--skip-scholar", action="store_true",
                        help="Skip Google Scholar fetch; only re-compute highlights/news")
    args = parser.parse_args()

    if not GITHUB_TOKEN and not (args.dry_run or args.local):
        print("ERROR: GITHUB_TOKEN not set in agent/.env")
        sys.exit(1)

    today        = date.today().isoformat()
    changed_files = {}   # rel_path → content string to push

    # ── 1. Load existing data ─────────────────────────────────────────────────
    existing_pubs = load_json(PUBS_PATH, [])
    existing_news = load_json(NEWS_PATH, [])
    existing_sw   = load_software()

    log(f"Loaded {len(existing_pubs)} existing publications, "
        f"{len(existing_news)} news items, {len(existing_sw)} software tools")

    # ── 2. Fetch from Google Scholar ──────────────────────────────────────────
    if not args.skip_scholar:
        log("Fetching from Google Scholar…")
        fetched = fetch_from_scholar(SCHOLAR_ID)

        pubs, num_new = merge_publications(existing_pubs, fetched)
        log(f"Merged: {num_new} new publications added ({len(pubs)} total)")
    else:
        log("Skipping Google Scholar fetch (--skip-scholar)")
        pubs = existing_pubs
        num_new = 0

    # ── 3. Select highlighted publications ───────────────────────────────────
    log("Selecting highlighted publications (first/co-first/senior author, by citations)…")
    pubs = select_highlights(pubs, top_n=HIGHLIGHT_TOP_N)

    pubs_json = json.dumps(pubs, indent=2, ensure_ascii=False)
    save_json(PUBS_PATH, pubs)
    changed_files[PUBS_PATH] = pubs_json

    # ── 4. Auto-generate news from recent publications ────────────────────────
    log("Generating news from recent publications…")
    news, num_news_added = generate_news_from_pubs(pubs, existing_news)
    log(f"  Added {num_news_added} news item(s)")

    news_json = json.dumps(news, indent=2, ensure_ascii=False)
    save_json(NEWS_PATH, news)
    changed_files[NEWS_PATH] = news_json

    # ── 5. Detect new software tools ──────────────────────────────────────────
    log("Scanning publications for new software tools…")
    sw_list, num_sw_added = detect_software_from_pubs(pubs, existing_sw)
    log(f"  {num_sw_added} new tool(s) detected")

    sw_json = json.dumps(sw_list, indent=2, ensure_ascii=False)
    save_json(SW_PATH, sw_json)

    # Regenerate software.html
    log("Regenerating software.html…")
    sw_html = generate_software_html(sw_list)
    save_text("software.html", sw_html)
    changed_files["software.html"] = sw_html
    changed_files[SW_PATH] = sw_json

    # ── 6. Push to GitHub ─────────────────────────────────────────────────────
    if args.dry_run:
        log("\n[dry-run] Summary of changes (not pushed):")
        for path in changed_files:
            log(f"  • {path}")
        return

    if args.local:
        log("Files written locally. Skipping GitHub push.")
        return

    log(f"\nPushing {len(changed_files)} file(s) to GitHub…")
    date_str = datetime.now().strftime("%Y-%m-%d")
    for rel_path, content in changed_files.items():
        try:
            push_file(rel_path, content,
                      f"Auto-update {rel_path} [{date_str}]",
                      dry_run=False)
        except Exception as e:
            log(f"  ❌ Failed to push {rel_path}: {e}")

    log("\nUpdate complete.")
    log(f"  Publications: {len(pubs)} total, {HIGHLIGHT_TOP_N} highlighted")
    log(f"  News items:   {len(news)} total, {num_news_added} new")
    log(f"  Software:     {len(sw_list)} tools, {num_sw_added} newly detected")


if __name__ == "__main__":
    main()
