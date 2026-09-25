"""Artifact validation against schema_v1.json plus invariant checks (§5, §12).

A minimal, stdlib-only subset of JSON Schema (draft 2020-12: type, required,
properties, enum, $ref, $defs) is implemented here rather than depending on
the `jsonschema` package: the project's dependency policy (§4) is psycopg +
pglast, stdlib otherwise, and schema_v1.json only ever uses that small,
closed set of keywords.
"""

from __future__ import annotations

import gzip
import importlib.resources
import json
import re
from pathlib import Path

_SUSPICIOUS_PATTERNS = {
    "email": re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
    "uuid": re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    ),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}

#: Belt-and-suspenders (§7.2): these keys must never appear anywhere in a
#: captured artifact, regardless of what schema_v1.json's type checks catch.
FORBIDDEN_KEYS = ("most_common_vals", "most_common_val_nulls")

_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def load_schema() -> dict:
    text = importlib.resources.files("pgspec").joinpath("schema_v1.json").read_text()
    return json.loads(text)


def load_artifact(path: str | Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _resolve(node: dict, root: dict) -> dict:
    if "$ref" in node:
        ref = node["$ref"]
        target = root
        for part in ref.removeprefix("#/").split("/"):
            target = target[part]
        return target
    return node


def _matches_type(instance, expected: str) -> bool:
    if expected == "number" and isinstance(instance, bool):
        return False
    return isinstance(instance, _TYPE_MAP[expected])


def _validate_node(instance, node: dict, root: dict, path: str, errors: list[str]) -> None:
    node = _resolve(node, root)

    if "type" in node:
        expected = node["type"]
        expected = [expected] if isinstance(expected, str) else expected
        if not any(_matches_type(instance, t) for t in expected):
            errors.append(
                f"{path}: expected type {expected}, got {type(instance).__name__}"
            )
            return

    if "enum" in node and instance not in node["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {node['enum']}")

    if isinstance(instance, dict):
        for key in node.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        for key, subnode in node.get("properties", {}).items():
            if key in instance:
                _validate_node(instance[key], subnode, root, f"{path}.{key}", errors)


def validate_schema(artifact: dict) -> list[str]:
    schema = load_schema()
    errors: list[str] = []
    _validate_node(artifact, schema, schema, "$", errors)
    return errors


_FORBIDDEN_KEY_MARKER = "\0forbidden-key\0"


def _walk_strings(obj, path: str):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if key in FORBIDDEN_KEYS:
                yield f"{path}.{key}", f"{_FORBIDDEN_KEY_MARKER}{key}"
            yield from _walk_strings(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            yield from _walk_strings(value, f"{path}[{i}]")


def scan_for_suspicious_strings(artifact: dict) -> list[str]:
    """Regex scan (§5): flag any string value that looks like an email,
    UUID, or IP address -- exactly the value-shaped content I1 forbids.
    Pseudonyms (s_00, t_0007, c_0142) and hex digests never match these
    patterns, so this is silent on a clean artifact. Also flags the two
    forbidden MCV-value keys directly, as a second, independent guard
    alongside the static SQL-constant check in tests/test_no_forbidden_sql.py.
    """
    findings = []
    for path, value in _walk_strings(artifact, "$"):
        if value.startswith(_FORBIDDEN_KEY_MARKER):
            findings.append(
                f"{path}: forbidden key present ({value[len(_FORBIDDEN_KEY_MARKER):]!r})"
            )
            continue
        for kind, pattern in _SUSPICIOUS_PATTERNS.items():
            if pattern.search(value):
                findings.append(f"{path}: looks like a {kind} ({value!r})")
    return findings


def validate_artifact(path: str | Path) -> list[str]:
    """Validate a captured artifact against schema_v1.json plus the
    suspicious-string scan. Returns a list of human-readable problems;
    empty means the artifact passed."""
    artifact = load_artifact(path)
    errors = validate_schema(artifact)
    errors.extend(scan_for_suspicious_strings(artifact))
    return errors
