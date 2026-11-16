"""Validación pura de verbose_json; conserva palabras/segmentos y normaliza segundos."""

from __future__ import annotations

import copy
import json
import math

MAX_AUDIO_BYTES = 24 * 1024**2
MAX_MULTIPART_BYTES = 25 * 1024**2
MAX_RESPONSE_BYTES = 2 * 1024**2
MAX_SAFE_SECONDS = (2**53 - 1) / 1000


class SpeachesError(ValueError):
    """Sólo conserva un código propio; nunca token, cuerpo o excepción del proveedor."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(condition):
    if not condition:
        raise SpeachesError("SPEACHES_INVALID_RESPONSE")


def _tree(value):
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        require(depth <= 64 and nodes <= 100000)
        if type(item) is str:
            require(not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in item))
        elif type(item) in (int, float):
            require(type(item) is int or math.isfinite(item))
        elif item is None or type(item) is bool:
            pass
        elif type(item) is dict:
            require(all(type(key) is str for key in item))
            stack.extend((key, depth + 1) for key in item)
            stack.extend((v, depth + 1) for v in item.values())
        elif type(item) is list:
            stack.extend((v, depth + 1) for v in item)
        else:
            require(False)


def _seconds(value):
    require(type(value) in (int, float))
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise SpeachesError("SPEACHES_INVALID_RESPONSE") from None
    require(math.isfinite(result) and 0 <= result <= MAX_SAFE_SECONDS)
    return result


def _word(value, duration):
    require(type(value) is dict and set(value) == {"start", "end", "word", "probability"})
    require(type(value["word"]) is str and bool(value["word"].strip()))
    start, end = _seconds(value["start"]), _seconds(value["end"])
    require(
        start <= end <= duration + 0.1
        and type(value["probability"]) in (int, float)
        and math.isfinite(value["probability"])
        and 0 <= value["probability"] <= 1
    )
    value["start"], value["end"] = start, end


def normalize_verbose(value):
    try:
        _tree(value)
        require(
            type(value) is dict
            and {"language", "duration", "text", "words", "segments"} <= set(value)
        )
        require(value.get("task", "transcribe") == "transcribe")
        require(
            type(value["text"]) is str
            and (
                value["language"] is None
                or type(value["language"]) is str
                and 1 <= len(value["language"]) <= 32
            )
        )
        require(type(value["words"]) is list and type(value["segments"]) is list)
        result = copy.deepcopy(value)
        duration = _seconds(result["duration"])
        result["duration"] = duration
        require(
            len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            <= MAX_RESPONSE_BYTES
        )
        if not result["words"]:
            require(not result["segments"] and not result["text"].strip())
            return result
        require(bool(result["segments"]) and bool(result["text"].strip()))
        previous = 0.0
        for word in result["words"]:
            _word(word, duration)
            require(previous <= word["start"])
            previous = word["end"]
        flattened = []
        previous = 0.0
        for segment in result["segments"]:
            require(
                type(segment) is dict
                and {"start", "end", "text", "words"} <= set(segment)
                and type(segment["text"]) is str
                and type(segment["words"]) is list
                and bool(segment["words"])
            )
            start, end = _seconds(segment["start"]), _seconds(segment["end"])
            require(previous <= start <= end <= duration + 0.1)
            segment["start"], segment["end"] = start, end
            previous = end
            for word in segment["words"]:
                _word(word, duration)
                require(start <= word["start"] <= word["end"] <= end)
                flattened.append(word)
        require(flattened == result["words"])
        return result
    except SpeachesError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, UnicodeError):
        raise SpeachesError("SPEACHES_INVALID_RESPONSE") from None


def parse_verbose(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RESPONSE_BYTES:
        raise SpeachesError("SPEACHES_RESPONSE_TOO_LARGE")

    def unique(pairs):
        data = {}
        for key, value in pairs:
            require(key not in data)
            data[key] = value
        return data

    def invalid(_value):
        raise SpeachesError("SPEACHES_INVALID_RESPONSE")

    def finite(value):
        result = float(value)
        require(math.isfinite(result))
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=unique,
            parse_constant=invalid,
            parse_float=finite,
        )
    except SpeachesError:
        raise
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise SpeachesError("SPEACHES_INVALID_RESPONSE") from None
    return normalize_verbose(value)
