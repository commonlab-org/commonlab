# Home registry rollout — activated 2026-10-26

Target: `patrick@home:~/apps/commonlab`. The user explicitly authorized deployment
on 2026-10-26 after the initial preparation-only plan. This record supersedes
that staged plan. See [home-deployment.md](home-deployment.md) for current commands.

## Active release and flow

- Public Docker Hub repository: `pat381/commonlab`; no separate worker repository.
- Runtime revision: `3900f09e94640bd3c8e7242e0ec59249d31090e9`.
- App digest: `sha256:f18ac276786e467aa6e614efcf0d7ab39c5f37dd478421dc6b3d214fa07f1e59`.
- Worker digest: `sha256:f60a72b43c6d2ba6e59b54f6734571898332b586affa796c82e534d53cbba556`.
- `just deploy` runs `scripts/publish-release.sh`: clean committed checkout,
  `linux/amd64` builds, credential-free app/storage/Chromium smoke tests, immutable
  `worker-<full-git-sha>` and `app-<full-git-sha>` tags, then `latest` last.
- App label `io.commonlab.worker-image` contains the matching worker digest.
  Both revisions must match. Auxiliary tag uploads do not trigger incomplete
  releases; every changed `latest` digest triggers the updater.
- `commonlab-update.timer` is enabled and active; five-minute checks plus up to
  twenty seconds jitter. A real systemd no-change tick returned success/exit 0.
- `scripts/update-home.py` runs as patrick, serialized with `flock`, using
  `deploy/compose.registry.yaml`. No builds run on the host update path.
- Cloudflared and the separate dev deployment were not updated or restarted.

## Data, backups, and recovery

Preserved project name `commonlab`, no host ports, hardening and existing volumes:

- `commonlab_commonlab-data` at `/data`.
- `commonlab_generation-traces` at `/var/lib/commonlab/agent-traces`.
- Added `commonlab_generation-checkpoints` at `/var/lib/commonlab/checkpoints`.

The updater pulls and validates the pair and checks worker configuration before
stopping writers. It stops worker then app, archives the complete stopped SQLite
volume (including WAL/SHM) and existing checkpoint volume, and checks gzip
integrity. Backup helpers run as UID/GID 10001, not root with dropped capabilities.
Previous image IDs are tagged for recovery. Private configuration and image IDs
are saved alongside backups; no secrets are printed or committed.

The initial pre-migration backup is:

```text
~/.deploy is NOT used; all state is inside ~/apps/commonlab/.deploy/
backups/20261026T131845Z-3900f09e9464/data.tar.gz
```

The final verified rollout backup is under
`.deploy/backups/20261026T132757Z-3900f09e9464/`, including SQLite and checkpoints.
Observed permissions: `.env` and archives 0600, `.deploy` 0700. Preserve the
initial backup for an actual old-binary rollback; later backups contain the
upgraded database. No automatic pruning or database rollback is performed.

Failures before app startup can restart the old containers. After a new app may
have migrated SQLite, failures latch and stop the new app/worker rather than
run an old binary against the migrated database. Inspect the private failure
record, matching backups and images before recovery. Restoring old data can
lose newer writes and requires explicit operator approval.

## Rollout issues corrected

1. Production `.env` lacked the independent reviewer model required by the newer
   worker. Confirmed using credential-free `checkConfiguration()` probes. Added
   `CL_REVIEW_MODEL_PROVIDER=openrouter`,
   `CL_REVIEW_MODEL_ID=deepseek/deepseek-v4.1-flash-1017`, and
   `CL_REVIEW_REASONING_EFFORT=low`; no provider key changed. The updater now
   checks configuration before downtime. The author remains MiMo V2.6 Flash.
2. The old remembered hostname was stale: the tunnel now routes `common-lab.org`,
   not `commonlab.poberholzer.com`. Corrected updater default and documentation.
3. Cloudflare returns an explicit managed browser challenge for command-line
   public probes. The updater records `public_health: cloudflare-challenge` and
   warns instead of stopping a healthy origin. Plain 403s and other public
   failures are not silently ignored. Cloudflare protection was not changed.

Failed rollout records were preserved under `.deploy/failures/` before recovery;
no database restore was needed. Recovered with the new pinned pair only.

## Verification and qualifications

- Both exact images passed publisher smoke tests before `latest` was advanced.
- App and worker healthy, zero restarts after final rollout.
- Worker readiness includes authenticated protocol handshake and browser preflight.
- Independently verified checkpoint atomic write/fsync/rename/delete preflight.
- Origin `/healthz` and catalog verified from worker over the private network.
- Public hostname responds with Cloudflare managed challenge: public browser
  verification, sign-in, and actual paid generation were NOT performed.
- 27 deployment tests pass (fake publisher/Docker/Compose, failure latches,
  backup ordering, pre-downtime configuration checks, locking, challenge handling,
  installer payload parsing, and real Compose runtime parity).
- Dependency install reported two moderate and one high npm advisory; dependency
  remediation was not part of this deployment.
- External failure notifications are not configured; use systemd status/journal.

Cargo remains `0.1.0`. Deployments are identified by commit and image digest, not
by Cargo package version. Bump that version for intentional named application
releases, not every code or asset change.
