"""Pruebas sintéticas del extractor por rangos, sin HTTP ni dataset real."""

import binascii
import io
import json
import runpy
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

CORPUS = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11/immich_face_controlface_corpus.py")
)


def chunk(kind, payload):
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload))
    )


def png(pixel):
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + bytes([pixel]) * 32) * 32))
        + chunk(b"IEND", b"")
    )


def member_at(path, body, method, offset):
    if method == 8:
        codec = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        encoded = codec.compress(body) + codec.flush()
    else:
        encoded = body
    crc = binascii.crc32(body)
    header = CORPUS["LOCAL"].pack(
        b"PK\x03\x04", 20, 0, method, 0, 0, crc, len(encoded), len(body), len(path), 0
    )
    member = {
        "path": path,
        "version": 20,
        "flags": 0,
        "method": method,
        "crc32": crc,
        "packed": len(encoded),
        "size": len(body),
        "offset": offset,
    }
    return header + path.encode("ascii") + encoded, member


class CorpusTests(unittest.TestCase):
    def test_holdout_uses_second_complete_uuid_in_every_stratum(self):
        group = {}
        for race in CORPUS["RACES"]:
            for gender in CORPUS["GENDERS"]:
                for age in CORPUS["AGES"]:
                    index = len(group) + 1
                    for rank in (3, 2, 1):
                        identity = f"00000000-0000-0000-0000-{index * 4 + rank:012x}"
                        group[race, gender, age, identity] = {
                            view: {"packed": 1} for view in CORPUS["VIEWS"]
                        }
        original = CORPUS["build_plan"](group)
        holdout = CORPUS["build_plan"](group, CORPUS["HOLDOUT_COHORT"])
        third = CORPUS["build_plan"](group, CORPUS["THIRD_COHORT"])
        self.assertEqual(len(holdout["entries"]), 72)
        self.assertEqual(holdout["cohort"], CORPUS["HOLDOUT_COHORT"])
        self.assertEqual({row["split"] for row in holdout["entries"]}, {"holdout"})
        self.assertEqual(holdout["entries"][0]["id"], "cfh-01-o3")
        self.assertEqual(third["entries"][0]["id"], "cfh3-01-o3")
        self.assertNotIn("cohort", original)
        for previous, fresh, untouched in zip(
            original["entries"], holdout["entries"], third["entries"], strict=True
        ):
            self.assertEqual(
                int(fresh["source_identity"][-12:], 16),
                int(previous["source_identity"][-12:], 16) + 1,
            )
            self.assertEqual(
                int(untouched["source_identity"][-12:], 16),
                int(previous["source_identity"][-12:], 16) + 2,
            )

    def test_deflate_and_stored_verify_dimensions(self):
        for method in (0, 8):
            raw, member = member_at("sample.png", png(10), method, 0)
            reader = type(
                "Reader",
                (),
                {"read": lambda _self, start, length, data=raw: data[start : start + length]},
            )()
            self.assertEqual(CORPUS["read_png"](reader, member), (png(10), 32, 32))

    def test_reject_changed_local_name_and_payload(self):
        raw, member = member_at("sample.png", png(10), 0, 0)
        reader = type(
            "Reader", (), {"read": lambda _self, start, length: raw[start : start + length]}
        )()
        with self.assertRaisesRegex(ValueError, "LOCAL_MEMBER_NAME"):
            CORPUS["read_png"](reader, {**member, "path": "change.png"})
        raw = raw[:-1] + b"\0"
        with self.assertRaisesRegex(ValueError, "ZIP_MEMBER_CRC"):
            CORPUS["read_png"](reader, member)

    def test_emits_probe_manifest_and_preserves_bytes(self):
        group = {}
        buffer = io.BytesIO()
        index = 0
        for race in CORPUS["RACES"]:
            for gender in CORPUS["GENDERS"]:
                for age in CORPUS["AGES"]:
                    group[race, gender, age, f"{index:032x}"] = views = {}
                    for view in sorted(CORPUS["VIEWS"]):
                        index += 1
                        chunk_data, member = member_at(f"{index}.png", png(index), 8, buffer.tell())
                        views[view] = member
                        buffer.write(chunk_data)
        plan = CORPUS["build_plan"](group)
        raw = buffer.getvalue()
        reader = type(
            "Reader", (), {"read": lambda _self, start, length: raw[start : start + length]}
        )()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve() / "corpus"
            pin = CORPUS["extract"](reader, plan, output)
            manifest_raw = (output / "fixtures.json").read_bytes()
            self.assertEqual(pin, CORPUS["sha"](manifest_raw))
            manifest = json.loads(manifest_raw)
            self.assertEqual(manifest["schema"], 1)
            self.assertIs(manifest["synthetic"], True)
            self.assertEqual(len(manifest["fixtures"]), 72)
            first = manifest["fixtures"][0]
            self.assertEqual(first["path"], "images/cf-01-o3.png")
            self.assertEqual(first["condition"], "o3")
            self.assertEqual({row["condition"] for row in manifest["fixtures"]}, {"o3", "o4", "o5"})
            self.assertEqual(first["expected_faces"], 1)
            self.assertEqual(first["prepared_sha256"], CORPUS["sha"](png(1)))
            self.assertEqual((output / first["path"]).read_bytes(), png(1))
            with self.assertRaises(FileExistsError):
                CORPUS["extract"](reader, plan, output)
