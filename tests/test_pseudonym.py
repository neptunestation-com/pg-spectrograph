"""Tests for pgspec.pseudonym (Milestone 2), written before the implementation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat

import pytest

from pgspec.pseudonym import (
    assign_ordinals,
    digest,
    load_or_create_salt,
    rewrite_query_text,
    write_map_file,
    PseudonymMap,
)

SALT_A = b"a" * 32
SALT_B = b"b" * 32


def _oracle_order(names, salt):
    """Independent digest-sort oracle built from the same stdlib primitives,
    so the test doesn't just re-assert the implementation's own logic."""
    return sorted(
        set(names),
        key=lambda n: hmac.new(salt, n.encode("utf-8"), hashlib.sha256).digest(),
    )


def test_assign_ordinals_matches_digest_sort_order():
    names = [f"public.table_{i}" for i in range(50)]
    expected_order = _oracle_order(names, SALT_A)

    result = assign_ordinals(names, SALT_A, prefix="t")
    # invert {name: pseudonym} -> ordered list of names by ordinal
    by_ordinal = sorted(result.items(), key=lambda kv: kv[1])
    actual_order = [name for name, _pseudonym in by_ordinal]

    assert actual_order == expected_order


def test_assign_ordinals_bijective():
    names = [f"n_{i}" for i in range(1000)]
    result = assign_ordinals(names, SALT_A, prefix="t")
    assert len(result) == 1000
    assert len(set(result.values())) == 1000


def test_assign_ordinals_width_grows_beyond_10000():
    names = [f"n_{i}" for i in range(10_001)]
    result = assign_ordinals(names, SALT_A, prefix="t")
    assert len(set(result.values())) == 10_001
    for pseudonym in result.values():
        suffix = pseudonym.split("_", 1)[1]
        assert len(suffix) == 5


def test_assign_ordinals_different_salt_different_mapping():
    names = [f"n_{i}" for i in range(20)]
    result_a = assign_ordinals(names, SALT_A, prefix="t")
    result_b = assign_ordinals(names, SALT_B, prefix="t")
    assert result_a != result_b


def test_digest_is_hmac_sha256():
    assert digest(SALT_A, "public.orders") == hmac.new(
        SALT_A, b"public.orders", hashlib.sha256
    ).digest()


def test_load_or_create_salt_generates_and_persists(tmp_path):
    salt_file = tmp_path / ".pgspec-salt"
    assert not salt_file.exists()

    first = load_or_create_salt(salt_file)
    assert len(first) == 32
    assert salt_file.exists()
    mode = stat.S_IMODE(salt_file.stat().st_mode)
    assert mode == 0o600

    second = load_or_create_salt(salt_file)
    assert second == first


def test_write_map_file_permissions_and_digest(tmp_path):
    pmap = PseudonymMap(
        salt=SALT_A,
        mapping={"t_0000": "public.orders", "c_0000": "public.orders.customer_id"},
    )
    map_path = tmp_path / "spectrum-map.json"

    returned_digest = write_map_file(pmap, map_path)

    mode = stat.S_IMODE(map_path.stat().st_mode)
    assert mode == 0o600

    raw = map_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == returned_digest

    payload = json.loads(raw)
    assert payload["salt_digest"] == hashlib.sha256(SALT_A).hexdigest()
    assert payload["map"] == {
        "t_0000": "public.orders",
        "c_0000": "public.orders.customer_id",
    }


def test_rewrite_query_text_simple_select():
    sql = "SELECT customer_id, total FROM public.orders WHERE customer_id = $1"
    identifier_map = {
        "public.orders": "t_0007",
        "customer_id": "c_0142",
        "total": "c_0201",
    }
    text, unparsed = rewrite_query_text(sql, identifier_map)
    assert unparsed is False
    assert "orders" not in text
    assert "customer_id" not in text
    assert "total" not in text
    assert "t_0007" in text
    assert "c_0142" in text
    assert "c_0201" in text


def test_rewrite_query_text_keeps_pg_catalog_function_verbatim():
    sql = "SELECT count(*) FROM public.orders"
    identifier_map = {"public.orders": "t_0007"}
    text, unparsed = rewrite_query_text(sql, identifier_map)
    assert unparsed is False
    assert "count" in text


def test_rewrite_query_text_strips_comments():
    sql = "SELECT 1 /* secret_customer_name leak */"
    text, unparsed = rewrite_query_text(sql, {})
    assert unparsed is False
    assert "secret_customer_name" not in text


def test_rewrite_query_text_unparsable_returns_unparsed_flag():
    text, unparsed = rewrite_query_text("SELECT FROM WHERE ???", {})
    assert text is None
    assert unparsed is True
