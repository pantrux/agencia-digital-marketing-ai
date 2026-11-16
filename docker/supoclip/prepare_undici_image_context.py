"""Prepara el contexto mínimo de imagen SupoClip con los bundles Undici corregidos."""

import argparse
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
EVIDENCE = Path(".tools/evidence")
STAGING = Path(".tools/backup-source-build/supoclip-next-undici-image-v1")
LOCAL = Path(".tools/backup-source-build/supoclip-next-undici-local-v1")
BASELINE = "sha256:aeee7514bdf53e44f8241ce884533025d266c5c8f9d0c4956e1809080e26df28"
APP_LOCK = "d3d98cbe5f080c30a77cc16e7f6e128b2d3d52cb08a9ffa609f80b7ffb8bc2fe"
NEXT_ROOT = "node_modules/.pnpm/next@15.5.28_100467c9c5fc6755f21de0db9ea5f0ff/node_modules/next"
TARGETS = {
    "edge-runtime.index.js": NEXT_ROOT + "/dist/compiled/edge-runtime/index.js",
    "primitives.load.js": NEXT_ROOT + "/dist/compiled/@edge-runtime/primitives/load.js",
}
PREIMAGES = {
    TARGETS["edge-runtime.index.js"]: {
        "bytes": 525805,
        "sha256": "06703ed6b001b0f02f8043bb499f3716f2c3b76b7df628a1cea30d1ee671edb2",
    },
    TARGETS["primitives.load.js"]: {
        "bytes": 800754,
        "sha256": "d36b91e07ff16e770688afc5578702030e7eccaa3de59303cd6b0cef0707d230",
    },
}
PRODUCTS = {
    "edge-runtime.index.js": {
        "bytes": 548406,
        "sha256": "73740061b29896ff70fc31fc02e8cabad14710a4839a0dc4398333a3a02957dc",
    },
    "primitives.load.js": {
        "bytes": 848282,
        "sha256": "eda5210b76e9afb52c29292fc99069258a1e1a93959c373054a13b3d7f040594",
    },
}
DOCKERFILE_SHA = "e916ef3132b8911c916b87e0f32145a6496836f421cf20154e775a72c0f9b566"
ASSEMBLE_SHA = "938ed20ffc80756f4998ee625945f508079d2fb51e1a252ad7b4ef70c1ee6f95"
UNDICI_TAR = Path(".tools/backup-source-build/immich-sharp-builder-v1/npm/undici-6.29.1.tgz")
UNDICI_TAR_SPEC = {
    "bytes": 297796,
    "sha256": "29c4b8e0b2d86cb8795571c1959012ce057a14ecfd34858335f47c31e770a04f",
}
UNDICI_LICENSE = {
    "bytes": 1090,
    "sha256": "a6db8096b2707bc0102d256917d4d33f298ba36d8c3f25de067a2b5bb379db27",
}
LICENSE_PATH = "/usr/share/licenses/agency-undici-6.29.1/LICENSE"
CONTEXT_MEMBERS = frozenset(
    {"Dockerfile.frontend", "assemble_frontend.sh", "UNDICI-LICENSE", *PRODUCTS}
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def row(raw):
    return {"bytes": len(raw), "sha256": sha(raw)}


def canon(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def dockerfile(original):
    if sha(original) != DOCKERFILE_SHA:
        raise ValueError("dockerfile_preimage")
    before = b"COPY --from=standalone --chown=0:0 / /opt/supoclip/frontend/\n"
    if original.count(before) != 1:
        raise ValueError("dockerfile_copy")
    stage = [b"FROM scratch AS projected\n", b"COPY --from=standalone --chown=0:0 / /\n"]
    stage += [
        f"COPY --chown=0:0 --chmod=0444 {name} /{target}\n".encode()
        for name, target in TARGETS.items()
    ]
    merged = before.replace(b"--from=standalone", b"--from=projected")
    license_copy = f"COPY --chown=0:0 --chmod=0444 UNDICI-LICENSE {LICENSE_PATH}\n".encode()
    return b"".join(stage) + b"\n" + original.replace(before, merged + license_copy)


def project_manifest(baseline):
    if baseline["lock"] != APP_LOCK:
        raise ValueError("app_lock_preimage")
    files = dict(baseline["files"])
    if files.keys() & baseline["symlinks"].keys():
        raise ValueError("app_manifest_overlap")
    if any(files.get(target) != old for target, old in PREIMAGES.items()):
        raise ValueError("bundle_preimage")
    for name, target in TARGETS.items():
        files[target] = PRODUCTS[name]
    return {**baseline, "files": files}


def license_from(package):
    if row(package) != UNDICI_TAR_SPEC:
        raise ValueError("undici_tar")
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        (member,) = [item for item in archive if item.name == "package/LICENSE"]
        if not member.isreg() or member.size != UNDICI_LICENSE["bytes"]:
            raise ValueError("undici_license_member")
        with archive.extractfile(member) as stream:
            content = stream.read()
    if row(content) != UNDICI_LICENSE:
        raise ValueError("undici_license_digest")
    return content


def context_tar(members):
    if set(members) != CONTEXT_MEMBERS:
        raise ValueError("context_members")
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in sorted(members.items()):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o444
            archive.addfile(member, io.BytesIO(content))
    return out.getvalue()


def read_pin(relative, expected, cap):
    path = WORKSPACE / relative
    if path.resolve(strict=True) != path or not path.is_file() or path.stat().st_size > cap:
        raise ValueError("input_path_or_size")
    raw = path.read_bytes()
    if sha(raw) != expected:
        raise ValueError("input_digest")
    return raw


def pinned_json(relative, expected):
    raw = read_pin(relative, expected, 2 * 1024**2)
    return json.loads(raw), {"path": relative.as_posix(), **row(raw)}


def prepare():
    base, build_ref = pinned_json(
        EVIDENCE / "phase4-supoclip-frontend-runtime-build-v2.json",
        "bdc66383ea66915e5a91af2defec13d1cf18f563a447d4828346e077f34d86ba",
    )
    manifest, manifest_ref = pinned_json(
        EVIDENCE / "phase4-supoclip-frontend-standalone-manifest-v3.json",
        "c9a7b71e46109c066bc95e3ae8ab3528c56236b4ee783fc6cfb7555f79e65762",
    )
    trial, trial_ref = pinned_json(
        EVIDENCE / "phase4-supoclip-frontend-undici-trial-start-test-v1.json",
        "62c10af71bc0a6020ed26cecd42304eefb18da857cb435ea80022028730f4776",
    )
    overlay, overlay_ref = pinned_json(
        EVIDENCE / "phase4-supoclip-next-undici-overlay-v1.json",
        "8ac27bf50fa1c018e439d07ea5c033883b3fc42bd5093f0d4603d7d456110cb4",
    )
    source, source_ref = pinned_json(
        STAGING / "source-proof/receipt.json",
        "1770b379b3fe2a826e97a158a4d55ca7057fbab3daed8d3ad234daa7f43e7621",
    )
    attr, attr_ref = pinned_json(
        EVIDENCE / "phase4-supoclip-next-attribution-v1.json",
        "1c864ce79ffa9a0c9eb7750f9c193d32b9ddb9b3b8c8dc8a00f486bc1daf5d51",
    )
    node, node_ref = pinned_json(
        EVIDENCE / "phase4-supoclip-frontend-node-inventory-v1.json",
        "7a4fc7d5448bcdd53631c942e1905ee8f0b2a0ba29320139eb00bde902c046e6",
    )
    if (
        base["image"] != BASELINE
        or overlay["image_id"] != BASELINE
        or not (
            base["success"]
            and manifest["success"]
            and trial["success"]
            and trial["stopped"]
            and trial["pid"] == 0
            and trial["mounted_products_verified"]
            and trial["loopback_listener"]
            and trial["exit"] == 0
            and source["next_version"] == overlay["next_version"] == "15.5.28"
            and source["next_15_5_29_tasks_identical"]
            and source["ncc_closure_14_sri_and_snapshots_identical"]
        )
    ):
        raise ValueError("evidence_inconsistent")
    swc = Path(
        ".tools/backup-source-build/supoclip-frontend-npm-v1/tarballs/@next/swc-linux-x64-gnu/15.5.28.tgz"
    )
    swc_bytes = read_pin(
        swc, "6278c283a1097f72f1ad1a1972db14cb9a2af7d7437069ab1721af4b501e0d83", 64 * 1024**2
    )
    original = read_pin(Path("docker/supoclip/Dockerfile.frontend"), DOCKERFILE_SHA, 8192)
    assembly = read_pin(Path("docker/supoclip/assemble_frontend.sh"), ASSEMBLE_SHA, 8192)
    license_bytes = license_from(read_pin(UNDICI_TAR, UNDICI_TAR_SPEC["sha256"], 400000))
    members = {
        "Dockerfile.frontend": dockerfile(original),
        "assemble_frontend.sh": assembly,
        "UNDICI-LICENSE": license_bytes,
    }
    for name, spec in PRODUCTS.items():
        members[name] = read_pin(LOCAL / "overlay-v1" / name, spec["sha256"], spec["bytes"])
        if row(members[name]) != spec:
            raise ValueError("bundle_size")
    projected = project_manifest(manifest)
    raw_manifest = canon(projected)
    archive = context_tar(members)
    count = len(manifest["files"])
    links = len(manifest["symlinks"])
    size = sum(item["bytes"] for item in manifest["files"].values())
    if (count, links, size) != (3747, 114, base["artifact"]["bytes"]):
        raise ValueError("app_manifest_counts")
    old_scoped = {
        (item["name"], item["version"]) for item in attr["confirmed_version_rows_for_sca"]
    }
    if (
        len(old_scoped) != 18
        or ("undici", "6.21.0") not in old_scoped
        or len(node["families"]["physical"]) != 78
    ):
        raise ValueError("audit_inventory_counts")
    scoped = sorted(old_scoped - {("undici", "6.21.0")} | {("undici", "6.29.1")})
    plan = {
        "schema": 1,
        "kind": "supoclip_undici_corrected_image_context",
        "production_approved": False,
        "image_built": False,
        "baseline_image": BASELINE,
        "next_version": "15.5.28",
        "app_lock_sha256": APP_LOCK,
        "swc_linux_glibc": {"version": "15.5.28", "path": swc.as_posix(), **row(swc_bytes)},
        "proofs": {
            "build": build_ref,
            "baseline_standalone": manifest_ref,
            "trial_linux": trial_ref,
            "overlay": overlay_ref,
            "next_source": source_ref,
            "baseline_node": node_ref,
            "baseline_next_attribution": attr_ref,
        },
        "context": {
            "archive": row(archive),
            "members": {name: row(raw) for name, raw in members.items()},
        },
        "projected_manifest": row(raw_manifest),
        "application": {
            "files": count,
            "symlinks": links,
            "bytes": sum(item["bytes"] for item in projected["files"].values()),
            "preimages": PREIMAGES,
            "products": {target: PRODUCTS[name] for name, target in TARGETS.items()},
        },
        "build": {
            "platform": "linux/amd64",
            "network": "none",
            "pull": False,
            "standalone": "/opt/media-platform/supoclip-frontend-stage-v1/frontend-runtime-build-v2/standalone",
            "sdk": "/opt/media-platform/immich-js-seed-sdk-v1/inputs",
            "wolfi": "oci-layout:///var/lib/media-platform/build/supoclip-ffmpeg-sdk-v3/base-oci@sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72",
        },
        "final_scan": {
            "physical_apk_expected": 26,
            "physical_npm_expected": 78,
            "next_attribution_expected": [
                {"name": name, "version": version} for name, version in scoped
            ],
            "full_next_bundle_coverage": False,
            "os_node_builtin_undici_separate": True,
            "must_absent_from_all_oci_layers": list(PREIMAGES.values()),
            "must_verify_projected_app_tree": True,
            "must_verify_undici_license": {"path": LICENSE_PATH, **UNDICI_LICENSE},
            "next_15_5_29_upstream_moderate_fixes": [
                "CVE-2026-119346",
                "CVE-2026-119347",
                "CVE-2026-119348",
            ],
            "approval_requires_current_reports_and_full_image_identity": True,
        },
    }
    return plan, archive, raw_manifest


def write_exclusive(path, content):
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare", action="store_true", help="Publica datos en el destino fijo y nuevo."
    )
    args = parser.parse_args()
    plan, archive, manifest = prepare()
    output = WORKSPACE / STAGING / "context-v1"
    if args.prepare:
        if output.parent.resolve(strict=True) != output.parent:
            raise ValueError("output_parent")
        output.mkdir(mode=0o700)
        write_exclusive(output / "context.tar", archive)
        write_exclusive(output / "projected-standalone.json", manifest)
        write_exclusive(output / "plan.json", canon(plan))
    print(
        json.dumps(
            {
                "context": plan["context"]["archive"],
                "plan": row(canon(plan)),
                "projected_manifest": plan["projected_manifest"],
                "published": args.prepare,
                "output": str(output),
                "production_approved": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
