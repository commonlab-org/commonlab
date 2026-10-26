# Home registry rollout — staged plan, NOT activated

Prepared 2026-10-26. Target: `patrick@home:~/apps/commonlab`.

**Approval boundary:** preparation only. Do not push images, copy configuration to
production, stop services, migrate the database, or install/enable the update timer
until explicitly authorized. This document is a plan, not an installed updater.

## Decisions

- Public Docker Hub repository: `pat381/commonlab`.
- Publishing a release to Docker Hub triggers deployment; a Git push alone does not.
- A host-side systemd service/timer polls every five minutes. No watcher image,
  additional Docker Hub repository, inbound webhook, or public Docker socket is needed.
- Only CommonLab app and worker update automatically. Cloudflared and the separate
  development deployment are outside the updater's scope.
- Keep the Compose project name `commonlab`, private network, no published ports,
  existing secrets, and existing data/traces volumes.

## Read-only preflight findings

- Host architecture: x86_64; build `linux/amd64` images initially.
- Docker Engine 29.8.1, Compose 5.6.0, systemd 262 available.
- App healthy; app and worker use `commonlab:local` / `commonlab-worker:local`.
- Worker is running but its current image does not provide a health check.
- Database mount: `commonlab_commonlab-data` at `/data`.
- Worker trace mount: `commonlab_generation-traces` at
  `/var/lib/commonlab/agent-traces`.
- **Current worker has no persistent checkpoint mount.** The new Compose already
  defines one; first rollout must create and validate
  `commonlab_generation-checkpoints` at `/var/lib/commonlab/checkpoints`.
- `.env` permissions are 0600; noninteractive sudo is available.
- Approximately 361 GiB free on the deployment filesystem.
- No matching update timer found. No remote changes made during preflight.
- Local Docker Hub credential entries exist; write access to `pat381/commonlab`
  has NOT been verified by a push. Do not print or copy credential contents.
- Existing GitHub workflow builds/smoke-tests images but does not publish them.

## Image and release contract

Use one repository with distinct image tags:

| Tag/reference | Purpose |
| --- | --- |
| `pat381/commonlab:app-<full-git-sha>` | App release image; never overwrite |
| `pat381/commonlab:worker-<full-git-sha>` | Worker release image; never overwrite |
| `pat381/commonlab:latest` | Watched production release pointer (app image) |
| `pat381/commonlab@sha256:...` | Exact app/worker images used by deployment |

Publish the worker first. Build the app with OCI revision/source labels and a
custom `io.commonlab.worker-image` label containing the worker's **full digest
reference**. Test both exact images, including hardened runtime and checkpoint
storage checks. Push immutable release tags, then advance `latest` **last**, only
when the pair passes validation. Keep old release images for recovery.

This gives one commit point without a third image or repository. The watcher
resolves the `latest` digest once, pins that app digest, then reads and validates
its worker reference. Never resolve a mutable tag again halfway through rollout.
Allow only worker digest references in `pat381/commonlab`; reject missing or
malformed release metadata before stopping anything.

Every new digest pushed to `latest` triggers an update. Pushing only an auxiliary
worker/version tag does not constitute a complete release and must not deploy a
half-published pair. Re-pushing an unchanged digest is a no-op. Correct a bad
release with a new release, not by silently retrying it indefinitely.

## Files to implement and validate before activation

1. A registry-only home Compose configuration (or explicit override), preserving
   the current service hardening and volumes. Remove local build dependence from
   the production path; app and worker accept digest-pinned image references.
   Keep local build configuration available separately.
2. A publisher that builds, validates, and pushes the pair above. Nothing should
   publish simply because this preparation branch is committed. If later using
   GitHub Actions, use an explicit publish workflow and a Docker Hub write token
   stored as a repository secret; serialize production publishes.
3. A host updater plus systemd oneshot service and five-minute timer. Use `flock`
   to prevent overlapping automatic/manual updates. Root-owned executable/unit;
   private state and backup directories. Timer starts only after first rollout
   succeeds and approval is given.
4. Automated tests with fake Docker/Compose commands for no-change, pull failure,
   malformed metadata, backup failure, unhealthy app/worker, interruption, failed
   digest suppression, locking, and preservation of volumes/tunnel configuration.
5. Amend `docs/home-deployment.md` with publish, pause/resume, status, recovery,
   and initial registry migration instructions after these artifacts exist.

## Watcher transaction

1. Acquire lock; honor a persistent pause/failure latch. Compare registry digest
   with last successfully deployed digest. Registry/auth/network errors must leave
   running services untouched and emit an actionable journal error.
2. Validate release metadata and fetch both exact image digests **before downtime**.
   Validate Compose privately without logging resolved secrets. Ensure enough disk
   space for backups and both image pairs.
3. Record old image IDs/digests and private configuration. Initial local images
   have no registry digests: preserve/tag their image IDs explicitly. Do not prune
   recovery images or volumes automatically.
4. Stop worker gracefully first, then app; preserve/requeue operational checkpoints.
   Allow sufficient stop grace time and verify exit before backing up.
5. Archive the complete stopped SQLite `/data` directory (including WAL/SHM), plus
   existing checkpoint storage if present. Use a helper running as UID/GID 10001,
   NOT root with dropped capabilities. Backups must be mode 0600 inside a 0700
   directory. Check archive integrity, record image/config pairing, and abort on
   any backup error. Do not log private file contents.
6. Atomically write the candidate digest references. Start only app with
   `--no-build --pull never`, wait for healthy, check `/healthz`, catalog and an
   existing publication without secrets or paid generation calls.
7. Start worker only after app health passes. Check worker health, authenticated
   application connectivity, and checkpoint atomic write/fsync/rename/delete
   preflight under the actual read-only runtime constraints. Verify public HTTPS
   health through the existing tunnel. Never auto-publish generated results.
8. Persist successful release state atomically and report success in the journal.
   Cloudflared should remain running throughout; do not use stack-wide `down`.

The first registry rollout includes newer app/worker protocol and database
migrations. Review compatibility notes before this first deployment; it is not
merely changing image names. Preserve operator model choices/secrets, and do not
copy development provider settings into production by accident.

## Failure and recovery policy

- Before stopping services: abort safely, keep current deployment running.
- Before starting the new app: restore old image references and resume the old
  stack if backup/config preparation fails; verify recovery health.
- After the new app may have migrated SQLite: **do not blindly start the old
  binary against that database**. Stop the incompatible services, preserve failed
  state, latch/pause automation, and require operator recovery using the matching
  database/checkpoint backup and old images. Keep the tunnel container intact.
- Block repeated attempts of a failed digest until explicit operator retry or a
  reviewed newer release. Clearly record failure stage, release digest, backup
  path and recovery instructions without credentials/private payloads.
- Alerting: failures visible in `systemctl status` / journal; choose an external
  notification destination before treating unattended updates as fully operated.
- Image rollback is not a database rollback. Restoring a backup can discard
  writes after that backup; require operator approval.

## Activation sequence — only after approval

1. Verify Docker Hub repository ownership/public visibility and publisher access.
   Public pulls need no host login; authenticated pulls remain optional if rate
   limits require them. No watcher repository/setup on Docker Hub is needed.
2. Implement/test the artifacts above locally; validate both hardened images.
3. Publish the first validated release pair to `pat381/commonlab`, advancing
   `latest` only after all checks pass. No production watcher is enabled yet.
4. Back up the existing remote Compose/config and image IDs privately. Stage the
   registry Compose/updater and validate without printing `.env` values.
5. Perform the initial coordinated backup and deployment manually. Confirm data,
   publications, app/worker health, checkpoint persistence and public routing.
   Test sign-in manually; generation verification needs separate approval because
   it incurs provider charges.
6. Install/enable the timer; test a no-change tick. Verify pause/resume, failure
   reporting, and a registry update in a disposable environment before relying on
   unattended production updates.

Expect brief downtime during backup/app recreation; this is not a zero-downtime
SQLite deployment. Current production stays unchanged until explicit approval.
