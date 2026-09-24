"""The scenario catalogue: one entry per ontology-endpoint behaviour.

Each scenario is run twice where possible:

* **scripted** — the ``steps`` are sent straight to mesa-mcp (no LLM), which
  proves the endpoint and the DuckLake pipeline work on their own;
* **llm** — ``prompt`` is handed to the model with only ``tools`` exposed,
  and the end state is checked with the same ``verify``.

``verify`` inspects the end state only (DuckLake + iRODS), never the
model's wording, so a scenario passes for any tool sequence that leaves
the right history behind. ``required_tools`` adds the minimum the model
must have called for the run to count.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .harness.oracle import Term, fetch_term

OBO = "http://purl.obolibrary.org/obo/"
BIOME = OBO + "ENVO_00000428"

OLS_READ_TOOLS = {
    "mesa_ols_list_ontologies",
    "mesa_ols_get_ontology",
    "mesa_ols_search_terms",
    "mesa_ols_get_term",
    "mesa_ols_get_term_hierarchy",
    "mesa_ols_generate_template",
    "mesa_avu_from_term",
}
TAGGING_TOOLS = OLS_READ_TOOLS | {"mesa_avu_apply_term", "ds_list_avus"}


@dataclass
class Step:
    tool: str
    args: dict[str, Any] | Callable[[Any], dict[str, Any]]
    expect_error: str | None = None


@dataclass
class Scenario:
    id: str
    prompt: str | Callable[[Any], str]
    tools: set[str]
    required_tools: set[str]
    steps: list[Step]
    verify: Callable[[Any], list[str]]
    snapshot_delta: int | None = None
    scripted_only: bool = False
    needs_ticket: bool = False
    # How the scripted run answers a term-choice elicitation.
    scripted_choice: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None
    terms: list[tuple[str, str]] = field(default_factory=list)  # (ontology_id, iri) to pre-fetch


# ---------------------------------------------------------------------------
# verify helpers — each returns a list of failure strings
# ---------------------------------------------------------------------------


def _mirrored(ctx: Any, path: str) -> list[str]:
    """iRODS and DuckLake must agree on ``path``'s effective AVU set."""
    irods, lake = ctx.sandbox.irods_avus(path), ctx.lake.avus(path)
    if irods == lake:
        return []
    return [f"{path}: iRODS-only={sorted(irods - lake)} DuckLake-only={sorted(lake - irods)}"]


def _has_triple(ctx: Any, path: str, triple: tuple[str, str, str]) -> list[str]:
    avus = ctx.lake.avus(path)
    return [] if triple in avus else [f"{path}: expected {triple} in DuckLake, got {sorted(avus)}"]


def _last_row_provenance(
    ctx: Any,
    path: str,
    triple: tuple[str, str, str],
    *,
    source: str,
    target_type: str,
    op: str = "add",
) -> list[str]:
    rows = [r for r in ctx.lake.history(path) if (r.attribute, r.value, r.unit) == triple]
    if not rows:
        return [f"{path}: no history row for {triple}"]
    r = rows[0]  # newest first
    fails = []
    if r.op != op:
        fails.append(f"{triple}: op={r.op!r}, expected {op!r}")
    if r.source != source:
        fails.append(f"{triple}: source={r.source!r}, expected {source!r}")
    if r.actor != ctx.sandbox.actor:
        fails.append(f"{triple}: actor={r.actor!r}, expected {ctx.sandbox.actor!r}")
    if r.target_type != target_type:
        fails.append(f"{triple}: target_type={r.target_type!r}, expected {target_type!r}")
    return fails


def _payload_mentions(ctx: Any, index: int, *needles: str) -> list[str]:
    if ctx.tier != "scripted":
        return []
    text = json.dumps(ctx.outcomes[index].payload, default=str)
    return [f"step {index} result lacks {n!r}" for n in needles if n not in text]


def _final_mentions(ctx: Any, *needles: str) -> list[str]:
    if ctx.tier != "llm":
        return []
    return [f"final answer lacks {n!r}" for n in needles if n not in (ctx.final_text or "")]


# ---------------------------------------------------------------------------
# 1–4: read-only OLS tools. They must never create a snapshot.
# ---------------------------------------------------------------------------


def _read_only(
    id_: str,
    prompt: str,
    tools: set[str],
    required: set[str],
    steps: list[Step],
    verify: Callable[[Any], list[str]],
    terms: list[tuple[str, str]] | None = None,
) -> Scenario:
    return Scenario(
        id=id_,
        prompt=prompt,
        tools=tools,
        required_tools=required,
        steps=steps,
        verify=verify,
        snapshot_delta=0,
        terms=terms or [],
    )


READ_ONLY = [
    _read_only(
        "ols_ontologies",
        "Which ontologies does OLS offer, and what is the title of the ENVO ontology?",
        {"mesa_ols_list_ontologies", "mesa_ols_get_ontology"},
        {"mesa_ols_get_ontology"},
        [
            Step("mesa_ols_list_ontologies", {"size": 50}),
            Step("mesa_ols_get_ontology", {"ontology_id": "envo"}),
        ],
        lambda ctx: _payload_mentions(ctx, 1, "envo"),
    ),
    _read_only(
        "ols_search",
        "Find the ENVO term for 'biome', then find ENVO terms mentioning 'forest' that are "
        f"descendants of {BIOME}. Report the CURIE of the biome term.",
        {"mesa_ols_search_terms"},
        {"mesa_ols_search_terms"},
        [
            Step("mesa_ols_search_terms", {"query": "biome"}),
            Step("mesa_ols_search_terms", {"query": "biome", "ontology_id": "envo"}),
            Step(
                "mesa_ols_search_terms",
                {"query": "forest", "ontology_id": "envo", "descendants_of": BIOME},
            ),
        ],
        lambda ctx: (
            _payload_mentions(ctx, 1, BIOME)
            + _payload_mentions(ctx, 2, "iri")
            + _final_mentions(ctx, "ENVO:00000428")
        ),
    ),
    _read_only(
        "ols_term_details",
        f"Look up the ENVO term {BIOME}: give its label, its CURIE, its child terms, and "
        "generate an ENVO metadata template.",
        {"mesa_ols_get_term", "mesa_ols_get_term_hierarchy", "mesa_ols_generate_template"},
        {"mesa_ols_get_term"},
        [
            Step("mesa_ols_get_term", {"ontology_id": "envo", "iri": BIOME}),
            Step("mesa_ols_get_term_hierarchy", {"ontology_id": "envo", "iri": BIOME}),
            Step("mesa_ols_generate_template", {"ontology_id": "envo"}),
        ],
        lambda ctx: (
            _payload_mentions(ctx, 0, ctx.term("envo", BIOME).label, ctx.term("envo", BIOME).curie)
            + _final_mentions(ctx, ctx.term("envo", BIOME).curie)
        ),
        terms=[("envo", BIOME)],
    ),
    _read_only(
        "avu_from_term",
        f"Without writing anything, show the AVU that the ENVO term {BIOME} would produce "
        "for the value 'tropical moist broadleaf forest'.",
        {"mesa_avu_from_term", "mesa_ols_get_term"},
        {"mesa_avu_from_term"},
        [
            Step(
                "mesa_avu_from_term",
                {"ontology_id": "envo", "iri": BIOME, "value": "tropical moist broadleaf forest"},
            )
        ],
        lambda ctx: _payload_mentions(
            ctx, 0, *ctx.term("envo", BIOME).expected_avu("tropical moist broadleaf forest")[::2]
        ),
        terms=[("envo", BIOME)],
    ),
]


# ---------------------------------------------------------------------------
# 5: mesa_avu_apply_term across ontologies
# ---------------------------------------------------------------------------

APPLY_TERMS: list[tuple[str, str, str]] = [
    ("envo", BIOME, "tropical moist broadleaf forest"),
    ("go", OBO + "GO_0006915", "apoptotic process"),
    ("chebi", OBO + "CHEBI_15377", "water"),
    ("uberon", OBO + "UBERON_0000955", "brain"),
    ("pato", OBO + "PATO_0000146", "25 degrees Celsius"),
    ("ncbitaxon", OBO + "NCBITaxon_9606", "Homo sapiens"),
    ("uo", OBO + "UO_0000027", "degree Celsius"),
    ("obi", OBO + "OBI_0000070", "assay"),
    ("ror", "https://ror.org/03m2x1q45", "University of Arizona"),
]


def _apply_term(
    ontology: str, iri: str, value: str, *, on: str = "file", scenario_id: str | None = None
) -> Scenario:
    target_type = "data_object" if on == "file" else "collection"

    def verify(ctx: Any) -> list[str]:
        path = ctx.paths[on]
        triple = ctx.term(ontology, iri).expected_avu(value)
        return (
            _has_triple(ctx, path, triple)
            + _last_row_provenance(
                ctx, path, triple, source="mesa-mcp:mesa_avu_apply_term", target_type=target_type
            )
            + _mirrored(ctx, path)
        )

    def prompt(ctx: Any) -> str:
        t = ctx.term(ontology, iri)
        what = "file" if on == "file" else "collection"
        return (
            f"Tag the {what} {ctx.paths[on]} with the {ontology.upper()} term "
            f"'{t.label}' ({t.curie}), using the value '{value}'."
        )

    return Scenario(
        id=scenario_id or f"apply_term_{ontology}",
        prompt=prompt,
        tools=TAGGING_TOOLS,
        required_tools={"mesa_avu_apply_term"},
        steps=[
            Step(
                "mesa_avu_apply_term",
                lambda ctx: {
                    "path": ctx.paths[on],
                    "ontology_id": ontology,
                    "iri": iri,
                    "value": value,
                },
            )
        ],
        verify=verify,
        snapshot_delta=1,
        terms=[(ontology, iri)],
    )


APPLY = [_apply_term(o, i, v) for o, i, v in APPLY_TERMS] + [
    _apply_term(
        "envo",
        BIOME,
        "tropical moist broadleaf forest",
        on="coll",
        scenario_id="apply_term_envo_collection",
    ),
]


# ---------------------------------------------------------------------------
# 6–7: term resolution edge cases
# ---------------------------------------------------------------------------


def _choose_matching(value: str):
    def choose(message: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        prop = (schema.get("properties") or {}).get("iri") or {}
        options = list(
            zip(prop.get("enum", []), prop.get("enumNames", prop.get("enum", [])), strict=False)
        )
        for iri, label in options:
            if value.lower() in str(label).lower():
                return {"iri": iri}
        return {"iri": options[0][0]} if options else None

    return choose


def _verify_choice(ctx: Any) -> list[str]:
    chosen = [e["content"]["iri"] for e in ctx.elicitations if e.get("content")]
    if not chosen:
        return ["no term was chosen in the elicitation round"]
    triple = fetch_term("envo", chosen[-1]).expected_avu("tropical moist broadleaf forest")
    path = ctx.paths["file"]
    return (
        _has_triple(ctx, path, triple)
        + _last_row_provenance(
            ctx, path, triple, source="mesa-mcp:mesa_avu_apply_term", target_type="data_object"
        )
        + _mirrored(ctx, path)
    )


TERM_EDGES = [
    Scenario(
        id="apply_term_choice",
        prompt=lambda ctx: (
            f"Tag {ctx.paths['file']} with the ENVO biome value "
            "'tropical moist broadleaf forest'. You do not know the term IRI; "
            "let the apply tool offer you the choices."
        ),
        tools={"mesa_avu_apply_term"},
        required_tools={"mesa_avu_apply_term"},
        steps=[
            Step(
                "mesa_avu_apply_term",
                lambda ctx: {
                    "path": ctx.paths["file"],
                    "ontology_id": "envo",
                    "value": "tropical moist broadleaf forest",
                },
            )
        ],
        verify=_verify_choice,
        snapshot_delta=1,
        scripted_choice=_choose_matching("tropical moist broadleaf forest"),
    ),
    Scenario(
        id="apply_term_curie_without_label",
        prompt="",
        tools=set(),
        required_tools=set(),
        steps=[
            Step(
                "mesa_avu_apply_term",
                lambda ctx: {
                    "path": ctx.paths["file"],
                    "ontology_id": "envo",
                    "curie": "ENVO:00000428",
                    "value": "x",
                },
                expect_error="invalid_argument",
            )
        ],
        verify=lambda ctx: (
            []
            if not ctx.lake.avus(ctx.paths["file"])
            else ["a failed apply_term still left AVUs behind"]
        ),
        snapshot_delta=0,
        scripted_only=True,
    ),
    _apply_term(
        "chebi",
        OBO + "CHEBI_16977",
        "L-alanine, 25 °C (±0.5)",
        scenario_id="apply_term_unicode_value",
    ),
]


# ---------------------------------------------------------------------------
# 8–10: batching, deletes, time travel
# ---------------------------------------------------------------------------

BATCH_TERMS = [
    ("envo", BIOME, "tropical moist broadleaf forest"),
    ("chebi", OBO + "CHEBI_15377", "water"),
    ("uo", OBO + "UO_0000027", "degree Celsius"),
]


def _batch_triples(ctx: Any) -> list[tuple[str, str, str]]:
    return [ctx.term(o, i).expected_avu(v) for o, i, v in BATCH_TERMS]


def _verify_batch(ctx: Any) -> list[str]:
    path = ctx.paths["file"]
    triples = _batch_triples(ctx)
    fails = [f for t in triples for f in _has_triple(ctx, path, t)]
    rows = [r for r in ctx.lake.history(path) if (r.attribute, r.value, r.unit) in set(triples)]
    snaps = {r.snapshot_id for r in rows}
    if len(snaps) != 1:
        fails.append(f"batch spread over snapshots {sorted(snaps)}; expected exactly one")
    fails += [
        f"{r.attribute}: source={r.source!r}" for r in rows if r.source != "mesa-mcp:ds_add_avus"
    ]
    return fails + _mirrored(ctx, path)


def _verify_delete_time_travel(ctx: Any) -> list[str]:
    path = ctx.paths["file"]
    triple = ctx.term("envo", BIOME).expected_avu("tropical moist broadleaf forest")
    rows = [r for r in ctx.lake.history(path) if (r.attribute, r.value, r.unit) == triple]
    ops = [r.op for r in reversed(rows)]  # chronological
    fails = []
    if ops != ["add", "delete"]:
        return [f"history ops for {triple} = {ops}; expected ['add', 'delete']"]
    add, delete = rows[1], rows[0]
    if triple in ctx.lake.avus(path):
        fails.append("deleted AVU is still in the effective set")
    if triple not in ctx.lake.avus_as_of(path, add.ts):
        fails.append("time travel to the add's timestamp does not show the AVU")
    diff = ctx.lake.diff(add.snapshot_id, delete.snapshot_id)
    if not any(d.op == "delete" and (d.attribute, d.value, d.unit) == triple for d in diff):
        fails.append(f"diff({add.snapshot_id},{delete.snapshot_id}) lacks the delete")
    return fails + _mirrored(ctx, path)


HISTORY = [
    Scenario(
        id="batch_add_avus",
        prompt=lambda ctx: (
            "In ONE batched call, add these three AVUs to "
            f"{ctx.paths['file']} (attribute, value, unit): "
            + "; ".join(f"({a}, {v}, {u})" for a, v, u in _batch_triples(ctx))
        ),
        tools={"ds_add_avus", "ds_list_avus"},
        required_tools={"ds_add_avus"},
        steps=[
            Step(
                "ds_add_avus",
                lambda ctx: {
                    "target": ctx.paths["file"],
                    "avus": [
                        {"attribute": a, "value": v, "unit": u} for a, v, u in _batch_triples(ctx)
                    ],
                },
            )
        ],
        verify=_verify_batch,
        snapshot_delta=1,
        terms=[(o, i) for o, i, _ in BATCH_TERMS],
    ),
    Scenario(
        id="delete_and_time_travel",
        prompt=lambda ctx: (
            f"Tag {ctx.paths['file']} with the ENVO term {BIOME} using the value "
            "'tropical moist broadleaf forest'. Then remove exactly that AVU "
            "(same attribute, value and unit) again."
        ),
        tools=TAGGING_TOOLS | {"ds_delete_avu"},
        required_tools={"mesa_avu_apply_term", "ds_delete_avu"},
        steps=[
            Step(
                "mesa_avu_apply_term",
                lambda ctx: {
                    "path": ctx.paths["file"],
                    "ontology_id": "envo",
                    "iri": BIOME,
                    "value": "tropical moist broadleaf forest",
                },
            ),
            Step(
                "ds_delete_avu",
                lambda ctx: dict(
                    zip(
                        ("attribute", "value", "unit"),
                        ctx.term("envo", BIOME).expected_avu("tropical moist broadleaf forest"),
                        strict=True,
                    ),
                    target_type="path",
                    target=ctx.paths["file"],
                ),
            ),
        ],
        verify=_verify_delete_time_travel,
        snapshot_delta=2,
        terms=[("envo", BIOME)],
    ),
]


# ---------------------------------------------------------------------------
# 11–13: DataCite, policies, ticket provenance
# ---------------------------------------------------------------------------

DATACITE_RECORD = {
    "identifier": "10.5072/mesa-e2e",
    "identifierType": "DOI",
    "titles": ["MESA e2e fixture"],
    "creators": [{"name": "Tester, E2E"}],
    "publisher": "CyVerse",
    "publicationYear": 2026,
    "resourceTypeGeneral": "Dataset",
}


def _verify_datacite(ctx: Any) -> list[str]:
    path = ctx.paths["file"]
    avus = ctx.lake.avus(path)
    fails = (
        []
        if ("datacite.identifier", "10.5072/mesa-e2e", "") in avus
        else [f"datacite.identifier missing; DuckLake has {sorted(avus)}"]
    )
    rows = ctx.lake.history(path)
    fails += [
        f"{r.attribute}: unit={r.unit!r} (DataCite AVUs carry no unit)" for r in rows if r.unit
    ]
    fails += [
        f"{r.attribute}: source={r.source!r}"
        for r in rows
        if r.source != "mesa-mcp:mesa_avu_apply_datacite"
    ]
    if len({r.snapshot_id for r in rows}) != 1:
        fails.append("DataCite apply spread over more than one snapshot")
    return fails + _mirrored(ctx, path)


def _verify_policy(ctx: Any) -> list[str]:
    attr = f"mesa.policy.{ctx.policy_name}"
    rows = [r for r in ctx.lake.history(ctx.sandbox.root) if r.attribute == attr]
    got = [(r.op, r.source) for r in reversed(rows)]
    want = [("add", "mesa-mcp:mesa_policy_enable"), ("delete", "mesa-mcp:mesa_policy_disable")]
    fails = [] if got == want else [f"policy history {got}; expected {want}"]
    if any(a == attr for a, _, _ in ctx.lake.avus(ctx.sandbox.root)):
        fails.append("disabled policy is still effective in DuckLake")
    if any(a == attr for a, _, _ in ctx.sandbox.irods_avus(ctx.sandbox.root)):
        fails.append("disabled policy AVU still on the collection in iRODS")
    return fails


def _verify_ticket(ctx: Any) -> list[str]:
    path = ctx.paths["file"]
    rows = [r for r in ctx.lake.history(path) if r.attribute == "mesa_e2e.ticket_check"]
    if not rows:
        return ["no DuckLake row for the ticket-mediated AVU"]
    return (
        []
        if rows[0].via_ticket == ctx.ticket
        else [f"via_ticket={rows[0].via_ticket!r}, expected the ticket in use"]
    ) + _mirrored(ctx, path)


PROVENANCE = [
    Scenario(
        id="datacite_apply",
        prompt=lambda ctx: (
            f"Attach DataCite metadata to {ctx.paths['file']} using canonical "
            "naming: DOI 10.5072/mesa-e2e, title 'MESA e2e fixture', creator "
            "'Tester, E2E', publisher CyVerse, year 2026, resource type Dataset. "
            "Check the template first."
        ),
        tools={"mesa_datacite_template", "mesa_datacite_validate", "mesa_avu_apply_datacite"},
        required_tools={"mesa_avu_apply_datacite"},
        steps=[
            Step("mesa_datacite_template", {}),
            Step(
                "mesa_avu_apply_datacite",
                lambda ctx: {
                    "target": ctx.paths["file"],
                    "record": DATACITE_RECORD,
                    "naming": "canonical",
                },
            ),
        ],
        verify=_verify_datacite,
        snapshot_delta=1,
    ),
    Scenario(
        id="policy_toggle",
        prompt=lambda ctx: (
            f"Enable the MESA policy '{ctx.policy_name}' on the project "
            f"{ctx.sandbox.root}, then disable it again."
        ),
        tools={"mesa_policy_enable", "mesa_policy_disable", "ds_list_policies"},
        required_tools={"mesa_policy_enable", "mesa_policy_disable"},
        steps=[
            Step(
                "mesa_policy_enable",
                lambda ctx: {"project_path": ctx.sandbox.root, "policy_name": ctx.policy_name},
            ),
            Step(
                "mesa_policy_disable",
                lambda ctx: {"project_path": ctx.sandbox.root, "policy_name": ctx.policy_name},
            ),
        ],
        verify=_verify_policy,
        snapshot_delta=2,
    ),
    Scenario(
        id="ticket_provenance",
        prompt=lambda ctx: (
            f"Use the iRODS ticket {ctx.ticket}, then add the AVU "
            f"(mesa_e2e.ticket_check, yes, no unit) to {ctx.paths['file']}."
        ),
        tools={"ds_use_ticket", "ds_add_avu"},
        required_tools={"ds_use_ticket", "ds_add_avu"},
        steps=[
            Step("ds_use_ticket", lambda ctx: {"ticket": ctx.ticket}),
            Step(
                "ds_add_avu",
                lambda ctx: {
                    "target_type": "path",
                    "target": ctx.paths["file"],
                    "attribute": "mesa_e2e.ticket_check",
                    "value": "yes",
                },
            ),
        ],
        verify=_verify_ticket,
        snapshot_delta=1,
        needs_ticket=True,
    ),
]


SCENARIOS: list[Scenario] = READ_ONLY + APPLY + TERM_EDGES + HISTORY + PROVENANCE
SCENARIO_IDS = [s.id for s in SCENARIOS]


def prefetch_terms(scn: Scenario) -> dict[tuple[str, str], Term]:
    return {(o, i): fetch_term(o, i) for o, i in scn.terms}
