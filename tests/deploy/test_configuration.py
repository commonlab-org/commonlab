import ast
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ConfigurationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("docker"), "Docker CLI required for Compose validation")
    def test_registry_preserves_home_runtime(self):
        environment = {"PATH": os.environ["PATH"], "HOME": str(Path.home()),
                       "ADMIN_TOKEN": "a" * 32, "GENERATION_WORKER_TOKEN": "b" * 32,
                       "COMMONLAB_APP_IMAGE": "pat381/commonlab@sha256:" + "a" * 64,
                       "COMMONLAB_WORKER_IMAGE": "pat381/commonlab@sha256:" + "b" * 64}
        def config(file):
            result = subprocess.run([
                "docker", "compose", "--project-directory", str(ROOT),
                "--env-file", "/dev/null", "-f", file,
                "--profile", "generation", "--profile", "tunnel", "config", "--format", "json",
            ], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, "Compose validation failed")
            return json.loads(result.stdout)
        home = config("compose.home.yaml")
        registry = config("deploy/compose.registry.yaml")
        for service in ("app", "generation-worker"):
            self.assertNotIn("build", registry["services"][service])
        for value in (home, registry):
            for service in ("app", "generation-worker"):
                for key in ("build", "image", "pull_policy", "stop_grace_period"):
                    value["services"][service].pop(key, None)
        self.assertEqual(home, registry)

    def test_installer_payload_compiles_without_running_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ssh = root / "ssh"
            ssh.write_text('#!/bin/sh\nexec tee "$CAPTURE_PAYLOAD" >/dev/null\n')
            ssh.chmod(0o755)
            payload = root / "install.py"
            environment = dict(os.environ, PATH=str(root) + ":" + os.environ["PATH"],
                               CAPTURE_PAYLOAD=str(payload))
            result = subprocess.run(["bash", str(ROOT / "scripts/install-home-updater.sh")],
                                    env=environment, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0)
            source = payload.read_text()
            tree = ast.parse(source)
            assignment = tree.body[0]
            assert isinstance(assignment, ast.Assign)
            files = ast.literal_eval(assignment.value)
            self.assertEqual(set(files), {"scripts/update-home.py", "deploy/compose.registry.yaml"})
            self.assertIn("OnUnitInactiveSec=5min", source)
            self.assertIn("TimeoutStartSec=30min", source)
            commands = [node.value for statement in tree.body[1:]
                        for node in ast.walk(statement) if isinstance(node, ast.Constant)]
            for command in ("enable", "restart", "up"):
                self.assertNotIn(command, commands)


if __name__ == "__main__":
    unittest.main()
