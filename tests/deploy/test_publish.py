"""Exercise the real publisher with fake executables; never contact a registry."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
name = os.path.basename(sys.argv[0])
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
mode = os.environ.get("FAIL_MODE", "")
if name == "git":
    if args == ["rev-parse", "HEAD"]: print("c" * 40)
    elif args[0] == "status":
        if mode == "dirty": print(" M source")
    elif args[0] == "rev-parse": print(os.environ["PUBLISH_LOCK"])
elif name == "docker":
    if args[:3] == ["buildx", "imagetools", "inspect"]:
        if mode == "exists": sys.exit(0)
        print("registry unavailable" if mode == "registry" else "manifest unknown", file=sys.stderr)
        sys.exit(1)
    if args[0] == "run" and "--entrypoint" in args and mode == "browser": sys.exit(1)
    if args[:2] == ["image", "inspect"]:
        print(json.dumps([{"RepoDigests": ["pat381/commonlab@sha256:" + "b" * 64]}]))
    elif args[0] == "inspect": print("healthy")
    elif args[0] == "port": print("127.0.0.1:40000")
    elif args[0] == "push" and ":worker-" in args[-1] and mode == "worker-push": sys.exit(1)
elif name == "curl":
    if mode == "app": sys.exit(1)
    url = next((arg for arg in args if arg.startswith("http://")), "")
    if url.endswith("healthz"): print("ok")
    elif url.endswith("catalog.json"): print('{"schemaVersion":1}')
    elif url.endswith("commonlab.sqlite3"): print("404")
    elif url.endswith("api/auth/register"): print('{"username":"release-smoke"}')
    elif url.endswith("/"): print("<title>CommonLab</title>")
'''


class PublishTests(unittest.TestCase):
    def publish(self, mode=""):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            for name in ("git", "docker", "curl"):
                script = path / name
                script.write_text(FAKE)
                script.chmod(0o755)
            log = path / "commands.jsonl"
            environment = dict(os.environ, PATH=str(path) + ":" + os.environ["PATH"],
                               COMMAND_LOG=str(log), PUBLISH_LOCK=str(path / "lock"),
                               FAIL_MODE=mode)
            result = subprocess.run(["bash", str(ROOT / "scripts/publish-release.sh")],
                                    cwd=ROOT, env=environment, capture_output=True,
                                    text=True, timeout=30)
            events = [json.loads(line) for line in log.read_text().splitlines()]
            return result, events

    def test_success_commits_latest_last(self):
        result, events = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        pushes = [event for event in events if event[:2] == ["docker", "push"]]
        self.assertEqual([event[-1].split(":")[-1].split("-")[0] for event in pushes],
                         ["worker", "app", "latest"])
        builds = [event for event in events if event[:3] == ["docker", "buildx", "build"]]
        self.assertIn("io.commonlab.worker-image=pat381/commonlab@sha256:" + "b" * 64,
                      builds[-1])
        browser = next(i for i, event in enumerate(events) if "--entrypoint" in event)
        first_push = next(i for i, event in enumerate(events) if event[:2] == ["docker", "push"])
        self.assertLess(browser, first_push)

    def test_prepublication_failures_do_not_push(self):
        for mode in ("dirty", "exists", "registry", "browser", "app"):
            with self.subTest(mode=mode):
                result, events = self.publish(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(event[:2] == ["docker", "push"] for event in events))

    def test_partial_push_never_advances_latest(self):
        result, events = self.publish("worker-push")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(event[:2] == ["docker", "push"] and event[-1].endswith(":latest")
                             for event in events))


if __name__ == "__main__":
    unittest.main()
