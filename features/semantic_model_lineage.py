"""
Semantic Model (Dataset) Lineage Feature
Independent dataset analysis showing Power Query (M) expressions, DAX measures, and data sources
"""

import re
import time
from typing import Dict, List, Any, Optional

# DEBUG: Module loaded indicator
print("=" * 80)
print("🔥 SEMANTIC_MODEL_LINEAGE.PY MODULE LOADED - NEW VERSION WITH OWNER/REFRESH")
print("=" * 80)


class SemanticModelLineage:
    """
    Analyzes Power BI Semantic Models (Datasets) independently of reports
    
    Features:
    - Power Query (M) expression extraction
    - DAX measure mapping from expressions array
    - Data source parsing (SQL Server, Excel, Web, etc.)
    - Server/Database extraction from M expressions
    - Table schema and column metadata
    - Relationship mapping
    """
    
    def __init__(self, scanner_service):
        """
        Initialize lineage analyzer
        
        Args:
            scanner_service: PowerBIScanner instance
        """
        self.scanner = scanner_service
    
    def get_dataset_lineage(self, workspace_id: str, dataset_id: str) -> Dict[str, Any]:
        """
        Get comprehensive lineage for a semantic model
        
        Args:
            workspace_id: Power BI workspace GUID
            dataset_id: Dataset GUID
            
        Returns:
            dict: {
                'success': bool,
                'dataset_id': str,
                'dataset_name': str,
                'queries': [List of tables with M expressions],
                'relationships': [List of relationships],
                'metadata': {table stats}
            }
        """
        print(f"\n🗄️ ===============================================")
        print(f"🗄️ SEMANTIC MODEL LINEAGE REQUEST")
        print(f"🗄️ ===============================================")
        print(f"   Workspace: {workspace_id}")
        print(f"   Dataset: {dataset_id}")
        
        start_time = time.time()
        
        try:
            # Run Scanner API scan
            print("   📊 Running Scanner API scan...")
            scan_data = self.scanner.run_scan(workspace_id=workspace_id)
            
            if not scan_data or "workspaces" not in scan_data:
                return {
                    'success': False,
                    'error': 'Failed to scan workspace'
                }
            
            # Find dataset in results
            dataset_data = None
            dataset_name = None
            
            for ws in scan_data["workspaces"]:
                for dataset in ws.get("datasets", []):
                    if dataset.get("id") == dataset_id:
                        dataset_data = dataset
                        dataset_name = dataset.get('name', 'Unknown Dataset')
                        print(f"   ✅ Found dataset: {dataset_name}")
                        break
                if dataset_data:
                    break
            
            if not dataset_data:
                return {
                    'success': False,
                    'error': f'Dataset {dataset_id} not found in workspace'
                }
            
            # Extract metadata
            tables = dataset_data.get('tables', [])
            datasources = dataset_data.get('datasources', [])
            relationships = dataset_data.get('relationships', [])
            expressions = dataset_data.get('expressions', [])
            upstream_datasets = dataset_data.get('upstreamDatasets', [])

            # Debug: Log all available dataset fields
            print(f"   🔍 Available dataset fields: {list(dataset_data.keys())}")

            # Check for composite model (upstream datasets)
            if upstream_datasets:
                print(f"   🔗 COMPOSITE MODEL DETECTED: {len(upstream_datasets)} upstream dataset(s)")
                for upstream in upstream_datasets:
                    print(f"      → Upstream dataset: {upstream.get('targetDatasetId', 'Unknown ID')}")

            # Extract ownership and refresh information
            owner = dataset_data.get('configuredBy', 'Unknown')
            created_date = dataset_data.get('createdDate', 'Unknown')

            # Try alternative owner fields if configuredBy is not available
            if owner == 'Unknown':
                owner = dataset_data.get('createdBy', 'Unknown')

            print(f"   👤 Raw owner data: configuredBy={dataset_data.get('configuredBy')}, createdBy={dataset_data.get('createdBy')}")

            # Get refresh information - Scanner API doesn't include this, need separate API call
            # Try to fetch refresh history using Power BI REST API
            last_refresh_time = None
            last_refresh_status = None
            try:
                print(f"   🔄 Fetching refresh history from Power BI REST API...")
                refresh_history = self._get_refresh_history(workspace_id, dataset_id)
                if refresh_history and len(refresh_history) > 0:
                    # Get the most recent refresh
                    last_refresh = refresh_history[0]
                    last_refresh_time = last_refresh.get('endTime') or last_refresh.get('startTime')
                    last_refresh_status = last_refresh.get('status', 'Unknown')
                    print(f"   ✅ Last refresh: {last_refresh_time} (Status: {last_refresh_status})")
                else:
                    print(f"   ℹ️ No refresh history found")
            except Exception as refresh_error:
                print(f"   ⚠️ Could not fetch refresh history: {refresh_error}")

            print(f"   📊 Dataset has {len(tables)} tables, {len(datasources)} datasources, {len(relationships)} relationships")
            print(f"   📐 Found {len(expressions)} DAX expressions")
            print(f"   👤 Owner: {owner}")
            
            # Build DAX measures map from expressions array
            dax_measures_by_table = self._extract_dax_measures(expressions)
            
            # Process tables and build query list
            queries = []
            for table in tables:
                try:
                    table_name = table.get('name', 'Unknown')
                    
                    # Extract M expression
                    m_expression = self._extract_m_expression(table)
                    
                    # Get columns (regular columns + calculated columns)
                    columns = self._extract_columns(table, table_name, dax_measures_by_table)
                    
                    # Map to datasources
                    table_datasources = self._map_datasources(table, datasources, m_expression)
                    
                    # Extract optimized/cleaned query display
                    cleaned_query = self._extract_cleaned_query(m_expression, columns)

                    # Build query object
                    query = {
                        'tableName': table_name,
                        'mExpression': m_expression,
                        'cleanedQuery': cleaned_query,  # NEW: Optimized query display
                        'columns': columns,
                        'datasources': table_datasources,
                        'isHidden': table.get('isHidden', False)
                    }

                    queries.append(query)
                
                except Exception as e:
                    print(f"      ⚠️ Error processing table {table.get('name')}: {e}")
                    continue
            
            # Process relationships
            relationships_data = self._process_relationships(relationships)

            # Process upstream datasets (composite models)
            upstream_datasets_info = []
            if upstream_datasets:
                print(f"   📥 Fetching lineage for {len(upstream_datasets)} upstream dataset(s)...")
                for upstream in upstream_datasets:
                    try:
                        upstream_id = upstream.get('targetDatasetId')
                        if upstream_id:
                            print(f"      🔍 Fetching upstream dataset: {upstream_id}")
                            # Recursively fetch upstream dataset lineage
                            upstream_lineage = self.get_dataset_lineage(workspace_id, upstream_id)
                            if upstream_lineage.get('success'):
                                upstream_datasets_info.append({
                                    'dataset_id': upstream_id,
                                    'dataset_name': upstream_lineage.get('dataset_name', 'Unknown'),
                                    'tables': upstream_lineage.get('queries', []),
                                    'relationships': upstream_lineage.get('relationships', []),
                                    'metadata': upstream_lineage.get('metadata', {})
                                })
                                print(f"      ✅ Fetched {len(upstream_lineage.get('queries', []))} tables from upstream dataset")
                    except Exception as upstream_error:
                        print(f"      ⚠️ Error fetching upstream dataset {upstream.get('targetDatasetId')}: {upstream_error}")

            # Build result
            result = {
                'success': True,
                'dataset_id': dataset_id,
                'dataset_name': dataset_name,
                'workspace_id': workspace_id,
                'queries': queries,
                'relationships': relationships_data,
                'upstream_datasets': upstream_datasets_info,  # NEW: Include upstream dataset lineage
                'metadata': {
                    'tables_count': len(tables),
                    'relationships_count': len(relationships),
                    'datasources_count': len(datasources),
                    'measures_count': len(expressions),
                    'owner': owner,
                    'created_date': created_date,
                    'last_refresh_time': last_refresh_time,
                    'last_refresh_status': last_refresh_status,
                    'has_composite_models': len(upstream_datasets) > 0,
                    'upstream_datasets_count': len(upstream_datasets)
                }
            }

            elapsed = time.time() - start_time
            print(f"   ✅ Lineage processed in {elapsed:.1f}s")
            print(f"   📤 Returning {len(queries)} tables from primary dataset")
            if upstream_datasets_info:
                total_upstream_tables = sum(len(ud['tables']) for ud in upstream_datasets_info)
                print(f"   📤 + {total_upstream_tables} tables from {len(upstream_datasets_info)} upstream dataset(s)")

            return result
        
        except Exception as e:
            print(f"   ❌ Error: {str(e)}")
            import traceback
            traceback.print_exc()
            
            return {
                'success': False,
                'error': str(e)
            }
    
    def _extract_dax_measures(self, expressions: List[Dict]) -> Dict[str, List[Dict]]:
        """
        Extract DAX measures from expressions array and map to tables
        
        Args:
            expressions: List of expression objects from Scanner API
            
        Returns:
            dict: {table_name: [list of measures]}
        """
        measures_by_table = {}
        
        for expr in expressions:
            table_name = expr.get('table')
            measure_name = expr.get('name')
            measure_expr = expr.get('expression')
            
            if table_name and measure_name:
                if table_name not in measures_by_table:
                    measures_by_table[table_name] = []
                
                measures_by_table[table_name].append({
                    'name': measure_name,
                    'expression': measure_expr,
                    'columnType': 'Measure'
                })
        
        if measures_by_table:
            sample_table = list(measures_by_table.keys())[0]
            print(f"   📐 Found measures in {len(measures_by_table)} tables")
            print(f"      Example: '{sample_table}' has {len(measures_by_table[sample_table])} measures")
        
        return measures_by_table

    def _extract_m_expression(self, table: Dict) -> Optional[str]:
        """
        Extract Power Query (M) expression from table

        Args:
            table: Table object from Scanner API

        Returns:
            str: M expression or None
        """
        source_expression = table.get('source', [])

        if source_expression:
            for expr in source_expression:
                if isinstance(expr, dict) and expr.get('expression'):
                    return str(expr.get('expression', ''))

        return None

    def _extract_columns(self, table: Dict, table_name: str,
                        dax_measures_by_table: Dict[str, List[Dict]]) -> List[Dict]:
        """
        Extract columns from table including DAX measures

        Args:
            table: Table object from Scanner API
            table_name: Name of the table
            dax_measures_by_table: Map of DAX measures from expressions array

        Returns:
            List of column objects with metadata
        """
        columns = []

        # Extract regular columns and calculated columns
        for col in table.get('columns', []):
            try:
                col_type = col.get('columnType', 'Column')
                col_expr = col.get('expression')

                # Safely convert expression to string
                col_expr_str = str(col_expr) if col_expr else None

                columns.append({
                    'name': str(col.get('name', 'Unknown')),
                    'dataType': str(col.get('dataType', 'Unknown')),
                    'columnType': str(col_type),
                    'expression': col_expr_str,
                    'isReferenced': bool(col.get('isReferenced', False)),
                    'isHidden': bool(col.get('isHidden', False))
                })

            except Exception as e:
                print(f"      ⚠️ Error processing column in {table_name}: {e}")
                continue

        # Add DAX measures from expressions array
        if table_name in dax_measures_by_table:
            dax_measures = dax_measures_by_table[table_name]
            print(f"      📐 Adding {len(dax_measures)} DAX measures to {table_name}")

            for measure in dax_measures:
                columns.append({
                    'name': str(measure['name']),
                    'dataType': 'Measure',
                    'columnType': 'Measure',
                    'expression': str(measure['expression']) if measure['expression'] else None,
                    'isReferenced': False,
                    'isHidden': False
                })

        return columns

    def _map_datasources(self, table: Dict, datasources: List[Dict],
                         m_expression: Optional[str]) -> List[Dict]:
        """
        Map datasources to table and extract connection details

        Args:
            table: Table object
            datasources: List of datasource objects from Scanner API
            m_expression: Power Query (M) expression for the table

        Returns:
            List of datasource objects with connection details
        """
        table_datasources = []

        # Try to get datasource from Scanner API
        for ds in datasources:
            try:
                ds_connection = ds.get('connectionDetails', {})
                table_datasources.append({
                    'type': str(ds.get('datasourceType', 'Unknown')),
                    'server': str(ds_connection.get('server', 'N/A')),
                    'database': str(ds_connection.get('database', 'N/A')),
                    'path': str(ds_connection.get('path', 'N/A')),
                    'url': str(ds_connection.get('url', 'N/A'))
                })
            except Exception as e:
                print(f"      ⚠️ Error processing datasource: {e}")
                continue

        # If no datasources found, try to parse from M expression
        if not table_datasources and m_expression:
            parsed_ds = self._parse_datasource_from_m(m_expression)
            if parsed_ds:
                table_datasources.append(parsed_ds)

        return table_datasources

    def _extract_cleaned_query(self, m_expression: Optional[str], columns: List[Dict]) -> Optional[str]:
        """
        Extract cleaned/optimized query display from M expression or DAX measures

        Priority:
        1. Extract SQL SELECT statement if M contains Value.NativeQuery
        2. Extract DAX if table is calculated table
        3. Fall back to shortened M expression

        Args:
            m_expression: Power Query (M) expression
            columns: List of columns (to check for DAX measures)

        Returns:
            str: Cleaned query string or None
        """
        if not m_expression:
            # Check if this is a calculated table (has DAX measures)
            measure_columns = [col for col in columns if col.get('columnType') == 'Measure']
            if measure_columns:
                # Show first measure as representative DAX
                first_measure = measure_columns[0]
                return f"-- Calculated Table with {len(measure_columns)} measures\n{first_measure.get('expression', 'N/A')}"
            return None

        # 1. Try to extract SQL from Value.NativeQuery
        if 'Value.NativeQuery' in m_expression:
            # Pattern: Value.NativeQuery(..., "SELECT ...", ...)
            sql_match = re.search(r'Value\.NativeQuery\s*\([^,]+,\s*"([^"]+(?:""[^"]*)*)"', m_expression, re.DOTALL)
            if sql_match:
                sql_query = sql_match.group(1)
                # Unescape double quotes ("" -> ")
                sql_query = sql_query.replace('""', '"')
                return sql_query.strip()

        # 2. Try to extract SQL from Sql.Database with query parameter
        if 'Sql.Database' in m_expression and 'Query' in m_expression:
            # Pattern: [Query="SELECT ..."]
            query_match = re.search(r'\[Query\s*=\s*"([^"]+(?:""[^"]*)*)"', m_expression, re.DOTALL)
            if query_match:
                sql_query = query_match.group(1)
                sql_query = sql_query.replace('""', '"')
                return sql_query.strip()

        # 3. Check if table is calculated (has expression in source)
        if '#table' in m_expression.lower() and 'type table' in m_expression.lower():
            # This is likely a calculated table, show simplified version
            return f"-- Power Query Calculated Table\n{m_expression[:200]}..."

        # 4. For simple table references, extract just the source table name
        if m_expression.startswith('let') and 'Source' in m_expression:
            # Try to extract the main source reference
            source_match = re.search(r'Source\s*=\s*([^,\n]+)', m_expression)
            if source_match:
                source_ref = source_match.group(1).strip()
                # If it's a simple reference like #"TableName", show it
                if source_ref.startswith('#"') and source_ref.endswith('"'):
                    return f"-- Direct Table Reference\n{source_ref}"

        # 5. If M expression is suspiciously short (< 20 chars), it's likely just a table name reference
        #    Show a helpful message instead
        if len(m_expression) < 20:
            return f"-- Reference to external dataset or calculated table\n-- Table name: {m_expression}"

        # 6. Fall back to truncated M expression (first 300 chars)
        if len(m_expression) > 300:
            return m_expression[:300] + "\n\n... (M expression truncated)"

        return m_expression

    def _parse_datasource_from_m(self, m_expression: str) -> Optional[Dict]:
        """
        Parse datasource information from M expression using regex

        Args:
            m_expression: Power Query (M) expression

        Returns:
            dict: Datasource information or None
        """
        if not m_expression:
            return None

        datasource = {
            'type': 'Unknown',
            'server': 'N/A',
            'database': 'N/A',
            'path': 'N/A',
            'url': 'N/A'
        }

        # SQL Server
        if 'Sql.Database' in m_expression:
            datasource['type'] = 'SQL Server'

            # Extract server: Sql.Database("server", "database")
            server_match = re.search(r'Sql\.Database\s*\(\s*"([^"]+)"', m_expression)
            if server_match:
                datasource['server'] = server_match.group(1)

            # Extract database
            db_match = re.search(r'Sql\.Database\s*\([^,]+,\s*"([^"]+)"', m_expression)
            if db_match:
                datasource['database'] = db_match.group(1)

        # Excel
        elif 'Excel.Workbook' in m_expression:
            datasource['type'] = 'Excel'

            path_match = re.search(r'File\.Contents\s*\(\s*"([^"]+)"', m_expression)
            if path_match:
                datasource['path'] = path_match.group(1)

        # Web
        elif 'Web.Contents' in m_expression:
            datasource['type'] = 'Web'

            url_match = re.search(r'Web\.Contents\s*\(\s*"([^"]+)"', m_expression)
            if url_match:
                datasource['url'] = url_match.group(1)

        # CSV
        elif 'Csv.Document' in m_expression:
            datasource['type'] = 'CSV'

        # SharePoint
        elif 'SharePoint.' in m_expression:
            datasource['type'] = 'SharePoint'

        # Power Query
        else:
            datasource['type'] = 'Power Query'

        return datasource if datasource['type'] != 'Unknown' else None

    def _get_refresh_history(self, workspace_id: str, dataset_id: str) -> List[Dict]:
        """
        Get refresh history for a dataset using Power BI REST API

        Args:
            workspace_id: Workspace GUID
            dataset_id: Dataset GUID

        Returns:
            List of refresh history objects (most recent first)
        """
        import requests

        # Use the scanner's access token
        headers = {
            'Authorization': f'Bearer {self.scanner.access_token}',
            'Content-Type': 'application/json'
        }

        # Power BI REST API endpoint for refresh history
        url = f'https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/refreshes'

        try:
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()
                refreshes = data.get('value', [])
                return refreshes
            else:
                print(f"      ⚠️ Refresh history API returned status {response.status_code}")
                return []

        except requests.exceptions.Timeout:
            print(f"      ⚠️ Timeout fetching refresh history")
            return []
        except Exception as e:
            print(f"      ⚠️ Error fetching refresh history: {e}")
            return []

    def _process_relationships(self, relationships: List[Dict]) -> List[Dict]:
        """
        Process relationships for frontend display

        Args:
            relationships: List of relationship objects from Scanner API

        Returns:
            List of processed relationship objects
        """
        processed = []

        for rel in relationships:
            try:
                processed.append({
                    'fromTable': rel.get('fromTable', 'Unknown'),
                    'fromColumn': rel.get('fromColumn', 'Unknown'),
                    'toTable': rel.get('toTable', 'Unknown'),
                    'toColumn': rel.get('toColumn', 'Unknown'),
                    'crossFilteringBehavior': rel.get('crossFilteringBehavior', 'Unknown'),
                    'cardinality': rel.get('cardinality', 'Unknown')
                })
            except Exception as e:
                print(f"      ⚠️ Error processing relationship: {e}")
                continue

        return processed


# ============================================================================
# FLASK ROUTE INTEGRATION
# ============================================================================

def create_dataset_lineage_route(app, scanner_service):
    """
    Add semantic model lineage route to Flask app

    Args:
        app: Flask application instance
        scanner_service: PowerBIScanner instance
    """

    @app.route('/api/dataset/lineage')
    def get_dataset_lineage():
        """
        API endpoint to get semantic model lineage

        Query Parameters:
            workspace_id: Power BI workspace GUID
            dataset_id: Dataset GUID

        Returns:
            JSON with dataset lineage including tables, M expressions, DAX measures
        """
        from flask import request, jsonify

        workspace_id = request.args.get('workspace_id')
        dataset_id = request.args.get('dataset_id')

        if not workspace_id or not dataset_id:
            return jsonify({
                'success': False,
                'error': 'workspace_id and dataset_id are required'
            }), 400

        try:
            analyzer = SemanticModelLineage(scanner_service)
            result = analyzer.get_dataset_lineage(workspace_id, dataset_id)

            return jsonify(result)

        except Exception as e:
            print(f"❌ Error in dataset lineage: {str(e)}")
            import traceback
            traceback.print_exc()

            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
