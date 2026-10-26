set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

# Show available commands. Nothing deploys by default.
default:
    @just --list

# Build, smoke-test, and publish a release to Docker Hub (triggers an enabled host timer).
deploy:
    bash scripts/publish-release.sh

# Install host files and systemd units, WITHOUT starting services or enabling the timer.
install-updater:
    bash scripts/install-home-updater.sh

# First deployment, or an immediate registry check. Stops services if latest changed.
update-home:
    ssh patrick@home 'python3 ~/apps/commonlab/scripts/update-home.py'

# Enable automatic registry checks AFTER a successful manual update-home.
enable-auto-update:
    ssh patrick@home 'test -f ~/apps/commonlab/.deploy/success.json && test ! -e ~/apps/commonlab/.deploy/failure.json && test ! -e ~/apps/commonlab/.deploy/in-progress.json && sudo systemctl enable --now commonlab-update.timer'

# Pause automatic checks; does not interrupt an already running update.
pause-auto-update:
    ssh patrick@home 'sudo systemctl disable --now commonlab-update.timer'

# Inspect the updater's latest result; no secrets or resolved Compose output.
deploy-status:
    ssh patrick@home 'systemctl status commonlab-update.timer --no-pager; systemctl show commonlab-update.service -p Result -p ExecMainStatus -p ActiveState'

# Local checks only: never build, publish, SSH, or deploy.
test-deploy:
    bash -n scripts/publish-release.sh scripts/install-home-updater.sh
    python3 -m unittest discover -s tests/deploy -v
    just --summary
