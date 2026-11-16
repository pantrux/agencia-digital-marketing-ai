"""Selecciona y extrae 24 identidades sintéticas de ControlFace10K por rangos HTTPS."""

import argparse
import binascii
import hashlib
import json
import re
import struct
import sys
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import urlsplit

REVISION = "a03589de1a9e028b2d16fa1eb0e019a6930e817c"
DATASET = "https://huggingface.co/datasets/HuMInGameLab/ControlFace10K"
ZIP_URL = f"{DATASET}/resolve/{REVISION}/controlface10k.zip"
PAPER = "https://arxiv.org/abs/2409.08345"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
ZIP_BYTES = 3_137_641_968
ZIP_ETAG = '"1ed68f617170b0ecb95295496734d8f5e6c21b0dbf3a82d07747d84746f628f7"'
CENTRAL_OFFSET = 3_135_435_987
CENTRAL_BYTES = 2_205_959
CENTRAL_SHA = "82ffa55d26d32d4c0fa7d4d1629f5e2ecb776b69209954f6fa1de1414b949292"
CENTRAL_ENTRIES = 13_386
MAX_TRANSFER = 30_000_000
MAX_MEMBER = 2_000_000
RACES = ("African", "Asian", "Caucasian", "Indian")
GENDERS = ("female", "male")
AGES = ("25", "50", "65")
VIEWS = {"3", "4", "5"}
HOLDOUT_COHORT = "second-uuid-holdout-v1"
THIRD_COHORT = "third-uuid-holdout-v1"
COHORTS = {HOLDOUT_COHORT: (1, "cfh"), THIRD_COHORT: (2, "cfh3")}
HISTORICAL_PLAN_SHA = "d541d0c5b151bd1adc01e9bb3ca8fdb7ddac2628ac205ff6dfa52a6e67850f23"
HOLDOUT_PLAN_SHA = "a9f043845234377a476943fbe2ee3df71de9d99d31d1368e6504c28cc21149ef"
THIRD_PLAN_SHA = "15d0709011bf55c5c596069274483e60780ddd8eccf40f1fa47b6b363676aa41"
COHORT_PLAN_SHAS = {HOLDOUT_COHORT: HOLDOUT_PLAN_SHA, THIRD_COHORT: THIRD_PLAN_SHA}
NAME = re.compile(
    r"ControlFace10k/(African|Asian|Caucasian|Indian)/(female|male)/(25|50|65)/"
    r"identity-([a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12})/"
    r"r([0-3])_g([01])_a(25|50|65)_o([345])_[A-Za-z0-9]{6}\.png"
)
CENTRAL = struct.Struct("<4s6H3L5H2L")
LOCAL = struct.Struct("<4s5H3L2H")
END = struct.Struct("<4s4H2LH")


def require(condition, error):
    if not condition:
        raise ValueError(error)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class CDNRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        host = target.hostname or ""
        require(
            urlsplit(req.full_url).hostname == "huggingface.co"
            and target.scheme == "https"
            and target.username is None
            and target.password is None
            and target.port in (None, 443)
            and (host == "cas-bridge.xethub.hf.co" or host.endswith(".aws.cdn.hf.co")),
            "REDIRECT",
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class RangeReader:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), CDNRedirect())
        self.bytes = 0

    def read(self, start, length):
        stop = start + length - 1
        require(
            0 <= start <= stop < ZIP_BYTES and self.bytes + length <= MAX_TRANSFER, "RANGE_LIMIT"
        )
        req = urllib.request.Request(
            ZIP_URL, headers={"Range": f"bytes={start}-{stop}", "Accept-Encoding": "identity"}
        )
        with self.opener.open(req, timeout=30) as response:
            require(
                response.status == 206
                and response.headers.get("Content-Range") == f"bytes {start}-{stop}/{ZIP_BYTES}"
                and response.headers.get("Content-Encoding", "identity") == "identity"
                and response.headers.get("Content-Length") == str(length)
                and response.headers.get("ETag") == ZIP_ETAG,
                "RANGE_RESPONSE",
            )
            raw = response.read(length + 1)
        require(len(raw) == length, "RANGE_LENGTH")
        self.bytes += len(raw)
        return raw


def members(reader):
    end = reader.read(ZIP_BYTES - END.size, END.size)
    require(
        END.unpack(end)
        == (
            b"PK\x05\x06",
            0,
            0,
            CENTRAL_ENTRIES,
            CENTRAL_ENTRIES,
            CENTRAL_BYTES,
            CENTRAL_OFFSET,
            0,
        ),
        "ZIP_END",
    )
    raw = reader.read(CENTRAL_OFFSET, CENTRAL_BYTES)
    require(sha(raw) == CENTRAL_SHA, "CENTRAL_SHA")
    groups, seen, pos = {}, set(), 0
    for _ in range(CENTRAL_ENTRIES):
        require(pos + CENTRAL.size <= len(raw), "CENTRAL_TRUNCATED")
        (
            marker,
            _made,
            version,
            flags,
            method,
            _time,
            _date,
            crc,
            packed,
            size,
            nlen,
            extra,
            comment,
            disk,
            _internal,
            external,
            offset,
        ) = CENTRAL.unpack_from(raw, pos)
        first = pos + CENTRAL.size
        pos = first + nlen + extra + comment
        require(
            marker == b"PK\x01\x02" and disk == 0 and nlen > 0 and pos <= len(raw), "CENTRAL_ENTRY"
        )
        name = raw[first : first + nlen].decode("ascii")
        require(name not in seen, "CENTRAL_DUPLICATE")
        seen.add(name)
        if not name.endswith(".png"):
            continue
        match = NAME.fullmatch(name)
        require(match is not None, "PNG_MEMBER_NAME")
        race, gender, age, identity, race_id, gender_id, repeated_age, view = match.groups()
        require(
            (race_id, gender_id, repeated_age)
            == (str(RACES.index(race)), str(int(gender == "male")), age),
            "PNG_MEMBER_LABEL",
        )
        require(
            10 <= version <= 20
            and flags in (0, 0x800)
            and method in (0, 8)
            and ((external >> 16) & 0o170000) in (0, 0o100000)
            and 0 < packed <= MAX_MEMBER
            and 0 < size <= MAX_MEMBER
            and 0 <= offset < offset + LOCAL.size + nlen + packed <= CENTRAL_OFFSET,
            "PNG_MEMBER_FORMAT",
        )
        group = groups.setdefault((race, gender, age, identity), {})
        require(view not in group, "PNG_VIEW_DUPLICATE")
        group[view] = {
            "path": name,
            "version": version,
            "flags": flags,
            "method": method,
            "crc32": crc,
            "packed": packed,
            "size": size,
            "offset": offset,
        }
    require(
        pos == len(raw)
        and len(groups) == 3336
        and all(set(views) == VIEWS for views in groups.values()),
        "ZIP_INVENTORY",
    )
    return groups


def build_plan(groups, cohort=None):
    require(cohort is None or cohort in COHORTS, "COHORT")
    rank, prefix = COHORTS.get(cohort, (0, "cf"))
    entries = []
    for race_idx, race in enumerate(RACES):
        for gender_idx, gender in enumerate(GENDERS):
            for age_idx, age in enumerate(AGES):
                identities = sorted(
                    key[3]
                    for key, views in groups.items()
                    if key[:3] == (race, gender, age) and set(views) == VIEWS
                )
                require(len(identities) > rank, "SAMPLE_STRATUM")
                identity = identities[rank]
                views = groups[(race, gender, age, identity)]
                split = (
                    "calibration" if (race_idx + gender_idx + age_idx) % 2 == 0 else "validation"
                )
                if cohort is not None:
                    split = "holdout"
                label = f"{prefix}-{len(entries) // 3 + 1:02d}"
                for view in sorted(VIEWS):
                    fixture = f"{label}-o{view}"
                    entries.append(
                        {
                            "id": fixture,
                            "identity": label,
                            "split": split,
                            "source_identity": identity,
                            "member": views[view],
                        }
                    )
    require(len(entries) == 72, "SAMPLE_SIZE")
    require(len({item["source_identity"] for item in entries}) == 24, "SAMPLE_IDENTITY")
    expected_split = "holdout" if cohort else "calibration"
    require(
        sum(item["split"] == expected_split for item in entries) == (72 if cohort else 36),
        "SAMPLE_SPLIT",
    )
    require(
        CENTRAL_BYTES + END.size + sum(item["member"]["packed"] + 4126 for item in entries)
        <= MAX_TRANSFER,
        "SAMPLE_BUDGET",
    )
    return {
        "schema": 1,
        "synthetic": True,
        "dataset": DATASET,
        "revision": REVISION,
        "central_sha256": CENTRAL_SHA,
        "license": "CC BY 4.0",
        "license_url": LICENSE_URL,
        "paper": PAPER,
        "entries": entries,
        **({"cohort": cohort} if cohort else {}),
    }


def read_png(reader, member):
    offset = member["offset"]
    header = reader.read(offset, LOCAL.size)
    magic, version, flags, method, _time, _date, crc, packed, size, nlen, extra = LOCAL.unpack(
        header
    )
    path = member["path"].encode("ascii")
    require(
        magic == b"PK\x03\x04"
        and version == member["version"]
        and flags == member["flags"]
        and method == member["method"]
        and crc == member["crc32"]
        and packed == member["packed"]
        and size == member["size"]
        and nlen == len(path)
        and nlen + extra <= 4096
        and offset + LOCAL.size + nlen + extra + packed <= CENTRAL_OFFSET,
        "LOCAL_HEADER",
    )
    raw = reader.read(offset + LOCAL.size, nlen + extra + packed)
    require(raw[:nlen] == path, "LOCAL_MEMBER_NAME")
    payload = raw[nlen + extra :]
    if method == 8:
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        payload = decoder.decompress(payload, size + 1)
        require(
            decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail, "ZIP_DEFLATE"
        )
    require(len(payload) == size and binascii.crc32(payload) == crc, "ZIP_MEMBER_CRC")
    require(
        payload.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR") and len(payload) >= 45,
        "PNG_SIGNATURE",
    )
    width, height = struct.unpack_from(">2I", payload, 16)
    require(32 <= width <= 4096 and 32 <= height <= 4096, "PNG_SIZE")
    require(binascii.crc32(payload[12:29]) == int.from_bytes(payload[29:33], "big"), "PNG_IHDR_CRC")
    require(payload.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"), "PNG_IEND")
    return payload, width, height


def extract(reader, plan, output):
    require(
        output.is_absolute() and output.parent.resolve(strict=True) == output.parent,
        "OUTPUT_PARENT",
    )
    output.mkdir(mode=0o700, exist_ok=False)
    images = output / "images"
    images.mkdir(mode=0o700)
    fixtures = []
    hashes = set()
    for row in plan["entries"]:
        payload, width, height = read_png(reader, row["member"])
        digest = sha(payload)
        require(digest not in hashes, "PNG_DUPLICATE_CONTENT")
        hashes.add(digest)
        relative = f"images/{row['id']}.png"
        with (output / relative).open("xb") as stream:
            stream.write(payload)
        fixtures.append(
            {
                **{k: row[k] for k in ("id", "identity", "split")},
                "condition": row["id"].rsplit("-", 1)[1],
                "path": relative,
                "prepared_sha256": digest,
                "width": width,
                "height": height,
                "expected_faces": 1,
                "source_member": row["member"]["path"],
                "source_identity": row["source_identity"],
            }
        )
    metadata = {k: v for k, v in plan.items() if k != "entries"}
    if "cohort" in metadata:
        metadata["plan_sha256"] = sha(canonical(plan))
    manifest = canonical(metadata | {"fixtures": fixtures})
    with (output / "fixtures.json").open("xb") as stream:
        stream.write(manifest)
    return sha(manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    planner = modes.add_parser("plan")
    extraction = modes.add_parser("extract")
    extraction.add_argument("output", type=Path)
    extraction.add_argument("--plan-sha256", required=True)
    for command in (planner, extraction):
        command.add_argument("--cohort", choices=tuple(COHORTS))
    args = parser.parse_args()
    try:
        reader = RangeReader()
        groups = members(reader)
        if args.cohort:
            historical = build_plan(groups)
            require(sha(canonical(historical)) == HISTORICAL_PLAN_SHA, "HISTORICAL_PLAN_PIN")
        plan = build_plan(groups, args.cohort)
        if args.cohort:
            old_ids = {row["source_identity"] for row in historical["entries"]}
            if args.cohort == THIRD_COHORT:
                second = build_plan(groups, HOLDOUT_COHORT)
                require(sha(canonical(second)) == HOLDOUT_PLAN_SHA, "HOLDOUT_PLAN_PIN")
                old_ids.update(row["source_identity"] for row in second["entries"])
            require(
                not old_ids.intersection(row["source_identity"] for row in plan["entries"]),
                "COHORT_IDENTITY_OVERLAP",
            )
        plan_pin = sha(canonical(plan))
        if args.cohort:
            require(plan_pin == COHORT_PLAN_SHAS[args.cohort], "HOLDOUT_PLAN_PIN")
        if args.mode == "plan":
            result = {
                "plan_sha256": plan_pin,
                "metadata_bytes": reader.bytes,
                "compressed_sample_bytes": sum(row["member"]["packed"] for row in plan["entries"]),
                "identities": 24,
                "fixtures": 72,
                "plan": plan,
            }
        else:
            require(args.plan_sha256 == plan_pin, "PLAN_PIN")
            manifest_pin = extract(reader, plan, args.output)
            result = {
                "manifest_sha256": manifest_pin,
                "transfer_bytes": reader.bytes,
                "identities": 24,
                "fixtures": 72,
            }
        print(canonical({"success": True, **result}).decode(), end="")
        return 0
    except (OSError, ValueError, struct.error, zlib.error) as error:
        message = (
            str(error)
            if type(error) is ValueError and re.fullmatch(r"[A-Z0-9_]+", str(error))
            else type(error).__name__
        )
        print(json.dumps({"success": False, "error": message}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
