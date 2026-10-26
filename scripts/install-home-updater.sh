#!/usr/bin/env bash
# Explicit staging only: does NOT deploy images or enable the timer.
set -euo pipefail
cd "$(dirname "$0")/.."
payload=$(mktemp)
trap 'rm -f "$payload"' EXIT
python3 - >"$payload" <<'PY'
from pathlib import Path
files = {path: Path(path).read_text() for path in (
    "scripts/update-home.py", "deploy/compose.registry.yaml")}
print("FILES = " + repr(files))
print(r'''
import fcntl
import getpass
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

os.umask(0o077)
root = Path.home() / "apps/commonlab"
if not (root / ".env").is_file():
    raise SystemExit("Expected existing ~/apps/commonlab/.env; nothing installed")
user = getpass.getuser()
if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user) or any(c.isspace() for c in str(root)):
    raise SystemExit("Unsupported systemd username/path")
subprocess.run(["sudo", "-n", "true"], check=True)
for unit in ("commonlab-update.timer", "commonlab-update.service"):
    if subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0:
        raise SystemExit("Pause timer and wait for updater to finish before reinstalling")
state = root / ".deploy"
state.mkdir(mode=0o700, exist_ok=True)
state.chmod(0o700)
with (state / "update.lock").open("w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    backup = state / ("install-backup-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    backup.mkdir()
    for relative, content in FILES.items():
        target = root / relative
        if target.exists():
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, saved)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".install-tmp")
        temporary.write_text(content)
        temporary.replace(target)
    service = f"""[Unit]
Description=Coordinated CommonLab registry update
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
User={user}
WorkingDirectory={root}
UMask=0077
ExecStart=/usr/bin/python3 {root}/scripts/update-home.py
TimeoutStartSec=30min
"""
    timer = """[Unit]
Description=Check CommonLab Docker Hub release every five minutes

[Timer]
OnBootSec=5min
OnUnitInactiveSec=5min
RandomizedDelaySec=20s
Unit=commonlab-update.service

[Install]
WantedBy=timers.target
"""
    with tempfile.TemporaryDirectory() as temporary:
        for name, content in (("commonlab-update.service", service), ("commonlab-update.timer", timer)):
            path = Path(temporary) / name
            path.write_text(content)
            subprocess.run(["sudo", "-n", "install", "-m", "644", str(path),
                            "/etc/systemd/system/" + name], check=True)
        subprocess.run(["sudo", "-n", "systemctl", "daemon-reload"], check=True)
print("Updater installed but NOT enabled. App, worker, tunnel, and .env unchanged.")
print("After publishing: just update-home; verify health; then just enable-auto-update.")
''')
PY
ssh -o BatchMode=yes patrick@home 'python3 -' <"$payload"
