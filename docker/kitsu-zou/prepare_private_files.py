"""Contrato puro de archivos Kitsu; no lee bóveda, genera claves ni abre red."""

import base64
import hashlib
import json
import re
import sys

OWNER = "agencia-digital-marketing-ai:kitsu-base-files:v1"
TARGETS = {
    "config": "/opt/media-platform/kitsu-base-v1",
    "data": "/var/lib/media-platform/kitsu-base-v1",
}
MARKER = ".agency-kitsu-files-owner.json"
LAYOUT_SHA = "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9"
MAX_REQUEST = 256 * 1024
FILE_POLICY = {
    name: {
        "uid": 70 if name in {"postgres_password", "postgres_pgpass"} else 1000,
        "gid": 70 if name in {"postgres_password", "postgres_pgpass"} else 1000,
        "mode": "0400",
        "path": "valkey.conf" if name == "valkey.conf" else "secrets/" + name,
    }
    for name in (
        "postgres_password",
        "postgres_pgpass",
        "valkey_password",
        "zou_db_password",
        "zou_kv_password",
        "zou_secret_key",
        "zou_admin_password",
        "valkey.conf",
    )
}
DATA_DIRS = {
    "postgres-apps": 70,
    "valkey": 1000,
    "zou": 1000,
    "zou/previews": 1000,
    "zou/tmp": 1000,
}
IDENTIFIERS = {
    "postgres_admin_user",
    "postgres_admin_database",
    "zou_database_user",
    "zou_database_name",
}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_REQUEST, "JSON_SIZE")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "JSON_DUPLICATE")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=unique,
            parse_constant=lambda _: require(False, "JSON_CONSTANT"),
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("JSON_FORMAT") from error


def make_plan(identifiers, files):
    result = {
        "schema": 1,
        "owner": OWNER,
        "targets": dict(TARGETS),
        "identifiers": identifiers,
        "files": files,
        "runtime_verified": False,
        "production_approved": False,
        "secrets_in_plan": False,
    }
    validate_plan(result)
    return decode(canonical(result))


def validate_plan(plan):
    require(
        type(plan) is dict
        and set(plan)
        == {
            "schema",
            "owner",
            "targets",
            "identifiers",
            "files",
            "runtime_verified",
            "production_approved",
            "secrets_in_plan",
        },
        "PLAN_FIELDS",
    )
    require(
        type(plan["schema"]) is int
        and plan["schema"] == 1
        and plan["owner"] == OWNER
        and plan["targets"] == TARGETS
        and plan["runtime_verified"] is False
        and plan["production_approved"] is False
        and plan["secrets_in_plan"] is False,
        "PLAN_SCOPE",
    )
    ids = plan["identifiers"]
    require(type(ids) is dict and set(ids) == IDENTIFIERS, "IDENTIFIERS")
    require(
        all(type(v) is str and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", v) for v in ids.values()),
        "IDENTIFIER_FORMAT",
    )
    require(ids["postgres_admin_user"] != ids["zou_database_user"], "ADMIN_ROLE_REUSED")
    require(type(plan["files"]) is dict and set(plan["files"]) == set(FILE_POLICY), "FILE_SET")
    for name, policy in FILE_POLICY.items():
        row = plan["files"][name]
        require(type(row) is dict and set(row) == {"sha256", "bytes", *policy}, "FILE_FIELDS")
        require(
            all(type(row[k]) is type(v) and row[k] == v for k, v in policy.items()), "FILE_POLICY"
        )
        require(
            type(row["bytes"]) is int
            and 0
            < row["bytes"]
            <= (8192 if name == "valkey.conf" else 16384 if name == "postgres_pgpass" else 4096)
            and type(row["sha256"]) is str
            and re.fullmatch(r"[a-f0-9]{64}", row["sha256"]),
            "FILE_IDENTITY",
        )
    return plan


def secret_value(raw):
    require(type(raw) is bytes and 12 <= len(raw) <= 4096, "SECRET_SIZE")
    try:
        value = raw.decode("utf-8", "strict").removesuffix("\n")
    except UnicodeError as error:
        raise ValueError("SECRET_ENCODING") from error
    require(
        len(value) >= 12 and not any(ord(c) < 32 or ord(c) == 127 for c in value), "SECRET_CONTROL"
    )
    require(
        not re.search(
            r"CHANGE_ME|REPLACE_ME|PENDIENTE|POR_DEFINIR|COMPLETAR|GENERAR",
            value,
            flags=re.IGNORECASE,
        ),
        "SECRET_PENDING",
    )
    return value


def pgpass(identifiers, password):
    def escaped(value):
        return value.replace("\\", "\\\\").replace(":", "\\:")

    return (
        "127.0.0.1:5432:"
        + escaped(identifiers["postgres_admin_database"])
        + ":"
        + escaped(identifiers["postgres_admin_user"])
        + ":"
        + escaped(password)
        + "\n"
    ).encode()


def valkey_config(password):
    quoted = password.replace("\\", "\\\\").replace('"', '\\"')
    return (
        'bind 0.0.0.0\nprotected-mode yes\nport 6379\ndaemonize no\nlogfile ""\n'
        "pidfile /tmp/valkey.pid\ndir /data\nappendonly yes\nappendfsync everysec\n"
        'maxmemory 128mb\nmaxmemory-policy noeviction\nrequirepass "' + quoted + '"\n'
    ).encode()


def validate_bundle(plan, files):
    validate_plan(plan)
    require(type(files) is dict and set(files) == set(FILE_POLICY), "PAYLOAD_SET")
    for name, raw in files.items():
        require(
            type(raw) is bytes
            and len(raw) == plan["files"][name]["bytes"]
            and digest(raw) == plan["files"][name]["sha256"],
            "PAYLOAD_HASH",
        )
    secrets = {
        name: secret_value(raw)
        for name, raw in files.items()
        if name not in {"postgres_pgpass", "valkey.conf"}
    }
    require(files["valkey_password"] == files["zou_kv_password"], "VALKEY_PASSWORD_MISMATCH")
    require(secrets["postgres_password"] != secrets["zou_db_password"], "ADMIN_PASSWORD_REUSED")
    require(
        files["postgres_pgpass"] == pgpass(plan["identifiers"], secrets["postgres_password"]),
        "PGPASS_MISMATCH",
    )
    require(files["valkey.conf"] == valkey_config(secrets["valkey_password"]), "VALKEY_CONFIG")
    return files


def decode_files(plan, encoded):
    require(type(encoded) is dict and set(encoded) == set(FILE_POLICY), "PAYLOAD_SET")
    require(
        all(type(value) is str and len(value) <= 24576 for value in encoded.values()),
        "PAYLOAD_ENCODING",
    )
    try:
        files = {name: base64.b64decode(value, validate=True) for name, value in encoded.items()}
    except ValueError as error:
        raise ValueError("PAYLOAD_ENCODING") from error
    return validate_bundle(plan, files)


def main():
    # La entrada predeterminada contiene sólo el plan público, no valores secretos.
    try:
        plan = decode(sys.stdin.buffer.read(MAX_REQUEST + 1))
        validate_plan(plan)
        print(
            canonical(
                {
                    "mode": "plan",
                    "plan_sha256": digest(canonical(plan)),
                    "targets": TARGETS,
                    "files": plan["files"],
                    "runtime_verified": False,
                    "production_approved": False,
                }
            ).decode()
        )
        return 0
    except (ValueError, TypeError, KeyError, RecursionError):
        print('{"success":false,"error":"PLAN_REJECTED","production_approved":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
