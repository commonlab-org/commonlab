#!/usr/bin/env bash
# Explicit release command. Never invoked by a default recipe or ordinary Git push.
set -euo pipefail
cd "$(dirname "$0")/.."
repo=pat381/commonlab
revision=$(git rev-parse HEAD)
if [[ -n $(git status --porcelain --untracked-files=normal) ]]; then
  echo 'Refusing to publish a dirty checkout. Commit the release first.' >&2
  exit 1
fi
for tool in docker curl python3 flock; do command -v "$tool" >/dev/null; done
# Serialize local publishers. Cross-machine publishers must also be coordinated.
exec 9>"$(git rev-parse --git-path commonlab-publish.lock)"
flock -n 9 || { echo 'Another publisher is running.' >&2; exit 1; }
worker="$repo:worker-$revision"
app="$repo:app-$revision"
container="commonlab-release-test-$$"
check_volume="commonlab-release-check-$$"
cleanup() {
  docker rm -fv "$container" >/dev/null 2>&1 || true
  docker volume rm -f "$check_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Never overwrite a version tag. Registry errors fail closed, not as "tag absent".
for image in "$worker" "$app"; do
  if output=$(docker buildx imagetools inspect "$image" 2>&1); then
    echo "Version tag already exists: $image; use a new commit." >&2
    exit 1
  elif ! grep -Eqi 'manifest unknown|not found|does not exist' <<<"$output"; then
    echo "Cannot establish whether $image exists; check registry access." >&2
    exit 1
  fi
done

build() {
  docker buildx build --platform linux/amd64 --network host \
    --allow=network.host --load \
    --label "org.opencontainers.image.revision=$revision" \
    --label org.opencontainers.image.source=https://github.com/davidwatts117/commonlab \
    "$@" .
}

smoke_app() {
  docker run -d --name "$container" -p 127.0.0.1::8000 \
    --read-only --tmpfs /tmp:rw,noexec,nosuid,size=67108864 \
    --cap-drop ALL --security-opt no-new-privileges:true "$app" >/dev/null
  healthy=false
  for _ in $(seq 1 60); do
    if [[ $(docker inspect -f '{{.State.Health.Status}}' "$container") == healthy ]]; then
      healthy=true; break
    fi
    sleep 2
  done
  [[ $healthy == true ]] || { echo 'App image failed health check.' >&2; exit 1; }
  port=$(docker port "$container" 8000/tcp)
  origin="http://$port"
  curl -fsS "$origin/healthz" | grep -qx ok
  curl -fsS "$origin/" | grep -q '<title>CommonLab'
  curl -fsS "$origin/simulations/catalog.json" | grep -q '"schemaVersion"'
  curl -fsS -o /dev/null "$origin/simulations/pendulum"
  [[ $(curl -s -o /dev/null -w '%{http_code}' "$origin/commonlab.sqlite3") == 404 ]]
  curl -fsS -X POST "$origin/api/auth/register" \
    -H 'content-type: application/json' -H 'X-CommonLab-Request: 1' \
    -d '{"username":"release-smoke","password":"release smoke test password"}' \
    | grep -q '"release-smoke"'
  cleanup
}

build -f worker/Dockerfile -t "$worker"
# Credential-free smoke test, including real persistent-volume permissions,
# atomic checkpoint preflight and Chromium launch. No paid model calls.
docker run --rm --init --read-only --user 10001:10001 \
  --tmpfs /tmp:rw,noexec,nosuid,size=536870912,mode=1777 \
  --mount type=volume,src="$check_volume",dst=/var/lib/commonlab/checkpoints \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --entrypoint node "$worker" --input-type=module -e '
    import {CheckpointStore} from "./checkpoints.mjs";
    import {chromium} from "playwright";
    await CheckpointStore.open("/var/lib/commonlab/checkpoints");
    const browser = await chromium.launch({args: ["--no-sandbox", "--disable-dev-shm-usage"]});
    const page = await browser.newPage();
    await page.setContent("<title>release-smoke</title>");
    if (await page.title() !== "release-smoke") throw new Error("Browser smoke failed");
    await browser.close();
  '
# Validate the app before uploading anything. Label-only final build is cached.
build -t "$app"
smoke_app

docker push "$worker"
worker_digest=$(docker image inspect "$worker" | python3 -c '
import json, re, sys
refs = json.load(sys.stdin)[0]["RepoDigests"]
refs = [r for r in refs if re.fullmatch(r"pat381/commonlab@sha256:[0-9a-f]{64}", r)]
if len(refs) != 1: raise SystemExit("Cannot identify pushed worker digest")
print(refs[0])
')
build -t "$app" --label "io.commonlab.worker-image=$worker_digest"
smoke_app
docker push "$app"
# Commit point: no host sees a release until both images have passed smoke tests.
docker tag "$app" "$repo:latest"
docker push "$repo:latest"
echo "Published $revision. An enabled home timer will deploy within five minutes."
