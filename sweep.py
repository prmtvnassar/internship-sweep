"""
Opportunity sweep: polls public job-board APIs for hardware/FPGA/ASIC/
photonics/quantum roles at firms known to run international-friendly
internship programs, and posts new matches to a private Discord channel.

This replaces the one-off research sweep with something that actually
keeps running. Greenhouse publishes a public, unauthenticated JSON API
per company; this hits it directly. No scraping, no login, nothing
fragile. Lever is checked too if a company uses it instead.

Stdlib only.

Env vars:
    DISCORD_OPPORTUNITIES_WEBHOOK   Discord webhook URL. If unset, logs
                                    what it would have posted and exits 0.
    Every run rewrites state/opps.json, the list of open postings. Postings that
    closed (the board stopped listing them, or the deadline passed) are removed.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


STATE_PATH = Path(__file__).parent / "state" / "opportunities_seen.json"
MAX_POSTS_PER_RUN = 80
MAX_SEEN = 2000
USER_AGENT = "internship-sweep/1.0 (+https://github.com/prmtvnassar/internship-sweep)"

KEYWORDS = [
    "fpga", "hardware engineer", "asic", "photonic", "hardware intern",
    "asic physical design", "optical engineer", "rf engineer",
    "signal integrity", "embedded systems", "chip design", "vlsi",
    "analog design", "quantum engineer", "silicon photonics",
    "photonics engineer", "quantum hardware", "quantum computing engineer",
    "quantum error correction", "integrated photonics", "quantum architect",
]

# Bare "quantum" and "silicon" were dropped: they matched sales, product,
# and marketing titles ("Sales Enablement Manager, Quantum Networking")
# that have nothing to do with an engineering role. This excludes a title
# even if it hit a keyword above, since a compound keyword can still land
# inside a non-technical title (e.g. "Account Executive, Quantum Hardware
# Sales").
EXCLUDE_PATTERNS = [
    "sales", "account executive", "business development", "marketing",
    "product manager", "recruiter", "talent", "customer success",
    "partnerships", "channel manager",
]

# Confirmed live via a public job-board API as of 2026-10-02. Add more
# as they're found; a 404 from a guessed token just means no board under
# that token, see the comment at the bottom for ones tried and dropped.
GREENHOUSE_BOARDS = {
    "imc": "IMC",
    "janestreet": "Jane Street",
    "jumptrading": "Jump Trading",
    "virtu": "Virtu Financial",
    "riverlane": "Riverlane",
    "psiquantum": "PsiQuantum",
    "ionq": "IonQ",
    "lightmatter": "Lightmatter",
    # Found by probing each employer's real careers page (2026-10-07).
    "xscapephotonicsinc": "Xscape Photonics",
    "graphcore": "Graphcore",
    "tenstorrent": "Tenstorrent",
    "sambanovasystems": "SambaNova",
    "asteralabs": "Astera Labs",
    "optiverus": "Optiver",
    "wehrtyou": "Hudson River Trading",
    "quberesearchandtechnologies": "QRT",
    "drweng": "DRW",
    # Tennessee and interest-picked startups (added 2026-11-17).
    "xai": "xAI",
    "kairospower": "Kairos Power",
    "oklo": "Oklo",
    "coreweave": "CoreWeave",
    "togetherai": "Together AI",
    "astranis": "Astranis",
    "relativity": "Relativity Space",
    "vast": "Vast (space stations)",
    "rocketlab": "Rocket Lab",
    "spacex": "SpaceX",
    "redwoodmaterials": "Redwood Materials",
}

LEVER_BOARDS = {
    "rigetti": "Rigetti Computing",
    "atomcomputing": "Atom Computing",
    "alice-bob": "Alice & Bob",
    "eliyan": "Eliyan",
}

# Quantinuum only answers on Lever's EU host.
LEVER_EU_BOARDS = {"quantinuum": "Quantinuum"}

ASHBY_BOARDS = {
    "neurophos": "Neurophos",
    "lumai": "Lumai",
    "cerebras": "Cerebras",
    "etched": "Etched",
    "d-Matrix": "d-Matrix",
    "axelera": "Axelera AI",
    "salience-labs": "Salience Labs",
    # Interest-picked startups (added 2026-11-17).
    "helion": "Helion",
    "extropic": "Extropic",
    "normalcomputing": "Normal Computing",
    "matx": "MatX",
    "sandboxaq": "SandboxAQ",
    "crusoe": "Crusoe",
    "lambda": "Lambda",
    "modal": "Modal",
    "saronic": "Saronic",
    "runpod": "RunPod",
    "sfcompute": "SF Compute",
    "primeintellect": "Prime Intellect",
    "friendliai": "FriendliAI",
}

# Also found but not polled: Workable boards for Infleqtion, Quandela,
# Universal Quantum and Nu Quantum rate-limit hard. Trading firms whose boards
# had no hardware titles at all (Tower, Squarepoint, Man, Akuna, Flow Traders,
# XTX, Da Vinci) are skipped as noise.

# DRW, QRT, G-Research, Optiver, IQM, Pasqal, Orca Computing, Quantinuum,
# Oxford Ionics, Nu Quantum, Xanadu, Ayar Labs, Celestial AI, Neurophos,
# Rockley Photonics, and POET either have no public Greenhouse/Lever board
# under a guessable token, or the board exists but is empty. These need a
# manual check against their own careers pages periodically.


INTERN_TITLE = re.compile(r"\bintern(s|ship|ships)?\b|co-?op|student|trainee|placement|apprentic", re.I)
FULL_TIME = re.compile(r"full[- ]?time|new grad|graduate (program|role|scheme|hire)|permanent", re.I)


def keep_role(title: str, location: str) -> bool:
    """Only internships, US and abroad (US roles added 2026-11-17). Full-time isn't the goal."""
    return bool(INTERN_TITLE.search(title) and not FULL_TIME.search(title))


def _day(stamp, ms: bool = False) -> str:
    """Unix timestamp (seconds, or milliseconds with ms=True) to an ISO date."""
    try:
        return datetime.date.fromtimestamp(int(stamp) / (1000 if ms else 1)).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


# Startups where any technical internship counts, not just hardware.
OPEN_BOARDS = {
    "xAI", "Kairos Power", "Oklo", "CoreWeave", "Together AI", "Astranis", "Relativity Space",
    "Vast (space stations)", "Rocket Lab", "SpaceX", "Redwood Materials", "Helion", "Extropic",
    "Normal Computing", "MatX", "SandboxAQ", "Crusoe", "Lambda", "Modal", "Saronic", "RunPod",
    "SF Compute", "Prime Intellect", "FriendliAI",
}

# Tennessee roles of any technical kind (", TN" avoids Franklin, MA and the like).
TENNESSEE = re.compile(r",\s*TN\b|Tennessee|Oak Ridge|Nashville|Knoxville|Chattanooga|Memphis", re.I)
TECH_TITLE = re.compile(
    r"engineer|software|hardware|electrical|data|machine learning|\bml\b|\bai\b|research|"
    r"robotic|science|scientist|developer|firmware|embedded|quantum|nuclear|energy|computing",
    re.I,
)


# Software engineering and developer roles are out (2026-11-17), unless the
# title is also hardware work (FPGA developer, embedded, firmware, software-defined radio hardware).
SOFTWARE_TITLE = re.compile(
    r"software|developer|\bdev\b|full[- ]?stack|front[- ]?end|back[- ]?end|\bweb\b|devops|"
    r"\bsde\b|\bswe\b|mobile|\bios\b|android|programmer|site reliability|\bsre\b",
    re.I,
)


def is_software(title: str) -> bool:
    return bool(SOFTWARE_TITLE.search(title)) and not HARDWARE_TITLE.search(title)


def wanted(title: str, location: str, company: str) -> bool:
    if is_software(title):
        return False
    if not keep_role(title, location) or any(x in title.lower() for x in EXCLUDE_PATTERNS):
        return False
    if matches_keywords(title):
        return True
    return bool(TECH_TITLE.search(title)) and (company in OPEN_BOARDS or bool(TENNESSEE.search(location)))


def matches_keywords(text: str) -> bool:
    low = text.lower()
    if any(x in low for x in EXCLUDE_PATTERNS):
        return False
    return any(k in low for k in KEYWORDS)


def fetch_greenhouse(board_token: str, company_name: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    data = json.loads(fetch(url))
    items = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        location = (job.get("location") or {}).get("name", "")
        if wanted(title, location, company_name):
            items.append({
                "id": str(job.get("id")),
                "title": re.sub(r"\s+", " ", title),
                "link": job.get("absolute_url", ""),
                "source": company_name,
                "location": location,
                "posted": (job.get("first_published") or "")[:10],
                "deadline": (job.get("application_deadline") or "")[:10],
                "ref": str(job.get("id")),
            })
    return items


def fetch_lever(board_token: str, company_name: str, host: str = "api.lever.co") -> list[dict]:
    url = f"https://{host}/v0/postings/{board_token}?mode=json"
    data = json.loads(fetch(url))
    items = []
    for job in data:
        title = job.get("text", "")
        location = (job.get("categories") or {}).get("location", "")
        if wanted(title, location, company_name):
            items.append({
                "id": f"lv-{job.get('id')}",
                "title": re.sub(r"\s+", " ", title),
                "link": job.get("hostedUrl", ""),
                "source": company_name,
                "location": location,
                "posted": _day(job.get("createdAt"), ms=True),
                "ref": str(job.get("id")),
            })
    return items


def fetch_ashby(board_token: str, company_name: str) -> list[dict]:
    data = json.loads(fetch(f"https://api.ashbyhq.com/posting-api/job-board/{board_token}"))
    items = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        location = job.get("location") or ""
        if wanted(title, location, company_name):
            items.append({
                "id": f"ab-{job.get('id')}",
                "title": re.sub(r"\s+", " ", title),
                "link": job.get("jobUrl", ""),
                "source": company_name,
                "location": location,
                "posted": (job.get("publishedAt") or "")[:10],
                "ref": str(job.get("id")),
            })
    return items


# Community-maintained internship lists that publish machine-readable data
# (the same listings.json schema; bots keep them current). Simplify has ~47k
# stars. They are internship lists by nature; US and non-US roles both qualify.
LISTING_REPOS = {
    "SimplifyJobs/Summer2027-Internships": "Simplify list",
    "vanshb03/Summer2027-Internships": "vanshb03 list",
}

# Companies already polled directly above, which is fresher than a list.
COVERED = {
    "imc", "jane street", "jump trading", "virtu", "virtu financial",
    "riverlane", "psiquantum", "ionq", "lightmatter", "rigetti", "rigetti computing",
}

# Their Canadian postings require enrolment at a Canadian school (checked by
# reading each posting, 2026-10-07), so they aren't open to a student at a US
# university. Qualcomm, Marvell, Ciena and others in Canada don't say this.
REQUIRES_CANADIAN_SCHOOL = {"amd", "nokia", "lumentum", "royal bank of canada"}

HARDWARE_TITLE = re.compile(
    r"fpga|asic|photon|quantum|\brtl\b|vlsi|silicon|optic|analog|\brf\b|signal integrity|"
    r"design verification|semiconductor|\bchip|embedded|firmware|layout|physical design|"
    r"circuit|hardware|electrical|electronics|mixed[- ]signal|power electronics|packaging",
    re.I,
)

NON_US = re.compile(
    r"\b(UK|United Kingdom|England|Scotland|Wales|Ireland|Canada|Germany|France|Netherlands|"
    r"Switzerland|Singapore|Japan|Israel|Australia|India|China|Taiwan|Korea|Sweden|Norway|"
    r"Denmark|Finland|Belgium|Spain|Italy|Austria|Poland|Egypt|UAE|Saudi|Hong Kong|Mexico|"
    r"Brazil|Czech|Romania|Portugal|Greece|Turkey|Vietnam|Malaysia|Thailand|Philippines|"
    r"Indonesia|Luxembourg|Estonia|Lithuania|New Zealand|South Africa)\b|"
    r"\b(London|Amsterdam|Zurich|Munich|Paris|Berlin|Toronto|Vancouver|Montreal|Dublin|"
    r"Dubai|Tokyo|Bangalore|Cambridge, UK|Bristol|Cardiff|Edinburgh|Shanghai|Beijing|Shenzhen|"
    r"Seoul|Taipei|Sydney|Melbourne|Tel Aviv|Haifa|Stockholm|Copenhagen|Oslo|Helsinki|Brussels|"
    r"Leuven|Madrid|Barcelona|Milan|Vienna|Warsaw|Prague|Eindhoven|Grenoble|Lausanne|Geneva|"
    r"Manchester|Oxford|Belfast|Glasgow|Hamburg|Frankfurt|Dresden|Cairo|Riyadh|Abu Dhabi|Ottawa)\b",
    re.I,
)


def fetch_listing_repo(repo: str, label: str) -> list[dict]:
    url = f"https://raw.githubusercontent.com/{repo}/dev/.github/scripts/listings.json"
    items = []
    for job in json.loads(fetch(url)):
        if not job.get("active") or not job.get("is_visible", True):
            continue
        # Simplify tags terms ("Summer 2027"); vanshb03 has no terms, so fall
        # back to "posted within the last year".
        terms = job.get("terms")
        if terms is not None:
            if not any("2027" in t for t in terms):
                continue
        elif time.time() - int(job.get("date_posted", 0)) > 365 * 86400:
            continue
        title = job.get("title", "")
        company = job.get("company_name", "")
        if company.lower() in COVERED or not job.get("url"):
            continue
        places = [p for p in job.get("locations", []) if p]
        in_tn = any(TENNESSEE.search(p) for p in places)
        relevant = HARDWARE_TITLE.search(title) or (in_tn and TECH_TITLE.search(title))
        if not relevant or is_software(title) or any(x in title.lower() for x in EXCLUDE_PATTERNS):
            continue
        if company.lower() in REQUIRES_CANADIAN_SCHOOL:
            places = [p for p in places if "canada" not in p.lower()]
        if not places or FULL_TIME.search(title):
            continue
        detail = ", ".join(places)
        if job.get("sponsorship") not in (None, "Other"):
            detail += f" | {job['sponsorship']}"
        items.append({
            "id": f"sj-{job.get('id')}",
            "title": re.sub(r"\s+", " ", f"{company} - {title}"),
            "link": job["url"],
            "source": label,
            "location": detail,
            "posted": _day(job.get("date_posted")),
            "auth": "US" if not any(NON_US.search(p) for p in places) else "Host-country-only",
        })
    return items


def load_seen() -> list[str]:
    if not STATE_PATH.exists():
        return []
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_seen(seen: list[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(seen[-MAX_SEEN:], indent=2), encoding="utf-8")


def post_to_discord(webhook: str, item: dict) -> None:
    payload = {
        "embeds": [{
            "title": item["title"][:256],
            "url": item["link"],
            "description": item["location"],
            "footer": {"text": item["source"]},
        }]
    }
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


STORE_PATH = Path(__file__).parent / "state" / "opps.json"
STORE_FIELDS = ("id", "title", "source", "location", "deadline", "posted", "auth", "tn")


def load_store() -> dict:
    if STORE_PATH.exists():
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    return {"items": {}}


def save_store(store: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(store, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def merge_store(store: dict, current: list[dict], ok_sources: set[str], today: str) -> list[dict]:
    """Upsert every posting seen now; drop the ones that closed. Returns the dropped records.

    Closed means: its source answered this run and no longer lists it, or its deadline passed.
    A source that failed to answer keeps its rows, so an outage never empties the page.
    """
    items = store.setdefault("items", {})
    live = set()
    for it in current:
        live.add(it["link"])
        rec = items.get(it["link"]) or {"first_seen": today}
        rec.update({k: it.get(k) or "" for k in STORE_FIELDS if k != "tn"})
        rec["tn"] = bool(it.get("tn"))
        rec["last_seen"] = today
        items[it["link"]] = rec
    closed = []
    for link, rec in list(items.items()):
        gone = rec["source"] in ok_sources and link not in live
        expired = bool(rec.get("deadline")) and rec["deadline"] < today
        if gone or expired:
            closed.append({"link": link, **items.pop(link)})
    return closed


def load_env() -> None:
    """Local runs: read KEY=value lines from the repo's gitignored .env without overriding real env vars."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and value.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"')


def main() -> int:
    load_env()
    webhook = os.environ.get("DISCORD_OPPORTUNITIES_WEBHOOK", "").strip()
    seen = load_seen()
    today = datetime.date.today().isoformat()
    new_items: list[dict] = []
    ok_sources: set[str] = set()

    jobs = (
        [(name, lambda t=t, n=name: fetch_greenhouse(t, n)) for t, name in GREENHOUSE_BOARDS.items()]
        + [(name, lambda t=t, n=name: fetch_lever(t, n)) for t, name in LEVER_BOARDS.items()]
        + [(name, lambda t=t, n=name: fetch_lever(t, n, host="api.eu.lever.co")) for t, name in LEVER_EU_BOARDS.items()]
        + [(name, lambda t=t, n=name: fetch_ashby(t, n)) for t, name in ASHBY_BOARDS.items()]
        + [(label, lambda r=repo, l=label: fetch_listing_repo(r, l)) for repo, label in LISTING_REPOS.items()]
    )
    for name, fn in jobs:
        try:
            new_items.extend(fn())
            ok_sources.add(name)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{name} fetch failed: {exc}", file=sys.stderr)

    unique: dict[str, dict] = {}
    urls: set[str] = set()
    for i in new_items:
        # Both lists carry many of the same postings: dedupe by id and by link.
        if i["id"] in unique or i["link"] in urls:
            continue
        i["tn"] = bool(TENNESSEE.search(i["location"]))
        unique[i["id"]] = i
        urls.add(i["link"])

    store = load_store()
    closed = merge_store(store, list(unique.values()), ok_sources, today)
    save_store(store)
    print(f"Opps store: {len(store['items'])} open, {len(closed)} closed and removed.")

    seen_set = set(seen)
    to_post = [i for i in unique.values() if i["id"] not in seen_set]
    if not to_post:
        print("Nothing new.")
        return 0

    if not webhook:
        print(f"DISCORD_OPPORTUNITIES_WEBHOOK not set. Would have posted {len(to_post)} item(s):")
        for i in to_post:
            print(f"  [{i['source']}] {i['title']} ({i['location']}) -> {i['link']}")
        return 0

    for item in to_post[:MAX_POSTS_PER_RUN]:
        try:
            post_to_discord(webhook, item)
        except OSError as exc:
            print(f"Discord post failed for {item['title']}: {exc}", file=sys.stderr)
            continue
        seen.append(item["id"])
        save_seen(seen)  # per post, so a crash mid-run can't cause reposts
        print(f"Posted [{item['source']}] {item['title']}")
        time.sleep(1)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
