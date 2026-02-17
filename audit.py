#!/usr/bin/env python3
"""
GitHub Actions Cost and Risk Auditor

Scans workflow YAML files and flags expensive or risky patterns, then suggests fixes.
Checks: missing cache, huge matrix builds, unpinned actions, secrets exposure, excessive checkout depth.
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

import requests
import yaml

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
REQUEST_TIMEOUT = 15

# Thresholds
MATRIX_LARGE_THRESHOLD = 20  # Flag if matrix produces >20 jobs
CHECKOUT_DEPTH_EXCESSIVE = 0  # fetch-depth: 0 = full history (expensive)
CACHEABLE_STEPS = ("pip", "npm", "cargo", "gradle", "maven", "go", "bundler")


def get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_workflow_yaml(path: Path) -> dict | None:
    """Load and parse a workflow YAML file."""
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        log.warning("Failed to parse %s: %s", path, e)
        return None


def check_unpinned_actions(workflow: dict, filepath: str) -> list[dict]:
    """Flag actions that use @main, @master, or no ref (implicit default)."""
    issues = []
    jobs = workflow.get("jobs", {})
    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if not uses or " " in uses:
                continue
            if "@" not in uses:
                issues.append({
                    "file": filepath,
                    "job": job_name,
                    "step": step.get("name", uses),
                    "severity": "high",
                    "check": "unpinned_action",
                    "message": f"Action '{uses}' has no version pin — use @v1 or @sha:xxx for reproducibility",
                    "suggestion": f"Pin to a specific version, e.g. {uses}@v1",
                })
            elif uses.endswith("@main") or uses.endswith("@master"):
                issues.append({
                    "file": filepath,
                    "job": job_name,
                    "step": step.get("name", uses),
                    "severity": "medium",
                    "check": "unpinned_action",
                    "message": f"Action '{uses}' uses @main/@master — can break on upstream changes",
                    "suggestion": f"Pin to a release tag, e.g. {uses.split('@')[0]}@v1",
                })
    return issues


def check_matrix_size(workflow: dict, filepath: str) -> list[dict]:
    """Flag jobs with large matrix dimensions (expensive)."""
    issues = []
    jobs = workflow.get("jobs", {})
    for job_name, job in jobs.items():
        strategy = job.get("strategy", {})
        matrix = strategy.get("matrix", {})
        if not matrix:
            continue
        # Estimate total jobs (product of lengths)
        total = 1
        for key, val in matrix.items():
            if isinstance(val, list):
                total *= len(val)
            elif isinstance(val, dict) and "include" in val:
                total *= len(val["include"])
            else:
                total *= 1
        if total > MATRIX_LARGE_THRESHOLD:
            issues.append({
                "file": filepath,
                "job": job_name,
                "severity": "medium",
                "check": "huge_matrix",
                "message": f"Matrix produces ~{total} jobs — high cost and long run time",
                "suggestion": "Consider splitting into smaller matrices or using concurrency limits",
            })
    return issues


def check_missing_cache(workflow: dict, filepath: str) -> list[dict]:
    """Flag jobs that run package installs without cache."""
    issues = []
    jobs = workflow.get("jobs", {})
    for job_name, job in jobs.items():
        steps = job.get("steps", [])
        has_cache = False
        for step in steps:
            uses = (step.get("uses") or "").lower()
            with_opts = step.get("with") or {}
            if "cache" in uses:  # actions/cache
                has_cache = True
                break
            if "setup-python" in uses and with_opts.get("cache"):
                has_cache = True
                break
            if "setup-node" in uses and with_opts.get("cache"):
                has_cache = True
                break
        has_install = False
        for step in steps:
            run = (step.get("run") or "").lower()
            uses = (step.get("uses") or "").lower()
            if any(c in run or c in uses for c in CACHEABLE_STEPS):
                has_install = True
                break
        if has_install and not has_cache:
            issues.append({
                "file": filepath,
                "job": job_name,
                "severity": "medium",
                "check": "missing_cache",
                "message": "Job appears to install packages but has no cache step",
                "suggestion": "Add actions/cache for pip, npm, cargo, etc. to speed builds and reduce API usage",
            })
    return issues


def check_secrets_exposure(workflow: dict, filepath: str) -> list[dict]:
    """Flag patterns that may expose secrets (echo, log, env in run)."""
    issues = []
    jobs = workflow.get("jobs", {})
    risky_patterns = [
        (re.compile(r"echo\s+\$?\{{?\s*secrets\.", re.I), "echo of secrets"),
        (re.compile(r"echo\s+\$?\{{?\s*env\.", re.I), "echo of env vars"),
        (re.compile(r"print\s*\(.*secrets\.", re.I), "print of secrets"),
        (re.compile(r"::debug::.*secrets\.", re.I), "debug log of secrets"),
        (re.compile(r"::set-output.*secrets\.", re.I), "set-output with secrets"),
    ]
    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            run = step.get("run") or ""
            for pattern, desc in risky_patterns:
                if pattern.search(run):
                    issues.append({
                        "file": filepath,
                        "job": job_name,
                        "step": step.get("name", "run"),
                        "severity": "high",
                        "check": "secrets_exposure",
                        "message": f"Possible {desc} — secrets may leak in logs",
                        "suggestion": "Never echo, print, or log secrets. Use mask.",
                    })
    return issues


def check_checkout_depth(workflow: dict, filepath: str) -> list[dict]:
    """Flag checkout with fetch-depth: 0 (full history — expensive)."""
    issues = []
    jobs = workflow.get("jobs", {})
    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if "checkout" not in uses.lower():
                continue
            depth = step.get("with", {}).get("fetch-depth")
            if depth == 0 or depth == "0":
                issues.append({
                    "file": filepath,
                    "job": job_name,
                    "step": step.get("name", uses),
                    "severity": "medium",
                    "check": "excessive_checkout_depth",
                    "message": "fetch-depth: 0 fetches full history — slow and uses more storage",
                    "suggestion": "Use fetch-depth: 1 (or omit) unless you need full history for versioning",
                })
    return issues


def audit_workflow(workflow: dict, filepath: str) -> list[dict]:
    """Run all checks on a workflow. Returns list of issues."""
    if not workflow:
        return []
    issues = []
    issues.extend(check_unpinned_actions(workflow, filepath))
    issues.extend(check_matrix_size(workflow, filepath))
    issues.extend(check_missing_cache(workflow, filepath))
    issues.extend(check_secrets_exposure(workflow, filepath))
    issues.extend(check_checkout_depth(workflow, filepath))
    return issues


def audit_local(workflows_dir: Path) -> list[dict]:
    """Audit workflow files in a local directory."""
    all_issues = []
    if not workflows_dir.exists():
        log.warning("Workflows dir %s does not exist", workflows_dir)
        return all_issues
    for path in workflows_dir.glob("*.yml"):
        if path.name.startswith("."):
            continue
        workflow = load_workflow_yaml(path)
        issues = audit_workflow(workflow, str(path))
        all_issues.extend(issues)
    for path in workflows_dir.glob("*.yaml"):
        if path.name.startswith("."):
            continue
        workflow = load_workflow_yaml(path)
        issues = audit_workflow(workflow, str(path))
        all_issues.extend(issues)
    return all_issues


def fetch_workflow_from_api(
    session: requests.Session,
    token: str,
    owner: str,
    repo: str,
    path: str = ".github/workflows",
) -> list[tuple[str, dict]]:
    """Fetch workflow file contents from GitHub API. Returns [(path, content_dict), ...]."""
    results = []
    url = f"{API_BASE}/repos/{owner}/{repo}/contents/{path}"
    resp = session.get(url, headers=get_headers(token), timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        return results
    for item in resp.json():
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        if not (name.endswith(".yml") or name.endswith(".yaml")):
            continue
        download_url = item.get("download_url")
        if not download_url:
            continue
        r2 = session.get(download_url, timeout=REQUEST_TIMEOUT)
        if r2.status_code != 200:
            continue
        try:
            content = yaml.safe_load(r2.text)
            results.append((f"{owner}/{repo}/{path}/{name}", content))
        except yaml.YAMLError:
            continue
    return results


def audit_repo_api(
    session: requests.Session,
    token: str,
    owner: str,
    repo: str,
) -> list[dict]:
    """Audit workflows in a repo via GitHub API."""
    all_issues = []
    workflows = fetch_workflow_from_api(session, token, owner, repo)
    for filepath, workflow in workflows:
        issues = audit_workflow(workflow, filepath)
        all_issues.extend(issues)
    return all_issues


def format_markdown(issues: list[dict]) -> str:
    """Format issues as Markdown for PR comment."""
    if not issues:
        return "## GitHub Actions Audit\n\nNo issues found."
    by_severity = {"high": [], "medium": [], "low": []}
    for i in issues:
        sev = i.get("severity", "medium")
        by_severity.get(sev, by_severity["medium"]).append(i)
    lines = [
        "## GitHub Actions Cost & Risk Audit",
        "",
        "| Severity | Check | Message | Suggestion |",
        "|----------|-------|---------|-------------|",
    ]
    for sev in ("high", "medium", "low"):
        for i in by_severity[sev]:
            msg = (i.get("message") or "").replace("|", "\\|")
            sug = (i.get("suggestion") or "").replace("|", "\\|")
            loc = f"{i.get('file', '')} › {i.get('job', '')}"
            lines.append(f"| {sev} | {i.get('check', '')} | {msg} | {sug} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit GitHub Actions workflows for cost and risk issues",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".github/workflows",
        help="Path to workflows dir (default: .github/workflows)",
    )
    parser.add_argument(
        "--repo",
        help="Audit remote repo (owner/repo) via API instead of local path",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON instead of human-readable",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Output Markdown (for PR comment)",
    )
    args = parser.parse_args()

    issues: list[dict] = []

    if args.repo:
        token = os.environ.get("ACTIONS_USAGE_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise SystemExit("Set ACTIONS_USAGE_TOKEN or GITHUB_TOKEN for --repo")
        parts = args.repo.split("/", 1)
        if len(parts) != 2:
            raise SystemExit("--repo must be owner/repo")
        owner, repo = parts
        session = requests.Session()
        issues = audit_repo_api(session, token, owner, repo)
        log.info("Audited %s via API: %d issues", args.repo, len(issues))
    else:
        workflows_dir = Path(args.path)
        issues = audit_local(workflows_dir)
        log.info("Audited %s: %d issues", workflows_dir, len(issues))

    if args.json:
        print(json.dumps({"issues": issues, "count": len(issues)}, indent=2))
    elif args.markdown:
        print(format_markdown(issues))
    else:
        for i in issues:
            sev = i.get("severity", "?").upper()
            loc = f"{i.get('file', '')}:{i.get('job', '')}"
            print(f"[{sev}] {i.get('check', '')} — {i.get('message', '')}")
            print(f"     {loc}")
            print(f"     Fix: {i.get('suggestion', '')}")
            print()

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
