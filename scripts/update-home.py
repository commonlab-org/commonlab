#!/usr/bin/env python3
"""Coordinated, fail-closed registry updater. Requires Python 3.10+ and Docker."""
import argparse
import fcntl
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPOSITORY = "pat381/commonlab"
DIGEST = re.compile(r"pat381/commonlab@sha256:[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
PROJECT = "commonlab"


class UpdateError(RuntimeError):
    pass


def run(*args, output=None, timeout=900):
    """Never relay subprocess output: Compose errors may include secrets."""
    result = subprocess.run(args, stdout=output or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        raise UpdateError(f"{args[0]} failed (exit {result.returncode}); inspect private host logs")
    return result.stdout.decode().strip() if output is None else ""


def inspect(reference):
    try:
        return json.loads(run("docker", "inspect", reference))[0]
    except (ValueError, IndexError) as error:
        raise UpdateError("Invalid Docker inspect response") from error


def atomic_json(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def release(app):
    references = [ref for ref in app.get("RepoDigests", []) if DIGEST.fullmatch(ref)]
    labels = app.get("Config", {}).get("Labels") or {}
    worker = labels.get("io.commonlab.worker-image", "")
    revision = labels.get("org.opencontainers.image.revision", "")
    if len(references) != 1 or not DIGEST.fullmatch(worker) or not REVISION.fullmatch(revision):
        raise UpdateError("Release lacks a pinned app/worker pair or full Git revision")
    return {"app": references[0], "worker": worker, "revision": revision}


def container(service):
    ids = run("docker", "ps", "--filter", f"label=com.docker.compose.project={PROJECT}",
              "--filter", f"label=com.docker.compose.service={service}",
              "--format", "{{.ID}}").split()
    if len(ids) != 1:
        raise UpdateError(f"Expected exactly one running {service}; manual intervention required")
    return inspect(ids[0])


def volume(info, destination):
    matches = [m["Name"] for m in info["Mounts"]
               if m.get("Type") == "volume" and m["Destination"] == destination]
    if len(matches) != 1:
        raise UpdateError(f"Missing unique named volume at {destination}")
    return matches[0]


def backup_volume(image, name, path):
    with path.open("xb") as stream:
        run("docker", "run", "--rm", "--read-only", "--user", "10001:10001",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--network", "none", "--mount",
            f"type=volume,source={name},destination=/source,readonly",
            "--entrypoint", "tar", image, "-C", "/source", "-czf", "-", ".",
            output=stream)
        stream.flush()
        os.fsync(stream.fileno())
    # Read the complete gzip stream, checking its CRC/truncation without extraction.
    with gzip.open(path, "rb") as stream:
        while stream.read(1024 * 1024):
            pass


def wait_healthy(identifier, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = inspect(identifier)["State"]
        if not state.get("Running"):
            raise UpdateError("Container exited before becoming healthy")
        if state.get("Health", {}).get("Status") == "healthy":
            return
        time.sleep(2)
    raise UpdateError("Container did not become healthy within deadline")


def compose(directory, images, *arguments):
    # Explicit env file; no resolved secrets are logged. Image references are non-secret.
    command = ["docker", "compose", "--project-directory", str(directory),
               "--env-file", str(directory / ".env"), "-p", PROJECT,
               "-f", str(directory / "deploy/compose.registry.yaml"),
               "--profile", "generation", "--profile", "tunnel"]
    # CLI env overrides are scoped to this child; do not mutate the timer environment.
    environment = os.environ.copy()
    environment.update(COMMONLAB_APP_IMAGE=images["app"], COMMONLAB_WORKER_IMAGE=images["worker"])
    result = subprocess.run(command + list(arguments), env=environment,
                            capture_output=True, timeout=300, check=False)
    if result.returncode:
        raise UpdateError("Registry Compose operation failed; inspect private service logs")


def stopped(info):
    if inspect(info["Id"])["State"].get("Running"):
        raise UpdateError("Container still running after stop; refusing backup")


def update(directory):
    state = directory / ".deploy"
    state.mkdir(mode=0o700, exist_ok=True)
    state.chmod(0o700)
    lock = (state / "update.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another update is running; skipping.")
        return
    with lock:
        if (state / "failure.json").exists() or (state / "in-progress.json").exists():
            raise UpdateError("Automation latched: inspect .deploy failure/in-progress records; recover before clearing")
        if (state / "paused").exists():
            print("Updates paused by .deploy/paused.")
            return
        stage = "resolve release"
        transaction = False
        migration_possible = False
        old = {}
        candidate = {}
        backup = None
        try:
            run("docker", "pull", f"{REPOSITORY}:latest")
            candidate = release(inspect(f"{REPOSITORY}:latest"))
            success = state / "success.json"
            if success.exists() and json.loads(success.read_text())["app"] == candidate["app"]:
                print("Already deployed " + candidate["revision"])
                return
            stage = "validate and pull pinned pair"
            for reference in (candidate["app"], candidate["worker"]):
                run("docker", "pull", reference)
            worker_labels = inspect(candidate["worker"])["Config"].get("Labels") or {}
            if worker_labels.get("org.opencontainers.image.revision") != candidate["revision"]:
                raise UpdateError("App and worker revisions do not match")
            compose(directory, candidate, "config", "--quiet")
            old = {service: container(service) for service in ("app", "generation-worker")}
            data_volume = volume(old["app"], "/data")
            checkpoint_mounts = [m for m in old["generation-worker"]["Mounts"]
                                 if m["Destination"] == "/var/lib/commonlab/checkpoints"]
            checkpoint_volume = None
            if checkpoint_mounts:
                checkpoint_volume = volume(old["generation-worker"], "/var/lib/commonlab/checkpoints")
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            backup = state / "backups" / (stamp + "-" + candidate["revision"][:12])
            backup.mkdir(parents=True, mode=0o700)
            # Save exact images/configuration before touching the running containers.
            for service, info in old.items():
                run("docker", "tag", info["Image"], f"commonlab-recovery:{service}-{stamp}")
            for source in (".env", "compose.home.yaml", "deploy/compose.registry.yaml"):
                path = directory / source
                if path.exists():
                    shutil.copyfile(path, backup / path.name)
            atomic_json(backup / "release.json", {
                "candidate": candidate,
                "previous": {name: {"id": info["Id"], "image": info["Image"]}
                             for name, info in old.items()},
                "data_volume": data_volume, "checkpoint_volume": checkpoint_volume,
            })
            if shutil.disk_usage(state).free < 2 * 1024**3:
                raise UpdateError("Less than 2 GiB available for backup; free disk space first")
            atomic_json(state / "in-progress.json", {"release": candidate, "backup": str(backup)})
            transaction = True
            stage = "stop writers and back up"
            for service in ("generation-worker", "app"):
                run("docker", "stop", "--time", "60", old[service]["Id"])
                stopped(old[service])
            backup_volume(old["app"]["Image"], data_volume, backup / "data.tar.gz")
            if checkpoint_volume:
                backup_volume(old["generation-worker"]["Image"], checkpoint_volume,
                              backup / "checkpoints.tar.gz")
            stage = "start app (database migration possible)"
            migration_possible = True  # Set BEFORE issuing a command that could start a migration.
            compose(directory, candidate, "up", "-d", "--no-build", "--pull", "never", "--no-deps", "app")
            app = container("app")
            wait_healthy(app["Id"])
            stage = "start worker and check storage"
            compose(directory, candidate, "up", "-d", "--no-build", "--pull", "never", "--no-deps", "generation-worker")
            worker = container("generation-worker")
            wait_healthy(worker["Id"])
            # Independently exercise atomic checkpoint write/fsync/rename/delete in-place.
            run("docker", "exec", worker["Id"], "node", "--input-type=module", "-e",
                'import {CheckpointStore} from "./checkpoints.mjs"; '
                'await CheckpointStore.open("/var/lib/commonlab/checkpoints");')
            stage = "check public routing"
            health = run("curl", "--fail", "--silent", "--show-error", "--max-time", "30",
                         "https://commonlab.poberholzer.com/healthz")
            if health != "ok":
                raise UpdateError("Public health endpoint did not return ok")
            run("curl", "--fail", "--silent", "--show-error", "--max-time", "30",
                "https://commonlab.poberholzer.com/simulations/catalog.json")
            atomic_json(state / "success.json", candidate)
            (state / "in-progress.json").unlink()
            print("Deployed " + candidate["revision"] + "; backup: " + str(backup))
        except Exception as error:
            if transaction:
                # Never run an old app against a potentially migrated database.
                if migration_possible:
                    for service in ("generation-worker", "app"):
                        try:
                            compose(directory, candidate, "stop", "--timeout", "60", service)
                        except Exception:
                            print(f"Could not confirm {service} stopped; check it manually", file=sys.stderr)
                else:
                    try:
                        run("docker", "start", old["app"]["Id"])
                        wait_healthy(old["app"]["Id"])
                        run("docker", "start", old["generation-worker"]["Id"])
                    except Exception:
                        print("Could not confirm old services recovered; check them manually", file=sys.stderr)
                atomic_json(state / "failure.json", {
                    "stage": stage, "backup": str(backup),
                    "migration_possible": migration_possible,
                    "message": "Inspect private logs; verify/restore paired images and backup before clearing latch.",
                })
            # Do not print arbitrary exception strings: they might contain configuration.
            reason = str(error) if isinstance(error, UpdateError) else type(error).__name__
            raise UpdateError(f"Update failed at '{stage}': {reason}. "
                              + (f"Recovery required; see {state}." if transaction
                                 else "Running services were not changed.")) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args()
    os.umask(0o077)
    try:
        update(arguments.directory.resolve())
    except (UpdateError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
