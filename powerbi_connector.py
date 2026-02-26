# powerbi_connector.py - Corrected for Client Credentials Flow
import requests
import json
from config import Config

class PowerBIConnector:
    """Handles all Power BI API interactions using client credentials"""
    
    def __init__(self):
        self.access_token = None
        self.base_url = "https://api.powerbi.com/v1.0/myorg"
        
    def authenticate(self):
        """Authenticate with Azure AD using client credentials (application permissions)"""
        print("🔑 Authenticating with Power BI using Client Credentials...")
        
        url = f"https://login.microsoftonline.com/{Config.TENANT_ID}/oauth2/v2.0/token"
        data = {
            "client_id": Config.CLIENT_ID,
            "scope": "https://analysis.windows.net/powerbi/api/.default",
            "client_secret": Config.CLIENT_SECRET,
            "grant_type": "client_credentials"
        }
        
        try:
            response = requests.post(url=url, data=data)
            response.raise_for_status()
            self.access_token = response.json().get("access_token")
            print("✅ Authentication successful!")
            return True
        except requests.exceptions.RequestException as e:
            print(f"❌ Authentication failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"Response: {e.response.text}")
            return False
    
    def _get_headers(self):
        """Get HTTP headers for API requests"""
        if not self.access_token:
            if not self.authenticate():
                raise Exception("Failed to authenticate with Power BI")
        
        return {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }
    
    def get_all_reports(self, workspace_id=None):
        """Get list of all reports in workspace"""
        if not workspace_id:
            workspace_id = Config.WORKSPACE_ID
            
        print(f"\n📊 Fetching reports from workspace...")
        
        url = f"{self.base_url}/groups/{workspace_id}/reports"
        
        try:
            response = requests.get(url, headers=self._get_headers())
            response.raise_for_status()
            
            reports = response.json().get('value', [])
            print(f"✅ Found {len(reports)} report(s)")
            
            if reports:
                print("\nReports in workspace:")
                for idx, report in enumerate(reports, 1):
                    print(f"   {idx}. {report['name']}")
            
            return reports
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching reports!")
            print(f"   Error: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"   Status Code: {e.response.status_code}")
                try:
                    error_detail = e.response.json()
                    print(f"   Details: {error_detail}")
                except:
                    print(f"   Response: {e.response.text[:200]}")
            return []
    
    def get_report_details(self, workspace_id, report_id, report_name="Report"):
        """Get detailed metadata for a specific report"""
        print(f"\n📄 Extracting metadata for: {report_name}")
        
        metadata = {
            'report_name': report_name,
            'report_id': report_id,
            'workspace_id': workspace_id
        }
        
        try:
            # 1. Get basic report info
            print("   → Getting report details...")
            url = f"{self.base_url}/groups/{workspace_id}/reports/{report_id}"
            response = requests.get(url, headers=self._get_headers())
            
            if response.status_code == 200:
                report_data = response.json()
                metadata.update({
                    'web_url': report_data.get('webUrl', 'N/A'),
                    'embed_url': report_data.get('embedUrl', 'N/A'),
                    'dataset_id': report_data.get('datasetId'),
                    'created_datetime': report_data.get('createdDateTime', 'Unknown'),
                    'modified_datetime': report_data.get('modifiedDateTime', 'Unknown'),
                    'description': report_data.get('description', '')
                })
                print("      ✓ Report details retrieved")
            else:
                print(f"      ⚠ Could not get report details (Status: {response.status_code})")
            
            # 2. Get dataset information
            if metadata.get('dataset_id'):
                print("   → Getting dataset details...")
                dataset_url = f"{self.base_url}/datasets/{metadata['dataset_id']}"
                dataset_response = requests.get(dataset_url, headers=self._get_headers())
                
                if dataset_response.status_code == 200:
                    dataset_data = dataset_response.json()
                    metadata.update({
                        'dataset_name': dataset_data.get('name', 'Unknown'),
                        'configured_by': dataset_data.get('configuredBy', 'Unknown'),
                        'is_refreshable': dataset_data.get('isRefreshable', False),
                        'target_storage_mode': dataset_data.get('targetStorageMode', 'Unknown')
                    })
                    print("      ✓ Dataset details retrieved")
                else:
                    print(f"      ⚠ Could not get dataset details (Status: {dataset_response.status_code})")
                
                # 3. Get data sources
                print("   → Getting data sources...")
                ds_url = f"{self.base_url}/datasets/{metadata['dataset_id']}/datasources"
                ds_response = requests.get(ds_url, headers=self._get_headers())
                
                if ds_response.status_code == 200:
                    metadata['data_sources'] = ds_response.json().get('value', [])
                    print(f"      ✓ Found {len(metadata.get('data_sources', []))} data source(s)")
                else:
                    metadata['data_sources'] = []
                    print("      ⚠ Could not retrieve data sources")
                
                # 4. Get refresh schedule
                print("   → Getting refresh schedule...")
                refresh_url = f"{self.base_url}/datasets/{metadata['dataset_id']}/refreshSchedule"
                refresh_response = requests.get(refresh_url, headers=self._get_headers())
                
                if refresh_response.status_code == 200:
                    metadata['refresh_schedule'] = refresh_response.json()
                    enabled = metadata['refresh_schedule'].get('enabled', False)
                    print(f"      ✓ Refresh schedule: {'Enabled' if enabled else 'Disabled'}")
                else:
                    metadata['refresh_schedule'] = None
                    print("      ⚠ Could not retrieve refresh schedule")
                
                # 5. Get refresh history
                print("   → Getting refresh history...")
                history_url = f"{self.base_url}/datasets/{metadata['dataset_id']}/refreshes?$top=5"
                history_response = requests.get(history_url, headers=self._get_headers())
                
                if history_response.status_code == 200:
                    metadata['refresh_history'] = history_response.json().get('value', [])
                    print(f"      ✓ Retrieved last {len(metadata.get('refresh_history', []))} refresh(es)")
                else:
                    metadata['refresh_history'] = []
                    print("      ⚠ Could not retrieve refresh history")
            else:
                print("   ⚠ No dataset ID found, skipping dataset details")
            
            # 6. Get report pages
            print("   → Getting report pages...")
            pages_url = f"{self.base_url}/groups/{workspace_id}/reports/{report_id}/pages"
            pages_response = requests.get(pages_url, headers=self._get_headers())
            
            if pages_response.status_code == 200:
                metadata['pages'] = pages_response.json().get('value', [])
                print(f"      ✓ Found {len(metadata.get('pages', []))} page(s)")
            else:
                metadata['pages'] = []
                print("      ⚠ Could not retrieve pages")
            
            print(f"✅ Metadata extraction complete for: {report_name}")
            return metadata
            
        except Exception as e:
            print(f"❌ Error extracting metadata: {str(e)}")
            import traceback
            traceback.print_exc()
            return metadata


# Test the connector
if __name__ == "__main__":
    print("="*70)
    print("POWER BI CONNECTOR TEST (Client Credentials)")
    print("="*70)
    
    # Validate configuration
    if not Config.validate():
        print("\n❌ Configuration invalid. Please check .env file.")
        exit(1)
    
    print("\n" + "="*70)
    print("Testing Power BI Connection")
    print("="*70)
    
    # Create connector and authenticate
    pbi = PowerBIConnector()
    
    if pbi.authenticate():
        print("\n" + "="*70)
        print("Fetching Reports from Workspace")
        print("="*70)
        
        # Get all reports
        reports = pbi.get_all_reports()
        
        if reports:
            print(f"\n✅ Successfully connected to Power BI!")
            print(f"✅ Found {len(reports)} report(s) in workspace")
            
            # Test getting details for first report
            if len(reports) > 0:
                print("\n" + "="*70)
                print("Testing Metadata Extraction")
                print("="*70)
                
                first_report = reports[0]
                print(f"\nExtracting metadata for: {first_report['name']}")
                
                metadata = pbi.get_report_details(
                    Config.WORKSPACE_ID,
                    first_report['id'],
                    first_report['name']
                )
                
                print("\n" + "="*70)
                print("Metadata Summary")
                print("="*70)
                print(f"Report Name: {metadata.get('report_name')}")
                print(f"Report ID: {metadata.get('report_id')}")
                print(f"Dataset: {metadata.get('dataset_name', 'N/A')}")
                print(f"Created: {metadata.get('created_datetime', 'Unknown')[:10]}")
                print(f"Modified: {metadata.get('modified_datetime', 'Unknown')[:10]}")
                print(f"Pages: {len(metadata.get('pages', []))}")
                print(f"Data Sources: {len(metadata.get('data_sources', []))}")
                print(f"Refreshable: {metadata.get('is_refreshable', 'Unknown')}")
                
                if metadata.get('refresh_schedule'):
                    schedule = metadata['refresh_schedule']
                    print(f"Refresh Enabled: {schedule.get('enabled', False)}")
                
                print("\n✅ Connector test successful!")
                print("\nAll Power BI connection features working correctly!")
        else:
            print("\n⚠️ No reports found in workspace")
            print("\nPossible reasons:")
            print("  1. Workspace ID is incorrect")
            print("  2. Service principal doesn't have access to the workspace")
            print("  3. Workspace doesn't contain any reports")
    else:
        print("\n❌ Connector test failed!")
        print("\nTroubleshooting:")
        print("  1. Check CLIENT_ID, CLIENT_SECRET, TENANT_ID in .env")
        print("  2. Verify service principal has Application permissions in Azure")
        print("  3. Ensure admin consent was granted")
        print("  4. Check service principal is a member of the workspace")
