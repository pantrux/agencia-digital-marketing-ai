import ast
import importlib.util
import tarfile
import typing
import unittest
from pathlib import Path

import pydantic

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".tools/backup-source-build/supoclip-runtime-source-v2/backend-source.tar"
RECIPE = ROOT / "docker/supoclip/prepare_transcript_timestamps.py"


def load_definitions(source):
    names = {
        "TRANSCRIPT_TIMESTAMP_PATTERN",
        "ViralityAnalysis",
        "_default_virality_analysis",
        "TranscriptSegment",
        "TranscriptAnalysis",
        "transcript_analysis_system_prompt",
        "build_transcript_system_prompt",
    }
    definitions = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names:
            definitions.append(node)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names for target in node.targets
        ):
            definitions.append(node)
    namespace = {
        "__name__": __name__,
        "Any": typing.Any,
        "List": list,
        "Literal": typing.Literal,
        "Optional": typing.Optional,
        "AliasChoices": pydantic.AliasChoices,
        "BaseModel": pydantic.BaseModel,
        "Field": pydantic.Field,
        "field_validator": pydantic.field_validator,
    }
    exec(compile(ast.Module(body=definitions, type_ignores=[]), "src/ai.py", "exec"), namespace)
    return namespace


@unittest.skipUnless(SOURCE.is_file(), "Requiere la captura privada SupoClip runtime source V2")
class TranscriptTimestampTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tarfile.open(SOURCE) as archive:
            source = archive.extractfile("backend/src/ai.py").read()
            duration = archive.extractfile("backend/src/clip_duration.py").read()
        spec = importlib.util.spec_from_file_location("supoclip_timestamp_recipe", RECIPE)
        cls.recipe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.recipe)
        cls.source = source
        cls.definitions = load_definitions(cls.recipe.patch_transcript_source(source))
        exec(compile(duration, "src/clip_duration.py", "exec"), cls.definitions)

    def test_schema_and_validation_accept_single_timestamps(self):
        segment = self.definitions["TranscriptSegment"]
        fields = segment.model_json_schema()["properties"]
        for field in ("start_time", "end_time"):
            self.assertIn("pattern", fields[field])
        for start, end in (("00:11", "00:29"), ("100:05", "100:25"), ("1:02:03", "1:02:23")):
            with self.subTest(start=start, end=end):
                parsed = segment(start_time=start, end_time=end, text="Una frase del video.")
                self.assertEqual((parsed.start_time, parsed.end_time), (start, end))

    def test_rejects_ranges_in_each_field_of_structured_output(self):
        analysis = self.definitions["TranscriptAnalysis"]
        with self.assertRaises(pydantic.ValidationError) as raised:
            analysis.model_validate(
                {
                    "most_relevant_segments": [
                        {
                            "start_time": "00:00 - 00:03",
                            "end_time": "00:03 - 00:03",
                            "text": "Una frase del video.",
                        }
                    ],
                    "summary": "Una frase.",
                    "key_topics": [],
                }
            )
        self.assertEqual(
            {error["loc"] for error in raised.exception.errors()},
            {
                ("most_relevant_segments", 0, "start_time"),
                ("most_relevant_segments", 0, "end_time"),
            },
        )

    def test_rejects_unaligned_format_and_impossible_seconds(self):
        segment = self.definitions["TranscriptSegment"]
        for value in ("00:60", "1:60:00", "[00:29]", "00:29\n", "00:29.500", " 00:29"):
            with self.subTest(value=value), self.assertRaises(pydantic.ValidationError):
                segment(start_time="00:11", end_time=value, text="Una frase del video.")

    def test_prompt_explains_that_each_field_takes_one_boundary(self):
        prompt = self.definitions["build_transcript_system_prompt"]("short")
        self.assertIn('start_time="00:12", end_time="00:35"', prompt)
        self.assertIn("Never copy a whole range into either field", prompt)
        self.assertIn("15-30 seconds", prompt)

    def test_preparation_rejects_a_different_source(self):
        with self.assertRaisesRegex(ValueError, "preimagen"):
            self.recipe.patch_transcript_source(self.source.replace(b"MM:SS", b"HH:MM", 1))
