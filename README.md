<!--
SPDX-License-Identifier: GPL-2.0-or-later
-->

# blktests-ci-watchdog

Observe-only health checker for a blktests-ci deployment. It runs on
GitHub-hosted runners, reads public GitHub and patchwork APIs, and files GitHub
issues in this repo. It never touches the cluster or the CI repos.

| Detector | Fires when |
|---|---|
| scale set | jobs queued > `queue_idle_minutes` while fewer than `max_runners` run (listener or cluster down), or oldest queued job > `queue_warn_hours` (GitHub cancels at 24 h) |
| kpd lock | lock heartbeat older than `kpd_stale_minutes`, or lock file missing |
| series without PR | patchwork series older than `series_grace_hours` with no `series/<id>=>` PR |

One issue per finding. It is opened on first sight, edited silently while it
persists, and commented and closed on recovery. A detector that errors
keeps its open issues (only successful detectors may close) and the run exits 1.

## Setup

1. Set `alert_repo` in `config.toml` to this repo. Issues are public, which is
   fine: every input is already public.
2. Settings -> Actions -> General -> Workflow permissions: read-only default.
   The workflow requests `issues: write` itself.
3. Watch the repo (Custom -> Issues) to get notified. The issue author is
   `github-actions[bot]`, so GitHub does not suppress it as your own activity.
4. Run the workflow once with `workflow_dispatch`. Each run writes a status
   table to its run page, so the cluster state is visible without an alert.
5. GitHub disables scheduled workflows in public repos after 60 days without
   repo activity. Push a commit now and then, or re-enable the workflow.

The `[[patchwork]]` detector is off in `config.toml`, see the comment there.
Keep the workflow limited to `schedule` and `workflow_dispatch`.

## Dry run

```
GITHUB_TOKEN=... python3 check.py --config config.toml --dry-run
```

Without a token the reconcile step cannot list issues. Detectors alone work
unauthenticated at 60 requests/hour.

## Limits

- It sees symptoms only. Orphaned VMs, missing devices and failed CronJobs need
  the in-cluster checker (not built).
- A fully dead cluster is caught only through queued jobs and the kpd lock.
