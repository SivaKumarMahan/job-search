#!/usr/bin/env python3
"""
Daily job finder.

Pulls fresh job posts from job-aggregator APIs (no scraping of LinkedIn/Naukri),
scores them against profile.yaml, removes ones already seen, and writes a
Markdown report that the GitHub Actions workflow posts as an Issue.

Sources (each enabled only if its secret is set):
  - JSearch (RapidAPI)  -> aggregates Google for Jobs: LinkedIn, Naukri, Indeed,
                           Glassdoor, company career pages, ...
      env: RAPIDAPI_KEY
  - Adzuna (India)      -> env: ADZUNA_APP_ID, ADZUNA_APP_KEY
Optional:
  - Claude re-ranking   -> env: ANTHROPIC_API_KEY
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "data" / "seen.json"
REPORT_DIR = ROOT / "reports"
SEEN_RETENTION_DAYS = 60
TIMEOUT = 30
# JSearch v5 moved job search from /search to /search-v2 ("Endpoint '/search' does not exist").
JSEARCH_URL = "https://jsearch.p.rapidapi.com/search-v2"

IST = timezone(timedelta(hours=5, minutes=30))


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def fetch_jsearch(cfg: dict) -> list[dict]:
    key = os.getenv("RAPIDAPI_KEY")
    if not key:
        log("JSearch: RAPIDAPI_KEY not set, skipping")
        return []

    date_posted = "today" if cfg.get("max_days_old", 1) <= 1 else "3days"
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    jobs: list[dict] = []

    # one call per query; locations are folded into the query text to save quota
    loc_text = " or ".join(cfg.get("locations", []))
    for q in cfg["queries"]:
        query = f"{q} in {loc_text}" if loc_text else q
        params = {
            "query": query,
            "num_pages": "1",
            "country": cfg.get("country", "in"),
            "date_posted": date_posted,
        }
        try:
            r = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=TIMEOUT)
        except Exception as e:  # network error: keep going with the next query
            log(f"JSearch: query '{query}' failed: {e}")
            continue
        if not r.ok:
            # log the API's own message (never the key) so failures are easy to diagnose
            body = re.sub(r"\s+", " ", r.text or "")[:300]
            if r.status_code == 404 and "no " in body.lower() and "found" in body.lower():
                log(f"JSearch: '{query}' -> 0 jobs (API says: {body})")
            else:
                log(f"JSearch: query '{query}' failed: HTTP {r.status_code}: {body}")
            continue
        try:
            payload = r.json().get("data") or []
        except ValueError as e:
            log(f"JSearch: query '{query}' returned invalid JSON: {e}")
            continue
        # JSearch v5 returns {"data": {"jobs": [...]}}; older versions returned {"data": [...]}
        data = payload.get("jobs", []) if isinstance(payload, dict) else payload
        data = data or []

        log(f"JSearch: '{query}' -> {len(data)} jobs")
        for j in data:
            city = ", ".join(x for x in [j.get("job_city"), j.get("job_state")] if x)
            where = city or j.get("job_location") or ("Remote" if j.get("job_is_remote") else "")
            jobs.append({
                "id": f"jsearch:{j.get('job_id')}",
                "title": j.get("job_title") or "",
                "company": j.get("employer_name") or "",
                "location": where,
                "remote": bool(j.get("job_is_remote")),
                "url": j.get("job_apply_link") or j.get("job_google_link") or "",
                "source": j.get("job_publisher") or "JSearch",
                "posted": j.get("job_posted_at_datetime_utc") or "",
                "description": j.get("job_description") or "",
            })
    return jobs


def fetch_adzuna(cfg: dict) -> list[dict]:
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        log("Adzuna: ADZUNA_APP_ID / ADZUNA_APP_KEY not set, skipping")
        return []

    country = cfg.get("country", "in")
    jobs: list[dict] = []
    for q in cfg["queries"]:
        for loc in cfg.get("locations", []) or [""]:
            params = {
                "app_id": app_id,
                "app_key": app_key,
                "what": q,
                "max_days_old": cfg.get("max_days_old", 1),
                "results_per_page": 50,
                "content-type": "application/json",
            }
            if loc:
                params["where"] = loc
            try:
                r = requests.get(
                    f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                    params=params, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.json().get("results", []) or []
            except Exception as e:
                log(f"Adzuna: '{q}' @ '{loc}' failed: {e}")
                continue

            log(f"Adzuna: '{q}' @ '{loc}' -> {len(data)} jobs")
            for j in data:
                jobs.append({
                    "id": f"adzuna:{j.get('id')}",
                    "title": j.get("title") or "",
                    "company": (j.get("company") or {}).get("display_name", ""),
                    "location": (j.get("location") or {}).get("display_name", ""),
                    "remote": False,
                    "url": j.get("redirect_url") or "",
                    "source": "Adzuna",
                    "posted": j.get("created") or "",
                    "description": j.get("description") or "",
                })
    return jobs


# --------------------------------------------------------------------------- #
# Filtering / scoring
# --------------------------------------------------------------------------- #
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip().lower()


def contains(text: str, term: str) -> bool:
    # word-boundary match so "ecs" doesn't match "checks"
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text) is not None


def fingerprint(job: dict) -> str:
    """Same job posted on several boards -> same fingerprint."""
    key = f"{norm(job['title'])}|{norm(job['company'])}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def score_job(job: dict, cfg: dict) -> tuple[int, list[str]] | None:
    title, desc = norm(job["title"]), norm(job["description"])
    if any(contains(title, t) for t in cfg.get("exclude_title", [])):
        return None
    if any(contains(desc, t) for t in cfg.get("exclude_description", [])):
        return None

    score, hits = 0, []
    for skill, w in (cfg.get("skills") or {}).items():
        if contains(title, skill) or contains(desc, skill):
            score += int(w)
            hits.append(skill)
    for word, w in (cfg.get("title_bonus") or {}).items():
        if contains(title, word):
            score += int(w)
    return score, hits


def location_ok(job: dict, cfg: dict) -> bool:
    locs = [l.lower() for l in cfg.get("locations", [])]
    if not locs:
        return True
    if job.get("remote") and cfg.get("include_remote", True):
        return True
    loc = (job.get("location") or "").lower()
    if not loc:  # unknown location: keep, let the human decide
        return True
    aliases = {"bangalore": ["bengaluru"], "bengaluru": ["bangalore"],
               "gurgaon": ["gurugram"], "gurugram": ["gurgaon"]}
    for l in locs:
        if l in loc or any(a in loc for a in aliases.get(l, [])):
            return True
    return "remote" in loc and cfg.get("include_remote", True)


# --------------------------------------------------------------------------- #
# Optional Claude re-ranking
# --------------------------------------------------------------------------- #
def to_int(value) -> int | None:
    """Claude may return the fit as 8, "8" or "8/10"; keep sorting safe."""
    m = re.match(r"\s*(\d+)", str(value)) if value is not None else None
    return int(m.group(1)) if m else None


def ai_rerank(jobs: list[dict], cfg: dict) -> None:
    ai = cfg.get("ai_summary") or {}
    key = os.getenv("ANTHROPIC_API_KEY")
    if not (ai.get("enabled") and key and jobs):
        return

    top = jobs[: int(ai.get("top_n", 15))]
    payload_jobs = [
        {"i": i, "title": j["title"], "company": j["company"],
         "location": j["location"], "description": j["description"][:2500]}
        for i, j in enumerate(top)
    ]
    prompt = (
        "You are screening job posts for this candidate:\n"
        f"{ai.get('resume_summary', '')}\n\n"
        "For each job, return a fit score 1-10 and ONE short line (max 20 words) "
        "on why it fits or what is missing. Reply with JSON only: "
        '[{"i": 0, "fit": 8, "why": "..."}]\n\n'
        f"Jobs:\n{json.dumps(payload_jobs, ensure_ascii=False)}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": ai.get("model", "claude-haiku-4-5"), "max_tokens": 2000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\[.*\]", text, re.S)
        for item in json.loads(m.group(0) if m else "[]"):
            idx = item.get("i")
            if isinstance(idx, int) and 0 <= idx < len(top):
                top[idx]["ai_fit"] = to_int(item.get("fit"))
                top[idx]["ai_why"] = str(item.get("why") or "")
        log(f"Claude: re-ranked {len(top)} jobs")
    except Exception as e:
        log(f"Claude re-rank skipped: {e}")


# --------------------------------------------------------------------------- #
# Seen-jobs state
# --------------------------------------------------------------------------- #
def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_seen(seen: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=0, sort_keys=True))


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def build_report(jobs: list[dict], stats: dict, today: str) -> str:
    lines = [f"# Job matches for {today}", ""]
    lines.append(
        f"Fetched **{stats['fetched']}** posts, **{stats['new']}** new, "
        f"**{len(jobs)}** match your profile."
    )
    lines.append("")
    if not jobs:
        lines.append("No new matching jobs today.")
        return "\n".join(lines) + "\n"

    has_ai = any("ai_fit" in j for j in jobs)
    header = "| # | Score | Role | Company | Location | Source | Matched skills |"
    sep = "|---|---|---|---|---|---|---|"
    if has_ai:
        header += " AI fit | Why |"
        sep += "---|---|"
    lines += [header, sep]
    for n, j in enumerate(jobs, 1):
        # <...> keeps URLs with spaces or parentheses from breaking the link
        role = f"[{md_escape(j['title'])}](<{j['url']}>)" if j["url"] else md_escape(j["title"])
        row = (f"| {n} | {j['score']} | {role} | {md_escape(j['company'])} | "
               f"{md_escape(j['location']) or '-'} | {md_escape(j['source'])} | "
               f"{', '.join(j['hits'][:6])} |")
        if has_ai:
            fit = j.get("ai_fit")
            row += f" {fit if fit is not None else '-'} | {md_escape(j.get('ai_why', ''))} |"
        lines.append(row)
    lines += ["", "_Apply links open the original posting. Review before applying._"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
def main() -> int:
    cfg = yaml.safe_load((ROOT / "profile.yaml").read_text())
    today = datetime.now(IST).strftime("%Y-%m-%d")

    raw = fetch_jsearch(cfg) + fetch_adzuna(cfg)
    if not os.getenv("RAPIDAPI_KEY") and not os.getenv("ADZUNA_APP_ID"):
        log("ERROR: no job source configured. Add RAPIDAPI_KEY and/or ADZUNA_APP_ID/ADZUNA_APP_KEY secrets.")
        return 1

    seen = load_seen()
    now_iso = datetime.now(timezone.utc).isoformat()

    by_fp: dict[str, dict] = {}
    for job in raw:
        fp = fingerprint(job)
        if fp in by_fp:  # same role/company from another board: keep the longer description
            if len(job["description"]) > len(by_fp[fp]["description"]):
                by_fp[fp].update(description=job["description"])
            continue
        job["fp"] = fp
        by_fp[fp] = job

    new_jobs = [j for fp, j in by_fp.items() if fp not in seen]

    matched = []
    for j in new_jobs:
        if not location_ok(j, cfg):
            continue
        res = score_job(j, cfg)
        if res is None:
            continue
        j["score"], j["hits"] = res
        if j["score"] >= cfg.get("min_score", 0):
            matched.append(j)

    matched.sort(key=lambda j: j["score"], reverse=True)
    matched = matched[: int(cfg.get("max_results", 30))]

    ai_rerank(matched, cfg)
    if any("ai_fit" in j for j in matched):
        matched.sort(key=lambda j: (j.get("ai_fit") or 0, j["score"]), reverse=True)

    # mark everything fetched as seen (even non-matches) so we don't re-score it tomorrow
    for fp in by_fp:
        seen.setdefault(fp, now_iso)
    save_seen(seen)

    stats = {"fetched": len(raw), "new": len(new_jobs)}
    report = build_report(matched, stats, today)
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / f"{today}.md").write_text(report)
    (ROOT / "latest_report.md").write_text(report)

    # outputs for the workflow
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"match_count={len(matched)}\n")
            f.write(f"report_date={today}\n")

    log(f"Done: {len(matched)} matches written to reports/{today}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
