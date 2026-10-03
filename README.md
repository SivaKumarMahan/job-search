# Daily Job Finder

A GitHub Actions workflow that runs every morning at 08:00 IST. It pulls new
DevOps/SRE/Platform job posts from job-aggregator APIs, scores them against
`profile.yaml`, skips jobs it has already shown you, and posts the ranked list
as a GitHub Issue. GitHub then emails you the Issue.

It reads from APIs, not by scraping LinkedIn or Naukri, so your accounts are
never at risk. JSearch pulls in LinkedIn and Naukri listings through Google
for Jobs.

## Setup (about 15 minutes)

1. **Create a private repo**, e.g. `job-finder`, and push these files to it.
2. **Get API keys** (you need at least one of the job sources):
   | Secret name | Where to get it | Notes |
   |---|---|---|
   | `RAPIDAPI_KEY` | rapidapi.com → search "JSearch" → Subscribe (Basic/free) | Covers LinkedIn, Naukri, Indeed, Glassdoor, career pages |
   | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | developer.adzuna.com → Register | Free, India supported |
   | `ANTHROPIC_API_KEY` *(optional)* | console.anthropic.com | Adds an AI fit score + one-line reason per job |
3. **Add them as secrets**: repo → Settings → Secrets and variables → Actions → New repository secret.
4. **Allow the workflow to write**: Settings → Actions → General → Workflow permissions → *Read and write permissions*.
5. **Edit `profile.yaml`**: queries, locations, skills and weights, exclusions, `resume_summary`.
6. **Test it**: Actions tab → *Daily job matches* → *Run workflow*.
7. **Get the email**: make sure you're *Watching* the repo, and that GitHub
   notifications → Email is on for Issues.

## Quota tips

- JSearch makes 1 call per query per day. The default 4 queries come to about 120 calls a month. Check
  your plan's monthly limit on RapidAPI.
- Adzuna makes 1 call per query per location per day (8 by default).
- To save quota, use fewer, broader queries and let `skills` do the filtering.

## Where to see the jobs

- **Download the job list from the run (easiest):** open the run under
  [Actions](https://github.com/SivaKumarMahan/job-search/actions), scroll to **Artifacts** at the bottom,
  and download **job-list-YYYY-MM-DD**. Unzip it and open:
  - `jobs-YYYY-MM-DD.html` in a browser: one card per job with an **Apply** button, match %, location,
    remote, job type, salary (when listed), posted time, source, other sites it is listed on, the skills
    it asks for (yours in green, others in red) and a short description. There is also a filter box.
  - `jobs-YYYY-MM-DD.csv` in Excel or Google Sheets: the same details, one row per job, with the apply links.

  Both files also list the jobs that were fetched but hidden, and why (for example "skill match 33% < 75%").
  Artifacts are kept for 30 days; you must be signed in to GitHub to download them.
- **Run page:** the same list is shown on the run's **Summary** page, with clickable links.
- **GitHub Issue (main view):** <https://github.com/SivaKumarMahan/job-search/issues?q=label%3Ajob-matches>.
  Each day's list is a new Issue, and GitHub emails it to you if you watch the repo.
  The **Apply** column links straight to the job posting (Naukri, LinkedIn, Indeed, company site, ...).
- **Report files:** every list is also saved in [`reports/`](reports/) as `YYYY-MM-DD.md`.

## How many jobs per run

- JSearch returns at most **10 jobs per call**. With 4 queries that is up to **40 jobs a day** before filtering.
- `pages_per_query: 2` fetches up to 20 per query (80 a day), but uses 2 calls per query.
- The report lists at most `max_results` jobs (30 by default).

## Skill match %

For each job, the script finds the technologies it mentions: your `skills`, plus the
`other_tech` terms you don't list. **Match %** = your skills / all technologies mentioned.
Example: Azure, AKS, Terraform and AWS mentioned → 3 of 4 are yours → 75%.
Jobs below `min_skill_match` (75 by default) are hidden; set it to 0 to turn the filter off.

## Tuning

- Too much noise → raise `min_skill_match` or `min_score`, or add words to `exclude_title`.
- Missing good jobs → lower `min_skill_match` (e.g. 60) or `min_score`, or add skills and title words.
- Results are de-duplicated across boards (same title + company) and remembered
  for 60 days in `data/seen.json`.
- Every day's list is also saved in `reports/YYYY-MM-DD.md`.

## Run locally

```bash
pip install -r requirements.txt
export RAPIDAPI_KEY=...   # and/or ADZUNA_APP_ID / ADZUNA_APP_KEY
python find_jobs.py && cat latest_report.md
```
