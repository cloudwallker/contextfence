#!/usr/bin/env python3
"""Create local demo credentials once, without printing or rotating them."""

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path


DEMO_PRINCIPALS = (
    ("acme", "writer", ("SOURCE_WRITER",)),
    ("acme", "alice", ("READER", "PRODUCER")),
    ("acme", "bob", ("READER",)),
    ("beta", "alice", ("READER", "PRODUCER")),
)


class ConfigurationError(Exception):
    """A fixed, credential-free error suitable for command-line output."""


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("Existing identities configuration is invalid; duplicate JSON keys.")
        result[key] = value
    return result


def validate_existing(env_path, identities_path):
    environment = {}
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" in line:
            key, value = line.split("=", 1)
            environment[key.strip()] = value.strip().strip("\"'")
    password = environment.get("POSTGRES_PASSWORD", "")
    if len(password) < 32 or password.lower().startswith(("replace", "changeme", "placeholder")):
        raise ConfigurationError("Existing .env is invalid; set a non-placeholder POSTGRES_PASSWORD of at least 32 characters. No credentials were replaced.")

    try:
        document = json.loads(identities_path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_json_object)
        if not isinstance(document, dict) or set(document) != {"principals"}:
            raise ValueError()
        principals = document["principals"]
        if not isinstance(principals, list) or not principals:
            raise ValueError()
        tokens = set()
        identities = {}
        for principal in principals:
            if not isinstance(principal, dict) or set(principal) != {"token", "tenant", "subject", "roles"}:
                raise ValueError()
            token, tenant, subject, roles = (principal[k] for k in ("token", "tenant", "subject", "roles"))
            if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token) or token in tokens:
                raise ValueError()
            if not isinstance(tenant, str) or not tenant or not isinstance(subject, str) or not subject:
                raise ValueError()
            if not isinstance(roles, list) or not roles or not all(isinstance(r, str) for r in roles):
                raise ValueError()
            if len(roles) != len(set(roles)) or not set(roles) <= {"READER", "PRODUCER", "SOURCE_WRITER"}:
                raise ValueError()
            if (tenant, subject) in identities:
                raise ValueError()
            tokens.add(token)
            identities[tenant, subject] = set(roles)
        if any(identities.get((tenant, subject)) != set(roles) for tenant, subject, roles in DEMO_PRINCIPALS):
            raise ValueError()
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ConfigurationError("Existing identities configuration is invalid or incomplete. No credentials were replaced.") from None


def write_new_private_file(path, content, mode=0o600):
    # O_EXCL avoids replacing another initializer's or the user's credentials.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
        file.write(content)
    # Make this explicit after creation: umask must not hide the bind-mounted
    # file from the runtime UID. Its enclosing host directory stays private.
    path.chmod(mode)


def bootstrap(root):
    root = root.resolve()
    env_path = root / ".env"
    identities_path = root / ".local" / "identities.json"
    env_exists, identities_exist = env_path.exists(), identities_path.exists()
    if env_exists != identities_exist:
        raise ConfigurationError("Local configuration is incomplete: .env and .local/identities.json must exist together. Existing files were preserved; repair the missing configuration explicitly.")
    if env_exists:
        validate_existing(env_path, identities_path)
        return "Existing local credentials and configuration were preserved."

    root.mkdir(parents=True, exist_ok=True)
    identities_path.parent.mkdir(parents=True, exist_ok=True)
    # The host directory protects tokens; the individual bind-mounted file must
    # remain readable by the container's unrelated, non-root UID.
    identities_path.parent.chmod(0o700)
    environment = (
        "# Generated local credentials. Do not commit this file.\n"
        "POSTGRES_USER=contextfence\nPOSTGRES_DB=contextfence\n"
        f"POSTGRES_PASSWORD={secrets.token_hex(32)}\n"
        "PG_PORT=55448\nAPI_A_PORT=58091\nAPI_B_PORT=58092\n"
    )
    identities = {"principals": [
        {"token": secrets.token_hex(32), "tenant": tenant, "subject": subject, "roles": list(roles)}
        for tenant, subject, roles in DEMO_PRINCIPALS
    ]}
    write_new_private_file(env_path, environment)
    write_new_private_file(identities_path, json.dumps(identities, indent=2) + "\n", mode=0o644)
    return "Initialized .env and .local/identities.json. Credentials were saved locally and were not printed."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="Project directory (defaults to this script's project).")
    arguments = parser.parse_args()
    try:
        print(bootstrap(arguments.root))
        return 0
    except ConfigurationError as error:
        print(f"Bootstrap failed: {error}", file=sys.stderr)
    except (OSError, UnicodeError):
        print("Bootstrap failed: local configuration could not be read or created. Existing credentials were not replaced; check permissions and repair any incomplete configuration explicitly.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
