"""Run the real initializer against disposable project directories."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "bootstrap.py"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "项目 fixture"
        self.root.mkdir()

    def run_bootstrap(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20,
        )

    def read_credentials(self):
        environment = dict(
            line.split("=", 1) for line in (self.root / ".env").read_text().splitlines()
            if line and not line.startswith("#")
        )
        identities = json.loads((self.root / ".local/identities.json").read_text())
        return environment, identities

    def assert_no_credentials(self, result, environment, identities):
        output = result.stdout + result.stderr
        self.assertNotIn(environment["POSTGRES_PASSWORD"], output)
        for principal in identities["principals"]:
            self.assertNotIn(principal["token"], output)

    def test_generates_strong_distinct_credentials_with_exact_principal_shape(self):
        result = self.run_bootstrap()
        self.assertEqual(0, result.returncode, result.stderr)
        environment, identities = self.read_credentials()
        self.assertRegex(environment["POSTGRES_PASSWORD"], r"^[0-9a-f]{64}$")
        self.assertEqual({"principals"}, set(identities))
        self.assertEqual(
            [("acme", "writer", ["SOURCE_WRITER"]),
             ("acme", "alice", ["READER", "PRODUCER"]),
             ("acme", "bob", ["READER"]),
             ("beta", "alice", ["READER", "PRODUCER"])],
            [(p["tenant"], p["subject"], p["roles"]) for p in identities["principals"]],
        )
        tokens = []
        for principal in identities["principals"]:
            self.assertEqual({"token", "tenant", "subject", "roles"}, set(principal))
            self.assertRegex(principal["token"], r"^[0-9a-f]{64}$")
            tokens.append(principal["token"])
        self.assertEqual(4, len(set(tokens)))
        self.assertNotIn(environment["POSTGRES_PASSWORD"], tokens)
        self.assert_no_credentials(result, environment, identities)

    def test_repeated_initialization_preserves_existing_bytes_and_credentials(self):
        self.assertEqual(0, self.run_bootstrap().returncode)
        env_path = self.root / ".env"
        env_path.write_text(env_path.read_text() + "# user-owned comment\nAPI_A_PORT=58101\n")
        initial_env = env_path.read_bytes()
        identities_path = self.root / ".local/identities.json"
        initial_identities = identities_path.read_bytes()
        result = self.run_bootstrap()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(initial_env, env_path.read_bytes())
        self.assertEqual(initial_identities, identities_path.read_bytes())
        self.assert_no_credentials(result, *self.read_credentials())

    def test_env_without_identities_fails_and_preserves_password(self):
        env_path = self.root / ".env"
        original = b"POSTGRES_PASSWORD=private-marker-value\n"
        env_path.write_bytes(original)
        result = self.run_bootstrap()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("incomplete", result.stderr.lower())
        self.assertEqual(original, env_path.read_bytes())
        self.assertFalse((self.root / ".local/identities.json").exists())
        self.assertNotIn("private-marker-value", result.stdout + result.stderr)

    def test_identities_without_env_fails_without_replacement(self):
        path = self.root / ".local/identities.json"
        path.parent.mkdir()
        original = '{"principals":[{"token":"private-token-marker"}]}'.encode()
        path.write_bytes(original)
        result = self.run_bootstrap()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("incomplete", result.stderr.lower())
        self.assertEqual(original, path.read_bytes())
        self.assertFalse((self.root / ".env").exists())
        self.assertNotIn("private-token-marker", result.stdout + result.stderr)

    def test_invalid_password_fails_without_resetting_any_file(self):
        self.assertEqual(0, self.run_bootstrap().returncode)
        env_path = self.root / ".env"
        env_path.write_text("POSTGRES_PASSWORD=\n", encoding="utf-8")
        original = (self.root / ".local/identities.json").read_bytes()
        result = self.run_bootstrap()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("invalid", result.stderr.lower())
        self.assertEqual(original, (self.root / ".local/identities.json").read_bytes())
        self.assertEqual("POSTGRES_PASSWORD=\n", env_path.read_text())

    def test_duplicate_json_keys_fail_without_echoing_token_or_resetting(self):
        self.assertEqual(0, self.run_bootstrap().returncode)
        environment, identities = self.read_credentials()
        path = self.root / ".local/identities.json"
        token = identities["principals"][0]["token"]
        original = ('{"principals": [], "principals": [{"token": "' + token + '"}]}').encode()
        path.write_bytes(original)
        result = self.run_bootstrap()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("invalid", result.stderr.lower())
        self.assertEqual(original, path.read_bytes())
        self.assert_no_credentials(result, environment, identities)

    def test_missing_demo_principal_is_invalid_and_never_recreated(self):
        self.assertEqual(0, self.run_bootstrap().returncode)
        environment, identities = self.read_credentials()
        identities["principals"].pop()
        path = self.root / ".local/identities.json"
        path.write_text(json.dumps(identities), encoding="utf-8")
        original = path.read_bytes()
        result = self.run_bootstrap()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("invalid", result.stderr.lower())
        self.assertEqual(original, path.read_bytes())
        self.assert_no_credentials(result, environment, identities)

    def test_independent_roots_get_independent_secrets(self):
        self.assertEqual(0, self.run_bootstrap().returncode)
        first_environment, first_identities = self.read_credentials()
        self.root = Path(self.directory.name) / "second-project"
        self.root.mkdir()
        self.assertEqual(0, self.run_bootstrap().returncode)
        second_environment, second_identities = self.read_credentials()
        self.assertNotEqual(first_environment["POSTGRES_PASSWORD"], second_environment["POSTGRES_PASSWORD"])
        first_tokens = {p["token"] for p in first_identities["principals"]}
        second_tokens = {p["token"] for p in second_identities["principals"]}
        self.assertFalse(first_tokens & second_tokens)

    @unittest.skipUnless(os.name == "posix", "Requires POSIX file permission semantics")
    def test_private_host_directory_and_container_readable_identity_file_even_with_strict_umask(self):
        # A restrictive host umask must not make the non-root bind mount unreadable.
        original_umask = os.umask(0o077)
        try:
            result = self.run_bootstrap()
        finally:
            os.umask(original_umask)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(0o700, (self.root / ".local").stat().st_mode & 0o777)
        self.assertEqual(0o600, (self.root / ".env").stat().st_mode & 0o777)
        self.assertEqual(0o644, (self.root / ".local/identities.json").stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
