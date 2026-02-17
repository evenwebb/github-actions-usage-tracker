<p align="center">
  <strong>📊 GitHub Actions Usage Tracker</strong>
</p>
<p align="center">
  Track your Actions usage across all repos. See minutes consumed, spot expensive workflows, and get alerted before you hit your limit.
</p>

<p align="center">
  <a href="https://github.com/evenwebb/github-actions-usage-tracker/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-blue.svg" alt="License"></a>
  <a href="https://github.com/evenwebb/github-actions-usage-tracker"><img src="https://img.shields.io/badge/python-3.12+-green.svg" alt="Python"></a>
  <a href="https://github.com/evenwebb/github-actions-usage-tracker"><img src="https://img.shields.io/github/stars/evenwebb/github-actions-usage-tracker?style=social" alt="Stars"></a>
</p>

---

## 📑 Table of Contents

- [Quick Start](#-quick-start)
- [Features](#-features)
- [Setup](#-setup)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [Project Structure](#-project-structure)
- [Local Development](#-local-development)
- [Troubleshooting](#-troubleshooting)
- [License](#-license)

---

## 🚀 Quick Start

1. **Fork or clone** this repo to your account
2. Add `ACTIONS_USAGE_TOKEN` secret (PAT with `repo` scope)
3. Enable **GitHub Pages** → Source: **GitHub Actions**
4. Run the workflow manually (enable **Backfill** for 90 days of history)
5. View your dashboard at `https://<username>.github.io/github-actions-usage-tracker/`

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 📈 **Overview dashboard** | Minutes used vs allowance, progress bar, cost projection |
| 📅 **Trigger breakdown** | Runs by event (schedule, push, manual, etc.) |
| 🖥️ **OS breakdown** | Linux / macOS / Windows billable minutes |
| 📊 **90-day trend** | Daily usage chart over time |
| 📆 **12-month history** | Monthly usage with allowance comparison |
| 🏆 **Top workflows** | Highest consumers across all repos |
| 💤 **Dormant workflows** | Workflows with no successful run in 30+ days |
| 📁 **Per-repo pages** | Workflow stats, success rate, efficiency, sparklines |
| ❌ **Failures page** | All failed runs with direct links to logs |
| 📋 **Collection logs** | API call counts, errors, backfill runs |
| 🔔 **Apprise alerts** | Notify when usage exceeds 80% of allowance |
| 🔍 **Cost & Risk Auditor** | Scans workflows for expensive/risky patterns and suggests fixes |
| 📋 **Audit in dashboard** | Stored audit results shown on main site |
| 📈 **Trends & comparisons** | This month vs last month, year-over-year |
| 📤 **Export** | CSV/JSON export of usage data (last 90 days) |
| 🔎 **Explore & filter** | Filter by repo, workflow, event, date range |
| 🐳 **Docker** | Run collect, generate, and audit in containers |

---

## ⚙️ Setup

### Prerequisites

- GitHub account with repos using Actions
- Personal access token (for cross-repo access)

### Step 1: Add repository secrets

| Secret | Required | Description |
|--------|----------|-------------|
| `ACTIONS_USAGE_TOKEN` | **Recommended** | PAT with `repo` scope. Lists all your repos and fetches workflow data. Falls back to `GITHUB_TOKEN` (limited to current repo only). |
| `APPRISE_URL` | Optional | Apprise URL for alerts (e.g. `mailto://user:pass@gmail.com`). Sends notification when usage > 80% of allowance. |

### Step 2: Repository variables (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_ACTIONS_ALLOWANCE` | `2000` | Your monthly free minutes (2000 for Free, 3000 for Pro). |

### Step 3: Enable GitHub Pages

1. Go to **Settings → Pages**
2. Set source to **GitHub Actions**

### Step 4: First run

- The workflow runs **daily at midnight UTC**
- To run immediately: **Actions → Collect & Deploy → Run workflow**
- For **initial setup**: enable **Backfill** to fetch 90 days of history (otherwise only last 48 hours)

---

## 🔧 Configuration

### Environment variables

| Variable | Description |
|----------|-------------|
| `ACTIONS_USAGE_TOKEN` | GitHub PAT with `repo` scope |
| `GITHUB_TOKEN` | Fallback (auto-provided in Actions) |
| `GITHUB_ACTIONS_ALLOWANCE` | Monthly minute allowance (default: 2000) |
| `APPRISE_URL` | Apprise notification URL |

### Billable minutes

GitHub applies multipliers: **Linux 1×**, **macOS 10×**, **Windows 2×**. Self-hosted runners are not billed. The tracker computes billable minutes from job duration and runner labels.

> **Note:** Public repos don't consume billable minutes. This tool is still useful for tracking usage patterns, identifying expensive workflows, and monitoring failures.

---

## 📖 Usage

### Automated (default)

The workflow runs daily and:

1. Fetches workflow runs from all your repos
2. Stores data in SQLite (`data/actions.db`)
3. Generates static HTML dashboard
4. Commits data + deploys to GitHub Pages

### Manual run with backfill

For initial setup or to refresh historical data:

1. Go to **Actions → Collect & Deploy**
2. Click **Run workflow**
3. Enable **Backfill** (fetches 90 days)
4. Click **Run workflow**

### Local development

```bash
# Install dependencies
pip install -r requirements.txt

# Set token
export ACTIONS_USAGE_TOKEN=ghp_xxx   # or GITHUB_TOKEN

# Collect data (last 48 hours)
python collect.py

# Optional: backfill 90 days
python collect.py --backfill

# Generate dashboard
python generate.py

# Preview
python -m http.server 8000 --directory docs
# Open http://localhost:8000
```

### Cost & Risk Auditor

Scans workflow YAML files and flags expensive or risky patterns:

| Check | Description |
|-------|-------------|
| **Missing cache** | Jobs that install packages (pip, npm, etc.) without caching |
| **Huge matrix** | Matrix builds producing >20 jobs |
| **Unpinned actions** | Actions using `@main`, `@master`, or no ref |
| **Secrets exposure** | Echo/print/log of secrets (may leak in logs) |
| **Excessive checkout depth** | `fetch-depth: 0` (full history — slow) |

```bash
# Audit local .github/workflows
python audit.py

# Audit a remote repo (requires token)
python audit.py --repo owner/repo

# Output formats
python audit.py --json      # JSON for tooling
python audit.py --markdown  # Markdown for PR comments
```

**PR comment bot:** When you open a PR that touches `.github/workflows/`, the `Audit Workflows (PR)` workflow runs and posts a summary comment automatically.

**Dashboard integration:** Run `python collect.py --audit` (or use the default workflow) to store audit results. They appear on the **Audit** page of the dashboard.

### Docker

```bash
# Build
docker build -t github-actions-usage-tracker .

# Run collect + generate (set token first)
export ACTIONS_USAGE_TOKEN=ghp_xxx
docker run --rm -e ACTIONS_USAGE_TOKEN \
  -v $(pwd)/data:/app/data -v $(pwd)/docs:/app/docs \
  github-actions-usage-tracker

# Or with docker-compose
docker compose run --rm tracker
```

For backfill or audit:

```bash
docker compose run --rm tracker python collect.py --backfill
docker compose run --rm tracker python audit.py --markdown
```

---

## 📁 Project Structure

```
github-actions-usage-tracker/
├── .github/workflows/
│   ├── collect-and-deploy.yml    # Daily cron + manual trigger
│   └── audit-pr.yml              # PR comment bot for workflow audits
├── collect.py                     # Fetches data from GitHub API
├── generate.py                    # Builds static site from SQLite
├── audit.py                       # Cost & risk auditor for workflows
├── templates/
│   ├── base.html                  # Layout
│   ├── index.html                 # Overview
│   ├── history.html               # Monthly history
│   ├── explore.html               # Filterable runs
│   ├── audit.html                 # Workflow audit results
│   ├── repo.html                  # Per-repo detail
│   ├── failures.html              # Failed runs
│   └── logs.html                  # Collection logs
├── data/
│   └── actions.db                 # SQLite (committed)
├── docs/                          # Generated site (GitHub Pages)
│   └── export/                    # usage.json, usage.csv
├── Dockerfile                     # Container image
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## 🛠️ Local Development

**Dependencies:** `requests`, `Jinja2`

```bash
pip install -r requirements.txt
```

| Command | Description |
|---------|-------------|
| `python collect.py` | Fetch last 48 hours |
| `python collect.py --backfill` | Fetch last 90 days |
| `python generate.py` | Build dashboard to `docs/` |

---

## 🔍 Troubleshooting

<details>
<summary><strong>Dashboard shows "No data yet"</strong></summary>

- Run the workflow manually at least once
- Ensure `ACTIONS_USAGE_TOKEN` has `repo` scope
- Check the workflow run logs for errors
</details>

<details>
<summary><strong>Only current repo appears</strong></summary>

- `GITHUB_TOKEN` only has access to the current repo
- Add `ACTIONS_USAGE_TOKEN` (PAT with `repo` scope) to list all your repos
</details>

<details>
<summary><strong>GitHub Pages 404</strong></summary>

- Ensure Pages is set to **GitHub Actions** (not a branch)
- Wait a few minutes after the first deploy
- Check **Settings → Pages** for the published URL
</details>

<details>
<summary><strong>API rate limit (403)</strong></summary>

- Authenticated limit is 5,000 requests/hour
- Typical usage (repos × runs × jobs) stays well under the limit
- If hitting limits, reduce backfill scope or add delays between requests
</details>

---

## 📄 License

This project is licensed under the **GPL-3.0** License. See the [LICENSE](https://github.com/evenwebb/github-actions-usage-tracker/blob/main/LICENSE) file for details.

---

<p align="center">
  <strong>Built by <a href="https://github.com/evenwebb">evenwebb</a></strong>
</p>
<p align="center">
  <a href="https://github.com/evenwebb/github-actions-usage-tracker">View on GitHub</a> ·
  <a href="https://github.com/evenwebb/github-actions-usage-tracker/issues">Report an issue</a> ·
  <a href="https://github.com/evenwebb/github-actions-usage-tracker/stargazers">⭐ Star this repo</a>
</p>
