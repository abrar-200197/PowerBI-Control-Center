# scanner_connector.py - ENHANCED ERROR LOGGING
import requests
import time
import json
import os
from dotenv import load_dotenv

load_dotenv()

class PowerBIScanner:
    """Uses Admin Scanner API to get dataset expressions (M / SQL)"""

    def __init__(self):
        self.client_id = os.getenv('CLIENT_ID')
        self.client_secret = os.getenv('CLIENT_SECRET')
        self.tenant_id = os.getenv('TENANT_ID')
        self.workspace_id = os.getenv('WORKSPACE_ID')

        self.base_url = "https://api.powerbi.com/v1.0/myorg"
        self.access_token = None

    def get_access_token(self):
        """Forces a new token request and provides detailed error logging."""
        print("   🔐 [Scanner] Forcing a NEW Power BI access token request...")

        url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": self.client_id,
            "scope": "https://analysis.windows.net/powerbi/api/.default",
            "client_secret": self.client_secret,
            "grant_type": "client_credentials"
        }

        try:
            res = requests.post(url, data=data)
            # This will raise an HTTPError for bad responses (4xx or 5xx)
            res.raise_for_status()
            self.access_token = res.json().get("access_token")
            print("   ✅ [Scanner] New access token obtained successfully!")
            return self.access_token
        except requests.exceptions.HTTPError as http_err:
            print(f"   ❌ [Scanner] HTTP Error during token request: {http_err}")
            # --- DETAILED ERROR PRINTING ---
            if http_err.response is not None:
                print(f"      Status Code: {http_err.response.status_code}")
                try:
                    # Try to parse and print the JSON error response from the server
                    error_details = http_err.response.json()
                    print(f"      Error Details: {json.dumps(error_details, indent=2)}")
                except json.JSONDecodeError:
                    # If the response is not JSON, print the raw text
                    print(f"      Raw Response: {http_err.response.text}")
            # --- END OF DETAILED ERROR PRINTING ---
            raise  # Re-raise the exception to stop the script
        except Exception as e:
            print(f"   ❌ [Scanner] An unexpected error occurred during token request: {e}")
            raise

    def _get_headers(self):
        # Always get a fresh token for each run
        self.get_access_token()
        
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

    def run_scan(self, workspace_id=None):
        """Run Admin workspace scan and return full JSON result with detailed error logging.

        Args:
            workspace_id: Optional workspace ID to scan. If not provided, uses self.workspace_id from .env
        """
        try:
            headers = self._get_headers()
        except Exception:
            # Token request failed, can't proceed
            return None

        scan_url = (
            f"{self.base_url}/admin/workspaces/getInfo"
            "?lineage=True&datasourceDetails=True&datasetSchema=True&datasetExpressions=True"
        )

        # Use provided workspace_id or fall back to self.workspace_id
        target_workspace = workspace_id or self.workspace_id
        workspace_list = [target_workspace] if target_workspace else []
        if not workspace_list:
            print("❌ No workspace ID configured in environment")
            return None

        # Include all available metadata in scan request (INCLUDING VISUAL METADATA!)
        body = {
            "workspaces": workspace_list,
            "datasetExpressions": True,
            "datasetSchema": True,
            "datasourceDetails": True,
            "getArtifactUsers": False,
            "lineage": False,
            "reportExpressions": True,  # Get DAX expressions in reports
            "reportVisuals": True  # ⭐ GET VISUAL METADATA (titles, types, fields)
        }
        print(f"   Scan parameters: {workspace_list} (with schema, expressions, AND VISUALS)")
        print(f"   🔍 Requesting: datasetSchema={body['datasetSchema']}, datasetExpressions={body['datasetExpressions']}, reportVisuals={body['reportVisuals']}")
        print("\n1️⃣  Initiating Admin Scan Request...")

        try:
            res = requests.post(scan_url, headers=headers, json=body)
            
            # --- DETAILED ERROR PRINTING FOR SCAN API ---
            if res.status_code in (401, 403):
                print(f"❌ ACCESS DENIED ({res.status_code}) for Admin API.")
                print("   This is an Authorization issue. Check Power BI Tenant Settings and Workspace Access.")
                try:
                    error_details = res.json()
                    print(f"   Server Response: {json.dumps(error_details, indent=2)}")
                except json.JSONDecodeError:
                    print(f"   Raw Server Response: {res.text}")
                return None
            
            # Raise an exception for other bad status codes
            res.raise_for_status()
            # --- END OF DETAILED ERROR PRINTING ---

            scan_id = res.json()['id']
            print(f"   ✅ Scan initiated. ID: {scan_id}")

            # ... (rest of the run_scan method is the same)
            
            status_url = f"{self.base_url}/admin/workspaces/scanStatus/{scan_id}"
            print("\n2️⃣  Waiting for scan to complete...")

            # ⚡ PERFORMANCE OPTIMIZATION: Adaptive polling with exponential backoff
            # Start with short intervals for fast scans, increase for longer ones
            poll_interval = 1  # Start with 1 second
            max_poll_interval = 5  # Cap at 5 seconds
            poll_count = 0

            while True:
                s_res = requests.get(status_url, headers=headers)
                s_res.raise_for_status()
                s = s_res.json()
                state = s.get('status')
                poll_count += 1

                if state == "Succeeded":
                    print(f"   Status: {state} (after {poll_count} polls)")
                    break
                if state == "Failed":
                    print("❌ Scan failed on server side.")
                    return None

                print(f"   Status: {state} (poll #{poll_count}, waiting {poll_interval:.1f}s)")
                time.sleep(poll_interval)

                # Gradually increase poll interval to reduce API calls
                # Polls: 1s, 1.3s, 1.7s, 2.2s, 2.9s, 3.8s, then cap at 5s
                poll_interval = min(poll_interval * 1.3, max_poll_interval)

            print("\n3️⃣  Retrieving scan result...")
            result_url = f"{self.base_url}/admin/workspaces/scanResult/{scan_id}"
            result_res = requests.get(result_url, headers=headers)
            result_res.raise_for_status()
            data = result_res.json()

            # DEBUG: Check if visual metadata is included
            workspaces = data.get("workspaces", [])
            if workspaces and len(workspaces) > 0:
                ws = workspaces[0]
                reports = ws.get("reports", [])
                if reports:
                    first_report = reports[0]
                    print(f"\n🔍 DEBUG - First report '{first_report.get('name', 'Unknown')}' structure:")
                    print(f"   Report has keys: {list(first_report.keys())}")
                    if "pages" in first_report:
                        print(f"   ✅ Report HAS 'pages' key!")
                        pages = first_report.get("pages", [])
                        if pages:
                            print(f"      First page has keys: {list(pages[0].keys())}")
                            if "visuals" in pages[0]:
                                print(f"      ✅ Page HAS 'visuals' key with {len(pages[0]['visuals'])} visuals")
                                if pages[0].get("visuals"):
                                    print(f"         First visual has keys: {list(pages[0]['visuals'][0].keys())}")
                                else:
                                    print(f"         ❌ 'visuals' array is EMPTY")
                            else:
                                print(f"      ❌ Page does NOT have 'visuals' key")
                        else:
                            print(f"      ❌ 'pages' array is EMPTY")
                    else:
                        print(f"   ❌ Report does NOT have 'pages' key")

            return data

        except requests.exceptions.HTTPError as http_err:
            print(f"❌ HTTP Error during scan process: {http_err}")
            if http_err.response is not None:
                print(f"   Status Code: {http_err.response.status_code}")
                try:
                    print(f"   Error Details: {json.dumps(http_err.response.json(), indent=2)}")
                except:
                    print(f"   Raw Response: {http_err.response.text}")
            return None
        except Exception as e:
            print(f"❌ An unexpected error occurred during scan: {e}")
            return None

    def get_dataset_model(self, dataset_id, admin_client=None, workspace_id=None):
        """Extract dataset model info - simplified version using admin_client if provided

        Args:
            dataset_id: The dataset ID to extract model for
            admin_client: Optional admin client (not currently used)
            workspace_id: Optional workspace ID to scan. If not provided, uses self.workspace_id from .env
        """
        model = {
            "tables": [],
            "columns": {},
            "expressions": [],
            "measures": [],
            "relationships": []
        }

        # If admin_client is provided, we can fetch directly from cache
        if admin_client:
            # This would require adding scan cache to admin_client
            # For now, return empty model and let document creator handle gracefully
            print(f"   ✅ Scanner model extraction initialized (awaiting data)")
            return model

        # Otherwise try to run scan (for standalone use)
        data = self.run_scan(workspace_id=workspace_id)
        
        if not data or "workspaces" not in data:
            return model

        for ws in data["workspaces"]:
            for ds in ws.get("datasets", []):
                if ds.get("id") == dataset_id:
                    print(f"   🔍 Found dataset in scan results")
                    print(f"      Dataset has {len(ds.get('tables', []))} tables")

                    # DEBUG: Print what keys are available in the dataset object
                    print(f"      🔑 Dataset object keys: {list(ds.keys())}")

                    # DEBUG: Check if 'relationships' key exists
                    if 'relationships' in ds:
                        print(f"      ✅ 'relationships' key EXISTS in dataset")
                    else:
                        print(f"      ❌ 'relationships' key MISSING from dataset - Scanner API may not be returning relationship data")

                    # Extract tables, columns, and M expressions
                    for table in ds.get("tables", []):
                        tname = table.get("name")
                        if not tname: continue

                        model["tables"].append(tname)
                        print(f"      📋 Processing table: {tname}")

                        cols = []
                        for col in table.get("columns", []):
                            col_info = {
                                "name": col.get("name"),
                                "dataType": col.get("dataType"),
                                "columnType": col.get("columnType"),
                                "isReferenced": col.get("isReferenced", None)  # Track if column is used in the report
                            }
                            # Include DAX expression for calculated columns
                            if col.get("expression"):
                                col_info["expression"] = col.get("expression")
                            cols.append(col_info)
                        model["columns"][tname] = cols

                        # Check for M expressions in table source
                        table_sources = table.get("source", [])
                        print(f"         Source array length: {len(table_sources)}")

                        for src in table_sources:
                            expr = src.get("expression")
                            if expr:
                                print(f"         ✅ Found M expression ({len(expr)} chars)")
                                model["expressions"].append({
                                    "table": tname,
                                    "expression": expr
                                })
                            else:
                                print(f"         ⚠️ Source has no expression field")

                        if not table_sources:
                            print(f"         ℹ️ No source array (likely calculated table)")

                            # For calculated tables, check partitions for DAX expressions
                            partitions = table.get("partitions", [])
                            for partition in partitions:
                                if isinstance(partition, dict):
                                    part_source = partition.get("source", {})
                                    if isinstance(part_source, dict):
                                        dax_expr = part_source.get("expression", "")
                                        if dax_expr:
                                            print(f"         ✅ Found DAX table expression ({len(dax_expr)} chars)")
                                            # Store DAX table expressions separately
                                            model["expressions"].append({
                                                "table": tname,
                                                "expression": dax_expr,
                                                "expressionType": "DAX"  # Flag it as DAX, not M
                                            })

                        # Extract DAX measures
                        for measure in table.get("measures", []):
                            model["measures"].append({
                                "table": tname,
                                "name": measure.get("name"),
                                "expression": measure.get("expression"),
                                "description": measure.get("description")
                            })
                    
                    # Extract relationships
                    dataset_relationships = ds.get("relationships", [])
                    print(f"      🔗 Dataset has {len(dataset_relationships)} relationships")

                    for rel in dataset_relationships:
                        relationship = {
                            "name": rel.get("name"),
                            "fromTable": rel.get("fromTable"),
                            "fromColumn": rel.get("fromColumn"),
                            "toTable": rel.get("toTable"),
                            "toColumn": rel.get("toColumn"),
                            "type": rel.get("type"),
                            "joinType": rel.get("joinType"),
                            "isActive": rel.get("isActive")
                        }
                        model["relationships"].append(relationship)
                        print(f"         ✅ Relationship: {rel.get('fromTable')}[{rel.get('fromColumn')}] → {rel.get('toTable')}[{rel.get('toColumn')}]")

                    # ADDITIONAL: Check for dataset-level expressions array
                    # Some Scanner API responses include expressions at the dataset level
                    dataset_expressions = ds.get("expressions", [])
                    if dataset_expressions:
                        print(f"   🔍 Found {len(dataset_expressions)} dataset-level expressions")
                        for expr_obj in dataset_expressions:
                            # Dataset-level expressions might have different structure
                            expr_name = expr_obj.get("name") or expr_obj.get("table")
                            expr_text = expr_obj.get("expression")
                            if expr_name and expr_text:
                                print(f"      ✅ Dataset expression: {expr_name} ({len(expr_text)} chars)")
                                # Check if we already have this expression from table sources
                                existing = any(e.get("table") == expr_name for e in model["expressions"])
                                if not existing:
                                    model["expressions"].append({
                                        "table": expr_name,
                                        "expression": expr_text
                                    })

        print(f"   ✅ Scanner model found: {len(model['tables'])} table(s), "
              f"{sum(len(v) for v in model['columns'].values())} columns, "
              f"{len(model['measures'])} measure(s), "
              f"{len(model['relationships'])} relationship(s), "
              f"{len(model['expressions'])} expression(s)")
        return model
