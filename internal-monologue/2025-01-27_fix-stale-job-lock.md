# 2025-01-27 — Fix: "Toinen deploy on jo käynnissä" stuck lock

## Problem
After a failed launch (InferenceService went to Failed state), the job dict stayed
in `state: "running"` because `_run_launch` was blocked in `_wait_ready` (15-min
polling loop) which never returned True. The 409-guard `any(j["state"] == "running")`
then permanently blocked all new deploys until the server was restarted.

## Fix
Both `launcher.py` (start_launch) and `deployer.py` (start_deploy): before checking
for a running job, sweep through `_jobs` under the lock and forcibly mark any job
that has exceeded its maximum allowed age as `state: "error"`. Constants:
- `_MAX_JOB_AGE_S = 17 * 60` (launcher): 15-min Ready-wait + 2-min overhead
- `_MAX_DEPLOY_AGE_S = 4 * 3600` (deployer): generous for large model copies

Running step inside the stale job is also set to "error" so the UI step list shows
the right state if the user polls it.
