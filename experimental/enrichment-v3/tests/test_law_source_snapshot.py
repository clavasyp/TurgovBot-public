from __future__ import annotations

import pytest

from services.law_source_snapshot import (
    law_source_content_fingerprint,
    law_source_fingerprint,
    verify_law_source_content_hashes,
)


def _row(content_hash):
    return {
        "id": 7,
        "server_id": "memphis",
        "source_thread_id": 70,
        "title": "Law source",
        "content_hash": content_hash,
    }


@pytest.mark.parametrize("content_hash", [None, "", " \t\r\n"])
def test_strict_source_fingerprint_rejects_missing_content_hash(
    content_hash,
):
    with pytest.raises(ValueError, match="non-empty content_hash"):
        law_source_fingerprint(
            [_row(content_hash)],
            require_content_hash=True,
        )


def test_strict_source_fingerprint_accepts_legacy_opaque_nonempty_hash():
    fingerprint = law_source_fingerprint(
        [_row("legacy-md5-or-opaque-value")],
        require_content_hash=True,
    )

    assert fingerprint["documents"] == 1
    assert len(fingerprint["value"]) == 64


def test_legacy_non_strict_fingerprint_semantics_remain_available():
    # Other already-versioned evidence readers retain their historical
    # canonicalization; high-risk export/apply callers opt into strict mode.
    fingerprint = law_source_fingerprint([_row(None)])

    assert fingerprint["snapshot_version"] == 2
    assert fingerprint["documents"] == 1


def test_v3_source_fingerprint_binds_actual_content_not_stored_hash():
    first = {
        **_row("unchanged-stored-hash"),
        "content": "First exact source content",
    }
    changed = {
        **first,
        "content": "Changed exact source content",
    }

    baseline = law_source_content_fingerprint([first])
    drifted = law_source_content_fingerprint([changed])

    assert baseline["snapshot_version"] == 3
    assert baseline["value"] != drifted["value"]


@pytest.mark.parametrize("field", ["id", "source_thread_id"])
@pytest.mark.parametrize("alias", [True, 7.0, "7"])
def test_source_fingerprints_reject_identity_aliases(field, alias):
    row = {
        **_row("stored"),
        "content": "Exact source",
        field: alias,
    }

    with pytest.raises(ValueError, match="positive integer"):
        law_source_fingerprint([row])
    with pytest.raises(ValueError, match="positive integer"):
        law_source_content_fingerprint([row])


def test_legacy_bridge_verifies_md5_or_sha256_against_actual_content():
    import hashlib

    content = "Exact archived source content"
    encoded = content.encode("utf-8")
    for digest in (
        hashlib.md5(encoded, usedforsecurity=False).hexdigest(),
        hashlib.sha256(encoded).hexdigest(),
    ):
        verified = verify_law_source_content_hashes([
            {
                **_row(digest),
                "content": content,
            }
        ])
        assert verified[0]["content_hash"] == digest


@pytest.mark.parametrize(
    "content_hash",
    (
        "opaque-but-nonempty",
        "0" * 32,
        "0" * 64,
    ),
)
def test_legacy_bridge_rejects_unknown_or_mismatched_stored_hash(
    content_hash,
):
    with pytest.raises(
        ValueError,
        match="MD5 or SHA-256|does not match",
    ):
        verify_law_source_content_hashes([
            {
                **_row(content_hash),
                "content": "Exact archived source content",
            }
        ])
