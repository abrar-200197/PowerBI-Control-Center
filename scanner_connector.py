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

    def run_scan(self):
        """Run Admin workspace scan and return full JSON result with detailed error logging."""
        try:
            headers = self._get_headers()
        except Exception:
            # Token request failed, can't proceed
            return None

        scan_url = (
            f"{self.base_url}/admin/workspaces/getInfo"
            "?lineage=True&datasourceDetails=True&datasetSchema=True&datasetExpressions=True"
        )

        # Power BI Admin Scanner API expects list of workspace IDs
        workspace_list = [self.workspace_id] if self.workspace_id else []
        if not workspace_list:
            print("❌ No workspace ID configured in environment")
            return None
            
        body = {"workspaces": workspace_list}
        print(f"   Scan parameters: {workspace_list}")
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
            while True:
                s_res = requests.get(status_url, headers=headers)
                s_res.raise_for_status()
                s = s_res.json()
                state = s.get('status')
                print(f"   Status: {state}")
                if state == "Succeeded":
                    break
                if state == "Failed":
                    print("❌ Scan failed on server side.")
                    return None
                time.sleep(2)

            print("\n3️⃣  Retrieving scan result...")
            result_url = f"{self.base_url}/admin/workspaces/scanResult/{scan_id}"
            result_res = requests.get(result_url, headers=headers)
            result_res.raise_for_status()
            data = result_res.json()

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

    def get_dataset_model(self, dataset_id, admin_client=None):
        """Extract dataset model info - simplified version using admin_client if provided"""
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
        data = self.run_scan()
        
        if not data or "workspaces" not in data:
            return model

        for ws in data["workspaces"]:
            for ds in ws.get("datasets", []):
                if ds.get("id") == dataset_id:
                    # Extract tables, columns, and M expressions
                    for table in ds.get("tables", []):
                        tname = table.get("name")
                        if not tname: continue

                        model["tables"].append(tname)

                        cols = []
                        for col in table.get("columns", []):
                            col_info = {
                                "name": col.get("name"),
                                "dataType": col.get("dataType"),
                                "columnType": col.get("columnType")
                            }
                            # Include DAX expression for calculated columns
                            if col.get("expression"):
                                col_info["expression"] = col.get("expression")
                            cols.append(col_info)
                        model["columns"][tname] = cols

                        for src in table.get("source", []):
                            expr = src.get("expression")
                            if expr:
                                model["expressions"].append({
                                    "table": tname,
                                    "expression": expr
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
                    for rel in ds.get("relationships", []):
                        model["relationships"].append({
                            "name": rel.get("name"),
                            "fromTable": rel.get("fromTable"),
                            "fromColumn": rel.get("fromColumn"),
                            "toTable": rel.get("toTable"),
                            "toColumn": rel.get("toColumn"),
                            "type": rel.get("type"),
                            "joinType": rel.get("joinType"),
                            "isActive": rel.get("isActive")
                        })

        print(f"   ✅ Scanner model found: {len(model['tables'])} table(s), "
              f"{sum(len(v) for v in model['columns'].values())} columns, "
              f"{len(model['measures'])} measure(s), "
              f"{len(model['relationships'])} relationship(s), "
              f"{len(model['expressions'])} expression(s)")
        return model
