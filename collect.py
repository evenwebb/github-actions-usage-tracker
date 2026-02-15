#!/usr/bin/env python3
"""
Collect GitHub Actions workflow run data across all repos and store in SQLite.
Runs daily via GitHub Action; fetches runs from the last 24 hours incrementally.
Use --backfill to fetch up to 90 days of history on first run.
"""

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# GitHub API base
API_BASE = "https://api.github.com"
DB_PATH = Path(__file__).parent / "data" / "actions.db"
REQUEST_TIMEOUT = 15

# OS multipliers for billable minutes (Linux 1x, macOS 10x, Windows 2x)
OS_MULTIPLIERS = {
    "ubuntu-latest": 1,
    "ubuntu-22.04": 1,
    "ubuntu-24.04": 1,
    "ubuntu-20.04": 1,
    "macos-latest": 10,
    "macos-14": 10,
    "macos-13": 10,
    "macos-12": 10,
    "windows-latest": 2,
    "windows-2022": 2,
    "windows-2019": 2,
}


def get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_billable_multiplier(labels: list[str]) -> float:
    """Get billable minute multiplier from job labels. Self-hosted = 0."""
    for label in labels or []:
        label_lower = label.lower()
        if "self-hosted" in label_lower:
            return 0
        if label in OS_MULTIPLIERS:
            return OS_MULTIPLIERS[label]
        # Partial match for ubuntu-*, macos-*, windows-*
        if label_lower.startswith("ubuntu-"):
            return 1
        if label_lower.startswith("macos-"):
            return 10
        if label_lower.startswith("windows-"):
            return 2
    return 1  # Default to Linux


def fetch_repos(session: requests.Session, token: str) -> list[dict]:
    """Fetch all repos for the authenticated user."""
    repos = []
    url = f"{API_BASE}/user/repos"
    params = {"per_page": 100, "sort": "updated", "type": "owner"}
    while url:
        resp = session.get(url, params=params, headers=get_headers(token), timeout=REQUEST_TIMEOUT)
        params = None
        resp.raise_for_status()
        data = resp.json()
        repos.extend(data)
        url = resp.links.get("next", {}).get("url")
        if url:
            time.sleep(0.1)
    return repos


def fetch_workflow_runs(
    session: requests.Session,
    token: str,
    owner: str,
    repo: str,
    created_after: datetime,
) -> list[dict]:
    """Fetch completed workflow runs created after the given datetime."""
    runs = []
    url = f"{API_BASE}/repos/{owner}/{repo}/actions/runs"
    created = created_after.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"per_page": 100, "created": f">={created}", "status": "completed"}
    while url:
        resp = session.get(url, params=params, headers=get_headers(token), timeout=REQUEST_TIMEOUT)
        params = None
        if resp.status_code in (404, 451):
            # 404: repo has no Actions or no access; 451: DMCA removed
            break
        resp.raise_for_status()
        data = resp.json()
        runs.extend(data.get("workflow_runs", []))
        url = resp.links.get("next", {}).get("url")
        if url:
            time.sleep(0.1)
    return runs


def fetch_run_jobs(
    session: requests.Session,
    token: str,
    owner: str,
    repo: str,
    run_id: int,
) -> list[dict]:
    """Fetch jobs for a workflow run."""
    url = f"{API_BASE}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    resp = session.get(url, headers=get_headers(token), params={"per_page": 100}, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        return []
    data = resp.json()
    return data.get("jobs", [])


def compute_job_duration_seconds(job: dict) -> float | None:
    """Compute job duration in seconds from started_at and completed_at."""
    started = job.get("started_at")
    completed = job.get("completed_at")
    if not started or not completed:
        return None
    try:
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        return (end_dt - start_dt).total_seconds()
    except (ValueError, TypeError):
        return None


def compute_billable_minutes(jobs: list[dict]) -> tuple[float, float, float, float]:
    """Compute billable minutes from jobs. Returns (linux, macos, windows, total).
    GitHub rounds up to next minute. Linux 1x, macOS 10x, Windows 2x."""
    linux_mins = macos_mins = windows_mins = 0.0
    total = 0.0
    for job in jobs:
        duration_sec = compute_job_duration_seconds(job)
        if duration_sec is None or duration_sec <= 0:
            continue
        multiplier = get_billable_multiplier(job.get("labels", []))
        if multiplier == 0:  # Self-hosted
            continue
        # Round up to next minute (GitHub billing)
        base_minutes = math.ceil(duration_sec / 60)
        billable = base_minutes * multiplier
        total += billable
        if multiplier == 10:
            macos_mins += base_minutes
        elif multiplier == 2:
            windows_mins += base_minutes
        else:
            linux_mins += base_minutes
    return (linux_mins, macos_mins, windows_mins, total)


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_runs (
            run_id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            owner TEXT NOT NULL,
            workflow_name TEXT,
            workflow_id INTEGER,
            status TEXT,
            conclusion TEXT,
            event TEXT,
            created_at TEXT,
            run_started_at TEXT,
            html_url TEXT,
            duration_seconds REAL,
            billable_minutes_linux REAL DEFAULT 0,
            billable_minutes_macos REAL DEFAULT 0,
            billable_minutes_windows REAL DEFAULT 0,
            billable_minutes_total REAL DEFAULT 0,
            trigger_event TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_runs_repo ON workflow_runs(repo);
        CREATE INDEX IF NOT EXISTS idx_runs_created ON workflow_runs(created_at);
        CREATE INDEX IF NOT EXISTS idx_runs_conclusion ON workflow_runs(conclusion);
        CREATE INDEX IF NOT EXISTS idx_runs_event ON workflow_runs(event);

        CREATE TABLE IF NOT EXISTS collection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            repos_scanned INTEGER DEFAULT 0,
            repos_with_runs INTEGER DEFAULT 0,
            runs_collected INTEGER DEFAULT 0,
            runs_updated INTEGER DEFAULT 0,
            api_calls INTEGER DEFAULT 0,
            errors TEXT,
            backfill INTEGER DEFAULT 0
        );
    """)


def upsert_runs_batch(conn: sqlite3.Connection, runs: list[dict]) -> None:
    """Batch insert or update workflow runs."""
    if not runs:
        return
    conn.executemany("""
        INSERT INTO workflow_runs (
            run_id, repo, owner, workflow_name, workflow_id, status, conclusion,
            event, created_at, run_started_at, html_url, duration_seconds,
            billable_minutes_linux, billable_minutes_macos, billable_minutes_windows,
            billable_minutes_total, trigger_event
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            status = excluded.status,
            conclusion = excluded.conclusion,
            duration_seconds = excluded.duration_seconds,
            billable_minutes_linux = excluded.billable_minutes_linux,
            billable_minutes_macos = excluded.billable_minutes_macos,
            billable_minutes_windows = excluded.billable_minutes_windows,
            billable_minutes_total = excluded.billable_minutes_total,
            updated_at = CURRENT_TIMESTAMP
    """, [
        (
            r["run_id"], r["repo"], r["owner"], r["workflow_name"], r.get("workflow_id"),
            r.get("status"), r.get("conclusion"), r.get("event"), r.get("created_at"),
            r.get("run_started_at"), r.get("html_url"), r.get("duration_seconds"),
            r.get("billable_minutes_linux", 0), r.get("billable_minutes_macos", 0),
            r.get("billable_minutes_windows", 0), r.get("billable_minutes_total", 0),
            r.get("event"),
        )
        for r in runs
    ])


def send_apprise_alert(urls: str, title: str, body: str) -> bool:
    """Send notification via Apprise. Returns True if sent."""
    if not urls or not urls.strip():
        return False
    try:
        import subprocess
        result = subprocess.run(
            ["apprise", "-t", title, "-b", body, urls.strip()],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_alerts(conn: sqlite3.Connection, apprise_urls: str) -> None:
    """Check conditions and send Apprise alerts if needed."""
    if not apprise_urls:
        return
    cursor = conn.execute("""
        SELECT SUM(billable_minutes_total) FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
    """)
    month_total = (cursor.fetchone()[0] or 0)
    allowance = int(os.environ.get("GITHUB_ACTIONS_ALLOWANCE", "2000"))
    threshold = allowance * 0.8
    if month_total >= threshold:
        send_apprise_alert(
            apprise_urls,
            "GitHub Actions Usage Alert",
            f"Minutes used this month ({month_total:.0f}) exceeds 80% of allowance ({allowance}). "
            f"Current: {month_total:.0f}/{allowance} min.",
        )
    # Consecutive failures: would need more complex query; skip for now
    # Duration increase: would need historical comparison; skip for now


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect GitHub Actions workflow data")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch up to 90 days of history (for initial setup)",
    )
    args = parser.parse_args()

    token = os.environ.get("ACTIONS_USAGE_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set ACTIONS_USAGE_TOKEN or GITHUB_TOKEN")
    apprise_urls = os.environ.get("APPRISE_URL", "")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.backfill:
        created_after = datetime.now(timezone.utc) - timedelta(days=90)
        log.info("Backfill mode: fetching runs from last 90 days")
    else:
        created_after = datetime.now(timezone.utc) - timedelta(hours=48)
        log.info("Incremental mode: fetching runs from last 48 hours")

    conn.execute(
        "INSERT INTO collection_log (started_at, backfill) VALUES (?, ?)",
        (started_at, 1 if args.backfill else 0),
    )
    log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    session = requests.Session()
    api_calls = 0
    errors: list[str] = []

    _original_get = session.get

    def counted_get(*a, **kw):
        nonlocal api_calls
        api_calls += 1
        return _original_get(*a, **kw)

    session.get = counted_get

    log.info("Fetching repos...")
    repos = [r for r in fetch_repos(session, token) if not r.get("archived")]
    log.info("Found %d repos", len(repos))

    # Collect all runs across repos
    all_runs: list[tuple[dict, str, str]] = []  # (run, owner, name)
    for repo in repos:
        full_name = repo.get("full_name")
        if not full_name:
            continue
        owner, name = full_name.split("/", 1)
        try:
            runs = fetch_workflow_runs(session, token, owner, name, created_after)
        except requests.HTTPError as e:
            if e.response.status_code in (403, 404, 451):
                continue
            errors.append(f"{full_name}: {e}")
            log.warning("Error %s: %s", full_name, e)
            continue
        for run in runs:
            if run.get("id"):
                all_runs.append((run, owner, name))

    repos_with_runs = len({f"{o}/{n}" for _, o, n in all_runs})

    # Fetch jobs in parallel (max 5 concurrent to stay under rate limit)
    def fetch_jobs_for_run(args):
        run, owner, name = args
        jobs = fetch_run_jobs(session, token, owner, name, run["id"])
        return run, jobs

    run_data_list: list[dict] = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(fetch_jobs_for_run, item): item for item in all_runs}
        for future in as_completed(futures):
            run, jobs = future.result()
            _, owner, name = futures[future]
            full_name = f"{owner}/{name}"
            linux_m, macos_m, windows_m, total_m = compute_billable_minutes(jobs)
            duration_sec = sum(s for j in jobs if (s := compute_job_duration_seconds(j)) is not None)
            run_data_list.append({
                "run_id": run["id"],
                "repo": full_name,
                "owner": owner,
                "workflow_name": run.get("name") or "Unknown",
                "workflow_id": run.get("workflow_id"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "event": run.get("event"),
                "created_at": run.get("created_at"),
                "run_started_at": run.get("run_started_at"),
                "html_url": run.get("html_url"),
                "duration_seconds": duration_sec or None,
                "billable_minutes_linux": linux_m,
                "billable_minutes_macos": macos_m,
                "billable_minutes_windows": windows_m,
                "billable_minutes_total": total_m,
                "trigger_event": run.get("event"),
            })

    # Batch upsert
    for i in range(0, len(run_data_list), 50):
        upsert_runs_batch(conn, run_data_list[i : i + 50])

    for repo in repos:
        fn = repo.get("full_name", "")
        count = sum(1 for r in run_data_list if r["repo"] == fn)
        if count:
            log.info("  %s: %d runs", fn, count)

    total_runs = len(run_data_list)
    total_in_db = conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
    completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """UPDATE collection_log SET
            completed_at = ?, repos_scanned = ?, repos_with_runs = ?,
            runs_collected = ?, runs_updated = ?, api_calls = ?, errors = ?
        WHERE id = ?""",
        (
            completed_at,
            len(repos),
            repos_with_runs,
            total_runs,
            total_in_db,
            api_calls,
            json.dumps(errors) if errors else None,
            log_id,
        ),
    )

    conn.commit()
    check_alerts(conn, apprise_urls)
    conn.close()

    log.info(
        "Done. Collected %d runs, %d total in DB, %d API calls, %d errors",
        total_runs,
        total_in_db,
        api_calls,
        len(errors),
    )


if __name__ == "__main__":
    main()
