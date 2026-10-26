# Home deployment

`compose.home.yaml` runs CommonLab, its OpenRouter worker, and Cloudflared
without publishing host ports. The deployment directory is `~/apps/commonlab`
on `patrick@home`.

Releases are published to public `pat381/commonlab` and pulled by a coordinated
host updater. The app and worker use separate tags from that one repository;
`latest` points at the app image and carries the exact worker digest label.
A release is committed only after both images pass smoke tests. The updater
backs up SQLite, stops the worker and app, starts the pinned pair, and latches
on failure rather than retrying blindly. Cloudflared is never restarted by the
updater.

## Secrets and configuration

Edit `~/apps/commonlab/.env` on the host (permissions must stay `600`):

- `ADMIN_TOKEN`: generated administrator token; enter it at `/review.html`.
- `GENERATION_WORKER_TOKEN`: generated shared application/worker token.
- `CL_MODEL_API_KEY`: dedicated OpenRouter key, with provider-side spend limits.
- `CL_REVIEW_MODEL_PROVIDER=openrouter` and
  `CL_REVIEW_MODEL_ID=deepseek/deepseek-v4.1-flash-1017`: required image-capable
  independent reviewer (uses the author key unless a reviewer key is configured).
- `CL_REVIEW_REASONING_EFFORT=low`: reviewer reasoning setting.
- `CLOUDFLARE_TUNNEL_TOKEN`: rotated tunnel token.

Do not paste secrets or resolved Compose configuration into logs or chat.
In Cloudflare, configure the tunnel's public hostname
`common-lab.org` with service `http://app:8000`. The updater checks
`https://common-lab.org/healthz`; override `COMMONLAB_PUBLIC_URL` in the
systemd service if the hostname changes.
The browser connection uses HTTPS; the worker uses the private Docker network.
No existing tunnel containers need to be modified.

The registry worker defaults to `openrouter` and `xiaomi/mimo-v2.6-flash`;
`CL_MODEL_PROVIDER` and `CL_MODEL_ID` can override these in `.env`. Environment
changes require explicit recreation with the installed registry Compose file
and the pinned pair recorded in `.deploy/success.json`; a no-change updater
check does not recreate containers. Do not use the legacy local-image launch
command after migrating to registry images.

Registration does not grant AI access: approve the contributor in the review UI, then
request a generation at `/generate.html`. This incurs provider charges.
Generated engines remain private until explicitly submitted and reviewed.

## Optional local agent traces

To capture operator-only development transcripts, add this non-secret setting to the host `.env`:

```dotenv
CL_AGENT_TRACE_DIR=/var/lib/commonlab/agent-traces
CL_AGENT_TRACE_MAX_FILES=50
CL_AGENT_TRACE_MAX_BYTES=8388608
CL_AGENT_TRACE_MAX_DAYS=7
```

Recreate the worker using the installed registry Compose file and pinned pair. The Compose file provides a worker-only named volume (`commonlab_generation-traces`); the app and tunnel cannot serve it. Files are mode `0600` under a `0700` directory. Traces include prompts, intermediate source and tool results, exclude private reasoning, and redact known credentials. Do not paste them into public logs. Leaving `CL_AGENT_TRACE_DIR` empty disables tracing.

List or copy files locally as the operator, for example:

```sh
cd ~/apps/commonlab
docker compose -f compose.home.yaml exec -T generation-worker ls /var/lib/commonlab/agent-traces
# Inspect a particular named file privately; never serve this directory over HTTP.
docker cp commonlab-generation-worker-1:/var/lib/commonlab/agent-traces/generation-<job>-<time>-<uuid>.jsonl /secure/local/path/
```

Retention is independent of SQLite and runs at the start of each traced job. See [AI generation](ai-generation.md#optional-operator-only-agent-traces) for size caps, redaction and interrupted-run limitations.

## Registry releases and automatic updates

From a clean, committed checkout, `just deploy` builds and verifies both
`linux/amd64` images, refuses to overwrite an existing revision tag, publishes
immutable `app-<git-sha>` and `worker-<git-sha>` tags, and advances `latest`
last. It needs a Docker Hub login with write access to `pat381/commonlab`; no
Cargo version bump is required (the Git revision and image digest identify a
release).

The first registry rollout is manual:

```sh
just install-updater   # stage files/units; does not start or enable anything
just update-home       # coordinated first deployment from the registry
just enable-auto-update
```

Later `just deploy` publishes a release; an enabled timer checks every five
minutes and deploys it. If the updater fails, it writes a private record under
`~/apps/commonlab/.deploy/`, keeps the old pair running when safe, or stops
services without reusing an old binary against a possibly migrated database.

```sh
just deploy-status        # timer/service state and recent journal
just pause-auto-update    # stop future checks; a running update is not interrupted
```

Do not clear `failure.json` or `in-progress.json` until the named recovery step
is done; those latches block all further Docker changes. Backups contain `.env`
and are mode `0600` inside a `0700` directory. Keep them private.

The updater checks worker configuration before downtime, then verifies both
container health checks, authenticated worker handshake, atomic checkpoint
storage, and origin HTTP/catalog access. A Cloudflare browser challenge is
recorded as `public_health: cloudflare-challenge` with a warning, not mistaken
for a broken origin. Other public failures still fail the deployment. Browser
verification remains necessary when Cloudflare challenges automated probes.

## Legacy local builds (not the registry deployment)

Do not run legacy Compose `up` after migrating: it selects local images rather
than the pinned registry pair. These commands are retained only for offline
image builds and deliberate operator recovery.

Local builds use host networking to work around this host's BuildKit DNS failure.
Buildx requires an explicit entitlement (runtime networking stays private):

```sh
cd ~/apps/commonlab
docker buildx bake -f compose.home.yaml --allow=network.host --load \
  app generation-worker
```

Do not add `--build` to Compose on this host; use the explicit Buildx command
for local builds. Normal registry releases use `just deploy` instead.

```sh
cd ~/apps/commonlab
docker compose -f compose.home.yaml --profile generation --profile tunnel ps
docker compose -f compose.home.yaml logs --tail=100 app
```

Profiles keep the worker and tunnel out of an app-only first launch. Once
started, services restart automatically unless stopped. Use the same profiles
when stopping the whole stack. Never use `down -v` unless deleting all data is
intentional. See [deployment.md](deployment.md) for backups, recovery,
monitoring, and publication safety. Cloudflare ingress does not replace abuse
controls; configure suitable Cloudflare rate limits/access policies before
opening registration broadly.
