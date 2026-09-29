"""Exercise Windows entrypoints with a controlled Docker CLI boundary."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Requires Windows PowerShell")
class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "project with spaces"
        self.root.mkdir()
        (self.root / "compose.yaml").write_text("name: contextfence\nservices: {}\n")
        self.fake_bin = Path(self.directory.name) / "fake-bin"
        self.fake_bin.mkdir()
        self.record = Path(self.directory.name) / "docker-calls.jsonl"
        docker_double = self.fake_bin / "docker-double.py"
        docker_double.write_text(
            "import json, os, sys\n"
            "with open(os.environ['DOCKER_CALL_RECORD'], 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if 'up' in sys.argv or 'down' in sys.argv:\n"
            "    code = int(os.environ.get('DOCKER_MUTATION_EXIT', '0'))\n"
            "    if code: print('controlled Docker failure', file=sys.stderr)\n"
            "    sys.exit(code)\n"
        )
        (self.fake_bin / "docker.cmd").write_text(f'@"{sys.executable}" "{docker_double}" %*\n')
        self.environment = dict(os.environ)
        self.environment["PATH"] = str(self.fake_bin) + os.pathsep + self.environment.get("PATH", "")
        self.environment["DOCKER_CALL_RECORD"] = str(self.record)

    def invoke(self, script, *arguments):
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(SCRIPTS / script), "-Root", str(self.root), *arguments],
            env=self.environment, capture_output=True, text=True, errors="replace", timeout=30,
        )

    def calls(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.exists() else []

    def test_start_initializes_then_scopes_up_to_project_and_waits_for_health(self):
        result = self.invoke("start.ps1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.root / ".env").is_file())
        self.assertTrue((self.root / ".local/identities.json").is_file())
        mutations = [call for call in self.calls() if "up" in call or "down" in call]
        self.assertEqual(1, len(mutations))
        call = mutations[0]
        self.assertEqual("contextfence", call[call.index("--project-name") + 1])
        self.assertEqual(str(self.root / "compose.yaml"), call[call.index("--file") + 1])
        self.assertEqual(str(self.root / ".env"), call[call.index("--env-file") + 1])
        self.assertIn("--wait", call)
        self.assertIn("--build", call)
        self.assertNotIn("down", call)

    def test_start_preserves_docker_failure_and_exits_nonzero(self):
        self.environment["DOCKER_MUTATION_EXIT"] = "7"
        result = self.invoke("start.ps1")
        self.assertEqual(7, result.returncode)
        self.assertIn("controlled Docker failure", result.stderr)
        self.assertFalse(any("down" in call for call in self.calls()))

    def test_incomplete_config_prevents_any_docker_up(self):
        (self.root / ".env").write_text("POSTGRES_PASSWORD=existing-secret-marker\n")
        result = self.invoke("start.ps1")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("incomplete", result.stderr.lower())
        self.assertFalse(any("up" in call for call in self.calls()))
        self.assertNotIn("existing-secret-marker", result.stdout + result.stderr)

    def test_stop_scopes_down_without_deleting_volume_or_credentials(self):
        (self.root / ".env").write_text("POSTGRES_PASSWORD=existing-secret-marker\n")
        result = self.invoke("stop.ps1")
        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.calls()
        self.assertEqual(1, len(calls))
        self.assertEqual("contextfence", calls[0][calls[0].index("--project-name") + 1])
        self.assertIn("down", calls[0])
        self.assertNotIn("--volumes", calls[0])
        self.assertNotIn("-v", calls[0])
        self.assertTrue((self.root / ".env").is_file())

    def test_maven_uses_java21_and_explicit_settings_and_preserves_exit(self):
        java_home = Path(self.directory.name) / "jdk21"
        (java_home / "bin").mkdir(parents=True)
        (java_home / "release").write_text('JAVA_VERSION="21.0.1"\n')
        (java_home / "bin/java.exe").touch()
        (java_home / "bin/javac.exe").touch()
        self.environment["JAVA_HOME"] = str(java_home)
        settings = self.root / "settings.xml"
        settings.write_text("<settings/>\n")
        maven_double = self.fake_bin / "maven-double.py"
        maven_double.write_text("import json, os, sys\nprint(json.dumps({'java':os.environ['JAVA_HOME'], 'args':sys.argv[1:]}))\nsys.exit(6)\n")
        (self.fake_bin / "mvn.cmd").write_text(f'@"{sys.executable}" "{maven_double}" %*\n')
        result = self.invoke("maven.ps1", "-Settings", str(settings), "-MavenArgs", "verify")
        self.assertEqual(6, result.returncode, result.stderr)
        record = json.loads(result.stdout.strip())
        self.assertEqual(str(java_home), record["java"])
        self.assertIn("verify", record["args"])
        self.assertEqual(str(settings), record["args"][record["args"].index("--settings") + 1])

    def prepare_maven(self):
        java_home = Path(self.directory.name) / "jdk21"
        (java_home / "bin").mkdir(parents=True)
        (java_home / "release").write_text('JAVA_VERSION="21.0.1"\n')
        (java_home / "bin/java.exe").touch()
        (java_home / "bin/javac.exe").touch()
        self.environment["JAVA_HOME"] = str(java_home)
        maven_double = self.fake_bin / "maven-double.py"
        maven_double.write_text("import json, os, sys\nprint(json.dumps({'java':os.environ['JAVA_HOME'], 'args':sys.argv[1:]}))\n")
        (self.fake_bin / "mvn.cmd").write_text(f'@"{sys.executable}" "{maven_double}" %*\n')
        return java_home

    def test_maven_positional_goal_is_not_treated_as_project_directory(self):
        self.prepare_maven()
        result = self.invoke("maven.ps1", "-NoSystemProxy", "verify")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("verify", json.loads(result.stdout.strip())["args"])

    def test_maven_automatically_records_uncredentialed_proxy_only_in_local_settings(self):
        self.prepare_maven()
        self.environment["HTTPS_PROXY"] = "http://proxy.example.invalid:8123"
        result = self.invoke("maven.ps1", "-MavenArgs", "verify")
        self.assertEqual(0, result.returncode, result.stderr)
        arguments = json.loads(result.stdout.strip())["args"]
        settings_path = Path(arguments[arguments.index("--settings") + 1])
        self.assertEqual(self.root / ".local/maven-settings.auto.xml", settings_path)
        settings = ET.parse(settings_path)
        namespace = {"m": "http://maven.apache.org/SETTINGS/1.2.0"}
        self.assertEqual("proxy.example.invalid", settings.findtext(".//m:host", namespaces=namespace))
        self.assertEqual("8123", settings.findtext(".//m:port", namespaces=namespace))
        self.assertIsNone(settings.find(".//m:password", namespaces=namespace))

    def test_maven_authenticated_proxy_requires_explicit_settings_without_echoing_secret(self):
        self.prepare_maven()
        self.environment["HTTPS_PROXY"] = "http://user:private-password-marker@proxy.example.invalid:8123"
        result = self.invoke("maven.ps1", "-MavenArgs", "verify")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("authenticated proxy", result.stderr.lower())
        self.assertNotIn("private-password-marker", result.stdout + result.stderr)
        self.assertFalse((self.root / ".local/maven-settings.auto.xml").exists())


if __name__ == "__main__":
    unittest.main()
