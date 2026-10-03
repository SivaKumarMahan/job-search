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

import csv
import hashlib
import html
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
EXPORT_DIR = ROOT / "out"          # uploaded as a workflow artifact, not committed
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

    # locations are folded into the query text to save quota.
    # Each call returns at most 10 jobs; pages_per_query > 1 follows the API's
    # cursor to fetch more, and every extra page costs one more API call.
    loc_text = " or ".join(cfg.get("locations", []))
    pages = max(1, min(int(cfg.get("pages_per_query", 1)), 10))
    for q in cfg["queries"]:
        query = f"{q} in {loc_text}" if loc_text else q
        cursor = ""
        for page in range(1, pages + 1):
            params = {
                "query": query,
                "num_pages": "1",
                "country": cfg.get("country", "in"),
                "date_posted": date_posted,
            }
            if cursor:
                params["cursor"] = cursor
            label = f"'{query}'" + (f" page {page}" if pages > 1 else "")
            try:
                r = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=TIMEOUT)
            except Exception as e:  # network error: keep going with the next query
                log(f"JSearch: {label} failed: {e}")
                break
            if not r.ok:
                # log the API's own message (never the key) so failures are easy to diagnose
                body = re.sub(r"\s+", " ", r.text or "")[:300]
                if r.status_code == 404 and "no " in body.lower() and "found" in body.lower():
                    log(f"JSearch: {label} -> 0 jobs (API says: {body})")
                else:
                    log(f"JSearch: {label} failed: HTTP {r.status_code}: {body}")
                break
            try:
                payload = r.json().get("data") or []
            except ValueError as e:
                log(f"JSearch: {label} returned invalid JSON: {e}")
                break
            # JSearch v5 returns {"data": {"jobs": [...], "cursor": ...}}; older versions returned {"data": [...]}
            data = (payload.get("jobs", []) if isinstance(payload, dict) else payload) or []
            cursor = str(payload.get("cursor") or "") if isinstance(payload, dict) else ""

            log(f"JSearch: {label} -> {len(data)} jobs")
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
                    "posted_ago": j.get("job_posted_at") or "",
                    "employment_type": j.get("job_employment_type") or "",
                    "salary": jsearch_salary(j),
                    "google_link": j.get("job_google_link") or "",
                    "also_on": ", ".join(sorted({o.get("publisher", "") for o in (j.get("apply_options") or [])
                                                 if o.get("publisher")} - {j.get("job_publisher") or ""})),
                    "description": j.get("job_description") or "",
                    "apply_options": [(j.get("job_publisher") or "", j.get("job_apply_link") or "")] + [
                        (o.get("publisher") or "", o.get("apply_link") or "") for o in (j.get("apply_options") or [])],
                    "exp_months": ((j.get("job_required_experience") or {}).get("required_experience_in_months")),
                })
            if len(data) < 10 or not cursor:  # last page
                break
    return jobs


def jsearch_salary(j: dict) -> str:
    if j.get("job_salary_string"):
        return str(j["job_salary_string"])
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    lo, hi, per = num(j.get("job_min_salary")), num(j.get("job_max_salary")), j.get("job_salary_period")
    if lo or hi:
        rng = f"{lo:,.0f} - {hi:,.0f}" if lo and hi else f"{(lo or hi):,.0f}"
        return f"{rng} {j.get('job_salary_currency') or ''} {('per ' + str(per).lower()) if per else ''}".strip()
    return ""


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
                    "posted_ago": "",
                    "employment_type": " ".join(x for x in [j.get("contract_time"), j.get("contract_type")] if x)
                                       .replace("_", " "),
                    "salary": (f"INR {j['salary_min']:,.0f} - {j['salary_max']:,.0f} per year"
                               if j.get("salary_min") and j.get("salary_max") else ""),
                    "google_link": "",
                    "also_on": "",
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


def pick_source(job: dict, allowed: list[str]) -> tuple[str, str] | None:
    """Return (publisher, apply link) for the first allowed board the job is listed on, e.g. LinkedIn or Naukri."""
    options = job.get("apply_options") or [(job.get("source", ""), job.get("url", ""))]
    for name in allowed:
        for publisher, link in options:
            if name in (publisher or "").lower() and link:
                return publisher, link
    return None


def posted_within(job: dict, days: int) -> bool:
    raw = job.get("posted") or ""
    try:
        posted = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True  # unknown date: keep, the API already filtered by date
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return posted >= datetime.now(timezone.utc) - timedelta(days=days, hours=12)


_NUM = r"(\d{1,2}(?:\.\d)?)"
_YRS = r"\s*(?:\+\s*)?(?:years?|yrs?)"
EXP_RANGE = re.compile(_NUM + r"\s*(?:-|–|—|to|--)\s*" + _NUM + _YRS)
EXP_PLUS = re.compile(_NUM + r"\s*(?:\+|plus)\s*(?:years?|yrs?)")
EXP_MIN = re.compile(r"(?:minimum|min\.?|at least|atleast)\s*(?:of\s*)?" + _NUM + _YRS)
EXP_PLAIN = re.compile(_NUM + _YRS + r"\s*(?:of\s*)?(?:\w+\s+){0,3}?(?:experience|exp\b)")


def experience_required(job: dict) -> tuple[float, float | None] | None:
    """Required experience as (min_years, max_years or None). None when the post does not say."""
    months = job.get("exp_months")
    if isinstance(months, (int, float)) and months > 0:
        return months / 12, None
    text = norm(job.get("description", ""))
    found: list[tuple[float, float | None]] = []
    for lo, hi in EXP_RANGE.findall(text):
        found.append((float(lo), float(hi)))
    rest = EXP_RANGE.sub(" ", text)   # so "5-8 yrs of experience" is not also read as "8 yrs"
    for rx in (EXP_PLUS, EXP_MIN, EXP_PLAIN):
        for n in rx.findall(rest):
            found.append((float(n), None))
    found = [(lo, hi) for lo, hi in found if 0 < lo <= 30 and (hi is None or lo <= hi <= 40)]
    if not found:
        return None
    lo = min(f[0] for f in found)                       # the entry bar the post mentions
    his = [f[1] for f in found if f[1] is not None]
    return lo, (max(his) if his and all(f[1] is not None for f in found) else None)


def experience_text(exp: tuple[float, float | None] | None) -> str:
    if not exp:
        return "Not stated"
    lo, hi = exp
    fmt = lambda x: f"{x:g}"
    return f"{fmt(lo)}-{fmt(hi)} years" if hi is not None else f"{fmt(lo)}+ years"


def skill_match(title: str, desc: str, hits: list[str], cfg: dict) -> tuple[int, list[str]]:
    """Of the technologies the job mentions, what percent are in your skills list.

    A job's technologies = your skills it mentions (hits) + `other_tech` terms it mentions.
    Returns (percent, other_tech_found). A job that mentions no known technology scores 0.
    """
    others = [t for t in (cfg.get("other_tech") or [])
              if contains(title, t) or contains(desc, t)]
    total = len(hits) + len(others)
    return (round(100 * len(hits) / total) if total else 0), others


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
        f"Fetched **{stats['fetched']}** posts from the last {stats.get('days', 3)} days; "
        f"**{stats.get('pool', 0)}** are your roles on {stats.get('sources', 'all sites')}. "
        f"**{len(jobs)}** new jobs match your profile "
        f"(at least {stats.get('min_skill_match', 0)}% skill match). "
        f"The downloadable job list on the workflow run has every job from the last {stats.get('days', 3)} days."
    )
    lines.append("")
    if not jobs:
        lines.append("No new matching jobs today.")
        return "\n".join(lines) + "\n"

    has_ai = any("ai_fit" in j for j in jobs)
    header = "| # | Apply | Role | Company | Location | Source | Experience | Match % | Your skills it asks for | Other skills it asks for |"
    sep = "|---|---|---|---|---|---|---|---|---|---|"
    if has_ai:
        header += " AI fit | Why |"
        sep += "---|---|"
    lines += [header, sep]
    for n, j in enumerate(jobs, 1):
        # <...> keeps URLs with spaces or parentheses from breaking the link
        apply = f"[**Apply**](<{j['url']}>)" if j["url"] else "-"
        role = f"[{md_escape(j['title'])}](<{j['url']}>)" if j["url"] else md_escape(j["title"])
        row = (f"| {n} | {apply} | {role} | {md_escape(j['company'])} | "
               f"{md_escape(j['location']) or '-'} | {md_escape(j['source'])} | "
               f"{j.get('experience', '-')} | {j.get('match_pct', 0)}% | {', '.join(j['hits'][:8])} | "
               f"{', '.join(j.get('missing', [])[:6]) or '-'} |")
        if has_ai:
            fit = j.get("ai_fit")
            row += f" {fit if fit is not None else '-'} | {md_escape(j.get('ai_why', ''))} |"
        lines.append(row)
    lines += ["", "_Apply links open the original posting. Review before applying._"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Downloadable export (HTML + CSV), uploaded as a workflow artifact
# --------------------------------------------------------------------------- #
EXPORT_FIELDS = [
    ("new", "New since last run"), ("title", "Role"), ("company", "Company"), ("location", "Location"), ("remote", "Remote"),
    ("experience", "Experience required"), ("employment_type", "Job type"), ("salary", "Salary"), ("posted_ago", "Posted"),
    ("posted", "Posted (UTC)"), ("source", "Source"), ("also_on", "Also listed on"),
    ("match_pct", "Match %"), ("score", "Score"), ("hits", "Your skills it asks for"),
    ("missing", "Other skills it asks for"), ("ai_fit", "AI fit"), ("ai_why", "AI note"),
    ("url", "Apply link"), ("google_link", "Google Jobs link"), ("summary", "Description (short)"),
]


def summary_of(job: dict, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", job.get("description") or "")).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


def export_value(job: dict, key: str):
    if key == "summary":
        return summary_of(job)
    v = job.get(key, "")
    if isinstance(v, list):
        return ", ".join(v)
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return "" if v is None else v


def write_exports(matched: list[dict], others: list[dict], stats: dict, today: str) -> None:
    EXPORT_DIR.mkdir(exist_ok=True)
    with open(EXPORT_DIR / f"jobs-{today}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Status", "Reason"] + [label for _, label in EXPORT_FIELDS])
        for status, rows in (("Match", matched), ("Below filters", others)):
            for j in rows:
                w.writerow([status, j.get("reason", "")] + [export_value(j, k) for k, _ in EXPORT_FIELDS])
    (EXPORT_DIR / f"jobs-{today}.html").write_text(build_html(matched, others, stats, today), encoding="utf-8")
    log(f"Export: {EXPORT_DIR.name}/jobs-{today}.html and .csv ({len(matched)} matches, {len(others)} others)")


def build_html(matched: list[dict], others: list[dict], stats: dict, today: str) -> str:
    e = lambda v: html.escape(str(v if v is not None else ""), quote=True)

    def card(n: int, j: dict, dim: bool = False) -> str:
        link = (f'<a class="apply" href="{e(j["url"])}" target="_blank" rel="noopener">Apply ↗</a>'
                if j.get("url") else "")
        glink = (f' <a href="{e(j["google_link"])}" target="_blank" rel="noopener">Google Jobs</a>'
                 if j.get("google_link") else "")
        facts = [("Experience", j.get("experience") or "Not stated"),
                 ("Location", j.get("location") or "-"), ("Remote", "Yes" if j.get("remote") else "No"),
                 ("Job type", j.get("employment_type") or "-"), ("Salary", j.get("salary") or "Not listed"),
                 ("Posted", j.get("posted_ago") or (j.get("posted") or "")[:10] or "-"),
                 ("Source", j.get("source") or "-")]
        if j.get("also_on"):
            facts.append(("Also on", j["also_on"]))
        if j.get("ai_fit") is not None:
            facts.append(("AI fit", f"{j['ai_fit']}/10 - {j.get('ai_why', '')}"))
        if dim:
            facts.append(("Why hidden", j.get("reason", "")))
        dl = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in facts)
        hits = "".join(f'<span class="tag yes">{e(h)}</span>' for h in j.get("hits", []))
        miss = "".join(f'<span class="tag no">{e(h)}</span>' for h in j.get("missing", []))
        return f"""
<article class="job{' dim' if dim else ''}" data-text="{e((j.get('title','') + ' ' + j.get('company','') + ' ' + j.get('location','')).lower())}">
  <header>
    <div><span class="num">{n}</span>{'<span class="new">NEW</span>' if j.get('new') else ''}<h3>{e(j.get('title'))}</h3><p class="co">{e(j.get('company'))}</p></div>
    <div class="right"><span class="pct">{e(j.get('match_pct', 0))}% match</span>{link}</div>
  </header>
  <dl>{dl}</dl>
  <p class="skills">{hits}{miss}</p>
  <details><summary>Description</summary><p>{e(summary_of(j, 1500))}</p>{glink}</details>
</article>"""

    matched_html = "".join(card(i, j) for i, j in enumerate(matched, 1)) or "<p>No new matching jobs today.</p>"
    others_html = "".join(card(i, j, dim=True) for i, j in enumerate(others, 1)) or "<p>None.</p>"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jobs for {e(today)}</title>
<style>
:root {{ --bg:#f6f8fb; --card:#fff; --text:#172033; --muted:#5b6577; --line:#dfe5ee; --accent:#0f62fe; --ok:#e6f4ea; --okt:#137333; --no:#fde7e7; --not:#a50e0e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0d1117; --card:#151b24; --text:#e6eaf2; --muted:#9aa5b8; --line:#263041; --accent:#6ea0ff; --ok:#12301c; --okt:#7ee2a0; --no:#3a1717; --not:#ff9b9b; }} }}
* {{ box-sizing:border-box; }} body {{ margin:0; font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif; background:var(--bg); color:var(--text); }}
main {{ max-width:1000px; margin:0 auto; padding:24px 16px 60px; }} h1 {{ margin:0 0 4px; }} .sub {{ color:var(--muted); margin:0 0 16px; }}
input {{ width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:10px; background:var(--card); color:var(--text); font-size:1rem; margin-bottom:16px; }}
.job {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin-bottom:12px; }}
.job.dim {{ opacity:.8; }} .job header {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
.num {{ color:var(--muted); font-weight:700; margin-right:6px; }} h3 {{ display:inline; font-size:1.05rem; margin:0; }} .co {{ margin:2px 0 0; color:var(--muted); }}
.right {{ display:flex; flex-direction:column; align-items:flex-end; gap:8px; flex:none; }} .pct {{ font-weight:700; color:var(--accent); }}
a.apply {{ background:var(--accent); color:#fff; padding:7px 14px; border-radius:8px; text-decoration:none; font-weight:600; white-space:nowrap; }}
dl {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:6px 16px; margin:12px 0 8px; }} dt {{ font-size:.75rem; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }} dd {{ margin:0; font-size:.92rem; }}
.tag {{ display:inline-block; font-size:.8rem; padding:2px 8px; border-radius:999px; margin:2px 4px 2px 0; }} .tag.yes {{ background:var(--ok); color:var(--okt); }} .tag.no {{ background:var(--no); color:var(--not); }}
details {{ margin-top:6px; }} summary {{ cursor:pointer; color:var(--accent); }} details p {{ color:var(--muted); font-size:.92rem; line-height:1.5; }}
h2 {{ margin:28px 0 10px; }} .new {{ background:#137333; color:#fff; font-size:.7rem; font-weight:700; padding:2px 6px; border-radius:6px; margin-right:6px; vertical-align:2px; }} .legend {{ font-size:.85rem; color:var(--muted); }}
</style></head><body><main>
<h1>Jobs for {e(today)}</h1>
<p class="sub">Jobs posted in the last {e(stats.get('days', 3))} days on {e(stats.get('sources', 'all sites'))}, for your roles only.
Fetched {e(stats['fetched'])} posts; ignored {e(', '.join(f"{v} {k}" for k, v in (stats.get('ignored') or {}).items()))}.
<b>{len(matched)}</b> match your profile (at least {e(stats.get('min_skill_match', 0))}% skill match), {len(others)} more below your filters.
<span class="new">NEW</span> = not in an earlier run.</p>
<input id="q" placeholder="Filter by role, company or location..." oninput="for (const a of document.querySelectorAll('.job')) a.style.display = a.dataset.text.includes(this.value.toLowerCase()) ? '' : 'none'">
<p class="legend"><span class="tag yes">green</span> = your skills the job asks for, <span class="tag no">red</span> = other skills it asks for.</p>
<h2>Matches ({len(matched)})</h2>{matched_html}
<h2>Below your filters ({len(others)})</h2>{others_html}
</main></body></html>
"""


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

    days = int(cfg.get("max_days_old", 3))
    allowed = [x.lower() for x in (cfg.get("allowed_sources") or [])]
    roles = cfg.get("allowed_titles") or []
    exp_cfg = cfg.get("experience_filter") or {}
    ignored = {"older than %d days" % days: 0, "other job sites": 0, "other roles": 0}

    # Step 1: drop jobs outside the window, from other sites, or for other roles (not shown anywhere)
    pool = []
    for fp, j in by_fp.items():
        if not posted_within(j, days):
            ignored["older than %d days" % days] += 1
            continue
        if allowed:
            pick = pick_source(j, allowed)
            if not pick:
                ignored["other job sites"] += 1
                continue
            j["source"], j["url"] = pick
        if roles and not any(contains(norm(j["title"]), r) for r in roles):
            ignored["other roles"] += 1
            continue
        j["new"] = fp not in seen
        pool.append(j)
    log("Ignored: " + ", ".join(f"{v} {k}" for k, v in ignored.items()))

    # Step 2: score the rest; keep the ones that miss a filter in "others" with the reason
    matched, others = [], []
    for j in pool:
        j.setdefault("score", 0); j.setdefault("hits", []); j.setdefault("match_pct", 0); j.setdefault("missing", [])
        exp = experience_required(j)
        j["experience"] = experience_text(exp)
        if not location_ok(j, cfg):
            j["reason"] = "location"
            others.append(j); continue
        res = score_job(j, cfg)
        if res is None:
            j["reason"] = "excluded title or keyword"
            others.append(j); continue
        j["score"], j["hits"] = res
        j["match_pct"], j["missing"] = skill_match(norm(j["title"]), norm(j["description"]), j["hits"], cfg)
        min_years = float(exp_cfg.get("min_years", 0)) if exp_cfg.get("enabled", True) else 0
        if exp and min_years and exp[0] < min_years:
            j["reason"] = f"asks for {j['experience']} (below {min_years:g}+)"
            others.append(j)
        elif not exp and min_years and not exp_cfg.get("keep_not_stated", True):
            j["reason"] = "experience not stated"
            others.append(j)
        elif j["match_pct"] < cfg.get("min_skill_match", 0):
            j["reason"] = f"skill match {j['match_pct']}% < {cfg.get('min_skill_match', 0)}%"
            others.append(j)
        elif j["score"] < cfg.get("min_score", 0):
            j["reason"] = f"score {j['score']} < {cfg.get('min_score', 0)}"
            others.append(j)
        else:
            matched.append(j)

    order = lambda j: (j.get("new", False), j["match_pct"], j["score"])
    matched.sort(key=order, reverse=True)
    cap = int(cfg.get("max_results", 30))
    for j in matched[cap:]:
        j["reason"] = f"over max_results ({cap})"
    others += matched[cap:]
    matched = matched[:cap]
    others.sort(key=order, reverse=True)

    ai_rerank(matched, cfg)
    if any("ai_fit" in j for j in matched):
        matched.sort(key=lambda j: (j.get("new", False), j.get("ai_fit") or 0, j["match_pct"], j["score"]), reverse=True)

    # mark everything fetched as seen so the Issue only lists jobs you have not been sent before
    for fp in by_fp:
        seen.setdefault(fp, now_iso)
    save_seen(seen)

    new_matches = [j for j in matched if j.get("new")]
    stats = {"fetched": len(raw), "new": sum(1 for j in pool if j.get("new")), "pool": len(pool),
             "days": days, "ignored": ignored, "min_skill_match": cfg.get("min_skill_match", 0),
             "sources": ", ".join(cfg.get("allowed_sources") or []) or "all sites"}
    report = build_report(new_matches, stats, today)
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / f"{today}.md").write_text(report)
    (ROOT / "latest_report.md").write_text(report)
    write_exports(matched, others, stats, today)

    # outputs for the workflow
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"match_count={len(new_matches)}\n")
            f.write(f"report_date={today}\n")
            f.write(f"export_dir={EXPORT_DIR}\n")

    log(f"Done: {len(matched)} matches in the last {days} days ({len(new_matches)} new); report in reports/{today}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
