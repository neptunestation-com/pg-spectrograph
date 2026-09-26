"""pgspec deref: local de-pseudonymization using the map file (§7.5, §12).

Verifies the map file's digest matches the artifact's own
pseudonym_map_digest before using it: using the wrong map (from a
different capture, a different salt) would otherwise silently produce
plausible-looking but wrong real names rather than a clear error -- exactly
the kind of silent-wrong-answer this project's invariants rule out
everywhere else.
"""

from __future__ import annotations

import hashlib
import json
import re


def load_map_file(path: str) -> dict[str, str]:
    """Returns the pseudonym -> real-name map (the map file's own "map" key)."""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return payload["map"]


def verify_map_digest(map_path: str, expected_digest: str) -> bool:
    with open(map_path, "rb") as f:
        raw = f.read()
    actual = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    return actual == expected_digest


def _build_pattern(pseudonym_map: dict[str, str]) -> re.Pattern:
    keys = sorted(pseudonym_map.keys(), key=len, reverse=True)
    if not keys:
        return re.compile(r"(?!)")  # matches nothing
    alternation = "|".join(re.escape(k) for k in keys)
    return re.compile(rf"\b(?:{alternation})\b")


def _deref_string(text: str, pseudonym_map: dict[str, str], pattern: re.Pattern) -> str:
    if text in pseudonym_map:
        return pseudonym_map[text]
    return pattern.sub(lambda m: pseudonym_map.get(m.group(0), m.group(0)), text)


def _deref_value(obj, pseudonym_map: dict[str, str], pattern: re.Pattern):
    if isinstance(obj, str):
        return _deref_string(obj, pseudonym_map, pattern)
    if isinstance(obj, dict):
        return {k: _deref_value(v, pseudonym_map, pattern) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deref_value(v, pseudonym_map, pattern) for v in obj]
    return obj


def deref_artifact(artifact: dict, pseudonym_map: dict[str, str]) -> dict:
    """Restore real names everywhere a pseudonym appears: as a whole field
    value (pseudonym/table_pseudonym/etc.) and as a token embedded in a
    larger string (pseudonymized query text)."""
    pattern = _build_pattern(pseudonym_map)
    return _deref_value(artifact, pseudonym_map, pattern)
