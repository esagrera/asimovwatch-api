"""
Motor de cerca avancada per a public.entries.

Abast (decisio de producte, 2026-09-12):
- El constructor de regles combinables AND/OR nomes s'aplica als 3 eixos
  BIHP (human_protection_declared/verifiable/depth). Provider/Model es
  gestionen fora d'aquest motor, amb els <select> encadenats existents.
- Els filtres "basics" (review_status, risk_level, source_type,
  country_region, institution_type, needs_info) es passen com a camps
  plans al costat del filtre BIHP, no com a "rules" del constructor.
  Es converteixen internament amb AND implicit, reutilitzant la mateixa
  normalitzacio de valors que list_entries()/aggregate_entries() a main.py.
- Aquest endpoint (POST /entries/search) nomes cal invocar-lo des del
  frontend quan hi ha almenys una regla BIHP activa. Sense regles BIHP,
  GET /entries ja cobreix el cas normal i no cal duplicar-lo aqui.

Seguretat (obligatori, no opcional):
- Cap camp, operador ni valor arriba mai concatenat a una cadena SQL.
- Els noms de columna es validen contra llistes blanques abans d'entrar
  a cap f-string de SQL.
- Els valors sempre viatgen com a parametres psycopg2 (%s).
"""

from typing import Any, Dict, List, Optional, Tuple
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

BIHP_ALLOWED_VALUES = {"green", "yellow", "red", "unknown"}
REVIEW_STATUS_ALLOWED_VALUES = {"NEW", "IN_REVIEW", "APPROVED", "REJECTED"}

# ---------------------------------------------------------------------------
# Constructor de regles combinables: NOMES eixos BIHP.
# ---------------------------------------------------------------------------

BIHP_FIELDS = {
    "human_protection_declared",
    "human_protection_verifiable",
    "human_protection_depth",
}

BIHP_OPERATORS = [
    "equals", "not_equals", "is_any_of", "is_none_of", "is_empty", "is_not_empty",
]

OPERATORS_REQUIRING_VALUE = {"equals", "not_equals"}
OPERATORS_REQUIRING_LIST = {"is_any_of", "is_none_of"}
OPERATORS_NO_VALUE = {"is_empty", "is_not_empty"}

SORTABLE_FIELDS = {"detected_at", "ingested_at", "id", "published_date", "updated_at"}
SORT_DIRECTIONS = {"asc", "desc"}


class FilterRule(BaseModel):
    field: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[Any] = None
    group_operator: Optional[str] = None
    rules: Optional[List["FilterRule"]] = None

    @field_validator("group_operator")
    @classmethod
    def validate_group_operator(cls, v):
        if v is not None and v.upper() not in {"AND", "OR"}:
            raise ValueError("group_operator ha de ser 'AND' o 'OR'")
        return v.upper() if v else v


FilterRule.model_rebuild()


class FilterGroup(BaseModel):
    root_operator: str = Field(default="AND")
    rules: List[FilterRule] = Field(default_factory=list)

    @field_validator("root_operator")
    @classmethod
    def validate_root_operator(cls, v):
        if v.upper() not in {"AND", "OR"}:
            raise ValueError("root_operator ha de ser 'AND' o 'OR'")
        return v.upper()


class SortSpec(BaseModel):
    field: str = "detected_at"
    direction: str = "desc"

    @field_validator("field")
    @classmethod
    def validate_field(cls, v):
        if v not in SORTABLE_FIELDS:
            raise ValueError(f"sort.field no vàlid. Permès: {sorted(SORTABLE_FIELDS)}")
        return v

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v):
        if v.lower() not in SORT_DIRECTIONS:
            raise ValueError("sort.direction ha de ser 'asc' o 'desc'")
        return v.lower()


class EntriesSearchRequest(BaseModel):
    """Cos de POST /entries/search.

    Els camps "basics" son opcionals i es combinen amb AND implicit al
    costat del filtre BIHP (que pot ser buit -> nomes bàsics, equivalent
    a GET /entries)."""
    limit: int = 20
    offset: int = 0
    sort: SortSpec = Field(default_factory=SortSpec)
    filter: FilterGroup = Field(default_factory=FilterGroup)

    # --- Filtres basics (mateixa semantica que list_entries a main.py) ---
    review_status: Optional[str] = None
    risk_level: Optional[str] = None
    source_type: Optional[str] = None
    country_region: Optional[str] = None
    institution_type: Optional[str] = None
    needs_info: Optional[bool] = None
    processing_status: Optional[str] = None
    q: Optional[str] = None

    # --- Provider / Model (fora del constructor, pero combinables aqui) ---
    analyzed_provider: Optional[str] = None
    analyzed_model: Optional[str] = None


def _normalize_bihp_value(field: str, value: Any) -> str:
    v = str(value).strip().lower()
    if v not in BIHP_ALLOWED_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"Valor no vàlid per a {field}: '{value}'. Permès: {sorted(BIHP_ALLOWED_VALUES)}",
        )
    return v


def build_condition_sql(rule: FilterRule, params: List[Any]) -> str:
    """Construeix el fragment SQL (amb placeholders %s) per a una condicio
    de fulla. Nomes accepta camps BIHP: llista blanca estricta."""
    field = rule.field
    operator = rule.operator

    if field not in BIHP_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"Camp de filtre BIHP no permès: '{field}'. Permès: {sorted(BIHP_FIELDS)}",
        )
    if operator not in BIHP_OPERATORS:
        raise HTTPException(
            status_code=400,
            detail=f"Operador '{operator}' no permès per a camps BIHP. Permès: {BIHP_OPERATORS}",
        )

    if operator in OPERATORS_NO_VALUE:
        if operator == "is_empty":
            return f"({field} IS NULL OR BTRIM({field}) = '')"
        return f"({field} IS NOT NULL AND BTRIM({field}) <> '')"

    if operator in OPERATORS_REQUIRING_LIST:
        raw_values = rule.value
        if not isinstance(raw_values, list) or not raw_values:
            raise HTTPException(
                status_code=400,
                detail=f"L'operador '{operator}' requereix 'value' com a llista no buida",
            )
        normalized = [_normalize_bihp_value(field, v) for v in raw_values]
        placeholders = ", ".join(["%s"] * len(normalized))
        params.extend(normalized)
        if operator == "is_any_of":
            return f"{field} IN ({placeholders})"
        return f"({field} IS NULL OR {field} NOT IN ({placeholders}))"

    if operator in OPERATORS_REQUIRING_VALUE:
        if rule.value is None:
            raise HTTPException(
                status_code=400,
                detail=f"L'operador '{operator}' requereix un 'value' per al camp '{field}'",
            )
        normalized = _normalize_bihp_value(field, rule.value)
        params.append(normalized)
        if operator == "equals":
            return f"{field} = %s"
        return f"({field} IS NULL OR {field} <> %s)"

    raise HTTPException(status_code=500, detail=f"Operador no gestionat: {operator}")


def build_group_sql(group: "FilterGroup | FilterRule", params: List[Any], depth: int = 0) -> Optional[str]:
    """Construeix recursivament el SQL d'un grup de regles BIHP.
    Suporta un unic nivell addicional de subgrups (depth<=2)."""
    if depth > 2:
        raise HTTPException(status_code=400, detail="Massa nivells d'agrupació de regles (màxim 2)")

    if isinstance(group, FilterGroup):
        operator = group.root_operator
        rules = group.rules
    else:
        operator = group.group_operator or "AND"
        rules = group.rules or []

    fragments: List[str] = []
    for rule in rules:
        if rule.rules is not None:
            sub_sql = build_group_sql(rule, params, depth + 1)
            if sub_sql:
                fragments.append(f"({sub_sql})")
        elif rule.field is not None and rule.operator is not None:
            fragments.append(build_condition_sql(rule, params))

    if not fragments:
        return None

    joiner = f" {operator} "
    return joiner.join(fragments)


def _build_basic_filters_sql(body: EntriesSearchRequest, params: List[Any]) -> List[str]:
    """Reutilitza la mateixa semantica de normalitzacio que list_entries()
    a main.py per als camps basics + provider/model."""
    fragments: List[str] = []

    if body.review_status:
        v = body.review_status.strip().upper()
        if v not in REVIEW_STATUS_ALLOWED_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"review_status no vàlid: '{body.review_status}'. Permès: {sorted(REVIEW_STATUS_ALLOWED_VALUES)}",
            )
        fragments.append("review_status = %s")
        params.append(v)

    if body.risk_level:
        fragments.append("risk_level = %s")
        params.append(body.risk_level.strip().lower())

    if body.source_type:
        fragments.append("source_type = %s")
        params.append(body.source_type.strip().lower())

    if body.country_region:
        fragments.append("LOWER(country_region) = LOWER(%s)")
        params.append(body.country_region.strip())

    if body.institution_type:
        fragments.append("LOWER(institution_type) = LOWER(%s)")
        params.append(body.institution_type.strip())

    if body.needs_info is not None:
        fragments.append("needs_info = %s")
        params.append(body.needs_info)

    if body.processing_status:
        fragments.append("processing_status = %s")
        params.append(body.processing_status.strip().upper())

    if body.analyzed_provider:
        fragments.append("LOWER(analyzed_provider) = LOWER(%s)")
        params.append(body.analyzed_provider.strip())

    if body.analyzed_model:
        fragments.append("LOWER(analyzed_model) = LOWER(%s)")
        params.append(body.analyzed_model.strip())

    if body.q:
        fragments.append(
            "(LOWER(source_title) LIKE LOWER(%s) "
            "OR LOWER(raw_snippet) LIKE LOWER(%s) "
            "OR LOWER(summary_factual) LIKE LOWER(%s) "
            "OR LOWER(summary_factual_ca) LIKE LOWER(%s) "
            "OR LOWER(summary_factual_en) LIKE LOWER(%s))"
        )
        like_q = f"%{body.q}%"
        params.extend([like_q, like_q, like_q, like_q, like_q])

    return fragments


def build_entries_search_query(body: EntriesSearchRequest) -> Tuple[str, str, List[Any], List[Any], int]:
    """Retorna (count_query, data_query, count_params, data_params, safe_limit).

    Combina amb AND implicit: (bàsics + provider/model) AND (filtre BIHP).
    Si el filtre BIHP és buit, equival a nomes els bàsics."""
    safe_limit = min(max(body.limit, 1), 200)
    safe_offset = max(body.offset, 0)

    def build_where(params: List[Any]) -> str:
        fragments = _build_basic_filters_sql(body, params)
        bihp_sql = build_group_sql(body.filter, params)
        if bihp_sql:
            fragments.append(f"({bihp_sql})")
        return f"WHERE {' AND '.join(fragments)}" if fragments else ""

    count_params: List[Any] = []
    where_clause = build_where(count_params)
    count_query = f"SELECT COUNT(*) AS total FROM public.entries {where_clause}"

    data_params: List[Any] = []
    where_clause_data = build_where(data_params)

    sort_field = body.sort.field
    sort_dir = body.sort.direction.upper()

    data_query = f"""
        SELECT
            id, source_url, source_domain, source_title, source_type,
            source_language, country_region, institution_type, risk_level,
            review_status, reviewer, needs_info, published_date, detected_at,
            ingested_at, ingest_status, summary_factual, theme_tags,
            affected_principles, processing_status, relevance_score,
            relevance_reason, analyzed_provider, analyzed_model,
            human_protection_declared, human_protection_verifiable,
            human_protection_depth, enriched_at, enriched_model,
            summary_factual_ca, summary_factual_en,
            why_it_matters_ca, why_it_matters_en,
            debate_questions_ca, debate_questions_en,
            human_protection_notes_ca, human_protection_notes_en
        FROM public.entries
        {where_clause_data}
        ORDER BY {sort_field} {sort_dir} NULLS LAST, id DESC
        LIMIT %s OFFSET %s
    """
    data_params_with_page = data_params + [safe_limit, safe_offset]

    return count_query, data_query, count_params, data_params_with_page, safe_limit
