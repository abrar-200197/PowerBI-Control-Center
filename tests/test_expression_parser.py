"""Unit tests for M expression source parsing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metadata_extractor.expression_parser import extract_expression, parse_m_expression
from metadata_extractor.impact_builder import build_impact_index
from metadata_extractor.normalizer import normalize_workspaces


def test_sql_database_with_schema_item():
    expr = '''
    let
        Source = Sql.Database("edw.company.com", "EDW"),
        dbo_FactSales = Source{[Schema="dbo", Item="FactSales"]}[Data]
    in
        dbo_FactSales
    '''
    refs = parse_m_expression(expr)
    assert len(refs) >= 1
    r = refs[0]
    assert r.source_type == "Sql"
    assert r.server == "edw.company.com"
    assert r.database == "EDW"
    assert r.schema == "dbo"
    assert r.table == "FactSales"


def test_sql_database_query_option_with_m_escapes():
    raw = [{
        "expression": (
            'let\n    Source = Sql.Database("ashley-edw.database.windows.net", "ASHLEY_EDW", '
            '[Query="SELECT *#(lf)FROM PowerBI_Enterprise.DimDate#(lf)WHERE Fiscal_Year >= 2018"])\n'
            "in\n    Source"
        )
    }]
    expr = extract_expression(raw)
    refs = parse_m_expression(expr)
    assert len(refs) == 1
    assert refs[0].schema == "PowerBI_Enterprise"
    assert refs[0].table == "DimDate"
    assert refs[0].server.startswith("ashley-edw")


def test_native_query_bracket_tables():
    expr = '''
    let
        Source = Sql.Database("sql01", "DW"),
        q = Value.NativeQuery(Source, "SELECT * FROM [sales].[Orders] o JOIN [dbo].[Customer] c ON 1=1", null, [EnableFolding=true])
    in
        q
    '''
    refs = parse_m_expression(expr)
    keys = {r.table_key() for r in refs}
    assert any("orders" in k for k in keys)
    assert any("customer" in k for k in keys)


def test_normalizer_list_source_shape():
    raw = [{
        "id": "ws1",
        "name": "Finance",
        "reports": [{"id": "r1", "name": "Sales Dash", "datasetId": "d1"}],
        "datasets": [{
            "id": "d1",
            "name": "Sales Model",
            "tables": [{
                "name": "Sales",
                "source": [{
                    "expression": (
                        'let Source = Sql.Database("edw", "EDW", '
                        '[Query="SELECT * FROM dbo.FactSales"]) in Source'
                    )
                }],
                "columns": [{"name": "Amount", "dataType": "Double"}],
                "measures": [],
            }],
            "datasources": [],
        }],
    }]
    inv = normalize_workspaces(raw)
    t = inv["workspaces"][0]["datasets"][0]["tables"][0]
    assert t["sources"][0]["table"] == "FactSales"
    assert t["sources"][0]["schema"] == "dbo"
    impact = build_impact_index(inv)
    hits = [x for x in impact["tables"].values() if (x.get("table") or "").lower() == "factsales"]
    assert hits and hits[0]["impactSummary"]["reportCount"] == 1


if __name__ == "__main__":
    test_sql_database_with_schema_item()
    test_sql_database_query_option_with_m_escapes()
    test_native_query_bracket_tables()
    test_normalizer_list_source_shape()
    print("All tests passed.")
