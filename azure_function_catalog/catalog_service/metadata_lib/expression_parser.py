"""
Parse Power Query (M) expressions for physical source refs.
Handles Scanner API shapes + Sql.Database(..., [Query=...]) with #(lf) escapes.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SourceRef:
    source_type: str
    server: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None
    table: Optional[str] = None
    object_name: Optional[str] = None
    raw_snippet: Optional[str] = None

    def table_key(self) -> str:
        parts = [self.source_type or "unknown"]
        if self.server:
            parts.append(self.server.lower())
        if self.database:
            parts.append(self.database.lower())
        schema = (self.schema or "dbo").lower()
        table = (self.table or self.object_name or "unknown").lower()
        parts.append(f"{schema}.{table}")
        return "|".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["tableKey"] = self.table_key()
        return d


def decode_m_string(s: str) -> str:
    if not s:
        return ""
    return (
        s.replace("#(lf)", "\n")
        .replace("#(cr,lf)", "\r\n")
        .replace("#(cr)", "\r")
        .replace("#(tab)", "\t")
        .replace('""', '"')
    )


def extract_expression(source_field: Any) -> str:
    """Normalize Scanner table.source (often [{expression: '...'}])."""
    if source_field is None:
        return ""
    if isinstance(source_field, str):
        return decode_m_string(source_field)
    if isinstance(source_field, dict):
        return decode_m_string(str(source_field.get("expression") or ""))
    if isinstance(source_field, list):
        parts = []
        for item in source_field:
            if isinstance(item, dict) and item.get("expression") is not None:
                parts.append(str(item["expression"]))
            else:
                parts.append(str(item))
        return decode_m_string("\n".join(parts))
    return decode_m_string(str(source_field))


_RE_SQL_DB = re.compile(r'Sql\.Database\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"', re.I)
_RE_SQL_QUERY = re.compile(r'\[\s*Query\s*=\s*"((?:[^"]|"")*)"', re.I | re.DOTALL)
# Source{[Schema="dbo", Item="T"]}  or  Source{[Item="T", Schema="dbo"]}
_RE_SCHEMA_ITEM = re.compile(
    r'\[\s*Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"\s*\]|'
    r'\[\s*Item\s*=\s*"([^"]+)"\s*,\s*Schema\s*=\s*"([^"]+)"\s*\]|'
    r'\{\s*Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"\s*\}|'
    r'\{\s*Item\s*=\s*"([^"]+)"\s*,\s*Schema\s*=\s*"([^"]+)"\s*\}',
    re.I,
)
_RE_ORACLE = re.compile(r'Oracle\.Database\s*\(\s*"([^"]+)"', re.I)
_RE_ODATA = re.compile(r'OData\.Feed\s*\(\s*"([^"]+)"', re.I)
_RE_AS = re.compile(r'AnalysisServices\.Database\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"', re.I)
_RE_SNOW = re.compile(r'Snowflake\.Databases\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"', re.I)
_RE_NATIVE = re.compile(r'Value\.NativeQuery\s*\(\s*[^,]+,\s*"((?:[^"]|"")*)"', re.I | re.DOTALL)
_RE_EXCEL = re.compile(r'Excel\.Workbook\s*\(', re.I)
_RE_FILE = re.compile(r'File\.Contents\s*\(\s*"([^"]+)"', re.I)
_RE_FOLDER = re.compile(r'Folder\.Files\s*\(\s*"([^"]+)"', re.I)
_RE_CSV = re.compile(r'Csv\.Document\s*\(', re.I)
_RE_SHAREPOINT = re.compile(r'SharePoint\.(?:Tables|Files|Contents)\s*\(\s*"([^"]+)"', re.I)
_RE_WEB = re.compile(r'Web\.Contents\s*\(\s*"([^"]+)"', re.I)
_RE_LOCAL_NOW = re.compile(r'DateTime\.LocalNow\s*\(', re.I)
_RE_SQL_QUERY_CAPTURE = re.compile(
    r'\[\s*Query\s*=\s*"((?:[^"]|"")*)"', re.I | re.DOTALL
)
# SharePoint / Excel location helpers
_RE_SP_FOLDER_PATH = re.compile(
    r'#?"?Folder Path"?\s*=\s*"(https?://[^"]+)"', re.I
)
_RE_SP_NAME_EQ = re.compile(
    r'(?:\[Name\s*=\s*"([^"]+\.(?:xlsx?|csv|xlsb|xlsm))"\]'
    r'|\(\s*\[Name\]\s*=\s*"([^"]+\.(?:xlsx?|csv|xlsb|xlsm))"\s*\))',
    re.I,
)
_RE_HTTP_URL = re.compile(r'(https?://[^\s"\'\],}]+)', re.I)
_RE_EXCEL_EXT = re.compile(r'[^\\/"\'\s]+\.(?:xlsx?|csv|xlsb|xlsm)\b', re.I)

# Keywords that cannot be schema/table names
_SQL_NOISE = {
    "select", "where", "group", "order", "left", "right", "inner", "outer", "cross",
    "full", "join", "on", "and", "or", "not", "null", "as", "from", "into", "set",
    "case", "when", "then", "else", "end", "with", "union", "all", "distinct",
    "having", "limit", "offset", "top", "values", "insert", "update", "delete",
    "create", "drop", "alter", "table", "view", "index", "by", "asc", "desc",
    "in", "is", "like", "between", "exists", "over", "partition", "row_number",
    "cast", "convert", "isnull", "coalesce", "trim", "left", "right", "substring",
    "getdate", "year", "month", "day", "sum", "count", "avg", "min", "max",
    "concat", "replace", "charindex", "len", "format", "now", "member",
}

# Noise schemas/objects to deprioritize (still captured if only hit)
_NOISE_SCHEMAS = {"sys", "information_schema", "msdb", "master", "tempdb", "model"}
_NOISE_TABLES = {"all_objects", "objects", "columns", "tables", "sysobjects"}


def _strip_sql_noise(sql: str) -> str:
    """Remove block/line comments so FROM inside comments is not picked up."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n\r]*", " ", sql)
    return sql


def _ident_token(raw: str) -> str:
    """Normalize [Name] or Name -> Name."""
    s = (raw or "").strip()
    if s.startswith("[") and s.endswith("]") and len(s) >= 2:
        s = s[1:-1]
    return s.strip()


def _is_valid_ident(name: str) -> bool:
    if not name or len(name) > 128:
        return False
    low = name.lower()
    if low in _SQL_NOISE:
        return False
    if not re.match(r"^[A-Za-z_@#][\w@#$ ]*$", name):
        return False
    return True


def _looks_like_schema(schema: str) -> bool:
    """Reject SQL aliases (a, b, d) and keep real schemas (Retail_DW, dbo, ...)."""
    if not schema:
        return False
    s = schema.strip()
    # single-letter / short alias used in T-SQL
    if len(s) <= 2 and s.isalpha():
        return False
    if s.lower() in _SQL_NOISE:
        return False
    return True


def _looks_like_table(table: str) -> bool:
    if not table or not _is_valid_ident(table):
        return False
    # column-like multi-word with spaces is OK if bracketed source; reject pure keywords
    if table.lower() in _SQL_NOISE:
        return False
    return True


def _tables_from_sql(sql: str) -> List[Tuple[str, str]]:
    """
    Extract (schema, table) from arbitrary T-SQL / nested queries.

    Handles schema.table, [schema].[table], [schema].table, schema.[table],
    3-part names, FROM/JOIN inside subqueries and UNIONs.
    """
    if not sql:
        return []
    sql = _strip_sql_noise(decode_m_string(sql))

    part = r"(?:\[[^\]]+\]|[A-Za-z_@#][\w@#$]*)"
    qualified = rf"({part}(?:\s*\.\s*{part}){{1,3}})"

    found: List[Tuple[str, str]] = []

    # Primary: FROM / JOIN / APPLY / INTO / UPDATE / MERGE (not followed by subquery '(' only)
    ctx = re.compile(
        rf"\b(?:FROM|JOIN|APPLY|INTO|UPDATE|MERGE)\s+(?!\(){qualified}",
        re.I,
    )
    for m in ctx.finditer(sql):
        _collect_qualified(m.group(1), found)

    # Also: FROM dbo.Table with optional AS alias — already covered
    # USING clause
    using = re.compile(rf"\bUSING\s+(?!\(){qualified}", re.I)
    for m in using.finditer(sql):
        _collect_qualified(m.group(1), found)

    return _dedupe_schema_table(found)


def _collect_qualified(raw: str, found: List[Tuple[str, str]]) -> None:
    parts = [_ident_token(p) for p in re.split(r"\s*\.\s*", raw.strip())]
    parts = [p for p in parts if p]
    if len(parts) >= 2:
        schema, table = parts[-2], parts[-1]
    elif len(parts) == 1:
        schema, table = "dbo", parts[0]
    else:
        return
    if not _looks_like_schema(schema) or not _looks_like_table(table):
        return
    if schema.lower() in _SQL_NOISE:
        return
    found.append((schema, table))


def _dedupe_schema_table(found: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    out, seen = [], set()
    noise_last: List[Tuple[str, str]] = []
    for s, t in found:
        key = (s.lower(), t.lower())
        if key in seen:
            continue
        seen.add(key)
        if s.lower() in _NOISE_SCHEMAS or t.lower() in _NOISE_TABLES:
            noise_last.append((s, t))
        else:
            out.append((s, t))
    out.extend(noise_last)
    return out


def extract_tables_from_sql_text(sql: str) -> List[str]:
    """Public helper: list of 'schema.table' strings from SQL."""
    return [f"{s}.{t}" for s, t in _tables_from_sql(sql)]


def _push(
    refs: List[SourceRef],
    *,
    source_type: str,
    server: Optional[str] = None,
    database: Optional[str] = None,
    schema: Optional[str] = None,
    table: Optional[str] = None,
    snippet: str = "",
) -> None:
    obj = f"{schema}.{table}" if schema and table else table
    refs.append(
        SourceRef(
            source_type=source_type,
            server=server,
            database=database,
            schema=schema,
            table=table,
            object_name=obj,
            raw_snippet=(snippet or "")[:220],
        )
    )


def extract_file_source_info(expression: str) -> Dict[str, Optional[str]]:
    """
    Extract Excel / SharePoint / local file location from an M expression.

    Returns keys: siteUrl, folderUrl, fileName, fileUrl, localPath
    """
    empty = {
        "siteUrl": None,
        "folderUrl": None,
        "fileName": None,
        "fileUrl": None,
        "localPath": None,
    }
    if not expression:
        return empty
    expr = decode_m_string(expression)

    site = None
    m = _RE_SHAREPOINT.search(expr)
    if m:
        site = m.group(1).rstrip("/")

    folder = None
    fm = _RE_SP_FOLDER_PATH.search(expr)
    if fm:
        folder = fm.group(1)
        if not folder.endswith("/"):
            folder = folder + "/"

    file_name = None
    nm = _RE_SP_NAME_EQ.search(expr)
    if nm:
        file_name = nm.group(1) or nm.group(2)
    if not file_name:
        # Fall back: first .xlsx/.csv name appearing near SharePoint filters
        names = _RE_EXCEL_EXT.findall(expr)
        # Prefer names that are not inside a Windows path (drive letter)
        for cand in names:
            if ":" not in cand and "\\" not in cand:
                file_name = cand
                break
        if not file_name and names:
            # last path segment of a local file may still be useful as display name
            file_name = names[0].split("\\")[-1].split("/")[-1]

    local_path = None
    pm = _RE_FILE.search(expr)
    if pm:
        local_path = pm.group(1)

    file_url = None
    if folder and file_name:
        file_url = folder.rstrip("/") + "/" + file_name
    elif site and file_name:
        # site only — best effort open folder; still better than "Local File"
        file_url = site
    elif not file_url:
        # Web.Contents direct URL to a file
        wm = _RE_WEB.search(expr)
        if wm and _RE_EXCEL_EXT.search(wm.group(1) or ""):
            file_url = wm.group(1)

    return {
        "siteUrl": site,
        "folderUrl": folder,
        "fileName": file_name,
        "fileUrl": file_url,
        "localPath": local_path,
    }


def parse_m_expression(expression: str) -> List[SourceRef]:
    if not expression or not isinstance(expression, str):
        return []
    expr = decode_m_string(expression)
    refs: List[SourceRef] = []

    for m in _RE_SQL_DB.finditer(expr):
        server, database = m.group(1), m.group(2)
        # Use FULL remaining expression (large native SQL queries exceed 8k)
        rest = expr[m.start() :]
        objects: List[Tuple[str, str]] = []
        qm = _RE_SQL_QUERY.search(rest)
        if qm:
            objects.extend(_tables_from_sql(qm.group(1)))
        # Also scan full expression after this Sql.Database for navigation
        for sm in _RE_SCHEMA_ITEM.finditer(rest[:50000]):
            g = sm.groups()
            if g[0] and g[1]:
                schema, table = g[0], g[1]
            elif g[2] and g[3]:
                schema, table = g[3], g[2]
            elif g[4] and g[5]:
                schema, table = g[4], g[5]
            elif g[6] and g[7]:
                schema, table = g[7], g[6]
            else:
                continue
            objects.append((schema or "dbo", table))
        snippet = (qm.group(1)[:220] if qm else rest[:220])
        if objects:
            for schema, table in objects:
                _push(
                    refs,
                    source_type="Sql",
                    server=server,
                    database=database,
                    schema=schema,
                    table=table,
                    snippet=snippet,
                )
        else:
            _push(refs, source_type="Sql", server=server, database=database, snippet=snippet)

    for m in _RE_NATIVE.finditer(expr):
        for schema, table in _tables_from_sql(m.group(1)):
            _push(refs, source_type="SqlNative", schema=schema, table=table, snippet=m.group(1))

    for m in _RE_ORACLE.finditer(expr):
        _push(refs, source_type="Oracle", server=m.group(1), snippet=m.group(0))
    for m in _RE_ODATA.finditer(expr):
        _push(refs, source_type="OData", server=m.group(1), snippet=m.group(0))
    for m in _RE_AS.finditer(expr):
        _push(
            refs,
            source_type="AnalysisServices",
            server=m.group(1),
            database=m.group(2),
            snippet=m.group(0),
        )
    for m in _RE_SNOW.finditer(expr):
        _push(
            refs,
            source_type="Snowflake",
            server=m.group(1),
            database=m.group(2),
            snippet=m.group(0),
        )

    # File / Excel / SharePoint / Web
    file_info = extract_file_source_info(expr)
    has_excel = bool(_RE_EXCEL.search(expr) or _RE_CSV.search(expr))
    has_sp = bool(_RE_SHAREPOINT.search(expr))
    if has_excel or has_sp or file_info.get("fileName") or file_info.get("localPath"):
        kind = "Excel" if has_excel else ("SharePoint" if has_sp else "File")
        loc = (
            file_info.get("fileUrl")
            or file_info.get("localPath")
            or file_info.get("siteUrl")
            or file_info.get("folderUrl")
            or ("Local File" if has_excel else None)
        )
        fname = file_info.get("fileName")
        _push(
            refs,
            source_type=kind,
            server=loc,
            table=fname,
            snippet=expr[:220],
        )
    for m in _RE_FILE.finditer(expr):
        if not any(r.source_type in ("Excel", "File", "SharePoint") for r in refs):
            _push(refs, source_type="File", server=m.group(1), snippet=m.group(0))
    for m in _RE_FOLDER.finditer(expr):
        if not any(r.source_type in ("Excel", "File", "SharePoint", "Folder") for r in refs):
            _push(refs, source_type="Folder", server=m.group(1), snippet=m.group(0))
    for m in _RE_SHAREPOINT.finditer(expr):
        if not any(r.source_type in ("Excel", "SharePoint") for r in refs):
            _push(refs, source_type="SharePoint", server=m.group(1), snippet=m.group(0))
    for m in _RE_WEB.finditer(expr):
        if not any(r.source_type == "Web" and r.server == m.group(1) for r in refs):
            _push(refs, source_type="Web", server=m.group(1), snippet=m.group(0))
    if _RE_LOCAL_NOW.search(expr) and not refs:
        _push(refs, source_type="Expression", server="Internal Model", snippet="DateTime.LocalNow()")

    seen, out = set(), []
    for r in refs:
        k = r.table_key()
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def extract_sql_query(expression: str) -> Optional[str]:
    """Pull native SQL text from M Query= or Value.NativeQuery if present."""
    if not expression:
        return None
    expr = decode_m_string(expression)
    m = _RE_SQL_QUERY_CAPTURE.search(expr)
    if m:
        return decode_m_string(m.group(1)).strip()
    m = _RE_NATIVE.search(expr)
    if m:
        return decode_m_string(m.group(1)).strip()
    return None


def classify_source_display(expression: str, refs: List[SourceRef]) -> Dict[str, Any]:
    """
    Colleague-style display fields for one model table.
    Returns sourceTypeLabel, serverName, sqlSourceTables, sqlQuery,
    plus fileName / sourceUrl for Excel & SharePoint file sources.
    """
    expr = decode_m_string(expression or "")
    sql_query = extract_sql_query(expr)
    file_info = extract_file_source_info(expr)
    sql_tables: List[str] = []
    for r in refs:
        if r.source_type in ("Sql", "SqlNative") and (r.table or r.object_name):
            if r.schema and r.table:
                sql_tables.append(f"{r.schema}.{r.table}")
            elif r.object_name:
                sql_tables.append(r.object_name)
            elif r.table:
                sql_tables.append(r.table)
    # Always harvest from full SQL text (covers nested queries even if refs incomplete)
    if sql_query:
        for name in extract_tables_from_sql_text(sql_query):
            sql_tables.append(name)

    # Prefer first physical ref for server/type
    primary = next((r for r in refs if r.source_type not in ("ModelTable",)), None)
    if primary is None and refs:
        primary = refs[0]

    label = "Unknown"
    server_name = "N/A"
    source_url: Optional[str] = None
    file_name: Optional[str] = file_info.get("fileName")
    if primary:
        st = primary.source_type or "Unknown"
        if st in ("Sql", "SqlNative"):
            label = "SQL Server"
            parts = [primary.server, primary.database]
            server_name = " / ".join(p for p in parts if p) or "N/A"
        elif st in ("Excel", "File", "SharePoint", "Folder", "Web"):
            # Prefer rich file location over bare "Local File"
            loc = (
                file_info.get("fileUrl")
                or file_info.get("localPath")
                or file_info.get("folderUrl")
                or file_info.get("siteUrl")
                or primary.server
            )
            if st == "Excel" or (file_info.get("fileName") and _RE_EXCEL.search(expr)):
                label = "Excel"
            elif st == "SharePoint" or file_info.get("siteUrl"):
                label = "SharePoint Excel" if file_info.get("fileName") else "SharePoint"
            elif st == "Folder":
                label = "Folder"
            elif st == "Web":
                label = "Web"
            else:
                label = "File"
            server_name = loc or ("Local File" if label == "Excel" else "N/A")
            if file_info.get("fileUrl") and str(file_info["fileUrl"]).lower().startswith("http"):
                source_url = file_info["fileUrl"]
            elif file_info.get("folderUrl") and str(file_info["folderUrl"]).lower().startswith("http"):
                source_url = file_info["folderUrl"]
            elif file_info.get("siteUrl") and str(file_info["siteUrl"]).lower().startswith("http"):
                source_url = file_info["siteUrl"]
            elif loc and str(loc).lower().startswith("http"):
                source_url = str(loc)
            # Put file name into sqlSourceTables column equivalent for non-SQL
            if file_name and file_name not in sql_tables:
                sql_tables.insert(0, file_name)
        elif st == "OData":
            label = "OData"
            server_name = primary.server or "N/A"
            if primary.server and str(primary.server).lower().startswith("http"):
                source_url = primary.server
        elif st == "AnalysisServices":
            label = "Analysis Services"
            server_name = " / ".join(p for p in [primary.server, primary.database] if p) or "N/A"
        elif st == "Snowflake":
            label = "Snowflake"
            server_name = " / ".join(p for p in [primary.server, primary.database] if p) or "N/A"
        elif st == "Oracle":
            label = "Oracle"
            server_name = primary.server or "N/A"
        elif st == "Expression":
            label = "Expression"
            server_name = primary.server or "Internal Model"
        elif st == "ModelTable":
            label = "Unknown"
            server_name = "N/A"

    # Expression-only calculated tables (reference other queries, LocalNow, etc.)
    if label == "Unknown" and expr:
        if "Sql.Database" not in expr and (
            "DateTime.LocalNow" in expr
            or re.search(r"#\"[^\"]+\"", expr)
            or "Table." in expr
        ):
            label = "Expression"
            server_name = "Internal Model"

    # Dedupe sql tables preserve order
    seen_t, sql_unique = set(), []
    for t in sql_tables:
        k = t.lower()
        if k not in seen_t:
            seen_t.add(k)
            sql_unique.append(t)

    return {
        "sourceTypeLabel": label,
        "serverName": server_name,
        "sqlSourceTables": sql_unique,
        "sqlQuery": sql_query,
        "fileName": file_name,
        "sourceUrl": source_url,
    }


def enrich_from_datasource(
    source_type: Optional[str],
    connection_details: Optional[Dict[str, Any]],
    table_name: Optional[str] = None,
    schema: Optional[str] = None,
) -> Optional[SourceRef]:
    if not connection_details and not table_name:
        return None
    details = connection_details or {}
    server = details.get("server") or details.get("path") or details.get("url")
    database = details.get("database") or details.get("databaseName")
    st = source_type or details.get("kind") or details.get("type") or "Unknown"
    return SourceRef(
        source_type=str(st),
        server=str(server) if server else None,
        database=str(database) if database else None,
        schema=schema,
        table=table_name,
        object_name=table_name,
    )
