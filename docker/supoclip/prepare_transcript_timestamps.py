"""Prepara src/ai.py para la imagen derivada desde la fuente runtime V2 fijada."""

import argparse
import ast
import hashlib
import tarfile
from pathlib import Path

SOURCE_PATH = "backend/src/ai.py"
SOURCE_BYTES = 75_528
SOURCE_SHA256 = "f6cc7bf1199b77df7d5f704bb4c641735d513ff03293827fd34e5f64498c0305"
REPLACEMENTS = (
    (
        'TRANSCRIPT_ANALYSIS_CACHE_VERSION = "windowed-rerank-v6"',
        'TRANSCRIPT_ANALYSIS_CACHE_VERSION = "windowed-rerank-v7"',
    ),
    (
        "class TranscriptSegment(BaseModel):",
        'TRANSCRIPT_TIMESTAMP_PATTERN = r"^[0-9]{1,3}:(?:[0-5][0-9]:)?[0-5][0-9]$"\n'
        "\n\nclass TranscriptSegment(BaseModel):",
    ),
    (
        '    start_time: str = Field(description="Start timestamp in MM:SS format")\n'
        '    end_time: str = Field(description="End timestamp in MM:SS format")',
        "    start_time: str = Field(\n"
        "        pattern=TRANSCRIPT_TIMESTAMP_PATTERN,\n"
        '        description="One start timestamp copied from the transcript, e.g. 00:12",\n'
        "    )\n"
        "    end_time: str = Field(\n"
        "        pattern=TRANSCRIPT_TIMESTAMP_PATTERN,\n"
        '        description="One end timestamp copied from the transcript, e.g. 00:35",\n'
        "    )",
    ),
    (
        "- Use EXACT timestamps as they appear in the transcript\n"
        "- Never modify timestamp format (keep MM:SS structure)",
        "- Copy one exact timestamp from the transcript into each field.\n"
        "- Preserve the timestamp format as it appears in the transcript.\n"
        "- Never copy a whole range into either field. For adjacent rows [00:12 - 00:21] "
        'and [00:21 - 00:35], the two outer boundaries are start_time="00:12", '
        'end_time="00:35". This illustrates the field format. Apply the requested duration below.',
    ),
    (
        "- Look at transcript ranges like [02:25 - 02:35] and use different start/end times\n"
        "- NEVER use the same timestamp for both start_time and end_time\n"
        '- Example: start_time: "02:25", end_time: "02:35" (NOT "02:25" and "02:25")',
        "- NEVER use the same timestamp for both start_time and end_time",
    ),
    (
        "4. For each chosen segment, use the earliest timestamp in the selected range as "
        "start_time and the latest timestamp in the selected range as end_time.",
        "4. Put only the first boundary of the chosen lines in start_time and only the last "
        'boundary in end_time. For the two rows above, use start_time="00:12" and end_time="00:35".',
    ),
)


def patch_transcript_source(source: bytes) -> bytes:
    if len(source) != SOURCE_BYTES or hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError("La preimagen de src/ai.py difiere de la fuente runtime V2.")
    text = source.decode("utf-8")
    for before, after in REPLACEMENTS:
        if text.count(before) != 1:
            raise ValueError("El fragmento esperado de src/ai.py no es único.")
        text = text.replace(before, after)
    ast.parse(text, filename=SOURCE_PATH)
    return text.encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_archive", type=Path)
    parser.add_argument("context", type=Path, help="Directorio nuevo para el contexto Docker")
    args = parser.parse_args()
    with tarfile.open(args.source_archive) as archive:
        member = archive.getmember(SOURCE_PATH)
        if not member.isfile() or member.size != SOURCE_BYTES:
            raise ValueError("src/ai.py no es un archivo regular de la fuente runtime V2.")
        with archive.extractfile(member) as stream:
            patched = patch_transcript_source(stream.read(SOURCE_BYTES + 1))
    dockerfile = Path(__file__).with_name("Dockerfile.backend-transcript-timestamps")
    args.context.mkdir(mode=0o700)
    (args.context / "ai.py").write_bytes(patched)
    (args.context / "Dockerfile").write_bytes(dockerfile.read_bytes())
    print(f"{hashlib.sha256(patched).hexdigest()}  ai.py")


if __name__ == "__main__":
    main()
