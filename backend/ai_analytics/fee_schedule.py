"""Step-1 category & fee item identification against department fee schedules.

Shared pure logic (no I/O) consumed by both the direct-read analytics path
and the projection read path. ``STEP1_SOURCE_FIELDS`` is the single list of
``ai_line_items`` fields that carry step-1 intake/identification evidence;
it is projected by ``mongo_repository.SUMMARY_PROJECTION``, preserved by the
worker projection builder, and passed through the projection read adapter so
both paths expose identical fields to ``identify_fee_result``.

``identify_fee_result`` compares a record's source output
(``billing_category`` / ``billing_level`` / ``level_label_matched``) against
the department's *current* fee catalog (a compact list of
``fees_resources_final`` tiles built by ``compact_fee_catalog`` and attached
to the participation map by ``get_ai_participation_map``). Matching ignores
case and whitespace only — no synonym, level, or historical fee inference.
``billing_level`` may carry a fee *item* name (e.g. "Vehicle Fire"), not a
numbered level. Category-only results are valid selections of a category.

Catalog versioning: ``department_config['fee_catalog_version']`` must equal
``FEE_CATALOG_VERSION`` for matching to run. ``fee_catalog is None`` means
the department has multiple *differing* finalized fee schedule documents and
no authoritative catalog could be selected — the result is
``configuration_ambiguous`` rather than a guess.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ai_line_items fields carrying step-1 intake/identification evidence.
STEP1_SOURCE_FIELDS = (
    'billing_level',
    'level_identification_confidence',
    'level_identification_low_confidence',
    'level_identification_reasoning',
    'intake_status',
    'intake_evaluated_at',
    'intake_evaluation_count',
    'level_label_matched',
)

# Version of the ``fee_catalog`` shape produced by ``compact_fee_catalog``
# and attached to participation entries. Bumped if the tile shape changes.
FEE_CATALOG_VERSION = 1


def step1_source_fields(record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract the STEP1_SOURCE_FIELDS subset from a source record."""
    return {field: (record or {}).get(field) for field in STEP1_SOURCE_FIELDS}


def _text(value: Any) -> str:
    """Collapse whitespace in a string; anything else becomes ''."""
    return ' '.join(value.split()) if isinstance(value, str) else ''


def _key(value: Any) -> str:
    """Case/whitespace-insensitive match key for a text value."""
    return _text(value).casefold()


def compact_fee_catalog(fees: Any) -> List[Dict[str, str]]:
    """Reduce finalized fee tiles to the fields needed for result matching.

    Keeps only ``item`` / ``fee_category`` / ``fee_label_from_document`` and
    drops tiles with neither an item nor a category. Non-list input yields
    ``[]`` (the participation builder is the only producer, but stay
    defensive).
    """
    if not isinstance(fees, list):
        return []
    return [
        {
            key: _text(tile.get(key))
            for key in ('item', 'fee_category', 'fee_label_from_document')
        }
        for tile in fees
        if isinstance(tile, dict)
        and (_text(tile.get('item')) or _text(tile.get('fee_category')))
    ]


def identify_fee_result(
    record: Optional[Dict[str, Any]],
    department_config: Optional[Dict[str, Any]],
    config_status: str = 'available',
) -> Dict[str, Any]:
    """Describe source identification against this department's current catalog.

    Matching ignores case and whitespace only. No synonym, level, or
    historical fee inference. Category-only results are valid selections of
    a category.

    Returns ``{'label', 'match_status', 'description'}`` where
    ``match_status`` is one of: ``matched``, ``category_matched``,
    ``ambiguous``, ``unmatched``, ``configuration_unavailable``,
    ``configuration_ambiguous``, ``not_identified``.
    """
    record = record or {}
    category = _text(record.get('billing_category'))
    item = _text(record.get('billing_level')) or _text(
        record.get('level_label_matched')
    )
    intake = _text(record.get('intake_status'))
    intake_labels = {
        'IDENTIFIED': 'Identified',
        'CATEGORY_ONLY': 'Category identified',
        'NO_LEVELS_CONFIGURED': 'No levels configured',
        'RECYCLED': 'Reused result',
    }

    def result(label: str, status: str, description: str) -> Dict[str, Any]:
        if intake:
            description += ' · Intake: ' + intake_labels.get(
                intake, intake.replace('_', ' ').capitalize()
            )
        if record.get('level_identification_low_confidence'):
            description += ' · Low confidence'
        if config_status == 'stale':
            description += ' · Fee configuration is out of date'
        return {'label': label, 'match_status': status, 'description': description}

    def label(cat: str, fee: str) -> str:
        if cat and fee and _key(cat) != _key(fee):
            return f'{cat} / {fee}'
        return fee or cat

    raw_label = label(category, item)
    if not raw_label:
        return result(
            'Not identified', 'not_identified', 'No category or fee item recorded'
        )
    if (
        not department_config
        or department_config.get('fee_catalog_version') != FEE_CATALOG_VERSION
    ):
        return result(
            raw_label, 'configuration_unavailable', 'Fee schedule match unavailable'
        )
    catalog = department_config.get('fee_catalog')
    if catalog is None:
        return result(
            raw_label,
            'configuration_ambiguous',
            'Multiple department fee schedules; match unresolved',
        )
    category_tiles = [
        t
        for t in catalog
        if not category or _key(t.get('fee_category')) == _key(category)
    ]
    if not item:
        if category_tiles:
            canonical = sorted(
                {_text(t.get('fee_category')) for t in category_tiles}
            )[0]
            return result(
                canonical,
                'category_matched',
                'Category matches current fee schedule',
            )
        return result(
            raw_label, 'unmatched', 'Not matched to current fee schedule'
        )
    # Collect matches across both the canonical item name and the
    # document-label alias in one pass — a label that names one tile's
    # item and another tile's alias is genuinely ambiguous, and falling
    # back to the alias scan only when the item scan misses would hide
    # that.
    matches = [
        t
        for t in category_tiles
        if _key(t.get('item')) == _key(item)
        or _key(t.get('fee_label_from_document')) == _key(item)
    ]
    # Distinct fee selections are counted on normalized keys so tiles
    # that differ only by case/whitespace collapse into one selection
    # instead of reading as ambiguous.
    selection_keys = {
        (_key(t.get('fee_category')), _key(t.get('item'))) for t in matches
    }
    if len(selection_keys) == 1:
        key = next(iter(selection_keys))
        # Deterministic display label among equivalent spellings.
        cat, fee = min(
            (_text(t.get('fee_category')), _text(t.get('item')))
            for t in matches
            if (_key(t.get('fee_category')), _key(t.get('item'))) == key
        )
        return result(
            label(cat, fee),
            'matched',
            'Category and fee item match current fee schedule',
        )
    if len(selection_keys) > 1:
        return result(
            raw_label,
            'ambiguous',
            'More than one fee item matches; selection unresolved',
        )
    return result(raw_label, 'unmatched', 'Not matched to current fee schedule')
