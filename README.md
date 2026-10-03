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

## Tuning

- Too much noise → raise `min_score`, or add words to `exclude_title`.
- Missing good jobs → lower `min_score`, or add skills and title words.
- Results are de-duplicated across boards (same title + company) and remembered
  for 60 days in `data/seen.json`.
- Every day's list is also saved in `reports/YYYY-MM-DD.md`.

## Run locally

```bash
pip install -r requirements.txt
export RAPIDAPI_KEY=...   # and/or ADZUNA_APP_ID / ADZUNA_APP_KEY
python find_jobs.py && cat latest_report.md
```
