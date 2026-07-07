"""
Identifier normalization for the dependency coverage validator.

Separate from (not a reuse of) autovista.dependency_graph_builder's
_normalize_key -- that helper assumes a single already-clean identifier
string (schema/name values straight from a catalog query, or from
sqlglot's own already-unquoted AST) and just does a whole-string
strip+lower. The validator, by contrast, builds identifiers from raw
sys.sql_expression_dependencies text and needs to strip brackets/quotes
from EACH dot-separated segment independently (e.g. "[dbo].[Person]" is
two bracketed segments, not one bracketed whole string).
"""
from __future__ import annotations

_STRIP_CHARS = "[]\"' \t\n"


def _clean_segment(segment: str) -> str:
    return segment.strip(_STRIP_CHARS).lower()


def normalize_identifier(name: str) -> str:
    """Per-segment bracket/quote/whitespace stripping + lowercase, rejoined
    with '.'. "[dbo].[Person]" and "dbo.Person" and " DBO . PERSON " all
    normalize to "dbo.person"."""
    if not name:
        return ""
    segments = [s for s in name.split(".")]
    return ".".join(_clean_segment(s) for s in segments if _clean_segment(s))


def strict_and_loose_keys(name: str, home_database: str) -> tuple[str, str]:
    """Returns (strict_key, loose_key).

    strict_key: every segment normalized, nothing dropped.
    loose_key: same, but with a leading segment matching home_database
    dropped -- this is what lets "AdventureWorks2022.dbo.Person" and
    "dbo.Person" be recognized as the same object (a representation
    difference, not a real gap) when home_database == "AdventureWorks2022".
    Only drops the leading segment when at least 2 segments remain
    afterwards (schema.object), so a genuine 2-part name is never touched
    and a 4-part linked-server name (whose first segment is a server name,
    not the home database) is also left alone.
    """
    strict = normalize_identifier(name)
    if not strict:
        return strict, strict

    parts = strict.split(".")
    home = _clean_segment(home_database) if home_database else ""
    if home and len(parts) >= 3 and parts[0] == home:
        loose = ".".join(parts[1:])
    else:
        loose = strict
    return strict, loose
