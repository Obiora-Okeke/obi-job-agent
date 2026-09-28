"""Daily entry point: fetch from every configured source, score against
the profile, and write docs/jobs.json for the dashboard to read.

Run locally:   python src/main.py
Run in CI:     see .github/workflows/daily-job-scan.yml
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.profile import PROFILE as BASE_PROFILE
from src import fetch_greenhouse, fetch_lever, fetch_ashby, fetch_adzuna, fetch_github_lists
from src.scorer import rank_jobs
from src.claude_scorer import score_with_claude, sort_by_claude_score

# Search terms for the Adzuna aggregator — built from my target roles
# and priority topics so it isn't just "software engineer" noise.

# Some FAANG/big-tech AI & software roles, entry-level focused. Adzuna has
# no direct "employer" filter, so company names go straight in the
# search text — it's a soft signal (surfaces postings mentioning the
# company, not a guaranteed employer match), but it's the only lever
# Adzuna gives us since Google/Meta/Amazon/Apple/Netflix/Microsoft run
# their own ATS and aren't reachable via the Greenhouse/Lever/Ashby
# fetchers at all.
ADZUNA_QUERIES = [
    # Core software engineering
    "software engineer new grad",
    "software engineer entry level",
    "software engineer early career",
    "software engineer recent graduate",
    "software engineer I",
    "software engineer 1",
    "associate software engineer",
    "junior software engineer",
    "software developer entry level",
    "associate software developer",
    "software development engineer new grad",
    "software development engineer entry level",

    # Backend / full stack / frontend / product
    "backend engineer entry level",
    "backend software engineer new grad",
    "full stack engineer entry level",
    "full stack software engineer new grad",
    "frontend engineer entry level",
    "frontend software engineer new grad",
    "product engineer entry level",
    "product software engineer new grad",
    "application developer entry level",
    "applications engineer entry level",

    # Java / Python / React / APIs
    "Java software engineer entry level",
    "Java developer entry level",
    "Python software engineer entry level",
    "Python developer entry level",
    "React developer entry level",
    "React software engineer entry level",
    "API developer entry level",
    "REST API engineer entry level",

    # AWS / cloud / distributed systems
    "cloud engineer entry level",
    "AWS cloud engineer entry level",
    "cloud software engineer new grad",
    "cloud developer entry level",
    "distributed systems engineer entry level",
    "platform engineer entry level",
    "infrastructure engineer entry level",

    # Data engineering / analytics
    "data engineer entry level",
    "junior data engineer",
    "associate data engineer",
    "data analyst entry level",
    "technical data analyst entry level",
    "performance analyst entry level",
    "business intelligence analyst entry level",
    "analytics engineer entry level",
    "data operations analyst entry level",
    "data quality analyst entry level",

    # ML / AI
    "machine learning engineer entry level",
    "machine learning engineer new grad",
    "junior machine learning engineer",
    "AI engineer entry level",
    "AI software engineer entry level",
    "AI research engineer entry level",
    "research engineer entry level",
    "applied AI engineer entry level",

    # Forward deployed / solutions / customer-facing technical
    "forward deployed engineer entry level",
    "forward deployed software engineer",
    "solutions engineer entry level",
    "associate solutions engineer",
    "technical solutions engineer entry level",
    "customer engineer entry level",
    "implementation engineer entry level",
    "technical implementation engineer entry level",

    # Systems / IT / technical analyst
    "systems engineer entry level",
    "systems analyst entry level",
    "IT analyst entry level",
    "technology analyst entry level",
    "technical analyst entry level",
    "software systems analyst",
    "applications analyst entry level",
    "information systems analyst entry level",

    # QA / test / automation
    "software test engineer entry level",
    "quality engineer entry level software",
    "QA automation engineer entry level",
    "test automation engineer entry level",
    "software quality engineer entry level",

    # Research / technical roles
    "research software engineer entry level",
    "research programmer entry level",
    "technical research analyst entry level",
    "research data analyst entry level",
    "research computing analyst entry level",
    "scientific programmer entry level",
    "computational research assistant",
    "research assistant computer science",
    "research assistant data science",

    # Public-sector / government tech
    "government software engineer entry level",
    "government software developer entry level",
    "public sector software engineer",
    "public sector technology analyst",
    "government IT analyst entry level",
    "government systems analyst entry level",
    "government data analyst entry level",
    "municipal technology analyst",
    "city technology analyst",
    "county IT analyst",
    "state government IT analyst",
    "state government software developer",

    # School district / higher education tech
    "school district software engineer",
    "school district software developer",
    "school district systems analyst",
    "school district IT analyst",
    "education technology analyst",
    "education technology specialist",
    "higher education software developer",
    "higher education systems analyst",
    "university software developer",
    "university software engineer",
    "university systems analyst",
    "university data analyst",
    "academic technology analyst",
    "instructional technology systems analyst",

    # Sports tech / athletics tech
    "sports software engineer entry level",
    "sports technology analyst",
    "sports data analyst entry level",
    "sports analytics engineer",
    "sports analytics analyst",
    "sports data engineer entry level",
    "athletics data analyst",
    "athletics technology analyst",
    "sports performance analyst data",
    "sports research analyst",
    "sports product engineer",
    "sports platform engineer",

    # New grad programs / rotational programs
    "technology development program software engineer",
    "software engineering rotational program",
    "technology analyst new grad",
    "engineering development program software",
    "early career technology program",
    "technology rotational program new grad",
    "IT rotational program new grad",
    "software development program new grad",

    # Larger employers / common new-grad pipelines
    "software engineer new grad Amazon",
    "software engineer new grad Google",
    "software engineer new grad Meta",
    "software engineer new grad Microsoft",
    "software engineer new grad Apple",
    "software engineer new grad Oracle",
    "software engineer new grad Salesforce",
    "software engineer new grad Capital One",
    "software engineer new grad JPMorgan",
    "software engineer new grad Goldman Sachs",
    "software engineer new grad Bloomberg",
    "software engineer new grad Databricks",
    "software engineer new grad Snowflake",

    # Remote / California emphasis
    "software engineer entry level remote",
    "software engineer new grad remote",
    "software engineer entry level California",
    "software engineer new grad California",
    "software engineer entry level Los Angeles",
    "software engineer new grad Los Angeles",
    "software engineer entry level Southern California",
    "software engineer entry level San Francisco",
    "software engineer entry level Seattle",
    "systems analyst entry level Los Angeles",
    "IT analyst entry level Los Angeles",
    "data analyst entry level Los Angeles",
    "software developer school district California",
    "technology analyst school district California"
]

def load_companies():
    with open(ROOT / "config" / "companies.yaml") as f:
        return yaml.safe_load(f)


def load_profile() -> dict:
    """Starts from the public template (config/profile.py) and merges in
    a private override if PROFILE_OVERRIDE_JSON is set — a repo secret
    holding your real name/skills/salary floor/etc. as a JSON object.
    This is how the repo stays public and generic while your actual
    targeting data never gets committed anywhere. See README."""
    profile = dict(BASE_PROFILE)
    raw = os.environ.get("PROFILE_OVERRIDE_JSON")
    if not raw:
        return profile
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"PROFILE_OVERRIDE_JSON is set but not valid JSON ({e}) — using the public template instead.")
        return profile
    profile.update(overrides)
    print("Loaded private profile override from PROFILE_OVERRIDE_JSON.")
    return profile


def main():
    profile = load_profile()
    companies = load_companies()
    all_jobs = []

    print("Fetching Greenhouse boards...")
    for token in companies.get("greenhouse", []) or []:
        all_jobs.extend(fetch_greenhouse.fetch(token))

    print("Fetching Lever boards...")
    for token in companies.get("lever", []) or []:
        all_jobs.extend(fetch_lever.fetch(token))

    print("Fetching Ashby boards...")
    for token in companies.get("ashby", []) or []:
        all_jobs.extend(fetch_ashby.fetch(token))

    print("Fetching Adzuna...")
    all_jobs.extend(fetch_adzuna.fetch(ADZUNA_QUERIES))

    print("Fetching speedyapply new-grad list...")
    all_jobs.extend(fetch_github_lists.fetch_speedyapply())

    print("Fetching Simplify new-grad list...")
    all_jobs.extend(fetch_github_lists.fetch_simplify_new_grad())

    print(f"\nTotal raw postings: {len(all_jobs)}")
    ranked = rank_jobs(all_jobs, profile)
    print(f"After dedupe + exclusions: {len(ranked)}")

    threshold = profile.get("min_score_threshold", 0)
    ranked = [j for j in ranked if j["score"] >= threshold]
    print(f"After {threshold}+ score threshold: {len(ranked)}")

    # Optional second pass: re-score the top N keyword matches with Claude
    # for actual semantic judgment against your resume. No-ops cleanly if
    # ANTHROPIC_API_KEY or resume_text isn't set — everything below stays
    # on keyword scoring alone either way.
    rescore_n = profile.get("claude_rescore_top_n", 100)
    top_slice = ranked[:rescore_n]
    rest = ranked[rescore_n:]
    if top_slice:
        score_with_claude(top_slice, profile.get("resume_text", ""))
        top_slice = sort_by_claude_score(top_slice)
    ranked = top_slice + rest

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile_name": profile["name"],
        "total_jobs": len(ranked),
        "jobs": ranked,
    }

    out_path = ROOT / "docs" / "jobs.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
