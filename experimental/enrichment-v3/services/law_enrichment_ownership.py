"""Pure parser-backed ownership contract for persisted enrichment titles."""

from __future__ import annotations

from typing import Any, Mapping


def is_safe_enrichment_title_ownership(
    article: Mapping[str, Any],
    source_title: str | None,
    tombstone: bool,
) -> bool:
    """Return whether current title/provenance has one proven owner.

    ``source_title`` must be derived by the shared catalog parser from the
    current source bytes.  Tombstones deliberately override a printed header:
    their public title and provenance must both be empty.  A titleless normal
    article may remain titleless or contain a generated title, but an
    unattested/legacy title is never safe.
    """

    if not isinstance(article, Mapping):
        return False
    if source_title is not None and not isinstance(source_title, str):
        return False
    if not isinstance(tombstone, bool):
        return False

    title = article.get("title")
    provenance = article.get("title_provenance")
    if tombstone:
        return title is None and provenance is None
    if source_title is not None:
        return provenance == "source" and title == source_title
    if title is None:
        return provenance is None
    return (
        provenance == "generated"
        and isinstance(title, str)
        and bool(title.strip())
    )
