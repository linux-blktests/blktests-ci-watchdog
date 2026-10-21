#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Copyright (c) 2026 Western Digital Corporation or its affiliates.
#
# Authors: Dennis Maisenbacher (dennis.maisenbacher@wdc.com)

"""Outside-in health check for a blktests-ci deployment.

Reads only public GitHub and patchwork APIs and reports through GitHub issues
in a separate repository. It never touches the cluster or the CI repos.

Detectors (each returns findings keyed by a stable id):
  scale-set   jobs wait for a scale set that has free runner slots, or wait
              longer than ``queue_warn_hours`` (GitHub cancels at 24 h)
  kpd-lock    the kpd leader lock heartbeat is stale
  no-pr       a patchwork series older than ``series_grace_hours`` has no PR

Issue lifecycle, one issue per finding key: opened on first sight, body edited
silently while it persists (GitHub does not notify on edits), commented and
closed on recovery.

Run: python3 ci-health/check.py --config ci-health/config.toml [--dry-run]
Token: GITHUB_TOKEN (read access to the watched repos, issues:write on the
alert repo). Without a token only --dry-run works, at 60 requests/hour.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
PATCHWORK = "https://patchwork.kernel.org/api/1.3"
LABEL = "ci-health"
MARKER = "<!-- ci-health:{} -->"


@dataclass(frozen=True)
class Finding:
    key: str
    title: str
    body: str


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def hours(now: datetime, ts: str) -> float:
    return (now - parse_ts(ts)).total_seconds() / 3600


class Http:
    def __init__(self, token: str | None):
        self.token = token
        self.next_url: str | None = None

    def request(self, url: str, method: str = "GET", data: dict | None = None):
        headers = {"User-Agent": "blktests-ci-health"}
        if url.startswith(API):
            headers["Accept"] = "application/vnd.github+json"
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
        payload = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, payload, headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                m = re.search(r'<([^>]+)>;\s*rel="next"', resp.headers.get("Link", ""))
                self.next_url = m.group(1) if m else None
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            self.next_url = None
            return err.code, None

    def get_all(self, url: str, key: str | None = None) -> list:
        """GET every page following Link rel=next; key unwraps a dict page."""
        items = []
        while url:
            page = self.get(url)
            items += page[key] if key else page
            url = self.next_url
        return items

    def get(self, url: str):
        status, body = self.request(url)
        if status != 200:
            raise RuntimeError(f"GET {url} -> HTTP {status}")
        return body


def check_scale_sets(http: Http, cfg: dict, now: datetime, rows: list) -> list[Finding]:
    """Alert when jobs queue for a scale set that is not using its capacity."""
    out = []
    warn_h = cfg.get("queue_warn_hours", 12)
    idle_min = cfg.get("queue_idle_minutes", 30)
    for ss in cfg.get("scale_set", []):
        repo, label, max_runners = ss["repo"], ss["label"], ss["max_runners"]
        queued, running = [], 0
        for status in ("queued", "in_progress"):
            runs = http.get_all(f"{API}/repos/{repo}/actions/runs?status={status}&per_page=100", "workflow_runs")
            for run in runs:
                jobs = http.get_all(f"{API}/repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100", "jobs")
                for job in jobs:
                    if label not in job["labels"]:
                        continue
                    if job["status"] == "queued":
                        queued.append(job)
                    elif job["status"] == "in_progress":
                        running += 1
        oldest = max((hours(now, j["created_at"]) for j in queued), default=0.0)
        rows.append(("Scale set", f"{label} ({repo})", f"{running}/{max_runners} running, "
                     f"{len(queued)} queued, oldest wait {oldest:.1f} h"))
        if not queued:
            continue
        key = f"scale-set:{repo}:{label}"
        if running < max_runners and oldest * 60 > idle_min:
            out.append(Finding(key, f"Scale set {label} not picking up jobs ({repo})", (
                f"{len(queued)} job(s) queued, oldest {oldest:.1f} h, but only {running} of "
                f"{max_runners} runner slots in use. The ARC listener or cluster is likely down.")))
        elif oldest > warn_h:
            out.append(Finding(key, f"Scale set {label} queue saturated ({repo})", (
                f"{len(queued)} job(s) queued, oldest {oldest:.1f} h. All {running}/{max_runners} "
                f"slots busy. GitHub cancels queued jobs after 24 h.")))
    return out


def check_kpd_locks(http: Http, cfg: dict, now: datetime, rows: list) -> list[Finding]:
    """Alert when a kpd lock file has not been heartbeated recently."""
    out = []
    stale_min = cfg.get("kpd_stale_minutes", 30)
    for lock in cfg.get("kpd_lock", []):
        repo, path = lock["repo"], lock["path"]
        status, body = http.request(f"{API}/repos/{repo}/contents/{path}")
        key = f"kpd-lock:{repo}:{path}"
        if status == 404:
            out.append(Finding(key, f"kpd lock {path} missing", f"{repo}:{path} does not exist."))
            continue
        if status != 200:
            raise RuntimeError(f"lock {repo}:{path} -> HTTP {status}")
        data = json.loads(base64.b64decode(body["content"]))
        age_min = (now - datetime.strptime(data["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)).total_seconds() / 60
        rows.append(("kpd lock", path, f"holder {data['holder']}, heartbeat {age_min:.0f} min ago"))
        if age_min > stale_min:
            out.append(Finding(key, f"kpd lock {path} heartbeat stale", (
                f"Holder `{data['holder']}` last heartbeat {age_min:.0f} min ago "
                f"(limit {stale_min} min). No kpd is opening PRs for this project.")))
    return out


def series_ids_with_pr(http: Http, repo: str, oldest: datetime) -> set[int]:
    """Series ids of every PR created since ``oldest`` (open or closed)."""
    ids = set()
    url = f"{API}/repos/{repo}/pulls?state=all&sort=created&direction=desc&per_page=100"
    while url:
        pulls = http.get(url)
        for pull in pulls:
            m = re.match(r"series/(\d+)=>", pull["head"]["ref"])
            if m:
                ids.add(int(m.group(1)))
        if not pulls or parse_ts(pulls[-1]["created_at"]) < oldest:
            break
        url = http.next_url
    return ids


def check_missing_prs(http: Http, cfg: dict, now: datetime, rows: list) -> list[Finding]:
    """Alert on patchwork series that never got a PR (kpd not creating them)."""
    out = []
    grace = cfg.get("series_grace_hours", 6)
    for pw in cfg.get("patchwork", []):
        # Matches kpd's default 7 day lookback: kpd ignores older series, so a
        # series aging out of this window is not a recovery.
        oldest = now - timedelta(hours=pw.get("lookback_hours", 168))
        since = oldest.strftime("%Y-%m-%dT%H:%M:%S")
        series = http.get_all(
            f"{PATCHWORK}/series/?project={pw['project_id']}&since={since}&per_page=100&order=-date")
        known = series_ids_with_pr(http, pw["repo"], oldest)
        missing = [s for s in series if s["received_all"] and s["id"] not in known
                   and (now - datetime.fromisoformat(s["date"]).replace(tzinfo=timezone.utc))
                   > timedelta(hours=grace) and not re.match(r"\[[^\]]*\bRFC\b", s["name"] or "x")]
        rows.append(("Patchwork", pw["repo"], f"{len(series)} series in lookback, {len(missing)} without PR"))
        if missing:
            listing = "\n".join(f"- {s['id']}: {s['name']}" for s in missing[:20])
            out.append(Finding(f"no-pr:{pw['repo']}", f"Series without PR in {pw['repo']}", (
                f"{len(missing)} patchwork series older than {grace} h have no PR. Some may be "
                f"legitimately skipped (tags, state). Persistent entries mean kpd is stuck.\n\n{listing}")))
    return out


def reconcile(http: Http, alert_repo: str, findings: list[Finding], dry_run: bool,
              closable: tuple[str, ...] = ("",)) -> list[str]:
    """Open, edit or close issues so open issues match the findings.

    Only issues whose key starts with a ``closable`` prefix may be closed, so a
    detector that errored cannot close its own alerts. Returns actions.
    """
    actions = []
    issues = http.get_all(f"{API}/repos/{alert_repo}/issues?state=open&labels={LABEL}&per_page=100")
    by_key = {}
    duplicates = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        m = re.search(r"<!-- ci-health:(.+?) -->", issue.get("body") or "")
        if m:
            if m.group(1) in by_key:
                duplicates.append((m.group(1), issue))
            else:
                by_key[m.group(1)] = issue
    active = {f.key: f for f in findings}

    def call(method: str, path: str, data: dict):
        if not dry_run:
            status, _ = http.request(f"{API}/repos/{alert_repo}/{path}", method, data)
            if status >= 300:
                raise RuntimeError(f"{method} {path} -> HTTP {status}")

    for key, finding in active.items():
        body = f"{MARKER.format(key)}\n{finding.body}"
        if key in by_key:
            if by_key[key]["body"] != body:
                actions.append(f"update #{by_key[key]['number']} {key}")
                call("PATCH", f"issues/{by_key[key]['number']}", {"body": body})
        else:
            actions.append(f"open {key}")
            call("POST", "issues", {"title": finding.title, "body": body, "labels": [LABEL]})
    for key, issue in by_key.items():
        if key not in active and key.startswith(closable):
            actions.append(f"close #{issue['number']} {key}")
            call("POST", f"issues/{issue['number']}/comments", {"body": "Recovered. Closing."})
            call("PATCH", f"issues/{issue['number']}", {"state": "closed"})
    for key, issue in duplicates:
        if key in active or key.startswith(closable):
            actions.append(f"close duplicate #{issue['number']} {key}")
            call("PATCH", f"issues/{issue['number']}", {"state": "closed"})
    return actions


DETECTORS = {check_scale_sets: "scale-set:", check_kpd_locks: "kpd-lock:", check_missing_prs: "no-pr:"}


def write_summary(rows: list, findings: list[Finding], errors: list[str], now: datetime) -> None:
    """Status table for the Actions run page (GITHUB_STEP_SUMMARY), else stdout."""
    state = "FAILING" if findings else "OK"
    lines = [f"## CI health: {state}", f"Checked {now:%Y-%m-%d %H:%M} UTC", "",
             "| Check | Target | Status |", "|---|---|---|"]
    lines += [f"| {c} | {t} | {v} |" for c, t, v in rows]
    if findings:
        lines += ["", "### Open findings"] + [f"- {f.title}" for f in findings]
    if errors:
        lines += ["", "### Detector errors"] + [f"- {e}" for e in errors]
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a") as fh:
            fh.write(text)
    else:
        print(text)


def run(http: Http, cfg: dict, now: datetime, dry_run: bool) -> int:
    findings, errors, closable, rows = [], [], [], []
    for detector, prefix in DETECTORS.items():
        try:
            findings += detector(http, cfg, now, rows)
            closable.append(prefix)
        except Exception as err:  # one failing detector must not hide the others
            errors.append(f"{detector.__name__}: {err}")
    for f in findings:
        print(f"FINDING {f.key}: {f.title}")
    for e in errors:
        print(f"ERROR {e}", file=sys.stderr)
    write_summary(rows, findings, errors, now)
    # A failed detector keeps its open issues: only successful ones may close.
    for action in reconcile(http, cfg["alert_repo"], findings, dry_run, tuple(closable)):
        print(("DRY-RUN " if dry_run else "") + action)
    return 1 if errors else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true", help="print actions, change no issues")
    args = ap.parse_args()
    with open(args.config, "rb") as fh:
        cfg = tomllib.load(fh)
    http = Http(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    return run(http, cfg, datetime.now(timezone.utc), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
