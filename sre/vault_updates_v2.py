"""Writer futuro con candidata durable; no cambia el helper 0fc ni sus callsites."""

import json
import os
import re


def require(value, code):
    if not value:
        raise ValueError(code)


def decoded(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "JSON_DUPLICATE")
            result[key] = value
        return result

    return json.loads(
        raw, object_pairs_hook=pairs, parse_constant=lambda _value: require(False, "JSON_CONSTANT")
    )


def render_update(before, updates, parse_env, validate_comments, documentation, comments=None):
    require(
        updates and all(re.fullmatch(r"[A-Z][A-Z0-9_]*", key) for key in updates), "UPDATE_KEYS"
    )
    require(
        all(
            isinstance(value, str) and not any(char in value for char in "\r\n\x00")
            for value in updates.values()
        ),
        "UPDATE_VALUES",
    )
    current, errors = parse_env(before.decode("utf-8-sig", errors="strict"))
    require(current and not errors, "CURRENT_ENV_INVALID")
    lines, seen = before.decode("utf-8-sig").splitlines(), set()

    def literal(value):
        return (
            '"' + value + '"'
            if value != value.strip() or any(char in value for char in "#'\"")
            else value
        )

    def comment_block(key):
        block = comments[key] if comments and key in comments else documentation(key)
        require(
            isinstance(block, (list, tuple))
            and len(block) == 3
            and all(
                isinstance(line, str)
                and not any(char in line for char in "\r\n\x00")
                and line.startswith(prefix)
                for line, prefix in zip(
                    block, ("# Para qué:", "# Dónde:", "# Riesgo al rotar:"), strict=True
                )
            ),
            "UPDATE_COMMENTS",
        )
        return block

    for number, line in enumerate(lines):
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if match and match[1] in updates:
            lines[number] = match[1] + "=" + literal(updates[match[1]])
            seen.add(match[1])
    for key, value in updates.items():
        if key not in seen:
            block = comment_block(key)
            lines.extend(["", *block, key + "=" + literal(value)])
    text = "\n".join(lines).rstrip("\n") + "\n"
    after, errors = parse_env(text)
    require(
        not errors
        and not validate_comments(text)
        and set(after) == set(current) | set(updates)
        and all(after.get(key) == value for key, value in updates.items())
        and all(after.get(key) == value for key, value in current.items() if key not in updates),
        "UPDATE_ROUNDTRIP",
    )
    template = ["# Plantilla canónica. Obtener valores únicamente desde la bóveda .env.", ""]
    for key in after:
        template.extend([*comment_block(key), key + "=CHANGE_ME", ""])
    return text.encode("utf-8"), "\n".join(template).encode("utf-8"), after


def publish_update(
    queue,
    vault,
    template,
    publish_parent,
    updates,
    *,
    documentation,
    comments=None,
    replace=os.replace,
):
    """CAS cooperativo; no puede bloquear a editores que ignoren el lock privado."""
    for _attempt in range(5):
        with queue.store.lock("publish-v2"):
            before, identity = queue.reader(vault, 8 * 1024**2)
            candidate, example, values = render_update(
                before, updates, queue.parse_env, queue.validate_comments, documentation, comments
            )
            if candidate == before:
                return {
                    "vault_keys": len(values),
                    "updated_keys": sorted(updates),
                    "values_hidden": True,
                    "unchanged": True,
                    "committed": False,
                }
            generation = queue.enqueue(candidate, "ROOT_CANDIDATE", base=identity)["generation_id"]
            # La copia inmutable permanece en C:. Sólo este temporal propio cruza a .env.
            temp = queue.promotion_temp(publish_parent, generation, candidate, "vault")
            current, now = queue.reader(vault, 8 * 1024**2)
            if current != before or now != identity:
                queue.record(
                    generation,
                    "cas-conflict.json",
                    {"generation_id": generation, "publication": "NOT_ATTEMPTED"},
                )
                continue
            replace(temp, vault)
            queue.store.access.check(vault)
            observed, published = queue.reader(vault, 8 * 1024**2)
            require(observed == candidate, "PUBLICATION_UNCONFIRMED")
            queue.mark_committed(generation, published)
            result = {
                "vault_keys": len(values),
                "updated_keys": sorted(updates),
                "values_hidden": True,
                "generation_id": generation,
                "committed": True,
                "template_synced": False,
            }
            try:
                temp_example = queue.promotion_temp(publish_parent, generation, example, "template")
                replace(temp_example, template)
                result["template_synced"] = True
            except Exception as error:
                queue.record(
                    generation,
                    "template-error.json",
                    {"error_type": type(error).__name__, "vault_committed": True},
                )
                result["requires_manual_reconciliation"] = True
            return result
    raise RuntimeError("CAS_CONFLICT_RETRIES_EXHAUSTED")


def bootstrap_runtime(config_path, config_sha256, *, preparing=False):
    """Carga bytes fijados; no usa importlib ni ejecuta un .pyc vecino."""
    import hashlib
    import stat
    import sys
    import types
    from pathlib import Path

    def pinned(path, pin):
        path = Path(path)
        info = path.lstat()
        require(
            path.is_absolute()
            and stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and not getattr(info, "st_file_attributes", 0) & 0x400
            and info.st_size <= 256 * 1024,
            "BOOTSTRAP_FILE",
        )
        with path.open("rb") as stream:
            raw = stream.read(256 * 1024 + 1)
        require(
            len(raw) == info.st_size and hashlib.sha256(raw).hexdigest() == pin, "BOOTSTRAP_SHA"
        )
        return raw

    raw = pinned(config_path, config_sha256)
    config = decoded(raw)
    source = Path(config["code_root"]) / "vault_change_queue.py"
    raw_source = pinned(source, config["sources"][source.name])
    module = types.ModuleType("vault_changes_runtime_queue")
    module.__file__ = str(source)
    sys.modules[module.__name__] = module
    exec(compile(raw_source, str(source), "exec"), module.__dict__)
    return module.Runtime(config, config_path, config_sha256, __file__, preparing=preparing)


def update(updates, *, comments=None, config_path, config_sha256):
    """Sólo futuros operadores opt-in; los callsites históricos siguen usando 0fc."""
    from pathlib import Path

    runtime = bootstrap_runtime(config_path, config_sha256)
    return publish_update(
        runtime.queue,
        Path(runtime.config["vault_path"]),
        Path(runtime.config["template_path"]),
        Path(runtime.config["publish_parent"]),
        updates,
        documentation=runtime.documentation(),
        comments=comments,
    )
