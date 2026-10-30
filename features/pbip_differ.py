"""Diff engine for the Report Differences feature.

Pure comparison logic operating on the normalized structures produced by
features.pbip_parser.parse_pbip_zip(). No formatting/style diffs — only
structural changes (measures, Power Query M text, relationships, visuals).
"""
import difflib
from typing import Any, Dict, List, Tuple


def _diff_keys(a: dict, b: dict) -> Tuple[List[str], List[str], List[str]]:
    a_keys, b_keys = set(a.keys()), set(b.keys())
    return sorted(b_keys - a_keys), sorted(a_keys - b_keys), sorted(a_keys & b_keys)


def diff_measures(a_measures: Dict[str, Any], b_measures: Dict[str, Any]) -> Dict[str, Any]:
    added, removed, common = _diff_keys(a_measures, b_measures)
    modified = []
    for k in common:
        am, bm = a_measures[k], b_measures[k]
        if ((am.get('expression') or '') != (bm.get('expression') or '')
                or (am.get('formatString') or '') != (bm.get('formatString') or '')
                or (am.get('displayFolder') or '') != (bm.get('displayFolder') or '')):
            modified.append({'key': k, 'table': bm.get('table'), 'name': bm.get('name'),
                              'before': am, 'after': bm})
    return {
        'added': [{'key': k, **b_measures[k]} for k in added],
        'removed': [{'key': k, **a_measures[k]} for k in removed],
        'modified': modified,
    }


def _unified_diff(a_text: str, b_text: str, context: int = 3) -> List[str]:
    a_lines = (a_text or '').splitlines()
    b_lines = (b_text or '').splitlines()
    return list(difflib.unified_diff(a_lines, b_lines, lineterm='', n=context))


def diff_power_query(a_pq: Dict[str, str], b_pq: Dict[str, str]) -> Dict[str, Any]:
    added, removed, common = _diff_keys(a_pq, b_pq)
    modified = []
    for k in common:
        if (a_pq[k] or '') != (b_pq[k] or ''):
            modified.append({'key': k, 'diff': _unified_diff(a_pq[k], b_pq[k])})
    return {
        'added': [{'key': k, 'code': b_pq[k]} for k in added],
        'removed': [{'key': k, 'code': a_pq[k]} for k in removed],
        'modified': modified,
    }


def _rel_key(r: dict) -> Tuple[str, str]:
    return (r.get('fromColumn', ''), r.get('toColumn', ''))


def diff_relationships(a_rels: List[dict], b_rels: List[dict]) -> Dict[str, Any]:
    a_map = {_rel_key(r): r for r in a_rels}
    b_map = {_rel_key(r): r for r in b_rels}
    added, removed, common = _diff_keys(a_map, b_map)
    modified = []
    for k in common:
        ar, br = a_map[k], b_map[k]
        if (ar.get('crossFilteringBehavior') != br.get('crossFilteringBehavior')
                or ar.get('isActive') != br.get('isActive')):
            modified.append({'key': f"{k[0]} -> {k[1]}", 'before': ar, 'after': br})
    return {
        'added': [b_map[k] for k in added],
        'removed': [a_map[k] for k in removed],
        'modified': modified,
    }


def _field_set(fields: List[dict]):
    return {(f.get('table', ''), f.get('name', '')) for f in (fields or [])}


def _field_slot_key(f: dict) -> Tuple[str, str]:
    """Key used to detect 'same visual slot, different underlying column':
    the role (e.g. Axis/Values/Legend) plus the display name shown to the
    user. If a field keeps the same role+displayName but its table/name
    (the actual bound column) changes, that's a silent column swap."""
    return (f.get('role', '') or '', f.get('displayName', '') or f.get('name', '') or '')


def _field_slot_map(fields: List[dict]):
    out = {}
    for f in (fields or []):
        out[_field_slot_key(f)] = (f.get('table', ''), f.get('name', ''))
    return out


def diff_visuals(a_vis: Dict[str, Any], b_vis: Dict[str, Any]) -> Dict[str, Any]:
    added, removed, common = _diff_keys(a_vis, b_vis)
    modified = []
    for k in common:
        av, bv = a_vis[k], b_vis[k]
        changes = {}
        if (av.get('type') or '') != (bv.get('type') or ''):
            changes['type'] = {'before': av.get('type'), 'after': bv.get('type')}
        if (av.get('title') or '') != (bv.get('title') or ''):
            changes['title'] = {'before': av.get('title'), 'after': bv.get('title')}
        fields_added = sorted(_field_set(bv.get('fields')) - _field_set(av.get('fields')))
        fields_removed = sorted(_field_set(av.get('fields')) - _field_set(bv.get('fields')))
        if fields_added or fields_removed:
            changes['fields'] = {
                'added': [{'table': t, 'name': n} for t, n in fields_added],
                'removed': [{'table': t, 'name': n} for t, n in fields_removed],
            }

        # Detect "same slot, different underlying column" — the field shown
        # in the visual keeps the same role + display name, but is now bound
        # to a different table.column than before.
        a_slots = _field_slot_map(av.get('fields'))
        b_slots = _field_slot_map(bv.get('fields'))
        column_swaps = []
        for slot_key, a_col in a_slots.items():
            if slot_key not in b_slots:
                continue
            b_col = b_slots[slot_key]
            if a_col != b_col and a_col[1] and b_col[1]:
                role, display_name = slot_key
                column_swaps.append({
                    'role': role,
                    'displayName': display_name,
                    'before': f"{a_col[0]}.{a_col[1]}".strip('.'),
                    'after': f"{b_col[0]}.{b_col[1]}".strip('.'),
                })
        if column_swaps:
            changes['columnSwaps'] = column_swaps

        if changes:
            modified.append({'key': k, 'page': bv.get('page') or av.get('page'),
                              'title': bv.get('title') or av.get('title'), 'changes': changes})
    return {
        'added': [{'key': k, **b_vis[k]} for k in added],
        'removed': [{'key': k, **a_vis[k]} for k in removed],
        'modified': modified,
    }


def diff_pbip(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Compare two parsed PBIP structures (see pbip_parser.parse_pbip_zip)."""
    return {
        'measures': diff_measures(a.get('measures', {}), b.get('measures', {})),
        'powerQuery': diff_power_query(a.get('power_query', {}), b.get('power_query', {})),
        'relationships': diff_relationships(a.get('relationships', []), b.get('relationships', [])),
        'visuals': diff_visuals(a.get('visuals', {}), b.get('visuals', {})),
    }
