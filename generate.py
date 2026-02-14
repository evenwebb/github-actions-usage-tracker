#!/usr/bin/env python3
"""
Generate static HTML dashboard from SQLite data.
Outputs to docs/ for GitHub Pages deployment.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

DB_PATH = Path(__file__).parent / "data" / "actions.db"
TEMPLATES_DIR = Path(__file__).parent / "templates"
OUTPUT_DIR = Path(__file__).parent / "docs"

# Configurable via env
DEFAULT_ALLOWANCE = 2000  # Free plan


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_month_total(conn: sqlite3.Connection) -> float:
    """Total billable minutes for current month."""
    row = conn.execute("""
        SELECT COALESCE(SUM(billable_minutes_total), 0) AS total
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
    """).fetchone()
    return float(row["total"])


def get_repos_by_minutes(conn: sqlite3.Connection) -> list[dict]:
    """Repos ranked by minutes consumed this month."""
    rows = conn.execute("""
        SELECT repo, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY repo
        ORDER BY minutes DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_daily_trend(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """Daily usage for the last N days."""
    rows = conn.execute("""
        SELECT date(created_at) AS day, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE created_at >= date('now', ?)
        GROUP BY date(created_at)
        ORDER BY day ASC
    """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def get_failures(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Failed runs across all repos, reverse chronological."""
    rows = conn.execute("""
        SELECT run_id, repo, workflow_name, conclusion, created_at, html_url
        FROM workflow_runs
        WHERE conclusion IN ('failure', 'cancelled', 'timed_out', 'action_required')
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_repo_workflows(conn: sqlite3.Connection, repo: str) -> list[dict]:
    """Workflow stats for a specific repo."""
    rows = conn.execute("""
        SELECT
            workflow_name,
            COUNT(*) AS run_count,
            AVG(duration_seconds) AS avg_duration,
            SUM(billable_minutes_total) AS total_minutes,
            SUM(CASE WHEN conclusion = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN conclusion IN ('failure','cancelled','timed_out') THEN 1 ELSE 0 END) AS failure_count
        FROM workflow_runs
        WHERE repo = ?
        AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY workflow_name
        ORDER BY total_minutes DESC
    """, (repo,)).fetchall()
    return [dict(r) for r in rows]


def get_repo_recent_runs(conn: sqlite3.Connection, repo: str, limit: int = 30) -> list[dict]:
    """Recent runs for a repo (for sparkline data)."""
    rows = conn.execute("""
        SELECT created_at, billable_minutes_total, conclusion
        FROM workflow_runs
        WHERE repo = ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (repo, limit)).fetchall()
    return [dict(r) for r in rows]


def get_all_repos(conn: sqlite3.Connection) -> list[str]:
    """List of all repos that have runs."""
    rows = conn.execute("""
        SELECT DISTINCT repo FROM workflow_runs ORDER BY repo
    """).fetchall()
    return [r["repo"] for r in rows]


def get_cost_projection(month_total: float, allowance: int) -> dict:
    """Project end-of-month usage based on current pace."""
    now = datetime.now()
    days_in_month = (now.replace(day=28) + timedelta(days=4)).day
    day_of_month = now.day
    if day_of_month <= 0:
        day_of_month = 1
    if month_total <= 0:
        projected = 0
    else:
        # Linear extrapolation
        projected = month_total * (days_in_month / day_of_month)
    pct = (projected / allowance * 100) if allowance else 0
    if pct < 70:
        status = "green"
    elif pct < 90:
        status = "amber"
    else:
        status = "red"
    return {
        "projected": round(projected),
        "allowance": allowance,
        "percent": round(pct, 1),
        "status": status,
    }


def get_global_stats(conn: sqlite3.Connection) -> dict:
    """Overall stats for current month."""
    row = conn.execute("""
        SELECT
            COUNT(*) AS total_runs,
            SUM(CASE WHEN conclusion = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN conclusion IN ('failure','cancelled','timed_out') THEN 1 ELSE 0 END) AS failure_count,
            AVG(duration_seconds) AS avg_duration,
            SUM(billable_minutes_linux) AS linux_mins,
            SUM(billable_minutes_macos) AS macos_mins,
            SUM(billable_minutes_windows) AS windows_mins
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
    """).fetchone()
    d = dict(row)
    total = (d["success_count"] or 0) + (d["failure_count"] or 0)
    return {
        "total_runs": d["total_runs"] or 0,
        "success_count": d["success_count"] or 0,
        "failure_count": d["failure_count"] or 0,
        "success_rate": round((d["success_count"] or 0) / total * 100, 1) if total else 0,
        "avg_duration": round(d["avg_duration"] or 0, 1),
        "linux_mins": round(d["linux_mins"] or 0, 1),
        "macos_mins": round(d["macos_mins"] or 0, 1),
        "windows_mins": round(d["windows_mins"] or 0, 1),
    }


def get_trigger_breakdown(conn: sqlite3.Connection) -> list[dict]:
    """Runs by trigger event this month."""
    rows = conn.execute("""
        SELECT event AS trigger, COUNT(*) AS runs, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY event
        ORDER BY minutes DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_monthly_usage(conn: sqlite3.Connection, months: int = 12) -> list[dict]:
    """Monthly billable minutes for the last N months."""
    rows = conn.execute("""
        SELECT strftime('%Y-%m', created_at) AS month, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE created_at >= date('now', ?)
        GROUP BY strftime('%Y-%m', created_at)
        ORDER BY month ASC
    """, (f"-{months} months",)).fetchall()
    return [dict(r) for r in rows]


def get_top_workflows(conn: sqlite3.Connection, limit: int = 15) -> list[dict]:
    """Top workflows by minutes across all repos this month."""
    rows = conn.execute("""
        SELECT repo, workflow_name, COUNT(*) AS runs, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY repo, workflow_name
        ORDER BY minutes DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_dead_workflows(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Workflows that haven't had a successful run in N+ days."""
    rows = conn.execute("""
        SELECT repo, workflow_name, MAX(created_at) AS last_success
        FROM workflow_runs
        WHERE conclusion = 'success'
        GROUP BY repo, workflow_name
        HAVING last_success < date('now', ?)
    """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def get_collection_log(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Recent collection run logs."""
    import json
    try:
        rows = conn.execute("""
            SELECT started_at, completed_at, repos_scanned, repos_with_runs,
                   runs_collected, runs_updated, api_calls, errors, backfill
            FROM collection_log
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("errors"):
                try:
                    d["error_list"] = json.loads(d["errors"])
                    d["error_count"] = len(d["error_list"])
                except (json.JSONDecodeError, TypeError):
                    d["error_list"] = []
                    d["error_count"] = 0
            else:
                d["error_list"] = []
                d["error_count"] = 0
            result.append(d)
        return result
    except sqlite3.OperationalError:
        return []


def get_repo_monthly(conn: sqlite3.Connection, repo: str, months: int = 6) -> list[dict]:
    """Monthly usage for a specific repo."""
    rows = conn.execute("""
        SELECT strftime('%Y-%m', created_at) AS month, SUM(billable_minutes_total) AS minutes, COUNT(*) AS runs
        FROM workflow_runs
        WHERE repo = ? AND created_at >= date('now', ?)
        GROUP BY strftime('%Y-%m', created_at)
        ORDER BY month ASC
    """, (repo, f"-{months} months")).fetchall()
    return [dict(r) for r in rows]


def get_workflow_efficiency(conn: sqlite3.Connection, repo: str) -> list[dict]:
    """Workflows ranked by minutes per successful run (efficiency)."""
    rows = conn.execute("""
        SELECT workflow_name, COUNT(*) AS runs,
               SUM(CASE WHEN conclusion = 'success' THEN 1 ELSE 0 END) AS success_count,
               SUM(billable_minutes_total) AS total_minutes
        FROM workflow_runs
        WHERE repo = ? AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY workflow_name
        HAVING success_count > 0
    """, (repo,)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        mins_per_success = (d["total_minutes"] or 0) / (d["success_count"] or 1)
        d["mins_per_success"] = round(mins_per_success, 1)
        result.append(d)
    result.sort(key=lambda x: x["mins_per_success"], reverse=True)
    return result


def main() -> None:
    import os
    allowance = int(os.environ.get("GITHUB_ACTIONS_ALLOWANCE", str(DEFAULT_ALLOWANCE)))

    if not DB_PATH.exists():
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        placeholder = """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>GitHub Actions Usage</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:2rem auto;padding:1rem">
<h1>No data yet</h1>
<p>Run the workflow or <code>python collect.py</code> first. Data will appear after the first collection.</p>
<nav><a href="index.html">Overview</a> · <a href="history.html">History</a> · <a href="failures.html">Failures</a> · <a href="logs.html">Logs</a></nav>
</body></html>"""
        for name in ("index.html", "history.html", "failures.html", "logs.html"):
            (OUTPUT_DIR / name).write_text(placeholder)
        print("No database found. Created placeholder pages.")
        return

    conn = get_conn()
    month_total = get_month_total(conn)
    repos_ranked = get_repos_by_minutes(conn)
    daily_trend = get_daily_trend(conn, 90)
    failures = get_failures(conn)
    projection = get_cost_projection(month_total, allowance)
    all_repos = get_all_repos(conn)
    global_stats = get_global_stats(conn)
    trigger_breakdown = get_trigger_breakdown(conn)
    monthly_usage = get_monthly_usage(conn, 12)
    top_workflows = get_top_workflows(conn)
    dead_workflows = get_dead_workflows(conn)
    collection_log = get_collection_log(conn)

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    env.globals["now"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Main index
    index_tpl = env.get_template("index.html")
    (OUTPUT_DIR / "index.html").write_text(
        index_tpl.render(
            month_total=round(month_total),
            allowance=allowance,
            repos_ranked=repos_ranked,
            daily_trend=daily_trend,
            projection=projection,
            global_stats=global_stats,
            trigger_breakdown=trigger_breakdown,
            monthly_usage=monthly_usage,
            top_workflows=top_workflows,
            dead_workflows=dead_workflows,
            collection_log=collection_log,
        )
    )

    # Failures page
    failures_tpl = env.get_template("failures.html")
    (OUTPUT_DIR / "failures.html").write_text(
        failures_tpl.render(failures=failures)
    )

    # History page
    history_tpl = env.get_template("history.html")
    (OUTPUT_DIR / "history.html").write_text(
        history_tpl.render(monthly_usage=monthly_usage, allowance=allowance)
    )

    # Logs page
    logs_tpl = env.get_template("logs.html")
    (OUTPUT_DIR / "logs.html").write_text(
        logs_tpl.render(collection_log=collection_log)
    )

    # Per-repo pages
    repo_tpl = env.get_template("repo.html")
    for repo in all_repos:
        workflows = get_repo_workflows(conn, repo)
        recent = get_repo_recent_runs(conn, repo)
        repo_monthly = get_repo_monthly(conn, repo)
        workflow_efficiency = get_workflow_efficiency(conn, repo)
        safe_name = repo.replace("/", "_")
        (OUTPUT_DIR / f"repo_{safe_name}.html").write_text(
            repo_tpl.render(
                repo=repo,
                workflows=workflows,
                recent_runs=recent,
                repo_monthly=repo_monthly,
                workflow_efficiency=workflow_efficiency,
            )
        )
    conn.close()

    print(f"Generated: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
