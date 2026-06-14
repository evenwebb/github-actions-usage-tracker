#!/usr/bin/env python3
"""
Collect GitHub Actions workflow run data across all repos and store in SQLite.
Runs daily via GitHub Action; fetches runs from the last 48 hours incrementally.
Use --backfill to fetch up to 90 days of history on first run.
"""

import argparse
import json
import logging
import math
import os
import sqlite3
import subprocess
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

API_BASE = "https://api.github.com"
DB_PATH = Path(__file__).parent / "data" / "actions.db"
REQUEST_TIMEOUT = 15

# OS multipliers for billable minutes (Linux 1x, macOS 10x, Windows 2x)
OS_MULTIPLIERS = {
    "ubuntu-latest": 1,
    "ubuntu-24.04": 1,
    "ubuntu-24.04-arm": 1,
    "ubuntu-22.04": 1,
    "ubuntu-20.04": 1,
    "macos-latest": 10,
    "macos-15": 10,
    "macos-14": 10,
    "macos-13": 10,
    "macos-12": 10,
    "windows-latest": 2,
    "windows-2025": 2,
    "windows-2022": 2,
    "windows-2019": 2,
}


def get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _validate_token(token: str) -> None:
    """Check token validity and scopes early. Warns if scopes look insufficient."""
    try:
        resp = requests.get(
            f"{API_BASE}/user",
            headers=get_headers(token),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            scopes = resp.headers.get("X-OAuth-Scopes", "")
            log.info("Token valid. Scopes: %s", scopes or "(none — fine-grained token)")
        elif resp.status_code == 401:
            log.error("Token is invalid or expired (HTTP 401). Data collection will fail.")
        else:
            log.warning("Token check returned HTTP %s", resp.status_code)
    except requests.RequestException:
        pass  # Best-effort check, don't block collection


def get_billable_multiplier(labels: list[str]) -> float:
    """Get billable minute multiplier from job labels. Self-hosted = 0."""
    for label in labels or []:
        label_lower = label.lower()
        if "self-hosted" in label_lower:
            return 0
        if label in OS_MULTIPLIERS:
            return OS_MULTIPLIERS[label]
        if label_lower.startswith("ubuntu-"):
            return 1
        if label_lower.startswith("macos-") or "macos" in label_lower:
            return 10
        if label_lower.startswith("windows-"):
            return 2
        if "arm" in label_lower and "macos" in label_lower:
            return 10
    log.warning("Unknown runner label %r — defaulting to 1x multiplier", labels)
    return 1


def parse_github_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def compute_job_duration_seconds(job: dict) -> float | None:
    started = parse_github_datetime(job.get("started_at"))
    completed = parse_github_datetime(job.get("completed_at"))
    if not started or not completed:
        return None
    return (completed - started).total_seconds()


def compute_step_duration_seconds(step: dict) -> float | None:
    started = parse_github_datetime(step.get("started_at"))
    completed = parse_github_datetime(step.get("completed_at"))
    if not started or not completed:
        return None
    return (completed - started).total_seconds()


def compute_queue_seconds(run: dict) -> float | None:
    created = parse_github_datetime(run.get("created_at"))
    started = parse_github_datetime(run.get("run_started_at"))
    if not created or not started:
        return None
    queue_seconds = (started - created).total_seconds()
    return queue_seconds if queue_seconds >= 0 else None


def compute_billable_minutes(jobs: list[dict]) -> tuple[float, float, float, float]:
    """Returns (linux, macos, windows, total)."""
    linux_mins = macos_mins = windows_mins = 0.0
    total = 0.0
    for job in jobs:
        duration_sec = compute_job_duration_seconds(job)
        if duration_sec is None or duration_sec <= 0:
            continue
        multiplier = get_billable_multiplier(job.get("labels", []))
        if multiplier == 0:
            continue
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


def load_repo_list(path: Path) -> list[str]:
    """Read repo full_names from a plain-text file, one per line. Blank lines and # comments ok."""
    if not path.exists():
        return []
    repos = []
    for line in path.read_text().readlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            repos.append(stripped)
    return repos


def fetch_repos(session: requests.Session, token: str) -> list[dict]:
    repos = []
    url = f"{API_BASE}/user/repos"
    params = {"per_page": 100, "sort": "updated", "type": "owner"}
    while url:
        resp = session.get(url, params=params, headers=get_headers(token), timeout=REQUEST_TIMEOUT)
        params = None
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            if resp.status_code in (401, 403):
                # Fallback chain: env var hardcoded list → repos.txt file → current repo
                current = os.environ.get("GITHUB_REPOSITORY", "")
                hardcoded = os.environ.get("REPO_LIST", "")
                file_repos = load_repo_list(Path("repos.txt"))
                if hardcoded:
                    log.warning(
                        "Token cannot list /user/repos (HTTP %s). Using REPO_LIST env var (%d repos).",
                        resp.status_code,
                        len(hardcoded.split(",")),
                    )
                    return [{"full_name": r.strip(), "archived": False} for r in hardcoded.split(",") if r.strip()]
                if file_repos:
                    log.warning(
                        "Token cannot list /user/repos (HTTP %s). Using repos.txt (%d repos).",
                        resp.status_code,
                        len(file_repos),
                    )
                    return [{"full_name": r, "archived": False} for r in file_repos]
                if current:
                    log.warning(
                        "Token cannot list /user/repos (HTTP %s). Falling back to current repo only: %s",
                        resp.status_code,
                        current,
                    )
                    return [{"full_name": current, "archived": False}]
                log.error(
                    "Token cannot list /user/repos (HTTP %s) and no fallback configured. "
                    "Set ACTIONS_USAGE_TOKEN to a PAT with repo+user scope, or create repos.txt with one repo per line.",
                    resp.status_code,
                )
                return []
            raise
        repos.extend(resp.json())
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
    runs = []
    url = f"{API_BASE}/repos/{owner}/{repo}/actions/runs"
    created = created_after.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"per_page": 100, "created": f">={created}", "status": "completed"}
    while url:
        resp = session.get(url, params=params, headers=get_headers(token), timeout=REQUEST_TIMEOUT)
        params = None
        if resp.status_code in (404, 451):
            break
        resp.raise_for_status()
        runs.extend(resp.json().get("workflow_runs", []))
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
    jobs = []
    url = f"{API_BASE}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    params = {"per_page": 100}
    while url:
        resp = session.get(url, headers=get_headers(token), params=params, timeout=REQUEST_TIMEOUT)
        params = None
        if resp.status_code != 200:
            if resp.status_code in (401, 403):
                log.warning("Token lacks permission to fetch jobs for %s/%s run %s (HTTP %s)", owner, repo, run_id, resp.status_code)
            elif resp.status_code == 404:
                log.debug("Run %s/%s/%s no longer exists (deleted or purged)", owner, repo, run_id)
            elif resp.status_code >= 500:
                log.warning("Server error fetching jobs for %s/%s run %s (HTTP %s)", owner, repo, run_id, resp.status_code)
            else:
                log.warning("Unexpected status %s fetching jobs for %s/%s run %s", resp.status_code, owner, repo, run_id)
            return jobs
        data = resp.json()
        jobs.extend(data.get("jobs", []))
        url = resp.links.get("next", {}).get("url")
        if url:
            time.sleep(0.05)
    return jobs


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(conn: sqlite3.Connection) -> None:
    # Migrate old schema: run_id INTEGER PRIMARY KEY → (run_id, repo) composite key
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='workflow_runs'").fetchone()
    if row:
        table_sql = row[0] or ""
        # Detect old single-column PK: "run_id INTEGER PRIMARY KEY" but NOT "PRIMARY KEY (run_id, repo)"
        is_old_schema = (
            "run_id INTEGER PRIMARY KEY" in table_sql
            and "PRIMARY KEY (run_id" not in table_sql
        )
        if is_old_schema:
            log.info("Migrating workflow_runs to composite primary key (run_id, repo)...")
            conn.execute("ALTER TABLE workflow_runs RENAME TO workflow_runs_old")
            _create_tables(conn)
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO workflow_runs
                    SELECT run_id, repo, owner, workflow_name, workflow_id, status, conclusion,
                           event, created_at, run_started_at, html_url, duration_seconds, queue_seconds,
                           billable_minutes_linux, billable_minutes_macos, billable_minutes_windows,
                           billable_minutes_total, trigger_event, updated_at
                    FROM workflow_runs_old
                """)
                conn.execute("DROP TABLE workflow_runs_old")
                log.info("Migration complete.")
            except Exception as e:
                log.error("Migration failed: %s", e)
            return
    _create_tables(conn)
    ensure_column(conn, "workflow_runs", "queue_seconds", "REAL DEFAULT 0")


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS workflow_runs (
            run_id INTEGER NOT NULL,
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
            queue_seconds REAL DEFAULT 0,
            billable_minutes_linux REAL DEFAULT 0,
            billable_minutes_macos REAL DEFAULT 0,
            billable_minutes_windows REAL DEFAULT 0,
            billable_minutes_total REAL DEFAULT 0,
            trigger_event TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, repo)
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

        CREATE TABLE IF NOT EXISTS audit_results (
            repo TEXT PRIMARY KEY,
            issues_json TEXT NOT NULL,
            audited_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS workflow_jobs (
            job_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            repo TEXT NOT NULL,
            status TEXT,
            conclusion TEXT,
            started_at TEXT,
            completed_at TEXT,
            duration_seconds REAL,
            runner_labels TEXT,
            billable_multiplier REAL DEFAULT 0,
            billable_minutes REAL DEFAULT 0,
            created_at TEXT,
            FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_run ON workflow_jobs(run_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_repo_created ON workflow_jobs(repo, created_at);

        CREATE TABLE IF NOT EXISTS job_steps (
            job_id INTEGER NOT NULL,
            step_number INTEGER NOT NULL,
            repo TEXT NOT NULL,
            workflow_name TEXT,
            job_name TEXT,
            step_name TEXT,
            status TEXT,
            conclusion TEXT,
            started_at TEXT,
            completed_at TEXT,
            duration_seconds REAL,
            created_at TEXT,
            PRIMARY KEY (job_id, step_number),
            FOREIGN KEY(job_id) REFERENCES workflow_jobs(job_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_job_steps_repo_created ON job_steps(repo, created_at);
        """
    )


def upsert_runs_batch(conn: sqlite3.Connection, runs: list[dict]) -> None:
    if not runs:
        return
    conn.executemany(
        """
        INSERT INTO workflow_runs (
            run_id, repo, owner, workflow_name, workflow_id, status, conclusion,
            event, created_at, run_started_at, html_url, duration_seconds, queue_seconds,
            billable_minutes_linux, billable_minutes_macos, billable_minutes_windows,
            billable_minutes_total, trigger_event
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, repo) DO UPDATE SET
            status = excluded.status,
            conclusion = excluded.conclusion,
            duration_seconds = excluded.duration_seconds,
            queue_seconds = excluded.queue_seconds,
            billable_minutes_linux = excluded.billable_minutes_linux,
            billable_minutes_macos = excluded.billable_minutes_macos,
            billable_minutes_windows = excluded.billable_minutes_windows,
            billable_minutes_total = excluded.billable_minutes_total,
            updated_at = CURRENT_TIMESTAMP
        """,
        [
            (
                r["run_id"],
                r["repo"],
                r["owner"],
                r["workflow_name"],
                r.get("workflow_id"),
                r.get("status"),
                r.get("conclusion"),
                r.get("event"),
                r.get("created_at"),
                r.get("run_started_at"),
                r.get("html_url"),
                r.get("duration_seconds"),
                r.get("queue_seconds"),
                r.get("billable_minutes_linux", 0),
                r.get("billable_minutes_macos", 0),
                r.get("billable_minutes_windows", 0),
                r.get("billable_minutes_total", 0),
                r.get("event"),
            )
            for r in runs
        ],
    )


def replace_jobs_and_steps(
    conn: sqlite3.Connection,
    job_rows: list[dict],
    step_rows: list[dict],
    run_ids: list[int],
) -> None:
    if run_ids:
        placeholders = ",".join("?" for _ in run_ids)
        conn.execute(f"DELETE FROM job_steps WHERE job_id IN (SELECT job_id FROM workflow_jobs WHERE run_id IN ({placeholders}))", run_ids)
        conn.execute(f"DELETE FROM workflow_jobs WHERE run_id IN ({placeholders})", run_ids)
    if job_rows:
        conn.executemany(
            """
            INSERT INTO workflow_jobs (
                job_id, run_id, repo, workflow_name, job_name, status, conclusion,
                started_at, completed_at, duration_seconds, runner_labels,
                billable_multiplier, billable_minutes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["job_id"],
                    row["run_id"],
                    row["repo"],
                    row["workflow_name"],
                    row["job_name"],
                    row["status"],
                    row["conclusion"],
                    row["started_at"],
                    row["completed_at"],
                    row["duration_seconds"],
                    row["runner_labels"],
                    row["billable_multiplier"],
                    row["billable_minutes"],
                    row["created_at"],
                )
                for row in job_rows
            ],
        )
    if step_rows:
        conn.executemany(
            """
            INSERT INTO job_steps (
                job_id, step_number, repo, workflow_name, job_name, step_name,
                status, conclusion, started_at, completed_at, duration_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["job_id"],
                    row["step_number"],
                    row["repo"],
                    row["workflow_name"],
                    row["job_name"],
                    row["step_name"],
                    row["status"],
                    row["conclusion"],
                    row["started_at"],
                    row["completed_at"],
                    row["duration_seconds"],
                    row["created_at"],
                )
                for row in step_rows
            ],
        )


def build_job_and_step_rows(run_data: dict, jobs: list[dict]) -> tuple[list[dict], list[dict]]:
    job_rows = []
    step_rows = []
    for job in jobs:
        job_id = job.get("id")
        if not job_id:
            continue
        duration_seconds = compute_job_duration_seconds(job)
        multiplier = get_billable_multiplier(job.get("labels", []))
        base_minutes = math.ceil(duration_seconds / 60) if duration_seconds and duration_seconds > 0 else 0
        billable_minutes = 0 if multiplier == 0 else base_minutes * multiplier
        job_name = job.get("name") or "Unnamed job"
        job_rows.append(
            {
                "job_id": job_id,
                "run_id": run_data["run_id"],
                "repo": run_data["repo"],
                "workflow_name": run_data["workflow_name"],
                "job_name": job_name,
                "status": job.get("status"),
                "conclusion": job.get("conclusion"),
                "started_at": job.get("started_at"),
                "completed_at": job.get("completed_at"),
                "duration_seconds": duration_seconds,
                "runner_labels": json.dumps(job.get("labels", [])),
                "billable_multiplier": multiplier,
                "billable_minutes": billable_minutes,
                "created_at": run_data["created_at"],
            }
        )
        for step in job.get("steps", []) or []:
            step_number = step.get("number")
            if step_number is None:
                continue
            step_rows.append(
                {
                    "job_id": job_id,
                    "step_number": step_number,
                    "repo": run_data["repo"],
                    "workflow_name": run_data["workflow_name"],
                    "job_name": job_name,
                    "step_name": step.get("name") or f"Step {step_number}",
                    "status": step.get("status"),
                    "conclusion": step.get("conclusion"),
                    "started_at": step.get("started_at"),
                    "completed_at": step.get("completed_at"),
                    "duration_seconds": compute_step_duration_seconds(step),
                    "created_at": run_data["created_at"],
                }
            )
    return job_rows, step_rows


def send_apprise_alert(urls: str, title: str, body: str) -> bool:
    if not urls or not urls.strip():
        return False
    try:
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
    if not apprise_urls:
        return

    allowance = int(os.environ.get("GITHUB_ACTIONS_ALLOWANCE", "2000"))
    threshold = allowance * 0.8
    month_row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now') THEN billable_minutes_total END), 0) AS this_month,
            COALESCE(SUM(CASE WHEN strftime('%Y-%m', created_at) = strftime('%Y-%m', date('now', '-1 month')) THEN billable_minutes_total END), 0) AS last_month
        FROM workflow_runs
        """
    ).fetchone()
    this_month = float(month_row["this_month"] or 0)
    last_month = float(month_row["last_month"] or 0)
    if this_month >= threshold:
        send_apprise_alert(
            apprise_urls,
            "GitHub Actions Usage Alert",
            f"Minutes used this month ({this_month:.0f}) exceeds 80% of allowance ({allowance}). "
            f"Current: {this_month:.0f}/{allowance} min.",
        )

    if last_month > 0 and this_month > (last_month * 1.5):
        delta_pct = ((this_month - last_month) / last_month) * 100
        send_apprise_alert(
            apprise_urls,
            "GitHub Actions Monthly Spike",
            f"Usage is up {delta_pct:.0f}% versus last month: {this_month:.0f} vs {last_month:.0f} minutes.",
        )

    week_row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN created_at >= datetime('now', '-7 days') THEN billable_minutes_total END), 0) AS last_7d,
            COALESCE(SUM(CASE WHEN created_at >= datetime('now', '-14 days')
                               AND created_at < datetime('now', '-7 days')
                              THEN billable_minutes_total END), 0) AS prev_7d
        FROM workflow_runs
        """
    ).fetchone()
    last_7d = float(week_row["last_7d"] or 0)
    prev_7d = float(week_row["prev_7d"] or 0)
    if prev_7d > 0 and last_7d > (prev_7d * 1.5):
        delta_pct = ((last_7d - prev_7d) / prev_7d) * 100
        send_apprise_alert(
            apprise_urls,
            "GitHub Actions Weekly Spike",
            f"Usage is up {delta_pct:.0f}% in the last 7 days: {last_7d:.0f} vs {prev_7d:.0f} billable minutes.",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect GitHub Actions workflow data")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch up to 90 days of history (for initial setup)",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Also audit workflow files across repos and store results",
    )
    args = parser.parse_args()

    token = os.environ.get("ACTIONS_USAGE_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set ACTIONS_USAGE_TOKEN or GITHUB_TOKEN")
    apprise_urls = os.environ.get("APPRISE_URL", "")

    # Validate token scopes early to avoid wasting API calls
    _validate_token(token)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    original_get = session.get

    def counted_get(*a, **kw):
        nonlocal api_calls
        api_calls += 1
        return original_get(*a, **kw)

    session.get = counted_get

    log.info("Fetching repos...")
    repos = [repo for repo in fetch_repos(session, token) if not repo.get("archived")]
    log.info("Found %d repos", len(repos))

    all_runs: list[tuple[dict, str, str]] = []
    for repo in repos:
        full_name = repo.get("full_name")
        if not full_name:
            continue
        owner, name = full_name.split("/", 1)
        try:
            runs = fetch_workflow_runs(session, token, owner, name, created_after)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (403, 404, 451):
                continue
            errors.append(f"{full_name}: {exc}")
            log.warning("Error %s: %s", full_name, exc)
            continue
        for run in runs:
            if run.get("id"):
                all_runs.append((run, owner, name))
        time.sleep(0.15)  # Rate-limit: avoid secondary rate limits across repos

    repos_with_runs = len({f"{owner}/{name}" for _, owner, name in all_runs})

    def fetch_jobs_for_run(item: tuple[dict, str, str]) -> tuple[dict, list[dict]]:
        run, owner, name = item
        jobs = fetch_run_jobs(session, token, owner, name, run["id"])
        return run, jobs

    run_data_list: list[dict] = []
    job_rows: list[dict] = []
    step_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_jobs_for_run, item): item for item in all_runs}
        for future in as_completed(futures):
            run, jobs = future.result()
            _, owner, name = futures[future]
            full_name = f"{owner}/{name}"
            linux_m, macos_m, windows_m, total_m = compute_billable_minutes(jobs)
            duration_sec = sum(
                seconds for job in jobs if (seconds := compute_job_duration_seconds(job)) is not None
            )
            run_data = {
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
                "queue_seconds": compute_queue_seconds(run),
                "billable_minutes_linux": linux_m,
                "billable_minutes_macos": macos_m,
                "billable_minutes_windows": windows_m,
                "billable_minutes_total": total_m,
                "trigger_event": run.get("event"),
            }
            run_data_list.append(run_data)
            new_job_rows, new_step_rows = build_job_and_step_rows(run_data, jobs)
            job_rows.extend(new_job_rows)
            step_rows.extend(new_step_rows)

    for i in range(0, len(run_data_list), 50):
        upsert_runs_batch(conn, run_data_list[i : i + 50])

    replace_jobs_and_steps(conn, job_rows, step_rows, [row["run_id"] for row in run_data_list])

    for repo in repos:
        full_name = repo.get("full_name", "")
        count = sum(1 for row in run_data_list if row["repo"] == full_name)
        if count:
            log.info("  %s: %d runs", full_name, count)

    total_runs = len(run_data_list)
    total_in_db = conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
    completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        UPDATE collection_log SET
            completed_at = ?, repos_scanned = ?, repos_with_runs = ?,
            runs_collected = ?, runs_updated = ?, api_calls = ?, errors = ?
        WHERE id = ?
        """,
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

    if args.audit:
        log.info("Running workflow audit across repos...")
        try:
            from audit import audit_repo_api

            repos_to_audit = list({row["repo"] for row in run_data_list})
            for full_name in repos_to_audit:
                owner, name = full_name.split("/", 1)
                try:
                    issues = audit_repo_api(session, token, owner, name)
                    conn.execute(
                        "INSERT OR REPLACE INTO audit_results (repo, issues_json, audited_at) VALUES (?, ?, ?)",
                        (full_name, json.dumps(issues), datetime.now(timezone.utc).isoformat()),
                    )
                    if issues:
                        log.info("  %s: %d audit issues", full_name, len(issues))
                except Exception as exc:  # pragma: no cover - best-effort audit
                    log.warning("Audit failed for %s: %s", full_name, exc)
            conn.commit()
        except ImportError:
            log.warning("audit module not available, skipping audit")

    # Data retention cleanup (#9)
    retention_days = int(os.environ.get("RETENTION_DAYS", "365"))
    if retention_days > 0:
        deleted = conn.execute(
            "DELETE FROM workflow_runs WHERE created_at < date('now', ?)",
            (f"-{retention_days} days",),
        ).rowcount
        if deleted:
            log.info("Cleaned up %d runs older than %s days", deleted, retention_days)
        conn.execute("PRAGMA optimize")

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
