#!/usr/bin/env python3
"""
Generate static HTML dashboard from SQLite data.
Outputs to docs/ for GitHub Pages deployment.
"""

import csv
import json
import math
import os
import sqlite3
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

DB_PATH = Path(__file__).parent / "data" / "actions.db"
TEMPLATES_DIR = Path(__file__).parent / "templates"
OUTPUT_DIR = Path(__file__).parent / "docs"

DEFAULT_ALLOWANCE = 2000
FAILURE_CONCLUSIONS = ("failure", "cancelled", "timed_out", "action_required")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_month_total(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(billable_minutes_total), 0) AS total
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        """
    ).fetchone()
    return float(row["total"] or 0)


def get_repos_by_minutes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT repo, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY repo
        ORDER BY minutes DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def get_daily_trend(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    rows = conn.execute(
        """
        SELECT date(created_at) AS day, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE created_at >= date('now', ?)
        GROUP BY date(created_at)
        ORDER BY day ASC
        """,
        (f"-{days} days",),
    ).fetchall()
    return [dict(row) for row in rows]


def get_failures(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """
        SELECT run_id, repo, workflow_name, conclusion, created_at, html_url, billable_minutes_total
        FROM workflow_runs
        WHERE conclusion IN ('failure', 'cancelled', 'timed_out', 'action_required')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_repo_workflows(conn: sqlite3.Connection, repo: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            workflow_name,
            COUNT(*) AS run_count,
            AVG(duration_seconds) AS avg_duration,
            AVG(queue_seconds) AS avg_queue_seconds,
            SUM(billable_minutes_total) AS total_minutes,
            SUM(CASE WHEN conclusion = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN conclusion IN ('failure', 'cancelled', 'timed_out', 'action_required') THEN 1 ELSE 0 END) AS failure_count,
            SUM(CASE WHEN conclusion IN ('failure', 'cancelled', 'timed_out', 'action_required') THEN billable_minutes_total ELSE 0 END) AS wasted_minutes
        FROM workflow_runs
        WHERE repo = ?
          AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY workflow_name
        ORDER BY total_minutes DESC
        """,
        (repo,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_all_repo_workflows(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Batched version: returns {repo: [workflow_stats]} for all repos."""
    rows = conn.execute(
        """
        SELECT
            repo,
            workflow_name,
            COUNT(*) AS run_count,
            AVG(duration_seconds) AS avg_duration,
            AVG(queue_seconds) AS avg_queue_seconds,
            SUM(billable_minutes_total) AS total_minutes,
            SUM(CASE WHEN conclusion = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN conclusion IN ('failure', 'cancelled', 'timed_out', 'action_required') THEN 1 ELSE 0 END) AS failure_count,
            SUM(CASE WHEN conclusion IN ('failure', 'cancelled', 'timed_out', 'action_required') THEN billable_minutes_total ELSE 0 END) AS wasted_minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY repo, workflow_name
        ORDER BY total_minutes DESC
        """
    ).fetchall()
    result: dict[str, list[dict]] = {}
    for row in rows:
        d = dict(row)
        repo = d.pop("repo")
        result.setdefault(repo, []).append(d)
    return result


def get_repo_recent_runs(conn: sqlite3.Connection, repo: str, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        """
        SELECT created_at, billable_minutes_total, conclusion, queue_seconds
        FROM workflow_runs
        WHERE repo = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (repo, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_all_recent_runs(conn: sqlite3.Connection, limit: int = 30) -> dict[str, list[dict]]:
    """Batched version: returns {repo: [recent_runs]} for all repos using window functions."""
    rows = conn.execute(
        """
        SELECT repo, created_at, billable_minutes_total, conclusion, queue_seconds
        FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY repo ORDER BY created_at DESC) AS rn
            FROM workflow_runs
        )
        WHERE rn <= ?
        ORDER BY repo, created_at DESC
        """,
        (limit,),
    ).fetchall()
    result: dict[str, list[dict]] = {}
    for row in rows:
        d = dict(row)
        repo = d.pop("repo")
        result.setdefault(repo, []).append(d)
    return result


def get_all_repos(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT repo FROM workflow_runs ORDER BY repo").fetchall()
    return [row["repo"] for row in rows]


def get_cost_projection(month_total: float, allowance: int) -> dict:
    now = datetime.now()
    days_in_month = monthrange(now.year, now.month)[1]
    day_of_month = max(now.day, 1)
    projected = month_total * (days_in_month / day_of_month) if month_total > 0 else 0
    pct = (projected / allowance * 100) if allowance else 0
    if pct < 70:
        status = "green"
    elif pct < 90:
        status = "amber"
    else:
        status = "red"
    overage_mins = max(0, projected - allowance)
    estimated_cost = round(overage_mins * 0.008, 2) if overage_mins > 0 else 0
    return {
        "projected": round(projected),
        "allowance": allowance,
        "percent": round(pct, 1),
        "status": status,
        "overage_minutes": round(overage_mins),
        "estimated_cost_usd": estimated_cost,
    }


def get_global_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_runs,
            SUM(CASE WHEN conclusion = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN conclusion IN ('failure', 'cancelled', 'timed_out', 'action_required') THEN 1 ELSE 0 END) AS failure_count,
            AVG(duration_seconds) AS avg_duration,
            AVG(queue_seconds) AS avg_queue_seconds,
            SUM(billable_minutes_linux) AS linux_mins,
            SUM(billable_minutes_macos) AS macos_mins,
            SUM(billable_minutes_windows) AS windows_mins,
            SUM(CASE WHEN conclusion IN ('failure', 'cancelled', 'timed_out', 'action_required') THEN billable_minutes_total ELSE 0 END) AS wasted_minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        """
    ).fetchone()
    data = dict(row)
    total = (data["success_count"] or 0) + (data["failure_count"] or 0)
    total_minutes = (data["linux_mins"] or 0) + (data["macos_mins"] or 0) + (data["windows_mins"] or 0)
    wasted_minutes = data["wasted_minutes"] or 0
    return {
        "total_runs": data["total_runs"] or 0,
        "success_count": data["success_count"] or 0,
        "failure_count": data["failure_count"] or 0,
        "success_rate": round((data["success_count"] or 0) / total * 100, 1) if total else 0,
        "avg_duration": round(data["avg_duration"] or 0, 1),
        "avg_queue_seconds": round(data["avg_queue_seconds"] or 0, 1),
        "linux_mins": round(data["linux_mins"] or 0, 1),
        "macos_mins": round(data["macos_mins"] or 0, 1),
        "windows_mins": round(data["windows_mins"] or 0, 1),
        "wasted_minutes": round(wasted_minutes, 1),
        "wasted_pct": round((wasted_minutes / total_minutes) * 100, 1) if total_minutes else 0,
    }


def get_trigger_breakdown(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT event AS trigger, COUNT(*) AS runs, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY event
        ORDER BY minutes DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def get_monthly_usage(conn: sqlite3.Connection, months: int = 12) -> list[dict]:
    rows = conn.execute(
        """
        SELECT strftime('%Y-%m', created_at) AS month, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE created_at >= date('now', ?)
        GROUP BY strftime('%Y-%m', created_at)
        ORDER BY month ASC
        """,
        (f"-{months} months",),
    ).fetchall()
    return [dict(row) for row in rows]


def get_top_workflows(conn: sqlite3.Connection, limit: int = 15) -> list[dict]:
    rows = conn.execute(
        """
        SELECT repo, workflow_name, COUNT(*) AS runs, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY repo, workflow_name
        ORDER BY minutes DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_dead_workflows(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    rows = conn.execute(
        """
        SELECT repo, workflow_name, MAX(created_at) AS last_success
        FROM workflow_runs
        WHERE conclusion = 'success'
        GROUP BY repo, workflow_name
        HAVING last_success < date('now', ?)
        """,
        (f"-{days} days",),
    ).fetchall()
    now = datetime.now(timezone.utc)
    result = []
    for row in rows:
        d = dict(row)
        last = d.get("last_success", "")
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                d["days_ago"] = (now - last_dt).days
            except (ValueError, TypeError):
                d["days_ago"] = None
        else:
            d["days_ago"] = None
        result.append(d)
    return result


def get_collection_log(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    try:
        rows = conn.execute(
            """
            SELECT started_at, completed_at, repos_scanned, repos_with_runs,
                   runs_collected, runs_updated, api_calls, errors, backfill
            FROM collection_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    result = []
    for row in rows:
        data = dict(row)
        if data.get("errors"):
            try:
                data["error_list"] = json.loads(data["errors"])
                data["error_count"] = len(data["error_list"])
            except (json.JSONDecodeError, TypeError):
                data["error_list"] = []
                data["error_count"] = 0
        else:
            data["error_list"] = []
            data["error_count"] = 0
        result.append(data)
    return result


def get_repo_monthly(conn: sqlite3.Connection, repo: str, months: int = 6) -> list[dict]:
    rows = conn.execute(
        """
        SELECT strftime('%Y-%m', created_at) AS month, SUM(billable_minutes_total) AS minutes, COUNT(*) AS runs
        FROM workflow_runs
        WHERE repo = ? AND created_at >= date('now', ?)
        GROUP BY strftime('%Y-%m', created_at)
        ORDER BY month ASC
        """,
        (repo, f"-{months} months"),
    ).fetchall()
    return [dict(row) for row in rows]


def get_audit_results(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            """
            SELECT repo, issues_json, audited_at
            FROM audit_results
            ORDER BY repo
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    result = []
    for row in rows:
        try:
            issues = json.loads(row["issues_json"]) if row["issues_json"] else []
        except (json.JSONDecodeError, TypeError):
            issues = []
        high = sum(1 for issue in issues if issue.get("severity") == "high")
        medium = sum(1 for issue in issues if issue.get("severity") == "medium")
        result.append(
            {
                "repo": row["repo"],
                "issues": issues,
                "count": len(issues),
                "high": high,
                "medium": medium,
                "audited_at": row["audited_at"],
            }
        )
    return [item for item in result if item["count"] > 0]


def get_workflow_efficiency(conn: sqlite3.Connection, repo: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT workflow_name, COUNT(*) AS runs,
               SUM(CASE WHEN conclusion = 'success' THEN 1 ELSE 0 END) AS success_count,
               SUM(billable_minutes_total) AS total_minutes
        FROM workflow_runs
        WHERE repo = ? AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY workflow_name
        HAVING success_count > 0
        """,
        (repo,),
    ).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["mins_per_success"] = round((data["total_minutes"] or 0) / (data["success_count"] or 1), 1)
        result.append(data)
    result.sort(key=lambda item: item["mins_per_success"], reverse=True)
    return result


def get_month_comparison(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT strftime('%Y-%m', created_at) AS month, SUM(billable_minutes_total) AS minutes, COUNT(*) AS runs
        FROM workflow_runs
        WHERE created_at >= date('now', '-2 months')
        GROUP BY strftime('%Y-%m', created_at)
        ORDER BY month DESC
        LIMIT 2
        """
    ).fetchall()
    data = {row["month"]: dict(row) for row in rows}
    this_month = datetime.now().strftime("%Y-%m")
    last_month = (datetime.now().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return {
        "this_month": data.get(this_month, {}).get("minutes", 0) or 0,
        "last_month": data.get(last_month, {}).get("minutes", 0) or 0,
        "this_month_runs": data.get(this_month, {}).get("runs", 0) or 0,
        "last_month_runs": data.get(last_month, {}).get("runs", 0) or 0,
    }


def get_year_over_year(conn: sqlite3.Connection) -> dict:
    this_month = datetime.now().strftime("%Y-%m")
    last_year_month = f"{datetime.now().year - 1}-{datetime.now().month:02d}"
    rows = conn.execute(
        """
        SELECT strftime('%Y-%m', created_at) AS month, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) IN (?, ?)
        GROUP BY strftime('%Y-%m', created_at)
        """,
        (this_month, last_year_month),
    ).fetchall()
    data = {row["month"]: row["minutes"] for row in rows}
    return {
        "this_month": data.get(this_month, 0) or 0,
        "same_month_last_year": data.get(last_year_month, 0) or 0,
    }


def get_export_data(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    rows = conn.execute(
        """
        SELECT run_id, repo, workflow_name, event, conclusion, created_at,
               duration_seconds, queue_seconds, billable_minutes_total
        FROM workflow_runs
        WHERE created_at >= date('now', ?)
        ORDER BY created_at DESC
        """,
        (f"-{days} days",),
    ).fetchall()
    return [dict(row) for row in rows]


def get_audit_summary(audit_by_repo: list[dict]) -> dict | None:
    if not audit_by_repo:
        return None
    total = sum(item["count"] for item in audit_by_repo)
    if total == 0:
        return None
    return {"repos_with_issues": len(audit_by_repo), "total_issues": total}


def get_filter_options(conn: sqlite3.Connection) -> dict:
    repos = [row[0] for row in conn.execute("SELECT DISTINCT repo FROM workflow_runs ORDER BY repo").fetchall()]
    workflows = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT workflow_name FROM workflow_runs WHERE workflow_name IS NOT NULL ORDER BY workflow_name"
        ).fetchall()
    ]
    events = [
        row[0]
        for row in conn.execute("SELECT DISTINCT event FROM workflow_runs WHERE event IS NOT NULL ORDER BY event").fetchall()
    ]
    return {"repos": repos, "workflows": workflows, "events": events}


def get_wasted_minutes_summary(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN conclusion IN ('failure', 'cancelled', 'timed_out', 'action_required') THEN billable_minutes_total ELSE 0 END), 0) AS wasted_minutes,
            COALESCE(SUM(billable_minutes_total), 0) AS total_minutes,
            COUNT(CASE WHEN conclusion IN ('failure', 'cancelled', 'timed_out', 'action_required') THEN 1 END) AS failed_runs
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        """
    ).fetchone()
    wasted_minutes = float(row["wasted_minutes"] or 0)
    total_minutes = float(row["total_minutes"] or 0)
    return {
        "wasted_minutes": round(wasted_minutes, 1),
        "failed_runs": row["failed_runs"] or 0,
        "wasted_pct": round((wasted_minutes / total_minutes) * 100, 1) if total_minutes else 0,
    }


def get_queue_summary(conn: sqlite3.Connection, repo: str | None = None) -> dict:
    filters = ["queue_seconds IS NOT NULL", "queue_seconds > 0"]
    params: list[str] = []
    if repo:
        filters.append("repo = ?")
        params.append(repo)
    row = conn.execute(
        f"""
        SELECT AVG(queue_seconds) AS avg_queue_seconds, MAX(queue_seconds) AS max_queue_seconds
        FROM workflow_runs
        WHERE {' AND '.join(filters)}
        """,
        params,
    ).fetchone()
    samples = conn.execute(
        f"""
        SELECT queue_seconds
        FROM workflow_runs
        WHERE {' AND '.join(filters)}
        ORDER BY queue_seconds
        """,
        params,
    ).fetchall()
    values = [float(item["queue_seconds"]) for item in samples if item["queue_seconds"] is not None]
    if values:
        idx = max(0, int(math.ceil(len(values) * 0.95)) - 1)
        p95 = values[min(len(values) - 1, idx)]
    else:
        p95 = 0
    return {
        "avg_queue_seconds": round(row["avg_queue_seconds"] or 0, 1),
        "max_queue_seconds": round(row["max_queue_seconds"] or 0, 1),
        "p95_queue_seconds": round(p95, 1),
        "samples": len(values),
    }


def get_failure_cost_leaderboard(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """
        SELECT repo, workflow_name,
               COUNT(*) AS failed_runs,
               SUM(billable_minutes_total) AS wasted_minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
          AND conclusion IN ('failure', 'cancelled', 'timed_out', 'action_required')
        GROUP BY repo, workflow_name
        ORDER BY wasted_minutes DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _compute_streaks(rows: list[sqlite3.Row], key_fields: tuple[str, ...]) -> list[dict]:
    streaks: dict[tuple, dict] = {}
    closed: set[tuple] = set()
    for row in rows:
        key = tuple(row[field] for field in key_fields)
        if key in closed:
            continue
        is_failure = row["conclusion"] in FAILURE_CONCLUSIONS
        if key not in streaks:
            if not is_failure:
                closed.add(key)
                continue
            streaks[key] = {
                "repo": row["repo"],
                "workflow_name": row["workflow_name"],
                "count": 1,
                "last_created_at": row["created_at"],
                "minutes": float(row["billable_minutes_total"] or 0),
            }
            continue
        if is_failure:
            streaks[key]["count"] += 1
            streaks[key]["minutes"] += float(row["billable_minutes_total"] or 0)
        else:
            closed.add(key)
    active = [item for key, item in streaks.items() if key not in closed]
    active.sort(key=lambda item: (item["count"], item["minutes"]), reverse=True)
    return active


def get_failure_streaks(conn: sqlite3.Connection, repo: str | None = None) -> dict:
    params: list[str] = []
    where = "created_at >= date('now', '-180 days')"
    if repo:
        where += " AND repo = ?"
        params.append(repo)
    rows = conn.execute(
        f"""
        SELECT repo, workflow_name, conclusion, created_at, billable_minutes_total
        FROM workflow_runs
        WHERE {where}
        ORDER BY created_at DESC
        """
        ,
        params,
    ).fetchall()
    return {
        "repos": _compute_streaks(rows, ("repo",))[:10],
        "workflows": _compute_streaks(rows, ("repo", "workflow_name"))[:10],
    }


def get_monthly_burndown(conn: sqlite3.Connection, allowance: int) -> list[dict]:
    now = datetime.now()
    year = now.year
    month = now.month
    _, days_in_month = monthrange(year, month)
    rows = conn.execute(
        """
        SELECT CAST(strftime('%d', created_at) AS INTEGER) AS day, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY CAST(strftime('%d', created_at) AS INTEGER)
        ORDER BY day ASC
        """
    ).fetchall()
    minutes_by_day = {row["day"]: float(row["minutes"] or 0) for row in rows}
    cumulative = 0.0
    burndown = []
    daily_allowance = allowance / days_in_month if days_in_month else 0
    projected_end = 0.0
    for day in range(1, days_in_month + 1):
        cumulative += minutes_by_day.get(day, 0.0)
        pace_projection = cumulative * (days_in_month / day) if day else 0
        if day == now.day:
            projected_end = pace_projection
        burndown.append(
            {
                "day": day,
                "label": f"{year}-{month:02d}-{day:02d}",
                "daily_minutes": round(minutes_by_day.get(day, 0.0), 1),
                "cumulative_minutes": round(cumulative, 1),
                "remaining_allowance": round(max(0, allowance - cumulative), 1),
                "safe_remaining": round(max(0, allowance - (daily_allowance * day)), 1),
                "projected_end": round(pace_projection, 1),
            }
        )
    return burndown


def get_biggest_movers(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """
        WITH monthly AS (
            SELECT
                repo,
                SUM(CASE WHEN strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now') THEN billable_minutes_total ELSE 0 END) AS this_month,
                SUM(CASE WHEN strftime('%Y-%m', created_at) = strftime('%Y-%m', date('now', '-1 month')) THEN billable_minutes_total ELSE 0 END) AS last_month
            FROM workflow_runs
            GROUP BY repo
        )
        SELECT repo, this_month, last_month, (this_month - last_month) AS delta
        FROM monthly
        WHERE this_month > 0 OR last_month > 0
        ORDER BY delta DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    movers = []
    for row in rows:
        this_month = float(row["this_month"] or 0)
        last_month = float(row["last_month"] or 0)
        delta = float(row["delta"] or 0)
        movers.append(
            {
                "repo": row["repo"],
                "this_month": round(this_month, 1),
                "last_month": round(last_month, 1),
                "delta": round(delta, 1),
                "pct_change": round(((this_month - last_month) / last_month) * 100, 1) if last_month > 0 else None,
            }
        )
    return movers


def get_job_hotspots(conn: sqlite3.Connection, repo: str | None = None, limit: int = 10) -> list[dict]:
    try:
        filters = ["created_at >= date('now', '-30 days')"]
        params: list[object] = []
        if repo:
            filters.append("repo = ?")
            params.append(repo)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT repo, workflow_name, job_name,
                   COUNT(*) AS job_runs,
                   AVG(duration_seconds) AS avg_duration,
                   SUM(billable_minutes) AS billable_minutes
            FROM workflow_jobs
            WHERE {' AND '.join(filters)}
            GROUP BY repo, workflow_name, job_name
            ORDER BY billable_minutes DESC, avg_duration DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def get_step_hotspots(conn: sqlite3.Connection, repo: str | None = None, limit: int = 10) -> list[dict]:
    try:
        filters = ["created_at >= date('now', '-30 days')", "duration_seconds IS NOT NULL"]
        params: list[object] = []
        if repo:
            filters.append("repo = ?")
            params.append(repo)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT repo, workflow_name, job_name, step_name,
                   COUNT(*) AS step_runs,
                   AVG(duration_seconds) AS avg_duration,
                   SUM(duration_seconds) AS total_duration
            FROM job_steps
            WHERE {' AND '.join(filters)}
            GROUP BY repo, workflow_name, job_name, step_name
            HAVING step_runs >= 2
            ORDER BY total_duration DESC, avg_duration DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def main() -> None:
    allowance = int(os.environ.get("GITHUB_ACTIONS_ALLOWANCE", str(DEFAULT_ALLOWANCE)))

    if not DB_PATH.exists():
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        placeholder = """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>GitHub Actions Usage</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:2rem auto;padding:1rem">
<h1>No data yet</h1>
<p>Run the workflow or <code>python collect.py</code> first. Data will appear after the first collection.</p>
<nav><a href="index.html">Overview</a> · <a href="history.html">History</a> · <a href="explore.html">Explore</a> · <a href="audit.html">Audit</a> · <a href="failures.html">Failures</a> · <a href="logs.html">Logs</a></nav>
</body></html>"""
        for name in ("index.html", "history.html", "explore.html", "audit.html", "failures.html", "logs.html"):
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
    # Cross-reference: dormant workflows in repos that still have active runs
    dormant_repos_with_activity = set()
    if dead_workflows:
        active_repos = {row[0] for row in conn.execute(
            "SELECT DISTINCT repo FROM workflow_runs WHERE created_at >= date('now', '-7 days')"
        ).fetchall()}
        for dw in dead_workflows:
            if dw["repo"] in active_repos:
                dormant_repos_with_activity.add(dw["repo"])
    collection_log = get_collection_log(conn)
    audit_by_repo = get_audit_results(conn)
    audit_summary = get_audit_summary(audit_by_repo)
    month_comparison = get_month_comparison(conn)
    year_over_year = get_year_over_year(conn)
    export_data = get_export_data(conn, 90)
    filter_options = get_filter_options(conn)
    wasted_summary = get_wasted_minutes_summary(conn)
    queue_summary = get_queue_summary(conn)
    failure_costs = get_failure_cost_leaderboard(conn)
    failure_streaks = get_failure_streaks(conn)
    burndown = get_monthly_burndown(conn, allowance)
    biggest_movers = get_biggest_movers(conn)
    job_hotspots = get_job_hotspots(conn)
    step_hotspots = get_step_hotspots(conn)

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    env.globals["now"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

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
            dormant_repos_with_activity=dormant_repos_with_activity,
            collection_log=collection_log,
            month_comparison=month_comparison,
            year_over_year=year_over_year,
            audit_summary=audit_summary,
            wasted_summary=wasted_summary,
            queue_summary=queue_summary,
            failure_costs=failure_costs,
            failure_streaks=failure_streaks,
            biggest_movers=biggest_movers,
            job_hotspots=job_hotspots,
            step_hotspots=step_hotspots,
        )
    )

    failure_repos = sorted({item["repo"] for item in failures})
    failure_workflows = sorted({item.get("workflow_name") or "" for item in failures if item.get("workflow_name")})
    failure_conclusions = sorted({item.get("conclusion") or "" for item in failures if item.get("conclusion")})
    failures_tpl = env.get_template("failures.html")
    (OUTPUT_DIR / "failures.html").write_text(
        failures_tpl.render(
            failures=failures,
            failure_repos=failure_repos,
            failure_workflows=failure_workflows,
            failure_conclusions=failure_conclusions,
        )
    )

    history_tpl = env.get_template("history.html")
    (OUTPUT_DIR / "history.html").write_text(
        history_tpl.render(
            monthly_usage=monthly_usage,
            allowance=allowance,
            month_comparison=month_comparison,
            year_over_year=year_over_year,
            burndown=burndown,
        )
    )

    logs_tpl = env.get_template("logs.html")
    (OUTPUT_DIR / "logs.html").write_text(logs_tpl.render(collection_log=collection_log))

    audit_tpl = env.get_template("audit.html")
    (OUTPUT_DIR / "audit.html").write_text(audit_tpl.render(audit_by_repo=audit_by_repo))

    explore_tpl = env.get_template("explore.html")
    (OUTPUT_DIR / "explore.html").write_text(
        explore_tpl.render(
            filter_options=filter_options,
            export_data=export_data,
        )
    )

    export_dir = OUTPUT_DIR / "export"
    export_dir.mkdir(exist_ok=True)
    (export_dir / "usage.json").write_text(json.dumps(export_data, indent=2, default=str))
    if export_data:
        with open(export_dir / "usage.csv", "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=export_data[0].keys(), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(export_data)
    else:
        (export_dir / "usage.csv").write_text(
            "run_id,repo,workflow_name,event,conclusion,created_at,duration_seconds,queue_seconds,billable_minutes_total\n"
        )

    repo_tpl = env.get_template("repo.html")
    audit_by_repo_map = {item["repo"]: item for item in audit_by_repo}
    # Batch-fetch per-repo data to avoid N+1 queries
    all_workflows = get_all_repo_workflows(conn)
    all_recent_runs = get_all_recent_runs(conn)
    for repo in all_repos:
        workflows = all_workflows.get(repo, [])
        recent_runs = all_recent_runs.get(repo, [])
        repo_monthly = get_repo_monthly(conn, repo)
        workflow_efficiency = get_workflow_efficiency(conn, repo)
        repo_audit = audit_by_repo_map.get(repo, {})
        repo_queue_summary = get_queue_summary(conn, repo)
        repo_streaks = get_failure_streaks(conn, repo)
        repo_job_hotspots = get_job_hotspots(conn, repo, 8)
        repo_step_hotspots = get_step_hotspots(conn, repo, 8)
        safe_name = repo.replace("/", "_")
        (OUTPUT_DIR / f"repo_{safe_name}.html").write_text(
            repo_tpl.render(
                repo=repo,
                workflows=workflows,
                recent_runs=recent_runs,
                repo_monthly=repo_monthly,
                workflow_efficiency=workflow_efficiency,
                repo_audit=repo_audit,
                repo_queue_summary=repo_queue_summary,
                repo_streaks=repo_streaks,
                repo_job_hotspots=repo_job_hotspots,
                repo_step_hotspots=repo_step_hotspots,
            )
        )

    conn.close()
    print(f"Generated: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
