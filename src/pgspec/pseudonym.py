"""HMAC-based pseudonym map and pglast-based query-text rewriting (I2, §7.4-7.5).

Invariant I2: all schema/table/column/index identifiers are replaced by
deterministic pseudonyms. Ordinals are assigned by sorting HMAC-SHA256 digests,
never by name, so pseudonym assignment cannot leak identifier ordering.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pglast
from pglast.stream import RawStream
from pglast.visitors import Visitor

#: Default zero-padded ordinal width per pseudonym prefix. Grows automatically
#: (never truncates) when a collection exceeds what the default width can
#: represent; see assign_ordinals.
DEFAULT_ORDINAL_WIDTH: dict[str, int] = {
    "s": 2,
    "t": 4,
    "c": 4,
    "i": 4,
    "fn": 4,
}


def digest(salt: bytes, fq_name: str) -> bytes:
    """HMAC-SHA256(salt, fq_name): the sort key behind every pseudonym ordinal."""
    return hmac.new(salt, fq_name.encode("utf-8"), hashlib.sha256).digest()


def assign_ordinals(
    names: Iterable[str],
    salt: bytes,
    prefix: str,
    width: int | None = None,
) -> dict[str, str]:
    """Assign deterministic pseudonyms to a collection of identifiers.

    Ordinals are assigned by sorting names by digest(salt, name), never by the
    names themselves, so the pseudonym map cannot leak identifier ordering.
    Ties (a digest collision) fall back to first-seen order for determinism,
    since Python's set() iteration order for strings is not stable across
    process runs under hash randomization.

    Returns a dict mapping each real name to its pseudonym.
    """
    unique_names = sorted(dict.fromkeys(names), key=lambda n: digest(salt, n))
    n = len(unique_names)
    base_width = width if width is not None else DEFAULT_ORDINAL_WIDTH.get(prefix, 4)
    required_width = len(str(n - 1)) if n > 0 else 1
    final_width = max(base_width, required_width)
    return {
        name: f"{prefix}_{ordinal:0{final_width}d}"
        for ordinal, name in enumerate(unique_names)
    }


def load_or_create_salt(salt_file: str | Path) -> bytes:
    """Load the per-capture salt, generating and persisting 32 random bytes
    (mode 0600) if the file doesn't exist yet, per §7.5."""
    path = Path(salt_file)
    if path.exists():
        return path.read_bytes()
    salt = secrets.token_bytes(32)
    path.write_bytes(salt)
    os.chmod(path, 0o600)
    return salt


@dataclass
class PseudonymMap:
    """The full pseudonym map for one capture: the salt used to build it, and
    the pseudonym -> real fully-qualified-name mapping (§7.5)."""

    salt: bytes
    mapping: dict[str, str] = field(default_factory=dict)


def write_map_file(pmap: PseudonymMap, path: str | Path) -> str:
    """Write the local-only pseudonym map file (mode 0600) and return the
    sha256 hex digest of its canonical bytes, for the artifact's own
    pseudonym_map_digest field. This file stays with the customer; the
    artifact never carries anything but the digest."""
    payload = {
        "_warning": "KEEP THIS FILE; NOT NEEDED BY SUPABASE",
        "salt_digest": hashlib.sha256(pmap.salt).hexdigest(),
        "map": dict(sorted(pmap.mapping.items())),
    }
    canonical = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    out_path = Path(path)
    out_path.write_text(canonical)
    os.chmod(out_path, 0o600)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _IdentifierRewriter(Visitor):
    """Walks a parsed statement, rewriting relation, column, and user-defined
    function identifiers through identifier_map. pg_catalog / builtin
    functions (anything not present in identifier_map) pass through verbatim,
    per §7.4."""

    def __init__(self, identifier_map: dict[str, str]):
        super().__init__()
        self._map = identifier_map

    def visit_RangeVar(self, ancestors, node):
        if node.schemaname:
            qualified = f"{node.schemaname}.{node.relname}"
            if qualified in self._map:
                node.relname = self._map[qualified]
                node.schemaname = None
                return
        if node.relname in self._map:
            node.relname = self._map[node.relname]
            node.schemaname = None

    def visit_ColumnRef(self, ancestors, node):
        fields = list(node.fields)
        if not fields:
            return
        last = fields[-1]
        name = getattr(last, "sval", None)
        if name is None:
            return
        # A qualified reference (alias.column) is looked up as
        # "qualifier.column" first: the qualifier disambiguates which
        # table's column this is when the same bare column name exists on
        # more than one table in the query (e.g. two joined tables both
        # having an "id" column) -- falling back to the bare name alone
        # would silently collide and either leak the real name or guess
        # wrong. Only when unqualified, or when no qualifier-specific entry
        # exists, does the bare name alone apply.
        if len(fields) >= 2:
            qualifier = getattr(fields[-2], "sval", None)
            if qualifier is not None:
                qualified_key = f"{qualifier}.{name}"
                if qualified_key in self._map:
                    fields[-1] = pglast.ast.String(sval=self._map[qualified_key])
                    node.fields = tuple(fields)
                    return
        if name in self._map:
            fields[-1] = pglast.ast.String(sval=self._map[name])
            node.fields = tuple(fields)

    def visit_ResTarget(self, ancestors, node):
        # An UPDATE ... SET col = expr or INSERT ... (col, ...) target
        # column is stored as ResTarget.name, a plain string field --
        # entirely separate from ResTarget.val (the assigned expression,
        # itself walked and rewritten normally as any other node). Missing
        # this handler was a real leak: "UPDATE t SET real_column = ..."
        # left the assignment target's real name untouched even though
        # every ColumnRef elsewhere in the same statement was pseudonymized.
        if node.name is not None and node.name in self._map:
            node.name = self._map[node.name]

    def visit_FuncCall(self, ancestors, node):
        funcname = node.funcname
        if len(funcname) != 1:
            return
        name = funcname[0].sval
        if name in self._map:
            node.funcname = (pglast.ast.String(sval=self._map[name]),)


def rewrite_query_text(
    sql: str, identifier_map: dict[str, str]
) -> tuple[str | None, bool]:
    """Pseudonymize a normalized (pg_stat_statements-style) query text.

    Comments are removed as a side effect of the parse/deparse round-trip
    (they carry no AST representation). Returns (None, True) for anything
    that fails to parse, per §7.4 -- raw text is never emitted for an
    unparsable statement.
    """
    try:
        tree = pglast.parse_sql(sql)
    except pglast.parser.ParseError:
        return None, True
    _IdentifierRewriter(identifier_map)(tree)
    return RawStream()(tree), False
