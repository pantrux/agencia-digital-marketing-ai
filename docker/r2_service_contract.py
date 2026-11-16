"""Contrato puro del controlador R2; no consulta host, red, bóveda ni Docker."""

import hashlib
import json
import re

OWNER = "agencia-digital-marketing-ai:r2-service:v1"
ROOT = "/var/lib/media-platform/r2-sync-consumer"
MEDIA = "/mnt/producciones"
STATE = ROOT + "/state"
PROJECT = "aceptacion_http_20261106"
MEDIA_MARKER = ".agency-r2-service-root.json"
SECRET_NAMES = ("hmac.key", "r2-access.key", "r2-secret.key")
IMAGE = "sha256:fcb07c34a7cbbefd831d29429b2dd6c1ad881f8310cd174df2b0a9804b0602ef"
IMAGE_CONFIG = "sha256:26889cdc08ab87058f0576dc6aadf9bd0cf9b6afbfeb1cd13016257a0f309206"
IMAGE_RECEIPT = "deb2693be56e1d9b0458bc8bafd38673bd2c5fdd716605d1652714e5d6e597ed"
SOURCE_ROOT = "c0a312232342d92f1f0067606d54d9c0662a95fab8795f6d637a930ee76f91ee"
PROBE_SHA = "3a62a47c7709a3d340c12cffc974c4d0e945b1a90ebfcd50533d730545e882bc"
# Huella pública de firma de Python, heredada de la imagen oficial; no es una credencial.
PUBLIC_PYTHON_SIGNING_FINGERPRINT = "7169605F62C751356D054A26A821E680E5FA6305"
SHA = re.compile(r"[0-9a-f]{64}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
ACTIONS = ("prepare", "create-init", "start-init", "create-serve", "start-serve", "stop")
FIELDS = {
    "schema_version",
    "deployment_id",
    "image_id",
    "image_receipt_sha256",
    "source_root_sha256",
    "account_id",
    "max_object_bytes",
    "media_before",
    "share_evidence_sha256",
    "prepared",
    "init_container_id",
    "service_container_id",
    "init_result",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require(condition):
    if not condition:
        raise ValueError("R2_SERVICE_CONTRACT_REJECTED")


def identity(value):
    require(type(value) is dict and set(value) == {"device", "inode"})
    require(
        type(value["device"]) is int
        and value["device"] >= 0
        and type(value["inode"]) is int
        and value["inode"] > 0
    )


def proof(value, marker=None):
    require(
        type(value) is dict and set(value) == {"device", "inode", "marker_name", "marker_sha256"}
    )
    identity({key: value[key] for key in ("device", "inode")})
    require(
        type(value["marker_name"]) is str
        and re.fullmatch(r"\.[a-z0-9-]+(?:\.json)?", value["marker_name"]) is not None
        and SHA.fullmatch(value["marker_sha256"]) is not None
    )
    require(marker is None or value["marker_name"] == marker)


def core_hash(spec):
    return digest(
        {
            key: value
            for key, value in spec.items()
            if key not in {"prepared", "init_container_id", "service_container_id", "init_result"}
        }
    )


def validate_prepared(value, spec):
    require(
        type(value) is dict
        and set(value)
        == {
            "schema_version",
            "owner",
            "deployment_id",
            "core_sha256",
            "root",
            "state_proof",
            "media_proof",
            "project",
            "raw",
            "files",
            "config_sha256",
        }
    )
    require(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["owner"] == OWNER
        and value["deployment_id"] == spec["deployment_id"]
        and value["core_sha256"] == core_hash(spec)
    )
    for key in ("root", "project", "raw"):
        identity(value[key])
    proof(value["state_proof"], ".owner.json")
    proof(value["media_proof"], MEDIA_MARKER)
    require({key: value["media_proof"][key] for key in ("device", "inode")} == spec["media_before"])
    require(
        SHA.fullmatch(value["config_sha256"]) is not None
        and type(value["files"]) is dict
        and set(value["files"]) == {*SECRET_NAMES, "runtime.json"}
    )
    for row in value["files"].values():
        require(
            type(row) is dict
            and set(row)
            == {"device", "inode", "uid", "gid", "mode", "size", "mtime_ns", "ctime_ns"}
        )
        identity({key: row[key] for key in ("device", "inode")})
        require(
            all(type(row[key]) is int for key in row)
            and row["uid"] == 65532
            and row["gid"] == 65532
            and row["mode"] == 0o400
            and 0 < row["size"] <= 16384
            and row["mtime_ns"] > 0
            and row["ctime_ns"] > 0
        )


def validate_spec(spec):
    require(
        type(spec) is dict
        and set(spec) == FIELDS
        and type(spec["schema_version"]) is int
        and spec["schema_version"] == 1
    )
    require(
        type(spec["deployment_id"]) is str and UUID.fullmatch(spec["deployment_id"]) is not None
    )
    require(
        spec["image_id"] == IMAGE
        and spec["image_receipt_sha256"] == IMAGE_RECEIPT
        and spec["source_root_sha256"] == SOURCE_ROOT
    )
    require(
        type(spec["account_id"]) is str
        and re.fullmatch(r"[0-9a-f]{32}", spec["account_id"]) is not None
    )
    require(
        type(spec["max_object_bytes"]) is int
        and 0 < spec["max_object_bytes"] <= 2**53 - 1
        and SHA.fullmatch(spec["share_evidence_sha256"]) is not None
    )
    identity(spec["media_before"])
    for key in ("init_container_id", "service_container_id"):
        require(
            spec[key] is None or type(spec[key]) is str and SHA.fullmatch(spec[key]) is not None
        )
    if spec["prepared"] is not None:
        validate_prepared(spec["prepared"], spec)
    if spec["init_result"] is not None:
        result = spec["init_result"]
        require(
            type(result) is dict
            and set(result) == {"container_id", "exit_code", "core_sha256"}
            and result["container_id"] == spec["init_container_id"]
            and type(result["exit_code"]) is int
            and result["exit_code"] == 0
            and result["core_sha256"] == core_hash(spec)
        )


def validate_action(spec, action):
    validate_spec(spec)
    require(action in ACTIONS)
    if action != "prepare":
        require(spec["prepared"] is not None)
    if action == "start-init":
        require(spec["init_container_id"] is not None)
    if action in ("create-serve", "start-serve"):
        require(spec["init_result"] is not None)
    if action in ("start-serve", "stop"):
        require(spec["service_container_id"] is not None)


def container_name(spec, role):
    require(role in ("init", "serve"))
    return "media-r2-" + role + "-" + spec["deployment_id"]


def runtime_config(spec, prepared):
    return {
        "schema_version": 1,
        "media_root": "/media",
        "media_proof": prepared["media_proof"],
        "state_root": "/state",
        "state_proof": prepared["state_proof"],
        "account_id": spec["account_id"],
        "max_object_bytes": spec["max_object_bytes"],
        "hmac_secret_file": "/run/secrets/hmac.key",
        "r2_access_key_file": "/run/secrets/r2-access.key",
        "r2_secret_key_file": "/run/secrets/r2-secret.key",
        "listen_host": "0.0.0.0",
        "listen_port": 3010,
    }


def container_config(spec, role):
    validate_spec(spec)
    require(spec["prepared"] is not None and role in ("init", "serve"))
    mounts = [
        {
            "Type": "bind",
            "Source": STATE,
            "Target": "/state",
            "ReadOnly": False,
            "BindOptions": {"Propagation": "rprivate"},
        }
    ]
    if role == "serve":
        mounts.append(
            {
                "Type": "bind",
                "Source": MEDIA,
                "Target": "/media",
                "ReadOnly": False,
                "BindOptions": {"Propagation": "rprivate"},
            }
        )
    for name in ("runtime.json", *SECRET_NAMES):
        mounts.append(
            {
                "Type": "bind",
                "Source": ROOT + "/" + name,
                "Target": "/run/secrets/" + name,
                "ReadOnly": True,
                "BindOptions": {"Propagation": "rprivate"},
            }
        )
    labels = {
        "agency.owner": OWNER,
        "agency.deployment": spec["deployment_id"],
        "agency.role": role,
        "agency.core": core_hash(spec),
        "agency.config": spec["prepared"]["config_sha256"],
        "agency.sources": SOURCE_ROOT,
    }
    entry = (
        ["python", "-I", "-B", "-S", "/app/sync_runtime.py", "init-db"]
        if role == "init"
        else ["python", "-I", "-B", "-S", "/app/sync_http.py", "serve"]
    )
    host = {
        "NetworkMode": "none" if role == "init" else "bridge",
        "IpcMode": "private",
        "CgroupnsMode": "private",
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "CapAdd": [],
        "SecurityOpt": ["no-new-privileges:true"],
        "Privileged": False,
        "Devices": [],
        "NanoCpus": 1000000000,
        "Memory": 805306368,
        "MemorySwap": 805306368,
        "PidsLimit": 64,
        "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16777216,mode=1777"},
        "AutoRemove": False,
        "Mounts": mounts,
        "Binds": [],
        "VolumesFrom": [],
        "PublishAllPorts": False,
        "PortBindings": {}
        if role == "init"
        else {"3010/tcp": [{"HostIp": "192.168.0.85", "HostPort": "3010"}]},
        "RestartPolicy": {
            "Name": "no" if role == "init" else "unless-stopped",
            "MaximumRetryCount": 0,
        },
        "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "2"}},
    }
    return {
        "Image": IMAGE,
        "User": "65532:65532",
        "WorkingDir": "/app",
        "Entrypoint": entry,
        "Cmd": ["--config", "/run/secrets/runtime.json"],
        "Env": [
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "LANG=C.UTF-8",
            "LC_ALL=C.UTF-8",
            "GPG_KEY=" + PUBLIC_PYTHON_SIGNING_FINGERPRINT,
            "PYTHON_VERSION=3.12.16",
            "PYTHON_SHA256=b5796915dc45ff745cfb4e990a7e7c93d6f546328a102ce31448d69ebc37575c",
        ],
        "Labels": labels,
        "Tty": False,
        "OpenStdin": False,
        "StdinOnce": False,
        "ExposedPorts": {} if role == "init" else {"3010/tcp": {}},
        "HostConfig": host,
    }


def mounts_semantics(mounts):
    require(type(mounts) is list)
    result = []
    for mount in mounts:
        require(
            type(mount) is dict
            and set(mount) <= {"Type", "Source", "Target", "ReadOnly", "BindOptions"}
            and {"Type", "Source", "Target"} <= set(mount)
        )
        require(mount["Type"] == "bind" and type(mount.get("ReadOnly", False)) is bool)
        options = mount.get("BindOptions") or {}
        require(type(options) is dict and set(options) <= {"Propagation"})
        require(options.get("Propagation", "rprivate") == "rprivate")
        result.append(
            {
                "Type": "bind",
                "Source": mount["Source"],
                "Target": mount["Target"],
                "ReadOnly": mount.get("ReadOnly", False),
                "BindOptions": {"Propagation": "rprivate"},
            }
        )
    require(len({row["Target"] for row in result}) == len(result))
    return sorted(result, key=lambda row: row["Target"])


def verify_container(spec, role, info):
    desired = container_config(spec, role)
    cid = spec["init_container_id" if role == "init" else "service_container_id"]
    require(
        cid is not None
        and type(info) is dict
        and info.get("Id") == cid
        and info.get("Name") == "/" + container_name(spec, role)
        and info.get("Image") in (IMAGE, IMAGE_CONFIG)
    )
    actual = info.get("Config", {})
    host = info.get("HostConfig", {})
    for key, wanted in desired.items():
        if key == "HostConfig":
            continue
        if key == "Env":
            require(sorted(actual.get(key, [])) == sorted(wanted))
        elif key == "ExposedPorts":
            require((actual.get(key) or {}) == wanted)
        else:
            require(actual.get(key) == wanted)
    for key, wanted in desired["HostConfig"].items():
        got = host.get(key)
        if key in ("Binds", "VolumesFrom", "CapAdd", "Devices"):
            got = got or []
        if key == "PortBindings":
            got = got or {}
        if key in ("Mounts",):
            require(mounts_semantics(got) == mounts_semantics(wanted))
        else:
            require(got == wanted)
    require(
        not host.get("PidMode")
        and not host.get("UTSMode")
        and not host.get("UsernsMode")
        and not host.get("ExtraHosts")
        and not host.get("DeviceRequests")
        and not host.get("DeviceCgroupRules")
        and not host.get("GroupAdd")
        and not host.get("Dns")
        and not host.get("DnsOptions")
        and not host.get("DnsSearch")
        and not host.get("OomKillDisable")
        and not host.get("CpuQuota")
        and not host.get("CpuPeriod")
        and not host.get("CpusetCpus")
        and not host.get("CpusetMems")
        and not host.get("Links")
    )
    actual_mounts = info.get("Mounts", [])
    require(type(actual_mounts) is list and all(type(m.get("RW")) is bool for m in actual_mounts))
    projected = [
        {
            "Type": m.get("Type"),
            "Source": m.get("Source"),
            "Target": m.get("Destination"),
            "ReadOnly": not m.get("RW"),
            "BindOptions": {"Propagation": m.get("Propagation")},
        }
        for m in actual_mounts
    ]
    require(
        sorted(projected, key=lambda m: m["Target"])
        == sorted(desired["HostConfig"]["Mounts"], key=lambda m: m["Target"])
    )
    if role == "serve" and info.get("State", {}).get("Running") is True:
        require(
            info.get("NetworkSettings", {}).get("Ports") == desired["HostConfig"]["PortBindings"]
        )


def validate_observation(value):
    require(
        type(value) is dict
        and set(value)
        == {"controller_sha256", "remote_sha256", "contract_sha256", "layout_sha256", "layout"}
    )
    for key in ("controller_sha256", "remote_sha256", "contract_sha256", "layout_sha256"):
        require(type(value[key]) is str and SHA.fullmatch(value[key]) is not None)
    layout = value["layout"]
    require(
        type(layout) is dict
        and layout.get("owner") == "agencia-digital-marketing-ai:media-platform:v4.1"
        and layout.get("success") is True
    )
    rows = layout.get("targets")
    require(
        type(rows) is list
        and len(rows) == 2
        and {row.get("path") for row in rows} == {"/opt/media-platform", "/var/lib/media-platform"}
    )
    for row in rows:
        identity(row["directory"])
        require(row.get("role") == ("code" if row["path"] == "/opt/media-platform" else "state"))
        for key in ("marker_sha256", "receipt_sha256"):
            require(type(row.get(key)) is str and SHA.fullmatch(row[key]) is not None)


def make_plan(spec, action, observation):
    validate_action(spec, action)
    validate_observation(observation)
    plan = {
        "schema_version": 1,
        "owner": OWNER,
        "action": action,
        "spec": spec,
        "observation": observation,
    }
    plan["plan_sha256"] = plan_hash(plan)
    return plan


def plan_hash(plan):
    return digest({key: value for key, value in plan.items() if key != "plan_sha256"})


def verify_plan(plan, approved):
    require(
        type(plan) is dict
        and set(plan) == {"schema_version", "owner", "action", "spec", "observation", "plan_sha256"}
        and type(plan["schema_version"]) is int
        and plan["schema_version"] == 1
        and plan["owner"] == OWNER
    )
    validate_action(plan["spec"], plan["action"])
    validate_observation(plan["observation"])
    require(
        type(approved) is str
        and SHA.fullmatch(approved) is not None
        and plan["plan_sha256"] == approved == plan_hash(plan)
    )
