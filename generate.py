#!/usr/bin/env python3
"""
Generate static HTML dashboard from SQLite data.
Outputs to docs/ for GitHub Pages deployment.
"""

import csv
import json
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
    """Project end-of-month usage based on current pace. Includes estimated overage cost."""
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
    # GitHub overage: ~$0.008/min Linux, $0.016 Windows, $0.08 macOS (blended ~$0.008 for estimate)
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


def get_audit_results(conn: sqlite3.Connection) -> list[dict]:
    """Audit results by repo with issue counts."""
    try:
        rows = conn.execute("""
            SELECT repo, issues_json, audited_at FROM audit_results
            ORDER BY repo
        """).fetchall()
        result = []
        for r in rows:
            try:
                issues = json.loads(r["issues_json"]) if r["issues_json"] else []
            except (json.JSONDecodeError, TypeError):
                issues = []
            high = sum(1 for i in issues if i.get("severity") == "high")
            medium = sum(1 for i in issues if i.get("severity") == "medium")
            result.append({
                "repo": r["repo"],
                "issues": issues,
                "count": len(issues),
                "high": high,
                "medium": medium,
                "audited_at": r["audited_at"],
            })
        return [x for x in result if x["count"] > 0]
    except sqlite3.OperationalError:
        return []


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


def get_month_comparison(conn: sqlite3.Connection) -> dict:
    """This month vs last month comparison."""
    rows = conn.execute("""
        SELECT strftime('%Y-%m', created_at) AS month, SUM(billable_minutes_total) AS minutes, COUNT(*) AS runs
        FROM workflow_runs
        WHERE created_at >= date('now', '-2 months')
        GROUP BY strftime('%Y-%m', created_at)
        ORDER BY month DESC
        LIMIT 2
    """).fetchall()
    data = {r["month"]: dict(r) for r in rows}
    this_month = datetime.now().strftime("%Y-%m")
    last_month = (datetime.now().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return {
        "this_month": data.get(this_month, {}).get("minutes", 0) or 0,
        "last_month": data.get(last_month, {}).get("minutes", 0) or 0,
        "this_month_runs": data.get(this_month, {}).get("runs", 0) or 0,
        "last_month_runs": data.get(last_month, {}).get("runs", 0) or 0,
    }


def get_year_over_year(conn: sqlite3.Connection) -> dict:
    """This month vs same month last year."""
    this_month = datetime.now().strftime("%Y-%m")
    last_year = (datetime.now().year - 1, datetime.now().month)
    last_year_month = f"{last_year[0]}-{last_year[1]:02d}"
    rows = conn.execute("""
        SELECT strftime('%Y-%m', created_at) AS month, SUM(billable_minutes_total) AS minutes
        FROM workflow_runs
        WHERE strftime('%Y-%m', created_at) IN (?, ?)
        GROUP BY strftime('%Y-%m', created_at)
    """, (this_month, last_year_month)).fetchall()
    data = {r["month"]: r["minutes"] for r in rows}
    return {
        "this_month": data.get(this_month, 0) or 0,
        "same_month_last_year": data.get(last_year_month, 0) or 0,
    }


def get_export_data(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """Runs for export (CSV/JSON) - last N days."""
    rows = conn.execute("""
        SELECT run_id, repo, workflow_name, event, conclusion, created_at,
               duration_seconds, billable_minutes_total
        FROM workflow_runs
        WHERE created_at >= date('now', ?)
        ORDER BY created_at DESC
    """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def get_audit_summary(audit_by_repo: list[dict]) -> dict | None:
    """Summary of audit results for index card."""
    if not audit_by_repo:
        return None
    total = sum(r["count"] for r in audit_by_repo)
    if total == 0:
        return None
    return {
        "repos_with_issues": len(audit_by_repo),
        "total_issues": total,
    }


def get_filter_options(conn: sqlite3.Connection) -> dict:
    """Unique repos, workflows, events for filter dropdowns."""
    repos = [r[0] for r in conn.execute("SELECT DISTINCT repo FROM workflow_runs ORDER BY repo").fetchall()]
    workflows = [r[0] for r in conn.execute("SELECT DISTINCT workflow_name FROM workflow_runs WHERE workflow_name IS NOT NULL ORDER BY workflow_name").fetchall()]
    events = [r[0] for r in conn.execute("SELECT DISTINCT event FROM workflow_runs WHERE event IS NOT NULL ORDER BY event").fetchall()]
    return {"repos": repos, "workflows": workflows, "events": events}


def main() -> None:
    import os
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
    collection_log = get_collection_log(conn)
    audit_by_repo = get_audit_results(conn)
    audit_summary = get_audit_summary(audit_by_repo)
    month_comparison = get_month_comparison(conn)
    year_over_year = get_year_over_year(conn)
    export_data = get_export_data(conn, 90)
    filter_options = get_filter_options(conn)

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
            month_comparison=month_comparison,
            year_over_year=year_over_year,
            audit_summary=audit_summary,
        )
    )

    # Failures page (with filter options from failures data)
    failure_repos = sorted({f["repo"] for f in failures})
    failure_workflows = sorted({f.get("workflow_name") or "" for f in failures if f.get("workflow_name")})
    failure_conclusions = sorted({f.get("conclusion") or "" for f in failures if f.get("conclusion")})
    failures_tpl = env.get_template("failures.html")
    (OUTPUT_DIR / "failures.html").write_text(
        failures_tpl.render(
            failures=failures,
            failure_repos=failure_repos,
            failure_workflows=failure_workflows,
            failure_conclusions=failure_conclusions,
        )
    )

    # History page
    history_tpl = env.get_template("history.html")
    (OUTPUT_DIR / "history.html").write_text(
        history_tpl.render(
            monthly_usage=monthly_usage,
            allowance=allowance,
            month_comparison=month_comparison,
            year_over_year=year_over_year,
        )
    )

    # Logs page
    logs_tpl = env.get_template("logs.html")
    (OUTPUT_DIR / "logs.html").write_text(
        logs_tpl.render(collection_log=collection_log)
    )

    # Audit page
    audit_tpl = env.get_template("audit.html")
    (OUTPUT_DIR / "audit.html").write_text(
        audit_tpl.render(audit_by_repo=audit_by_repo)
    )

    # Explore page (filterable)
    explore_tpl = env.get_template("explore.html")
    (OUTPUT_DIR / "explore.html").write_text(
        explore_tpl.render(
            filter_options=filter_options,
            export_data=export_data,
        )
    )

    # Export files
    export_dir = OUTPUT_DIR / "export"
    export_dir.mkdir(exist_ok=True)
    (export_dir / "usage.json").write_text(
        json.dumps(export_data, indent=2, default=str)
    )
    if export_data:
        with open(export_dir / "usage.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=export_data[0].keys(), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(export_data)
    else:
        (export_dir / "usage.csv").write_text("run_id,repo,workflow_name,event,conclusion,created_at,duration_seconds,billable_minutes_total\n")

    # Per-repo pages
    repo_tpl = env.get_template("repo.html")
    audit_by_repo_map = {r["repo"]: r for r in audit_by_repo}
    for repo in all_repos:
        workflows = get_repo_workflows(conn, repo)
        recent = get_repo_recent_runs(conn, repo)
        repo_monthly = get_repo_monthly(conn, repo)
        workflow_efficiency = get_workflow_efficiency(conn, repo)
        repo_audit = audit_by_repo_map.get(repo, {})
        safe_name = repo.replace("/", "_")
        (OUTPUT_DIR / f"repo_{safe_name}.html").write_text(
            repo_tpl.render(
                repo=repo,
                workflows=workflows,
                recent_runs=recent,
                repo_monthly=repo_monthly,
                workflow_efficiency=workflow_efficiency,
                repo_audit=repo_audit,
            )
        )
    conn.close()

    print(f"Generated: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
