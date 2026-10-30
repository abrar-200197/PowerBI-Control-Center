"""Parse a PBIP project zip (.Report + .SemanticModel folders) into a
normalized structure used by the Report Differences feature.

No network calls — pure local parsing of TMDL text + PBIR visual JSON
already present inside the uploaded zip.
"""
import os
import re
import zipfile
from typing import Any, Dict, Optional, Tuple

_METADATA_KEYS = ('formatString:', 'displayFolder:', 'lineageTag:', 'isHidden',
                  'annotation', 'dataCategory:', 'description:')
_MEASURE_RE = re.compile(r"^measure\s+('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\S+)\s*=\s*(.*)$")


def _tmdl_indent(line: str) -> int:
    return len(line) - len(line.lstrip('\t'))


def _build_tmdl_tree(text: str):
    """Build a tree of {'text': str, 'children': [...]} nodes from TMDL text
    using tab-indentation (TMDL's native indentation style)."""
    roots = []
    stack = []  # list of (indent, node)
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith('///'):
            continue
        indent = _tmdl_indent(raw)
        node = {'text': raw.strip(), 'children': []}
        while stack and stack[-1][0] >= indent:
            stack.pop()
        (stack[-1][1]['children'] if stack else roots).append(node)
        stack.append((indent, node))
    return roots


def _flatten_text(node, skip_keys: Tuple[str, ...] = ()) -> str:
    lines = []

    def walk(n):
        for child in n['children']:
            if any(child['text'].startswith(k) for k in skip_keys):
                continue
            lines.append(child['text'])
            walk(child)

    walk(node)
    return '\n'.join(lines)


def _parse_measure_node(node) -> Optional[Dict[str, Any]]:
    m = _MEASURE_RE.match(node['text'])
    if not m:
        return None
    name = m.group(1).strip().strip("'\"")
    first_line = m.group(2).strip()
    extra = _flatten_text(node, skip_keys=_METADATA_KEYS)
    expression = (first_line + '\n' + extra).strip() if (first_line and extra) else (first_line or extra)
    fmt = folder = None
    for child in node['children']:
        if child['text'].startswith('formatString:'):
            fmt = child['text'].split(':', 1)[1].strip().strip('"')
        elif child['text'].startswith('displayFolder:'):
            folder = child['text'].split(':', 1)[1].strip().strip('"')
    return {'name': name, 'expression': expression.strip(), 'formatString': fmt, 'displayFolder': folder}


def _parse_relationship_node(node) -> Dict[str, Any]:
    kv = {}
    for child in node['children']:
        if ':' in child['text']:
            k, v = child['text'].split(':', 1)
            kv[k.strip()] = v.strip().strip('"')
    return {
        'id': node['text'][len('relationship'):].strip(),
        'fromColumn': kv.get('fromColumn', ''),
        'toColumn': kv.get('toColumn', ''),
        'crossFilteringBehavior': kv.get('crossFilteringBehavior', 'singleDirection'),
        'isActive': kv.get('isActive', 'true').lower() != 'false',
    }


def parse_tmdl_tables(text: str):
    """Returns (measures: dict[table.name]->info, power_query: dict[table]->m_code)."""
    measures, power_query = {}, {}
    for root in _build_tmdl_tree(text):
        if not root['text'].startswith('table '):
            continue
        table_name = root['text'][len('table'):].strip().strip("'\"")
        for child in root['children']:
            txt = child['text']
            if txt.startswith('measure '):
                info = _parse_measure_node(child)
                if info:
                    measures[f"{table_name}.{info['name']}"] = {'table': table_name, **info}
            elif txt.startswith('partition '):
                for pchild in child['children']:
                    if pchild['text'].startswith('source'):
                        m = re.match(r'^source\s*=\s*(.*)$', pchild['text'])
                        first = m.group(1).strip() if m else ''
                        extra = _flatten_text(pchild)
                        m_code = (first + '\n' + extra).strip() if (first and extra) else (first or extra)
                        if m_code:
                            power_query[table_name] = m_code
    return measures, power_query


def parse_tmdl_relationships(text: str):
    return [_parse_relationship_node(root) for root in _build_tmdl_tree(text)
            if root['text'].startswith('relationship')]


_EXPRESSION_RE = re.compile(r"^expression\s+('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\S+)\s*=\s*(.*)$")


def parse_tmdl_expressions(text: str):
    """Parse expressions.tmdl: shared/staging Power Query queries that are
    not loaded into any table (e.g. 'Enable Load' turned off). Returns
    dict[expression_name] -> m_code, using the same shape as table-bound
    Power Query entries so they flow through the same diff/text-compare
    logic as table partitions."""
    power_query = {}
    for root in _build_tmdl_tree(text):
        m = _EXPRESSION_RE.match(root['text'])
        if not m:
            continue
        name = m.group(1).strip().strip("'\"")
        first_line = m.group(2).strip()
        extra = _flatten_text(root, skip_keys=_METADATA_KEYS + ('lineageTag:', 'queryGroup:'))
        m_code = (first_line + '\n' + extra).strip() if (first_line and extra) else (first_line or extra)
        if m_code:
            power_query[name] = m_code
    return power_query


def parse_pbip_zip(zip_path: str) -> Dict[str, Any]:
    """Parse an uploaded PBIP project zip into normalized measures,
    Power Query (M) text, relationships, and visuals (keyed by GUID)."""
    measures: Dict[str, Any] = {}
    power_query: Dict[str, str] = {}
    relationships = []
    visuals: Dict[str, Any] = {}

    with zipfile.ZipFile(zip_path, 'r') as zf:
        names = zf.namelist()

        for n in names:
            nl = n.lower()
            if not nl.endswith('.tmdl') or '.semanticmodel' not in nl:
                continue
            try:
                text = zf.read(n).decode('utf-8-sig')
            except Exception:
                continue
            if os.path.basename(nl) == 'relationships.tmdl':
                relationships.extend(parse_tmdl_relationships(text))
            elif os.path.basename(nl) == 'expressions.tmdl':
                power_query.update(parse_tmdl_expressions(text))
            else:
                m, pq = parse_tmdl_tables(text)
                measures.update(m)
                power_query.update(pq)

        try:
            from visual_metadata_extractor import VisualMetadataExtractor
            extractor = VisualMetadataExtractor()
            layout = extractor._extract_pbir_layout_from_zip(zf, names)
            if layout and layout.get('sections'):
                visuals = extractor.extract_fields_from_definition(layout)
        except Exception as exc:
            print(f"⚠️ Report Differences: visual parsing failed: {exc}")

    return {
        'measures': measures,
        'power_query': power_query,
        'relationships': relationships,
        'visuals': visuals,
    }
