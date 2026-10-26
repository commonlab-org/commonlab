import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/update-home.py"
spec = importlib.util.spec_from_file_location("update_home", SCRIPT)
assert spec is not None and spec.loader is not None
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)
APP = "pat381/commonlab@sha256:" + "a" * 64
WORKER = "pat381/commonlab@sha256:" + "b" * 64
REVISION = "c" * 40
RELEASE = {"app": APP, "worker": WORKER, "revision": REVISION}


def image():
    return {"RepoDigests": [APP], "Config": {"Labels": {
        "io.commonlab.worker-image": WORKER,
        "org.opencontainers.image.revision": REVISION,
    }}}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / ".deploy"
        self.state.mkdir()
        (self.root / ".env").write_text("ADMIN_TOKEN=private\n")
        self.events = []
        self.inject_failure = None
        self.mismatch = False
        self.health = "ok"
        self.infos = {}
        for name in ("app", "generation-worker"):
            mounts = [{"Type": "volume", "Name": "commonlab_commonlab-data",
                       "Destination": "/data"}] if name == "app" else []
            self.infos[name] = {"Id": "old-" + name, "Image": "sha256:" + name,
                                "Mounts": mounts, "State": {"Running": True}}
        for name, fake in (("run", self.run_command), ("inspect", self.inspect),
                           ("container", lambda service: self.infos[service]),
                           ("compose", self.compose), ("backup_volume", self.backup),
                           ("public_check", self.public_check),
                           ("wait_healthy", lambda *args: self.events.append(("healthy", *args)))):
            patcher = patch.object(updater, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_command(self, *args, **kwargs):
        self.events.append(args)
        if self.inject_failure and self.inject_failure(args):
            raise updater.UpdateError("injected command failure")
        if args[:2] == ("docker", "stop"):
            self.infos[args[-1].removeprefix("old-")]["State"]["Running"] = False
        if args[:2] == ("docker", "start"):
            self.infos[args[-1].removeprefix("old-")]["State"]["Running"] = True
        return self.health if args[0] == "curl" and args[-1].endswith("healthz") else ""

    def inspect(self, ref):
        if ref in ("pat381/commonlab:latest", APP):
            return image()
        if ref == WORKER:
            return {"Config": {"Labels": {"org.opencontainers.image.revision":
                                          "d" * 40 if self.mismatch else REVISION}}}
        return self.infos[ref.removeprefix("old-")]

    def compose(self, directory, images, *arguments):
        event = ("compose", *arguments)
        self.events.append(event)
        self.assertEqual(images, RELEASE)
        if self.inject_failure and self.inject_failure(event):
            raise updater.UpdateError("injected compose failure")
        if arguments[0] == "up":
            self.infos[arguments[-1]]["State"]["Running"] = True

    def backup(self, image_ref, name, path):
        self.events.append(("backup", name))
        if self.inject_failure and self.inject_failure(("backup", name)):
            raise updater.UpdateError("injected backup failure")
        path.write_bytes(b"fake verified archive")

    def public_check(self):
        if self.health == "wrong response":
            raise updater.UpdateError("Public health endpoint did not return ok")
        return self.health

    def update(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            updater.update(self.root)

    def test_success_order_and_private_backup(self):
        self.update()
        self.assertEqual(json.loads((self.state / "success.json").read_text()),
                         {**RELEASE, "public_health": "ok"})
        self.assertFalse((self.state / "in-progress.json").exists())
        self.assertFalse((self.state / "failure.json").exists())
        events = self.events
        worker_stop = events.index(("docker", "stop", "--time", "60", "old-generation-worker"))
        app_stop = events.index(("docker", "stop", "--time", "60", "old-app"))
        backup = events.index(("backup", "commonlab_commonlab-data"))
        starts = [i for i, event in enumerate(events) if event[:2] == ("compose", "up")]
        self.assertLess(worker_stop, app_stop)
        self.assertLess(app_stop, backup)
        self.assertLess(backup, starts[0])
        self.assertEqual(events[starts[0]][-1], "app")
        self.assertEqual(events[starts[1]][-1], "generation-worker")
        self.assertFalse(any("cloudflared" in event or "down" in event for event in events))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        saved = next((self.state / "backups").glob("*/.env"))
        self.assertEqual(saved.read_text(), "ADMIN_TOKEN=private\n")

    def test_same_digest_is_noop(self):
        (self.state / "success.json").write_text(json.dumps(RELEASE))
        self.update()
        self.assertFalse(any(e[:2] == ("docker", "stop") for e in self.events))
        self.assertFalse((self.state / "backups").exists())

    def test_registry_failure_leaves_services_untouched(self):
        self.inject_failure = lambda event: event[:2] == ("docker", "pull")
        with self.assertRaisesRegex(updater.UpdateError, "not changed"):
            self.update()
        self.assertFalse((self.state / "failure.json").exists())
        self.assertFalse(any(e[:2] == ("docker", "stop") for e in self.events))

    def test_mismatched_revision_rejected_before_stop(self):
        self.mismatch = True
        with self.assertRaisesRegex(updater.UpdateError, "do not match"):
            self.update()
        self.assertFalse(any(e[:2] == ("docker", "stop") for e in self.events))

    def test_compose_validation_failure_leaves_services_untouched(self):
        self.inject_failure = lambda event: event[:2] == ("compose", "config")
        with self.assertRaisesRegex(updater.UpdateError, "not changed"):
            self.update()
        self.assertFalse(any(e[:2] == ("docker", "stop") for e in self.events))

    def test_backup_failure_restarts_old_pair_and_latches(self):
        self.inject_failure = lambda event: event[0] == "backup"
        with self.assertRaisesRegex(updater.UpdateError, "Recovery required"):
            self.update()
        failure = json.loads((self.state / "failure.json").read_text())
        self.assertFalse(failure["migration_possible"])
        self.assertIn(("docker", "start", "old-app"), self.events)
        self.assertIn(("docker", "start", "old-generation-worker"), self.events)
        self.assertFalse(any(e[:2] == ("compose", "up") for e in self.events))

    def test_migration_failure_never_restarts_old_images(self):
        self.inject_failure = lambda event: event[:2] == ("compose", "up")
        with self.assertRaises(updater.UpdateError):
            self.update()
        failure = json.loads((self.state / "failure.json").read_text())
        self.assertTrue(failure["migration_possible"])
        self.assertFalse(any(e[:2] == ("docker", "start") for e in self.events))
        self.assertIn(("compose", "stop", "--timeout", "60", "app"), self.events)

    def test_public_health_failure_latches(self):
        self.health = "wrong response"
        with self.assertRaisesRegex(updater.UpdateError, "did not return ok"):
            self.update()
        self.assertTrue((self.state / "failure.json").exists())
        self.assertFalse((self.state / "success.json").exists())

    def test_challenge_records_warning_but_keeps_healthy_pair_running(self):
        self.health = "cloudflare-challenge"
        self.update()
        result = json.loads((self.state / "success.json").read_text())
        self.assertEqual(result["public_health"], "cloudflare-challenge")
        self.assertFalse((self.state / "failure.json").exists())

    def test_worker_configuration_checked_before_stop(self):
        self.inject_failure = lambda event: event[:2] == ("compose", "run")
        with self.assertRaisesRegex(updater.UpdateError, "preflight worker configuration"):
            self.update()
        self.assertFalse(any(e[:2] == ("docker", "stop") for e in self.events))

    def test_checkpoint_volume_backed_up_when_present(self):
        self.infos["generation-worker"]["Mounts"].append({
            "Type": "volume", "Name": "commonlab_generation-checkpoints",
            "Destination": "/var/lib/commonlab/checkpoints"})
        self.update()
        self.assertIn(("backup", "commonlab_generation-checkpoints"), self.events)

    def test_bind_mount_database_rejected(self):
        self.infos["app"]["Mounts"][0]["Type"] = "bind"
        with self.assertRaisesRegex(updater.UpdateError, "named volume"):
            self.update()
        self.assertFalse(any(e[:2] == ("docker", "stop") for e in self.events))

    def test_latches_block_all_docker_calls(self):
        for name in ("failure.json", "in-progress.json"):
            with self.subTest(name=name):
                path = self.state / name
                path.write_text("{}")
                with self.assertRaisesRegex(updater.UpdateError, "latched"):
                    self.update()
                path.unlink()
        self.assertEqual(self.events, [])

    def test_pause_blocks_all_docker_calls(self):
        (self.state / "paused").touch()
        self.update()
        self.assertEqual(self.events, [])

    def test_overlapping_update_skipped(self):
        with (self.state / "update.lock").open("w") as lock:
            updater.fcntl.flock(lock, updater.fcntl.LOCK_EX | updater.fcntl.LOCK_NB)
            self.update()
        self.assertEqual(self.events, [])


class PublicCheckTests(unittest.TestCase):
    def test_cloudflare_challenge_is_inconclusive(self):
        error = updater.HTTPError(updater.PUBLIC_URL, 403, "Forbidden",
                                  {"cf-mitigated": "challenge"}, io.BytesIO())
        with patch.object(updater, "urlopen", side_effect=error), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(updater.public_check(), "cloudflare-challenge")

    def test_plain_forbidden_is_not_silently_ignored(self):
        error = updater.HTTPError(updater.PUBLIC_URL, 403, "Forbidden", {}, io.BytesIO())
        with patch.object(updater, "urlopen", side_effect=error), self.assertRaisesRegex(updater.UpdateError, "HTTP 403"):
            updater.public_check()

    def test_origin_error_is_not_silently_ignored(self):
        error = updater.HTTPError(updater.PUBLIC_URL, 502, "Bad Gateway", {}, io.BytesIO())
        with patch.object(updater, "urlopen", side_effect=error), self.assertRaisesRegex(updater.UpdateError, "HTTP 502"):
            updater.public_check()


class ReleaseTests(unittest.TestCase):
    def test_valid_pair(self):
        self.assertEqual(updater.release(image()), RELEASE)

    def test_bad_release_metadata(self):
        for value in ("other/repo@sha256:" + "b" * 64, "pat381/commonlab:latest", ""):
            with self.subTest(value=value):
                info = image()
                info["Config"]["Labels"]["io.commonlab.worker-image"] = value
                with self.assertRaises(updater.UpdateError):
                    updater.release(info)

    def test_missing_app_digest(self):
        info = image()
        info["RepoDigests"] = []
        with self.assertRaises(updater.UpdateError):
            updater.release(info)

    def test_nonrevision_release_rejected(self):
        info = image()
        info["Config"]["Labels"]["org.opencontainers.image.revision"] = "0.1.0"
        with self.assertRaises(updater.UpdateError):
            updater.release(info)


if __name__ == "__main__":
    unittest.main()
