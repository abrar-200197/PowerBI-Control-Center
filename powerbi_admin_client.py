import msal
import requests
import os
from dotenv import load_dotenv
import time

class PowerBIAdminClient:
    """
    Power BI Admin API client for organization-wide access
    """
    
    def __init__(self):
        load_dotenv()
        self.tenant_id = os.getenv('TENANT_ID')
        self.client_id = os.getenv('CLIENT_ID')
        self.client_secret = os.getenv('CLIENT_SECRET')
        
        self.authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        self.scope = ["https://analysis.windows.net/powerbi/api/.default"]
        self.base_url = "https://api.powerbi.com/v1.0/myorg"
        self.admin_url = "https://api.powerbi.com/v1.0/myorg/admin"
        
        self.token = None
    
    def authenticate(self):
        """Get access token using client credentials"""
        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=self.authority,
            client_credential=self.client_secret
        )
        
        result = app.acquire_token_for_client(scopes=self.scope)
        
        if "access_token" in result:
            self.token = result["access_token"]
            print("✅ Admin authentication successful")
            return True
        else:
            print(f"❌ Authentication failed: {result.get('error_description')}")
            return False
    
    def get_headers(self):
        """Return headers with authentication token"""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
    
    def get_all_workspaces(self):
        """
        Get ALL workspaces in the organization using Admin API
        No need to be added to individual workspaces!
        """
        url = f"{self.admin_url}/groups?$top=5000"
        response = requests.get(url, headers=self.get_headers())
        
        if response.status_code == 200:
            workspaces = response.json().get('value', [])
            print(f"✅ Found {len(workspaces)} workspaces in organization")
            return workspaces
        else:
            print(f"❌ Failed to get workspaces: {response.status_code}")
            print(f"   Response: {response.text}")
            return []
    
    def get_workspace_reports(self, workspace_id):
        """Get all reports in a specific workspace"""
        url = f"{self.base_url}/groups/{workspace_id}/reports"
        response = requests.get(url, headers=self.get_headers())
        
        if response.status_code == 200:
            reports = response.json().get('value', [])
            return reports
        else:
            return []
    
    def get_report_pages(self, workspace_id, report_id):
        """Get all pages for a specific report"""
        url = f"{self.base_url}/groups/{workspace_id}/reports/{report_id}/pages"
        response = requests.get(url, headers=self.get_headers())
        
        if response.status_code == 200:
            return response.json().get('value', [])
        else:
            return []
    
    def get_dataset_info(self, workspace_id, dataset_id):
        """Get dataset information"""
        url = f"{self.base_url}/groups/{workspace_id}/datasets/{dataset_id}"
        response = requests.get(url, headers=self.get_headers())
        
        if response.status_code == 200:
            return response.json()
        else:
            return None
    
    def get_dataset_datasources(self, workspace_id, dataset_id):
        """Get data sources for a dataset"""
        url = f"{self.base_url}/groups/{workspace_id}/datasets/{dataset_id}/datasources"
        response = requests.get(url, headers=self.get_headers())
        
        if response.status_code == 200:
            return response.json().get('value', [])
        else:
            return []
    
    def initiate_workspace_scan(self, workspace_ids=None):
        """
        Initiate a metadata scan for workspaces
        If workspace_ids is None, scans ALL workspaces
        """
        url = f"{self.admin_url}/workspaces/getInfo"
        
        # Scan specific workspaces or all
        if workspace_ids:
            payload = {
                "workspaces": workspace_ids,
                "datasetExpressions": True,
                "datasetSchema": True,
                "datasourceDetails": True,
                "getArtifactUsers": True,
                "lineage": True
            }
        else:
            # Scan ALL workspaces
            payload = {
                "datasetExpressions": True,
                "datasetSchema": True,
                "datasourceDetails": True,
                "getArtifactUsers": True,
                "lineage": True
            }
        
        response = requests.post(url, headers=self.get_headers(), json=payload)
        
        if response.status_code == 202:
            scan_id = response.json().get('id')
            print(f"✅ Scan initiated. Scan ID: {scan_id}")
            return scan_id
        else:
            print(f"❌ Failed to initiate scan: {response.status_code}")
            return None
    
    def get_scan_status(self, scan_id):
        """Check status of a workspace scan"""
        url = f"{self.admin_url}/workspaces/scanStatus/{scan_id}"
        response = requests.get(url, headers=self.get_headers())
        
        if response.status_code == 200:
            return response.json()
        else:
            return None
    
    def get_scan_result(self, scan_id):
        """
        Get results of completed scan
        Returns detailed metadata for all workspaces
        """
        url = f"{self.admin_url}/workspaces/scanResult/{scan_id}"
        response = requests.get(url, headers=self.get_headers())
        
        if response.status_code == 200:
            return response.json()
        else:
            return None
    
    def wait_for_scan_completion(self, scan_id, max_wait_seconds=300):
        """
        Wait for scan to complete
        Polls every 10 seconds
        """
        print(f"⏳ Waiting for scan to complete (max {max_wait_seconds}s)...")
        
        elapsed = 0
        while elapsed < max_wait_seconds:
            status = self.get_scan_status(scan_id)
            
            if status and status.get('status') == 'Succeeded':
                print("✅ Scan completed successfully!")
                return True
            elif status and status.get('status') == 'Failed':
                print("❌ Scan failed!")
                return False
            
            time.sleep(10)
            elapsed += 10
            print(f"   Still scanning... ({elapsed}s elapsed)")
        
        print("⏰ Scan timeout!")
        return False


# Test the admin client
if __name__ == "__main__":
    print("="*60)
    print("POWER BI ADMIN API TEST")
    print("="*60)
    
    client = PowerBIAdminClient()
    
    # Test authentication
    if not client.authenticate():
        print("\n❌ Authentication failed")
        exit(1)
    
    # Get all workspaces
    print("\n📊 Fetching all workspaces in organization...")
    workspaces = client.get_all_workspaces()
    
    if workspaces:
        print(f"\n✅ Found {len(workspaces)} workspaces:")
        for ws in workspaces[:10]:  # Show first 10
            print(f"   - {ws.get('name')} (ID: {ws.get('id')})")
        
        if len(workspaces) > 10:
            print(f"   ... and {len(workspaces) - 10} more")
    
    # Initiate organization-wide scan
    print("\n🔍 Initiating organization-wide metadata scan...")
    scan_id = client.initiate_workspace_scan()
    
    if scan_id:
        # Wait for completion
        if client.wait_for_scan_completion(scan_id):
            # Get results
            print("\n📥 Retrieving scan results...")
            results = client.get_scan_result(scan_id)
            
            if results:
                workspaces_data = results.get('workspaces', [])
                print(f"\n✅ Scan successful! Retrieved data for {len(workspaces_data)} workspaces")
                
                # Show sample
                if workspaces_data:
                    sample_ws = workspaces_data[0]
                    print(f"\nSample workspace data:")
                    print(f"   Name: {sample_ws.get('name')}")
                    print(f"   Reports: {len(sample_ws.get('reports', []))}")
                    print(f"   Datasets: {len(sample_ws.get('datasets', []))}")
