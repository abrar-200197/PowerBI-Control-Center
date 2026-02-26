# ai_generator.py - Updated for Azure OpenAI (GPT-4o)
import os
import json
import requests
from dotenv import load_dotenv
from openai import AzureOpenAI

# Import the scanner connector (make sure scanner_connector.py exists)
try:
    from scanner_connector import PowerBIScanner
except ImportError:
    print("⚠️ scanner_connector.py not found. Backend expressions will be skipped.")
    PowerBIScanner = None

# Load environment variables
load_dotenv()

# ---------- POWER BI FETCHER ----------

class PowerBIDataFetcher:
    """Fetches data from Power BI API"""
    
    def __init__(self, client_id, client_secret, tenant_id, group_id):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self.group_id = group_id
        self.access_token = None
        self.base_url = "https://api.powerbi.com/v1.0/myorg"
    
    def get_access_token(self):
        """Retrieve Azure AD access token for Power BI API access"""
        print("   🔐 Getting Power BI access token...")
        
        url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": self.client_id,
            "scope": "https://analysis.windows.net/powerbi/api/.default",
            "client_secret": self.client_secret,
            "grant_type": "client_credentials"
        }
        
        try:
            response = requests.post(url=url, data=data)
            response.raise_for_status()
            self.access_token = response.json().get("access_token")
            print("   ✅ Access token obtained")
            return self.access_token
        except Exception as e:
            raise Exception(f"Token request failed: {e}")
    
    def _get_headers(self):
        """Get headers with access token"""
        if not self.access_token:
            self.get_access_token()
        return {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }
    
    def get_report_details(self, report_id):
        """Get report metadata"""
        print(f"   📊 Fetching report details for {report_id}...")
        url = f"{self.base_url}/groups/{self.group_id}/reports/{report_id}"
        try:
            response = requests.get(url, headers=self._get_headers())
            response.raise_for_status()
            data = response.json()
            print("   ✅ Report details retrieved")
            return data
        except Exception as e:
            print(f"   ❌ Error fetching report: {e}")
            return None
    
    def get_report_pages(self, report_id):
        """Get all pages in a report"""
        print(f"   📄 Fetching report pages...")
        url = f"{self.base_url}/groups/{self.group_id}/reports/{report_id}/pages"
        try:
            response = requests.get(url, headers=self._get_headers())
            response.raise_for_status()
            data = response.json()
            pages = data.get('value', [])
            visible_pages = [p for p in pages if not p.get('isHidden', False)]
            print(f"   ✅ Found {len(visible_pages)} visible pages")
            return visible_pages
        except Exception as e:
            print(f"   ❌ Error fetching pages: {e}")
            return []
    
    def get_dataset_details(self, dataset_id):
        """Get dataset metadata"""
        print(f"   🗄️  Fetching dataset details...")
        url = f"{self.base_url}/groups/{self.group_id}/datasets/{dataset_id}"
        try:
            response = requests.get(url, headers=self._get_headers())
            response.raise_for_status()
            data = response.json()
            print("   ✅ Dataset details retrieved")
            return data
        except Exception as e:
            print(f"   ❌ Error fetching dataset: {e}")
            return None
    
    def get_dataset_datasources(self, dataset_id):
        """Get dataset data sources (Basic info)"""
        print(f"   🔌 Fetching data sources...")
        url = f"{self.base_url}/groups/{self.group_id}/datasets/{dataset_id}/datasources"
        try:
            response = requests.get(url, headers=self._get_headers())
            response.raise_for_status()
            data = response.json()
            sources = data.get('value', [])
            print(f"   ✅ Found {len(sources)} data source(s)")
            return sources
        except Exception as e:
            print(f"   ❌ Error fetching data sources: {e}")
            return []

    # --- NEW METHODS FOR DETAILED QUERY EXTRACTION ---

    def get_dataset_refresh_details(self, dataset_id):
        """Get dataset refresh and query details"""
        print(f"   🔄 Fetching dataset refresh details...")
        url = f"{self.base_url}/datasets/{dataset_id}"
        
        # Try group endpoint first
        group_url = f"{self.base_url}/groups/{self.group_id}/datasets/{dataset_id}"
        
        try:
            response = requests.get(group_url, headers=self._get_headers())
            if response.status_code != 200:
                response = requests.get(url, headers=self._get_headers())
            
            response.raise_for_status()
            data = response.json()
            
            details = {
                'id': data.get('id'),
                'name': data.get('name'),
                'configuredBy': data.get('configuredBy'),
                'isRefreshable': data.get('isRefreshable'),
                'targetStorageMode': data.get('targetStorageMode'),
                'createdDate': data.get('createdDate'),
                'contentProviderType': data.get('contentProviderType')
            }
            print("   ✅ Dataset refresh details retrieved")
            return details
        except Exception as e:
            print(f"   ⚠️ Could not get refresh details: {e}")
            return None
    
    def get_detailed_datasources(self, dataset_id):
        """Get detailed data source information including connection strings"""
        print(f"   🔌 Fetching detailed data sources...")
        url = f"{self.base_url}/datasets/{dataset_id}/datasources"
        
        # Try group endpoint first
        group_url = f"{self.base_url}/groups/{self.group_id}/datasets/{dataset_id}/datasources"

        try:
            response = requests.get(group_url, headers=self._get_headers())
            if response.status_code != 200:
                response = requests.get(url, headers=self._get_headers())
                
            response.raise_for_status()
            data = response.json()
            sources = data.get('value', [])
            
            detailed_sources = []
            for s in sources:
                detailed_sources.append({
                    'datasourceType': s.get('datasourceType'),
                    'datasourceId': s.get('datasourceId'),
                    'gatewayId': s.get('gatewayId'),
                    'connectionDetails': s.get('connectionDetails', {}),
                    'connectionString': self._build_connection_string(s)
                })
            print(f"   ✅ Found {len(detailed_sources)} detailed source(s)")
            return detailed_sources
        except Exception as e:
            print(f"   ❌ Error fetching detailed data sources: {e}")
            return []
    
    def _build_connection_string(self, source):
        conn = source.get('connectionDetails', {})
        t = source.get('datasourceType', '')
        
        server = conn.get('server', 'N/A')
        db = conn.get('database', 'N/A')
        url = conn.get('url', 'N/A')
        path = conn.get('path', 'N/A')
        
        if t == 'Sql':
            return f"Server={server};Database={db};"
        elif t == 'AnalysisServices':
            return f"Data Source={server};Catalog={db};"
        elif t == 'Web':
            return f"URL={url}"
        elif t == 'File':
            return f"Path={path}"
        elif t == 'SharePointList':
            return f"Site={url}"
        else:
            return f"{t}: Server={server}, Database={db}"
    
    def get_dataset_parameters(self, dataset_id):
        """Get dataset parameters if any"""
        print(f"   ⚙️ Fetching dataset parameters...")
        url = f"{self.base_url}/groups/{self.group_id}/datasets/{dataset_id}/parameters"
        try:
            response = requests.get(url, headers=self._get_headers())
            if response.status_code == 200:
                data = response.json()
                params = data.get('value', [])
                print(f"   ✅ Found {len(params)} parameter(s)")
                return params
            else:
                print("   ⚠️ No parameters or access denied")
                return []
        except Exception as e:
            print(f"   ⚠️ Could not get parameters: {e}")
            return []

    def get_dataset_relationships(self, dataset_id):
        """Attempt to fetch relationships via the regular Power BI API.

        This is used as a fallback when the scanner returns no relationships.  The
        endpoint may require at least dataset-level permissions but does not
        require admin rights.
        """
        print(f"   🔗 Fetching dataset relationships (API fallback)...")
        # try group endpoint first
        url1 = f"{self.base_url}/groups/{self.group_id}/datasets/{dataset_id}/relationships"
        url2 = f"{self.base_url}/datasets/{dataset_id}/relationships"
        for url in (url1, url2):
            try:
                response = requests.get(url, headers=self._get_headers())
                if response.status_code == 200:
                    data = response.json()
                    rels = data.get('value', []) if isinstance(data, dict) else []
                    print(f"   ✅ Retrieved {len(rels)} relationships from API")
                    return rels
                else:
                    # continue to next URL
                    continue
            except Exception as e:
                print(f"   ⚠️ Error fetching relationships from {url}: {e}")
        return []

    def get_dataset_refresh_history(self, dataset_id, top=20):
        """Fetch the most recent refresh history entries for a dataset.

        Larger default (20) provides more coverage; callers may override via
        argument if needed.
        """
        print(f"   📅 Fetching dataset refresh history (top {top})...")
        url = f"{self.base_url}/groups/{self.group_id}/datasets/{dataset_id}/refreshes?$top={top}"
        try:
            response = requests.get(url, headers=self._get_headers())
            response.raise_for_status()
            data = response.json()
            history = data.get('value', [])
            print(f"   ✅ Retrieved {len(history)} refresh record(s)")
            return history
        except Exception as e:
            print(f"   ⚠️ Could not get refresh history: {e}")
            return []
    #   def get_all_workspaces(self):
    #     """Get all reports in workspace"""
    #     print(f"   📚 Fetching all reports in workspace...")
    #     url = f"{self.base_url}/groups/{self.group_id}/reports"
    #     try:
    #         response = requests.get(url, headers=self._get_headers())
    #         response.raise_for_status()
    #         data = response.json()
    #         reports = data.get('value', [])
    #         print(f"   ✅ Found {len(reports)} report(s)")
    #         return reports
    #     except Exception as e:
    #         print(f"   ❌ Error fetching reports: {e}")
    #         return []
           
    def get_all_reports_in_workspace(self):
        """Get all reports in workspace"""
        print(f"   📚 Fetching all reports in workspace...")
        url = f"{self.base_url}/groups/{self.group_id}/reports"
        try:
            response = requests.get(url, headers=self._get_headers())
            response.raise_for_status()
            data = response.json()
            reports = data.get('value', [])
            print(f"   ✅ Found {len(reports)} report(s)")
            return reports
        except Exception as e:
            print(f"   ❌ Error fetching reports: {e}")
            return []

    def get_all_datasets_in_workspace(self):
        """Get all datasets in workspace (semantic models)"""
        print(f"   📚 Fetching all datasets in workspace...")
        url = f"{self.base_url}/groups/{self.group_id}/datasets"
        try:
            response = requests.get(url, headers=self._get_headers())
            response.raise_for_status()
            data = response.json()
            datasets = data.get('value', [])
            print(f"   ✅ Found {len(datasets)} dataset(s)")
            return datasets
        except Exception as e:
            print(f"   ❌ Error fetching datasets: {e}")
            return []
    
    def get_complete_metadata(self, dataset_id, report_id=None, history_top=20):
        """Get all metadata for a report or dataset/model including scanner expressions.

        If `report_id` is provided the returned metadata will also include report
        details and pages. Otherwise the function focuses on the dataset/semantic
        model only.

        `history_top` controls how many refresh history entries to fetch.
        """
        kind = 'report' if report_id else 'dataset'
        print(f"\n📥 Fetching complete metadata for {kind}...")
        
        # 1. Standard API Metadata
        metadata = {}
        if report_id:
            metadata['report'] = self.get_report_details(report_id)
            metadata['pages'] = self.get_report_pages(report_id)
        else:
            metadata['report'] = None
            metadata['pages'] = []

        metadata['dataset'] = self.get_dataset_details(dataset_id)
        metadata['data_sources'] = self.get_dataset_datasources(dataset_id)
        metadata['detailed_sources'] = self.get_detailed_datasources(dataset_id)
        metadata['refresh_details'] = self.get_dataset_refresh_details(dataset_id)
        metadata['refresh_history'] = self.get_dataset_refresh_history(dataset_id, top=history_top)
        metadata['parameters'] = self.get_dataset_parameters(dataset_id)

        # 2. Scanner API (Admin) Metadata for Backend Expressions
        if PowerBIScanner:
            try:
                scanner = PowerBIScanner()
                # Get tables, columns, expressions, measures, and relationships
                print(f"   🔍 Calling scanner.get_dataset_model('{dataset_id}')...")
                model = scanner.get_dataset_model(dataset_id)

                metadata['model_tables'] = model.get('tables', [])
                metadata['model_columns'] = model.get('columns', {})
                metadata['expressions'] = model.get('expressions', [])
                metadata['measures'] = model.get('measures', [])  # ADDED
                metadata['relationships'] = model.get('relationships', [])  # ADDED

                print(f"   ✅ Scanner data extracted:")
                print(f"      - Tables: {len(metadata['model_tables'])}")
                print(f"      - Measures: {len(metadata['measures'])}")
                print(f"      - Relationships: {len(metadata['relationships'])}")
                print(f"      - Expressions: {len(metadata['expressions'])}")

                # DEBUG: Print first measure if available
                if metadata['measures']:
                    print(f"      - Sample measure: {metadata['measures'][0].get('name', 'N/A')}")

            except Exception as e:
                print(f"   ⚠️ Scanner model unavailable: {e}")
                import traceback
                traceback.print_exc()
                metadata['model_tables'] = []
                metadata['model_columns'] = {}
                metadata['expressions'] = []
                metadata['measures'] = []
                metadata['relationships'] = []
        else:
            print("   ⚠️ Scanner module not available, skipping backend expressions.")
            metadata['expressions'] = []
            metadata['measures'] = []
            metadata['relationships'] = []

        # if scanner didn't provide any relationships, try the REST API directly
        if not metadata.get('relationships'):
            rels = self.get_dataset_relationships(dataset_id)
            if rels:
                metadata['relationships'] = rels
                print(f"   🔁 Fallback: relationships populated via API ({len(rels)})")

        # 3. Extract common fields
        if metadata['report']:
            metadata['report_name'] = metadata['report'].get('name', 'Unknown Report')
            metadata['report_id'] = report_id
        else:
            metadata['report_name'] = 'Unknown Report'
            metadata['report_id'] = report_id
        
        if metadata['dataset']:
            metadata['dataset_name'] = metadata['dataset'].get('name', 'Unknown Dataset')
            metadata['dataset_id'] = dataset_id
        else:
            metadata['dataset_name'] = 'Unknown Dataset'
            metadata['dataset_id'] = dataset_id
        
        print(f"✅ Metadata collection complete\n")
        return metadata


# ---------- AI DOC GENERATOR (Azure OpenAI) ----------

class AIDocGenerator:
    """Uses Azure OpenAI to generate documentation content"""
    
    def __init__(self, openai_api_key=None):
        # Configuration for Azure OpenAI
        self.api_key = openai_api_key or os.getenv('OPENAI_API_KEY')
        self.azure_endpoint = "https://agr-dev-openai.openai.azure.com/"
        self.api_version = "2024-12-01-preview"
        self.deployment_name = "gpt-4o"  # Your deployment name
        
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not set")
        
        # Initialize Azure OpenAI Client
        self.client = AzureOpenAI(
            api_key=self.api_key,
            api_version=self.api_version,
            azure_endpoint=self.azure_endpoint
        )
    
    def _call_openai(self, prompt, max_tokens=1000):
        try:
            resp = self.client.chat.completions.create(
                model=self.deployment_name,  # Use deployment name here
                messages=[
                    {"role": "system", "content": "You are a technical documentation expert specializing in Power BI and business intelligence."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=max_tokens
            )
            return resp.choices[0].message.content
        except Exception as e:
            print(f"   ❌ Azure OpenAI Error: {e}")
            return None

    def generate_overview(self, metadata):
        print("   🤖 AI generating overview...")
        report_name = metadata.get('report_name', 'Unknown Report')
        dataset_name = metadata.get('dataset_name', 'Unknown Dataset')
        pages = metadata.get('pages', [])
        data_sources = metadata.get('data_sources', [])
        
        page_names = [p.get('displayName', p.get('name', 'Unnamed')) for p in pages]
        page_list = "\n".join([f"- {n}" for n in page_names]) if page_names else "- No pages found"
        source_types = list(set([ds.get('datasourceType', 'Unknown') for ds in data_sources]))
        prompt = f"""
You are a Power BI documentation expert.

Create a comprehensive Report Overview section for this Power BI report:

**Report Details:**
- Report Name: {report_name}
- Dataset: {dataset_name}
- Number of Pages: {len(pages)}
- Page Names:
{page_list}
- Data Source Types: {', '.join(source_types) if source_types else 'Not specified'}

Write 3-4 paragraphs covering:
1. Report Purpose
2. Business Objective
3. Target Audience
4. Key Insights

Write in professional, clear business language.
"""
        content = self._call_openai(prompt, 800)
        if content:
            print("   ✅ Overview generated")
            return content
        return f"## Report Overview\n\n**{report_name}** provides analytical insights using data from {dataset_name}."

    def generate_data_sources_doc(self, data_sources):
        print("   🤖 AI documenting data sources...")
        if not data_sources:
            return "## Data Sources\n\nNo data source information available."
        
        details = []
        for i, ds in enumerate(data_sources, 1):
            t = ds.get('datasourceType', 'Unknown')
            conn = ds.get('connectionDetails', {})
            details.append(f"**Source {i}:**\n- Type: {t}\n- Server: {conn.get('server', conn.get('url', 'N/A'))}\n- Database: {conn.get('database', 'N/A')}\n- Path: {conn.get('path', 'N/A')}\n")
        
        sources_text = "\n".join(details)
        prompt = f"""
Analyze these Power BI data sources and create professional documentation:

{sources_text}

Provide:
1. Data Architecture Overview
2. Source Descriptions
3. Data Flow
4. Dependencies
5. Refresh Considerations
"""
        content = self._call_openai(prompt, 1200)
        if content:
            print("   ✅ Data sources documented")
            return content
        return f"## Data Sources\n\n{sources_text}"

    def generate_pages_documentation(self, pages):
        print("   🤖 AI documenting report pages...")
        if not pages:
            return "## Report Pages\n\nNo page information available."
        
        pages_info = []
        for idx, p in enumerate(pages, 1):
            name = p.get('displayName', p.get('name', 'Unnamed'))
            order = p.get('order', idx)
            pages_info.append(f"{idx}. **{name}** (Order: {order})")
        
        pages_list = "\n".join(pages_info)
        prompt = f"""
Document these Power BI report pages:

{pages_list}

For each page, explain:
1. Purpose
2. Key metrics
3. Use cases
4. Navigation tips
"""
        content = self._call_openai(prompt, 1500)
        if content:
            print("   ✅ Pages documented")
            return content
        return f"## Report Pages\n\n{pages_list}"

    def generate_user_guide(self, pages, report_name):
        print("   🤖 AI creating user guide...")
        if not pages:
            return "## User Guide\n\nUser guide not available."
        
        page_names = [p.get('displayName', p.get('name', 'Unnamed')) for p in pages]
        pages_text = "\n".join([f"{idx}. {n}" for idx, n in enumerate(page_names, 1)])
        
        prompt = f"""
Create a comprehensive user guide for the Power BI report: {report_name}

Report Pages:
{pages_text}

Include:
- Getting Started
- Page-by-Page Guide
- Tips & Best Practices
"""
        content = self._call_openai(prompt, 2000)
        if content:
            print("   ✅ User guide created")
            return content
        return f"## User Guide\n\nPages:\n{pages_text}"

    def generate_technical_details(self, metadata):
        print("   🤖 AI generating technical details...")
        report = metadata.get('report', {}) or {}
        dataset = metadata.get('dataset', {}) or {}
        report_id = metadata.get('report_id', 'N/A')
        dataset_id = metadata.get('dataset_id', 'N/A')
        prompt = f"""
Create technical documentation for this Power BI report:

Report ID: {report_id}
Report Name: {metadata.get('report_name', 'Unknown')}
Web URL: {report.get('webUrl', 'N/A')}
Dataset ID: {dataset_id}
Dataset Name: {metadata.get('dataset_name', 'Unknown')}
Refreshable: {dataset.get('isRefreshable', 'Unknown')}
Storage Mode: {dataset.get('targetStorageMode', 'Unknown')}

Include:
- Technical Specs
- Refresh Config
- Permissions & Access
- Maintenance Notes
"""
        content = self._call_openai(prompt, 1500)
        if content:
            print("   ✅ Technical details generated")
            return content
        return f"## Technical Details\n\nReport ID: {report_id}\nDataset ID: {dataset_id}"

    def generate_migration_steps(self, report_name):
        print("   🤖 AI generating migration procedure...")
        prompt = f"""
Create a detailed migration procedure for this Power BI report: {report_name}

Include:
- Pre-Migration Checklist
- Backup Steps
- Deployment Steps
- Security & Permissions
- Validation
- Rollback
"""
        content = self._call_openai(prompt, 2000)
        if content:
            print("   ✅ Migration procedure generated")
            return content
        return f"## Migration Procedure\n\nMigration procedure for {report_name} not available."

    # ---------- NEW SEMANTIC MODEL DOCUMENTATION ----------
    def generate_semantic_model_doc(self, metadata):
        """Generate documentation focused solely on the semantic model."""
        print("   🤖 AI generating semantic model documentation...")
        tables = metadata.get('model_tables', [])
        columns = metadata.get('model_columns', {})
        measures = metadata.get('measures', [])
        relationships = metadata.get('relationships', [])
        expressions = metadata.get('expressions', [])
        data_sources = metadata.get('data_sources', [])
        refresh_hist = metadata.get('refresh_history', [])
        refresh_schedule = metadata.get('refresh_details') or metadata.get('refresh_schedule') or {}

        tab_list = "\n".join([f"- {t}" for t in tables]) if tables else "- None"
        measure_list = "\n".join([f"- {m.get('name','')}" for m in measures]) if measures else "- None"
        rel_list = "\n".join([f"- {r.get('fromTable')} -> {r.get('toTable')}" for r in relationships]) if relationships else "- None"
        src_list = "\n".join([f"- {ds.get('datasourceType','')}" for ds in data_sources]) if data_sources else "- None"
        refresh_list = "\n".join([f"- {rh.get('startTime','')} : {rh.get('status','')}" for rh in refresh_hist]) if refresh_hist else "- None"
        schedule_text = "\n".join([f"{k}: {v}" for k, v in refresh_schedule.items()]) if refresh_schedule else "None"

        expr_summary = []
        for expr in expressions:
            expr_summary.append(f"Table: {expr.get('table')}  Expression: {expr.get('expression','')[:80]}...")
        expr_text = "\n".join(expr_summary) if expr_summary else "No expressions"

        prompt = f"""
You are a Power BI semantic model documentation expert.

Produce a markdown section describing the semantic model for the following dataset.

Tables:
{tab_list}

Columns by table:
{json.dumps(columns, indent=2)[:1000]}

DAX Measures:
{measure_list}

Relationships:
{rel_list}

Data Sources:
{src_list}

Refresh Schedule Details:
{schedule_text}

Refresh History (most recent):
{refresh_list}

SQL / M Expressions (sample):
{expr_text}

Write clear bullet lists and brief explanations for each component. Focus on the model structure and technical details rather than business usage.
"""
        content = self._call_openai(prompt, 2000)
        if content:
            print("   ✅ Semantic model documentation generated")
            return content
        # fallback
        fallback = (
            "## Semantic Model Overview\n"
            f"Tables:\n{tab_list}\n\n"
            f"Measures:\n{measure_list}\n\n"
            f"Relationships:\n{rel_list}\n\n"
            f"Refresh History:\n{refresh_list}\n"
        )
        return fallback


def     generate_complete_documentation(client_id, client_secret, tenant_id, group_id, report_id, dataset_id, openai_api_key=None, semantic_only=False, history_top=20, use_ai=True):
    print("\n" + "="*60)
    if semantic_only:
        print("🚀 Power BI SEMANTIC MODEL Documentation Generator")
    else:
        print("🚀 Power BI Documentation Generator")
    print("="*60 + "\n")
    
    fetcher = PowerBIDataFetcher(client_id, client_secret, tenant_id, group_id)
    ai_gen = AIDocGenerator(openai_api_key) if use_ai else None
    # metadata call supports optional report_id; when semantic_only we may not
    # have or need a report
    if semantic_only:
        metadata = fetcher.get_complete_metadata(dataset_id, report_id=None, history_top=history_top)
    else:
        metadata = fetcher.get_complete_metadata(dataset_id, report_id, history_top=history_top)
    
    print("📝 Generating documentation sections...\n")

    # if the caller wants only metadata, skip all AI work
    if not use_ai:
        if semantic_only:
            documentation = {
                'semantic_model': '',
                'metadata': metadata
            }
        else:
            documentation = {
                'overview': '',
                'data_sources': '',
                'pages': '',
                'user_guide': '',
                'technical_details': '',
                'migration': '',
                'metadata': metadata
            }
        print("⚠️ AI generation skipped; returning metadata-only structure.")
        return documentation
    
    if semantic_only:
        documentation = {
            'semantic_model': ai_gen.generate_semantic_model_doc(metadata),
            'metadata': metadata,
            'relationships': metadata.get('relationships', [])
        }
    else:
        documentation = {
            'overview': ai_gen.generate_overview(metadata),
            'data_sources': ai_gen.generate_data_sources_doc(metadata.get('data_sources', [])),
            'pages': ai_gen.generate_pages_documentation(metadata.get('pages', [])),
            'user_guide': ai_gen.generate_user_guide(metadata.get('pages', []), metadata.get('report_name', 'Report')),
            'technical_details': ai_gen.generate_technical_details(metadata),
            'migration': ai_gen.generate_migration_steps(metadata.get('report_name', 'Report')),
            'metadata': metadata,
            'relationships': metadata.get('relationships', [])
        }
    
    print("\n" + "="*60)
    print("✅ Documentation generation complete!")
    print("="*60 + "\n")
    
    return documentation


# ---------- Metadata-only helper ----------

def collect_metadata(client_id, client_secret, tenant_id, group_id,
                     dataset_id, report_id=None, history_top=20):
    """Return only the raw Power BI metadata without invoking OpenAI.

    This is useful when you need a "perfect" JSON file for other projects;
    it simply delegates to ``PowerBIDataFetcher.get_complete_metadata`` and
    does not call any of the AI generation routines.
    """
    fetcher = PowerBIDataFetcher(client_id, client_secret, tenant_id, group_id)
    return fetcher.get_complete_metadata(dataset_id, report_id, history_top=history_top)


if __name__ == "__main__":
    CLIENT_ID = os.getenv('CLIENT_ID')
    CLIENT_SECRET = os.getenv('CLIENT_SECRET')
    TENANT_ID = os.getenv('TENANT_ID')
    GROUP_ID = os.getenv('WORKSPACE_ID')
    REPORT_ID = os.getenv('TEST_REPORT_ID', '')
    DATASET_ID = os.getenv('TEST_DATASET_ID', '')
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

    if not (CLIENT_ID and CLIENT_SECRET and TENANT_ID and GROUP_ID and OPENAI_API_KEY):
        print("Please check your .env file for required credentials.")
        exit(1)

    if DATASET_ID:
        # For ad hoc execution you can also choose to skip the AI step by
        # setting use_ai=False.  This creates a JSON file with metadata only.
        docs = generate_complete_documentation(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            tenant_id=TENANT_ID,
            group_id=GROUP_ID,
            report_id=REPORT_ID or None,
            dataset_id=DATASET_ID,
            openai_api_key=OPENAI_API_KEY,
            semantic_only=(not bool(REPORT_ID)),
            use_ai=True  # set False to disable OpenAI calls
        )

        docs_to_save = {k: v for k, v in docs.items() if k != 'metadata'}
        with open('powerbi_documentation.json', 'w', encoding='utf-8') as f:
            json.dump(docs_to_save, f, indent=2, ensure_ascii=False)
        print("💾 Saved powerbi_documentation.json")
    else:
        print("Run main.py for interactive mode.")