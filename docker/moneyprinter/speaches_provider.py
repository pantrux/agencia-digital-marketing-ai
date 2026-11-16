"""Adaptador MIT de Speaches para MoneyPrinter; sin modelos locales ni fallback."""

from __future__ import annotations

import math
from typing import NamedTuple

from speaches_client import transcribe_audio
from speaches_contract import MAX_SAFE_SECONDS, SpeachesError, normalize_verbose

# Catálogo del commit upstream, conservado con su licencia MIT de Harry (2024).
PUNCTUATIONS = (
    "?",
    ",",
    ".",
    "、",
    ";",
    ":",
    "!",
    "…",
    "？",
    "，",
    "。",
    "；",
    "：",
    "！",
    "...",
    "،",
    "؛",
    "؟",
)
CLIENT_ERRORS = {
    "SPEACHES_INPUT_REJECTED",
    "SPEACHES_INPUT_CHANGED",
    "SPEACHES_AUDIO_TOO_LARGE",
    "SPEACHES_INVALID_RESPONSE",
    "SPEACHES_RESPONSE_TRUNCATED",
    "SPEACHES_AUTH_REJECTED",
    "SPEACHES_BUSY",
    "SPEACHES_REDIRECT_REJECTED",
    "SPEACHES_UPSTREAM_REJECTED",
    "SPEACHES_TIMEOUT",
    "SPEACHES_CANCELLED",
    "SPEACHES_TRANSPORT_FAILED",
    "SPEACHES_BUDGET_REJECTED",
    "SPEACHES_SECRET_FILE_REJECTED",
    "SPEACHES_SECRET_FILE_UNSUPPORTED",
    "SPEACHES_RESPONSE_TOO_LARGE",
}


class ProviderError(ValueError):
    """Sólo códigos propios; nunca incorpora cuerpos, rutas, tokens ni excepciones ajenas."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Cue(NamedTuple):
    text: str
    start: float
    end: float


class Outcome(NamedTuple):
    status: str
    cues: tuple[Cue, ...]


def select_provider(value):
    if type(value) is not str or value not in {"speaches", "edge"}:
        raise ProviderError("MPT_SUBTITLE_PROVIDER_REJECTED")
    return value


def convert_response(value, *, word_level=False):
    """Conserva tokens/timestamps; agrupa sólo por delimitadores finales, sin corregir guion."""
    if type(word_level) is not bool:
        raise ProviderError("MPT_SPEACHES_INPUT_REJECTED")
    try:
        data = normalize_verbose(value)
    except SpeachesError:
        raise ProviderError("MPT_SPEACHES_INVALID_RESPONSE") from None
    if not data["words"]:
        return Outcome("silence", ())
    cues = []
    for segment in data["segments"]:
        if word_level:
            cues.extend(
                Cue(word["word"].strip(), word["start"], word["end"]) for word in segment["words"]
            )
            continue
        pending, started, ended = [], None, None
        for word in segment["words"]:
            if started is None:
                started = word["start"]
            pending.append(word["word"])
            ended = word["end"]
            if word["word"].rstrip().endswith(PUNCTUATIONS):
                cues.append(Cue("".join(pending).strip(), started, ended))
                pending, started = [], None
        if pending:
            cues.append(Cue("".join(pending).strip(), started, ended))
    return Outcome("transcribed", tuple(cues))


def _timestamp(seconds):
    try:
        valid = (
            type(seconds) in (int, float)
            and math.isfinite(seconds)
            and 0 <= seconds <= MAX_SAFE_SECONDS
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ProviderError("MPT_SPEACHES_INVALID_RESPONSE")
    # Única cuantización: segundos reconocidos a milisegundos, sin añadir duración.
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def render_srt(outcome):
    if type(outcome) is not Outcome:
        raise ProviderError("MPT_SPEACHES_INVALID_RESPONSE")
    if outcome.status == "silence" and outcome.cues == ():
        return ""
    if outcome.status != "transcribed" or not outcome.cues:
        raise ProviderError("MPT_SPEACHES_INVALID_RESPONSE")
    previous = 0.0
    for cue in outcome.cues:
        if (
            type(cue) is not Cue
            or type(cue.text) is not str
            or not cue.text.strip()
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in cue.text)
        ):
            raise ProviderError("MPT_SPEACHES_INVALID_RESPONSE")
        _timestamp(cue.start)
        _timestamp(cue.end)
        if not previous <= cue.start <= cue.end:
            raise ProviderError("MPT_SPEACHES_INVALID_RESPONSE")
        previous = cue.end
    return "".join(
        f"{index}\n{_timestamp(cue.start)} --> {_timestamp(cue.end)}\n{cue.text}\n\n"
        for index, cue in enumerate(outcome.cues, 1)
    )


def transcribe(audio_path, token_file, *, word_level=False, language=None, cancel_event=None):
    """Usa 300 s totales, hasta 45 s al primer byte y 15 s de inactividad."""
    if type(word_level) is not bool:
        raise ProviderError("MPT_SPEACHES_INPUT_REJECTED")
    try:
        data = transcribe_audio(
            audio_path,
            token_file,
            language=language,
            cancel_event=cancel_event,
            total_seconds=300,
            idle_seconds=15,
            first_byte_seconds=45,
        )
        return convert_response(data, word_level=word_level)
    except ProviderError:
        raise
    except SpeachesError as error:
        code = "MPT_" + error.code if error.code in CLIENT_ERRORS else "MPT_SPEACHES_FAILED"
        raise ProviderError(code) from None
    except Exception:
        raise ProviderError("MPT_SPEACHES_FAILED") from None
