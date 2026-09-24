"""Independent ground truth for ontology-derived AVUs.

The oracle asks EBI OLS4 directly (stdlib HTTP, not mesa-mcp's client)
and applies the *documented* mapping rule:

    attribute = "<ontology_id lowercased>.<snake(label)>"
    value     = the caller's value, stripped
    unit      = the term's CURIE

It deliberately re-implements the rule instead of importing mesa-mcp's
``_label_to_snake`` / ``ontology_annotations_to_avus``: if mesa-mcp's
behaviour drifts from the documented contract, the e2e run should notice,
not silently agree with itself.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import lru_cache

OLS_BASE = "https://www.ebi.ac.uk/ols4/api/v2"


def snake(label: str) -> str:
    """Documented label → attribute-suffix rule (ASCII alnum + spaces only)."""
    s = re.sub(r"[^a-zA-Z0-9\s]", "", label or "")
    s = re.sub(r"\s+", "_", s.strip())
    return s.lower()


@dataclass(frozen=True)
class Term:
    ontology_id: str
    iri: str
    label: str
    curie: str

    def expected_avu(self, value: str) -> tuple[str, str, str]:
        return (f"{self.ontology_id.lower()}.{snake(self.label)}", value.strip(), self.curie)


class OracleUnavailable(RuntimeError):
    """OLS could not answer — the scenario is skipped, not failed."""


@lru_cache(maxsize=256)
def fetch_term(ontology_id: str, iri: str, base: str = OLS_BASE, timeout: float = 30.0) -> Term:
    encoded = urllib.parse.quote(urllib.parse.quote(iri, safe=""), safe="")
    # ``/entities/`` rather than ``/classes/``: some vocabularies (ROR) are
    # OWL individuals, which the classes endpoint answers with a 404.
    url = f"{base}/ontologies/{ontology_id.lower()}/entities/{encoded}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise OracleUnavailable(f"OLS lookup failed for {ontology_id} {iri}: {exc}") from exc
    label = data.get("label")
    if isinstance(label, list):
        label = label[0] if label else ""
    curie = data.get("curie") or data.get("obo_id") or ""
    if not label:
        raise OracleUnavailable(f"OLS returned no label for {ontology_id} {iri}")
    return Term(ontology_id=ontology_id.lower(), iri=data.get("iri", iri), label=label, curie=curie)
