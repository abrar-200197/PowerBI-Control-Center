# crash_test_analyzer.py - Power BI Report Health & Crash Diagnostics
"""
Comprehensive Power BI Report Crash Test Analyzer

Performs deep analysis to identify:
- Broken visuals (missing fields, invalid references)
- Data connectivity issues (refresh failures, timeouts)
- Configuration errors (invalid filters, broken expressions)
- Schema mismatches (visuals using non-existent columns)

Author: Power BI Control Center
Date: 2026-06-03
"""

import requests
import json
import re
import asyncio
from datetime import datetime, timedelta

# Import combined metadata fetcher
try:
    from combined_metadata_fetcher import CombinedMetadataFetcher
except ImportError:
    print("⚠️ combined_metadata_fetcher.py not found. Visual analysis will be limited.")
    CombinedMetadataFetcher = None


class CrashTestAnalyzer:
    """Analyzes Power BI reports for crashes, broken visuals, and data issues"""

    def __init__(self, workspace_id, report_id, dataset_id, access_token, client_id=None, client_secret=None, tenant_id=None, user_token=None):
        """
        Initialize crash test analyzer

        Args:
            workspace_id: Power BI workspace ID
            report_id: Power BI report ID
            dataset_id: Power BI dataset ID
            access_token: Valid Power BI access token
            client_id: Azure AD client ID (for visual extraction)
            client_secret: Azure AD client secret (for visual extraction)
            tenant_id: Azure AD tenant ID (for visual extraction)
            user_token: Signed-in user's SSO token (for Playwright embed)
        """
        self.workspace_id = workspace_id
        self.report_id = report_id
        self.dataset_id = dataset_id
        self.access_token = access_token or user_token
        self.base_url = "https://api.powerbi.com/v1.0/myorg"

        # Credentials for enhanced metadata
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self.user_token = user_token or access_token

        # Results storage
        self.issues = []
        self.warnings = []
        self.dataset_schema = {}
        self.report_metadata = {}
        self.visual_metadata = []  # NEW: Store visual metadata
        
        print(f"\n{'='*80}")
        print(f"🔬 CRASH TEST ANALYZER INITIALIZED")
        print(f"   Report ID: {report_id}")
        print(f"   Dataset ID: {dataset_id}")
        print(f"{'='*80}\n")

    def run_crash_test(self, include_visual_analysis=True, include_lineage_analysis=True, include_version_history=True, use_xmla_schema=False):
        """
        Execute comprehensive crash test analysis with lineage integration

        Args:
            include_visual_analysis: Whether to perform deep visual analysis (requires browser automation)
            include_lineage_analysis: Whether to include visual lineage for impact analysis
            include_version_history: Whether to include version history analysis
            use_xmla_schema: Whether to use XMLA schema analysis

        Returns:
            dict: Analysis results with structure:
                {
                    'health_score': 0-100,
                    'status': 'healthy' | 'warning' | 'critical',
                    'issues': [...],
                    'warnings': [...],
                    'summary': {...},
                    'recommendations': [],
                    'visual_analysis_performed': bool,
                    'lineage_analysis': {...}  # NEW!
                }
        """
        print(f"\n🔬 STARTING ENHANCED CRASH TEST ANALYSIS (WITH LINEAGE)\n")

        # Phase 1: Dataset Health Check
        print("1️⃣  Phase 1: Dataset Health Check")
        self._check_dataset_health()

        # Phase 2: Schema Analysis
        print("\n2️⃣  Phase 2: Dataset Schema Analysis")
        self._analyze_dataset_schema()

        # Phase 3: Visual Integrity Check (ENHANCED if credentials provided)
        print("\n3️⃣  Phase 3: Visual Integrity Check")
        visual_analysis_performed = False

        can_run_enhanced = CombinedMetadataFetcher and (
            all([self.client_id, self.client_secret, self.tenant_id]) or self.user_token
        )
        if include_visual_analysis and can_run_enhanced:
            print("   🚀 Using ENHANCED visual analysis (JavaScript Embed API + render scan)")
            try:
                # Run async visual extraction
                # CRITICAL FIX: Must actually await or run the async function!
                try:
                    # Check if there's already an event loop running
                    loop = asyncio.get_running_loop()
                    # We're already in an async context - cannot use asyncio.run()
                    print("   ⚠️  Already in async context - this should not happen in sync run_crash_test()")
                    print("   🔄 Falling back to standard visual check...")
                    self._check_visual_integrity()
                    visual_analysis_performed = False
                except RuntimeError:
                    # No event loop running - safe to create one with asyncio.run()
                    print("   ℹ️  Creating new event loop for visual analysis...")
                    try:
                        visual_analysis_performed = asyncio.run(self._check_visual_integrity_enhanced())
                        print(f"   ✅ Enhanced visual analysis completed: {visual_analysis_performed}")
                    except Exception as async_error:
                        print(f"   ⚠️  Async visual analysis failed: {async_error}")
                        import traceback
                        traceback.print_exc()
                        print(f"   🔄 Falling back to standard visual check...")
                        self._check_visual_integrity()
                        visual_analysis_performed = False
            except Exception as e:
                print(f"   ⚠️  Enhanced visual analysis failed: {e}")
                import traceback
                traceback.print_exc()

                # Check if the error is due to Playwright not being installed
                error_str = str(e).lower()
                if 'playwright' in error_str or 'chromium' in error_str or 'browser' in error_str:
                    print(f"   ❌ Playwright/Chromium not available in this environment")
                    print(f"   💡 Install Playwright with: pip install playwright && playwright install chromium")
                    self.warnings.append({
                        'category': 'Visual Integrity',
                        'severity': 'Warning',
                        'message': 'Enhanced visual analysis unavailable - Playwright not installed',
                        'description': 'The visual error detection feature requires Playwright and Chromium browser to be installed in the deployment environment.',
                        'recommendation': 'Install Playwright in the Docker container or Azure App Service. Add "playwright install chromium" to your deployment script.'
                    })

                print(f"   🔄 Falling back to standard visual check...")
                self._check_visual_integrity()
                visual_analysis_performed = False
        else:
            if include_visual_analysis and not can_run_enhanced:
                print("   ℹ️  Enhanced visual analysis requires client credentials or a signed-in user token")
            self._check_visual_integrity()
            visual_analysis_performed = False

        # Phase 4: Expression Validation
        print("\n4️⃣  Phase 4: Expression Validation")
        self._validate_expressions()
        
        # Phase 5: Filter Analysis
        print("\n5️⃣  Phase 5: Filter Configuration Analysis")
        self._analyze_filters()

        # Phase 6: Lineage-Based Impact Analysis (NEW!)
        lineage_analysis = {}
        if include_lineage_analysis and self.visual_metadata:
            print("\n6️⃣  Phase 6: Lineage-Based Impact Analysis")
            lineage_analysis = self._analyze_lineage_impact()

        # Calculate health score
        health_score = self._calculate_health_score()

        # Generate summary
        summary = self._generate_summary(health_score)

        # Generate recommendations (enhanced with lineage)
        recommendations = self._generate_recommendations(lineage_analysis)

        print(f"\n{'='*80}")
        print(f"✅ ENHANCED CRASH TEST COMPLETE")
        print(f"   Health Score: {health_score}/100")
        print(f"   Issues Found: {len(self.issues)}")
        print(f"   Warnings: {len(self.warnings)}")
        if lineage_analysis:
            print(f"   Lineage Analysis: {lineage_analysis.get('affected_tables_count', 0)} tables analyzed")
        print(f"{'='*80}\n")

        # Build root cause analysis from issues
        root_cause_analysis = []
        for issue in self.issues:
            if issue.get('root_cause') or issue.get('missing_fields'):
                root_cause_analysis.append({
                    'visual': issue.get('visual_name', issue.get('visual', 'Unknown')),
                    'page': issue.get('page', 'Unknown'),
                    'root_cause': issue.get('root_cause'),
                    'missing_fields': issue.get('missing_fields', []),
                    'error_type': issue.get('error_type'),
                    'error_reason': issue.get('error_reason'),  # NEW: Add human-readable error reason
                    'recommendation': issue.get('recommendation'),
                    'modified_by': issue.get('modified_by', 'N/A'),  # NEW: Add modified_by from Scanner API
                    'modified_date': issue.get('modified_date'),  # NEW: Add modified_date from Scanner API
                    'likely_culprit': issue.get('modified_by', 'N/A')  # NEW: Alias for frontend compatibility
                })

        # Build change impact summary (version history analysis)
        change_impact_summary = {}
        if include_version_history:
            # Extract modification info from issues that have it
            breaking_changes = []
            for issue in self.issues:
                if issue.get('category') == 'Broken Visual':
                    breaking_changes.append({
                        'visual': issue.get('visual_name', issue.get('visual', 'Unknown')),
                        'page': issue.get('page', 'Unknown'),
                        'type': issue.get('error_type', 'Unknown'),
                        'broken_fields': issue.get('missing_fields', []),
                        'modified_by': issue.get('modified_by', 'N/A'),  # Will be populated if available
                        'modified_date': issue.get('modified_date', None)
                    })

            change_impact_summary = {
                'breaking_changes': breaking_changes,
                'total_breaking_changes': len(breaking_changes)
            }

        return {
            'health_score': health_score,
            'status': self._get_health_status(health_score),
            'issues': self.issues,
            'warnings': self.warnings,
            'summary': summary,
            'recommendations': recommendations,
            'lineage_analysis': lineage_analysis,
            'root_cause_analysis': root_cause_analysis,  # NEW!
            'change_impact_summary': change_impact_summary,  # NEW!
            'visual_analysis_performed': visual_analysis_performed
        }

    def _check_dataset_health(self):
        """Check dataset refresh history for failures"""
        try:
            url = f"{self.base_url}/groups/{self.workspace_id}/datasets/{self.dataset_id}/refreshes?$top=5"
            headers = {'Authorization': f'Bearer {self.access_token}'}
            
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                refreshes = response.json().get('value', [])
                print(f"   ✓ Retrieved {len(refreshes)} refresh history entries")
                
                # Check for failures
                failures = 0
                for refresh in refreshes:
                    status = refresh.get('status', 'Unknown')
                    end_time = refresh.get('endTime', '')

                    if status == 'Failed':
                        failures += 1

                        # Extract detailed error message from serviceExceptionJson
                        error_message = 'Unknown error'
                        try:
                            service_exception = refresh.get('serviceExceptionJson')
                            if service_exception:
                                import json
                                if isinstance(service_exception, str):
                                    error_data = json.loads(service_exception)
                                else:
                                    error_data = service_exception

                                # Extract the most relevant error message
                                error_message = error_data.get('errorDescription',
                                               error_data.get('errorCode',
                                               error_data.get('message', 'Unknown error')))
                        except Exception as e:
                            print(f"   ⚠️  Could not parse error details: {e}")

                        self.issues.append({
                            'category': 'Dataset Health',
                            'severity': 'Critical',
                            'page': 'N/A',
                            'visual': 'N/A',
                            'description': f'Dataset refresh failed at {end_time}',
                            'error_message': error_message,  # NEW: Detailed error message
                            'recommendation': 'Check data source connectivity and credentials'
                        })
                    elif status == 'Disabled':
                        self.warnings.append({
                            'category': 'Dataset Health',
                            'severity': 'Warning',
                            'description': 'Dataset refresh is disabled',
                            'recommendation': 'Enable scheduled refresh to keep data current'
                        })
                
                if failures > 2:
                    self.issues.append({
                        'category': 'Dataset Health',
                        'severity': 'Critical',
                        'page': 'N/A',
                        'visual': 'N/A',
                        'description': f'{failures} out of last 5 refreshes failed',
                        'recommendation': 'Investigate data source issues immediately'
                    })
                    print(f"   ❌ CRITICAL: {failures}/5 recent refreshes failed")
                elif failures > 0:
                    print(f"   ⚠️  WARNING: {failures}/5 recent refreshes failed")
                else:
                    print(f"   ✅ All recent refreshes successful")
            else:
                self.warnings.append({
                    'category': 'Dataset Health',
                    'severity': 'Warning',
                    'description': f'Unable to retrieve refresh history (HTTP {response.status_code})',
                    'recommendation': 'Verify dataset permissions'
                })
                print(f"   ⚠️  Could not retrieve refresh history")
                
        except Exception as e:
            print(f"   ❌ Error checking dataset health: {e}")
            self.warnings.append({
                'category': 'Dataset Health',
                'severity': 'Warning',
                'description': f'Dataset health check failed: {str(e)}',
                'recommendation': 'Verify API permissions and dataset access'
            })

    def _analyze_dataset_schema(self):
        """Fetch and analyze dataset schema (tables, columns, measures)"""
        try:
            # Try to get schema via Scanner API or dataset tables endpoint
            from scanner_connector import PowerBIScanner

            scanner = PowerBIScanner()
            model = scanner.get_dataset_model(self.dataset_id, workspace_id=self.workspace_id)

            if model:
                # Extract schema
                tables = model.get('tables', [])
                columns = model.get('columns', {})
                measures = model.get('measures', [])

                print(f"   ✓ Found {len(tables)} tables")
                print(f"   ✓ Found {sum(len(cols) for cols in columns.values())} columns")
                print(f"   ✓ Found {len(measures)} measures")

                # Store schema for reference
                self.dataset_schema = {
                    'tables': tables,
                    'columns': columns,  # Dict: table_name -> [column_names]
                    'measures': {m.get('Name', m.get('name', '')): m for m in measures}
                }
            else:
                print(f"   ⚠️  Could not retrieve dataset schema")
                self.warnings.append({
                    'category': 'Schema Analysis',
                    'severity': 'Warning',
                    'description': 'Dataset schema unavailable',
                    'recommendation': 'Enable Scanner API permissions for detailed analysis'
                })

        except Exception as e:
            print(f"   ❌ Error analyzing schema: {e}")
            self.warnings.append({
                'category': 'Schema Analysis',
                'severity': 'Warning',
                'description': f'Schema analysis failed: {str(e)}',
                'recommendation': 'Check Scanner API permissions'
            })

    async def _check_visual_integrity_enhanced(self):
        """
        ENHANCED visual integrity check using JavaScript Embed API
        Provides actual visual metadata instead of relying on Scanner API
        NOW WITH: Schema validation and root-cause analysis
        """
        try:
            fetcher = CombinedMetadataFetcher(
                self.client_id,
                self.client_secret,
                self.tenant_id,
                user_token=self.user_token
            )

            # Extract visual metadata AND walk rendered pages for runtime errors
            result = await fetcher.visual_extractor.extract_visuals(
                self.workspace_id,
                self.report_id,
                timeout=180,
                detect_render_errors=True
            )

            if not result.get('success'):
                error_msg = result.get('error', '')
                print(f"   ❌ Visual extraction failed: {error_msg}")

                # Check if error contains broken visual information (e.g., "Report render error: {'message': 'Missing_References'...}")
                if 'Report render error:' in error_msg or 'Report load error:' in error_msg:
                    # Parse the error to extract broken visual info
                    import re
                    import ast

                    # Extract the error dict from the message
                    match = re.search(r"Report (?:render|load) error: (.+)$", error_msg)
                    if match:
                        try:
                            error_dict = ast.literal_eval(match.group(1))
                            visual_title = None
                            error_type = error_dict.get('message', 'Unknown')

                            # Extract visual title from detailedMessage
                            detailed_msg = error_dict.get('detailedMessage', '')
                            visual_match = re.search(r'visual titled: (.+?)(?:,|$)', detailed_msg)
                            if visual_match:
                                visual_title = visual_match.group(1)

                            if visual_title:
                                print(f"   ⚠️  Detected broken visual from error: '{visual_title}' ({error_type})")

                                # Get modification metadata
                                report_meta = getattr(self, 'report_metadata', {})
                                modified_by = report_meta.get('modified_by', 'N/A')
                                modified_date = report_meta.get('modified_date', None)

                                # Create issue for this broken visual
                                self.issues.append({
                                    'category': 'Broken Visual',
                                    'severity': 'Critical',
                                    'page': 'Unknown',  # Can't determine page from error alone
                                    'visual': visual_title,
                                    'visual_name': visual_title,
                                    'visual_type': 'Unknown',
                                    'message': f'Visual "{visual_title}" failed to render',
                                    'error_type': error_type,
                                    'error_reason': detailed_msg,
                                    'missing_fields': [],
                                    'root_cause': f"Visual rendering failed: {error_type}",
                                    'description': detailed_msg or 'This visual contains field reference errors or data binding issues.',
                                    'recommendation': 'Check if referenced fields exist in the dataset and verify data source connections.',
                                    'modified_by': modified_by,
                                    'modified_date': modified_date
                                })
                                print(f"   ✅ Created issue for broken visual: {visual_title}")
                                return True  # Analysis performed, even though extraction failed
                        except Exception as parse_error:
                            print(f"   ⚠️  Could not parse error details: {parse_error}")

                return False

            # Store visual metadata
            self.visual_metadata = result.get('pages', [])
            if result.get('render_scan_error'):
                self.warnings.append({
                    'category': 'Visual Integrity',
                    'severity': 'Warning',
                    'message': 'Runtime render scan could not complete — broken visuals on canvas may be missed',
                    'description': str(result.get('render_scan_error')),
                    'recommendation': 'Re-run Crash Test after confirming Playwright/Chromium is available and you are signed in.'
                })

            # Analyze visuals for issues
            total_visuals = sum(len(page.get('visuals', [])) for page in self.visual_metadata)
            print(f"   ✓ Analyzing {total_visuals} visuals across {len(self.visual_metadata)} pages")
            print(f"   ℹ️  Extraction method: {result.get('method', 'unknown')}")

            broken_visuals = 0
            blank_visuals = 0
            missing_titles = 0

            for page in self.visual_metadata:
                page_name = page.get('displayName', 'Unknown')
                page_has_errors = page.get('hasErrors', False)  # Check if page has error messages
                page_errors = page.get('errors', [])  # NEW: Get individual error details

                # Process individual visual errors from this page (BROKEN VISUALS)
                if page_has_errors and page_errors:
                    print(f"   🔴 Page '{page_name}' contains {len(page_errors)} broken visual(s)!")

                    # Create a specific issue for EACH broken visual
                    for error in page_errors:
                        broken_visuals += 1
                        visual_title = error.get('visualTitle', '')

                        # Handle undefined or empty visual titles
                        if not visual_title or visual_title == 'undefined':
                            visual_title = f'Untitled Visual {broken_visuals}'

                        error_message = error.get('message', 'Unknown error')
                        detailed_message = error.get('detailedMessage', '')

                        # Format the error reason for better readability
                        error_reason = self._format_error_reason(error_message, detailed_message)

                        # NEW: Use visual lineage to extract missing fields
                        # First try to extract from error message
                        missing_fields = self._extract_missing_fields(detailed_message, error_message)

                        # If no fields found in error message, use visual lineage approach
                        if not missing_fields:
                            # Find the broken visual in the page's visual list
                            print(f"         🔍 Searching for visual '{visual_title}' in page visual list to extract field lineage...")
                            found_visual = False
                            for visual in page.get('visuals', []):
                                visual_name = visual.get('title', '')
                                if visual_name == visual_title or (not visual_name and 'Untitled' in visual_title):
                                    found_visual = True
                                    # Found the visual - extract its field bindings
                                    fields = visual.get('fields', [])
                                    if fields:
                                        print(f"         💡 Found {len(fields)} field(s) in broken visual")
                                        missing_fields = []
                                        for field in fields:
                                            table_name = field.get('table', 'Unknown')
                                            # Use 'name' key (set by visual_metadata_extractor.py line 1027)
                                            field_name = field.get('name', 'Unknown')
                                            field_type = field.get('type', 'Column')

                                            # Check if field name is "Unknown" - this indicates a broken reference
                                            is_unknown_field = (field_name == 'Unknown' or 'Unknown' in str(field_name))

                                            # NEW: Verify against dataset schema if available
                                            verified = None
                                            if self.dataset_schema and not is_unknown_field:
                                                verified = self._verify_field_missing(table_name, field_name)
                                                verification_status = '✅ EXISTS' if verified == False else '❌ MISSING' if verified == True else '⚠️ UNKNOWN'
                                                print(f"            🔍 Schema check: {table_name}.{field_name} → {verification_status}")
                                            elif is_unknown_field:
                                                # If SDK couldn't resolve the field name, it's definitely missing
                                                verified = True
                                                print(f"            ❌ Unresolved field: {table_name}.{field_name} (SDK couldn't resolve name)")

                                            # ONLY add fields that are verified as missing OR have "Unknown" in the name
                                            if verified == True or is_unknown_field:
                                                missing_fields.append({
                                                    'table': table_name,
                                                    'field': field_name,
                                                    'type': field_type,
                                                    'full_reference': f"{table_name}.{field_name}",
                                                    'verified': verified
                                                })

                                        if missing_fields:
                                            print(f"         🔴 Identified {len(missing_fields)} MISSING field(s): {', '.join([f['full_reference'] for f in missing_fields])}")
                                        else:
                                            print(f"         ⚠️  All fields exist in schema - error may be due to permissions or data source issues")
                                    break

                            # DEEP SCAN: If visual not found by name, scan ALL visuals on page for missing fields
                            if not found_visual and not missing_fields:
                                print(f"         🔍 DEEP SCAN: Visual '{visual_title}' not found - scanning ALL visuals on page for missing fields...")
                                for visual in page.get('visuals', []):
                                    visual_name = visual.get('title', '[No Title]')
                                    fields = visual.get('fields', [])
                                    if not fields:
                                        continue

                                    # Check ALL fields in this visual against schema
                                    visual_missing_fields = []
                                    for field in fields:
                                        table_name = field.get('table', 'Unknown')
                                        field_name = field.get('name', 'Unknown')
                                        field_type = field.get('type', 'Column')

                                        is_unknown_field = (field_name == 'Unknown' or 'Unknown' in str(field_name))

                                        if self.dataset_schema and not is_unknown_field:
                                            verified = self._verify_field_missing(table_name, field_name)
                                            if verified == True or is_unknown_field:
                                                visual_missing_fields.append({
                                                    'table': table_name,
                                                    'field': field_name,
                                                    'type': field_type,
                                                    'full_reference': f"{table_name}.{field_name}",
                                                    'verified': verified
                                                })

                                    # If this visual has missing fields, it's likely the broken one!
                                    if visual_missing_fields:
                                        print(f"         💡 DEEP SCAN FOUND: Visual '{visual_name}' has {len(visual_missing_fields)} missing field(s)!")
                                        print(f"         🔴 Missing fields: {', '.join([f['full_reference'] for f in visual_missing_fields])}")
                                        missing_fields = visual_missing_fields
                                        # Update the visual title to the actual name we found
                                        if visual_title.startswith('Untitled Visual'):
                                            visual_title = visual_name if visual_name and visual_name != '[No Title]' else visual_title
                                            print(f"         ✅ Updated visual title to: '{visual_title}'")
                                        break

                        # Generate root cause analysis if fields were identified
                        if missing_fields:
                            root_cause = self._analyze_root_cause(missing_fields)
                        else:
                            # No fields identified - generic message
                            root_cause = "Unable to identify specific missing fields. Manual investigation required."

                        # Get visual type from visuals list (match by title or infer from broken visuals)
                        visual_type = self._find_visual_type(page, visual_title)

                        # If still unknown and this was an "undefined" visual, try advanced matching
                        if visual_type == 'Unknown' and visual_title.startswith('Untitled Visual'):
                            print(f"         🔍 Attempting to infer type for untitled visual...")

                            # Strategy 1: If there's only ONE visual without a proper title, assume it's this one
                            visuals_without_title = [v for v in page.get('visuals', []) if not v.get('title', '').strip()]
                            if len(visuals_without_title) == 1:
                                visual_type = visuals_without_title[0].get('type', 'Unknown')
                                print(f"         ✅ Inferred visual type '{visual_type}' for the only untitled visual on page")
                            elif len(visuals_without_title) > 1:
                                # Strategy 2: Use the first untitled visual as best guess
                                visual_type = visuals_without_title[0].get('type', 'Unknown')
                                print(f"         ⚠️  Found {len(visuals_without_title)} untitled visuals - using first one: '{visual_type}'")
                            else:
                                # All visuals have titles - the broken one is completely missing from the list
                                # This means the visual failed so badly that Power BI couldn't even return it
                                visual_type = 'Broken Visual (Unable to Load)'
                                print(f"         ⚠️  All visuals on page have titles - broken visual not in getVisuals() list")
                                print(f"         → Marking as '{visual_type}' - visual failed to load completely")

                        # Final fallback for any remaining Unknown types
                        if visual_type == 'Unknown':
                            visual_type = 'Unknown Visual Type'
                            print(f"         ⚠️  Could not determine visual type for '{visual_title}' on page '{page_name}'")

                        # Get modification metadata from analyzer if available
                        report_meta = getattr(self, 'report_metadata', {})
                        modified_by = report_meta.get('modified_by', 'N/A')
                        modified_date = report_meta.get('modified_date', None)

                        # Debug: Show what metadata we have
                        print(f"         📋 Report metadata: {report_meta}")
                        print(f"         👤 Modified by: '{modified_by}' (date: {modified_date})")

                        self.issues.append({
                            'category': 'Broken Visual',
                            'severity': 'Critical',
                            'page': page_name,
                            'visual': visual_title,
                            'visual_name': visual_title,
                            'visual_type': visual_type,  # NEW: Visual type (e.g., 'clusteredColumnChart')
                            'message': f'Visual "{visual_title}" failed to render',
                            'error_type': error_message,  # Technical error type (e.g., "Missing_References")
                            'error_reason': error_reason,  # Human-readable error explanation
                            'missing_fields': missing_fields,  # NEW: List of missing fields with table info
                            'root_cause': root_cause,  # NEW: Root cause analysis
                            'description': detailed_message or 'This visual contains field reference errors or data binding issues.',
                            'recommendation': self._generate_field_specific_recommendation(missing_fields),
                            'modified_by': modified_by,  # Modified by from Scanner API
                            'modified_date': modified_date  # Modified date from Scanner API
                        })
                        print(f"      ❌ BROKEN: {visual_title} ({visual_type}) - {error_message}")
                        if missing_fields:
                            print(f"         💡 Missing fields: {', '.join([f['field'] for f in missing_fields])}")

                # NEW: Check for BLANK VISUALS (visuals with no type or empty configuration)
                for visual in page.get('visuals', []):
                    visual_name = visual.get('name', 'Unknown')
                    visual_type = visual.get('type', '')  # Empty string if missing
                    visual_title = visual.get('title', '[No Title]')
                    visual_layout = visual.get('layout', {})
                    is_blank = visual.get('isBlank', False)  # NEW: Flag from enhanced extraction

                    # ⚠️ CRITICAL CHECK: Blank visuals (no type or empty)
                    # These are different from broken visuals - they might be intentionally empty or corrupted
                    if is_blank or not visual_type or visual_type == '' or visual_type == 'unknown':
                        blank_visuals += 1
                        self.issues.append({
                            'category': 'Blank Visual',  # NEW: Separate category
                            'severity': 'Critical',
                            'page': page_name,
                            'visual': visual_title,
                            'visual_name': visual_name,
                            'visual_type': 'blank/empty',  # NEW: Indicate it's blank
                            'message': f'Visual "{visual_title}" is blank or empty',
                            'error_type': 'No_Visual_Type',
                            'error_reason': 'Visual has no type configuration - likely corrupted, deleted, or never configured',
                            'missing_fields': [],  # No field info for blank visuals
                            'root_cause': 'Visual container exists but has no visual type. This could mean the visual was deleted, corrupted during publish, or never configured.',
                            'description': 'This visual appears to be blank or corrupted - it has no valid visual type. This usually means the visual has field reference errors, data binding issues, or was never properly configured.',
                            'recommendation': 'Delete this visual and recreate it in Power BI Desktop, or check if it was intentionally left blank as a placeholder.'
                        })
                        print(f"   ⚠️  BLANK VISUAL: {page_name} / {visual_title} ({visual_name}) - no type")

                    # Check for missing layout (rendering issue)
                    elif not visual_layout:
                        self.warnings.append({
                            'category': 'Visual Layout',
                            'severity': 'Warning',
                            'page': page_name,
                            'visual': visual_title,
                            'message': f'Visual "{visual_title}" has no layout information',
                            'recommendation': 'Visual may have rendering issues'
                        })

                    # Check for missing titles (minor issue)
                    elif not visual_title or visual_title == '[No Title]':
                        if visual_type not in ['slicer', 'shape', 'image', 'textbox']:
                            missing_titles += 1

            # Summary
            total_issues = broken_visuals + blank_visuals
            if total_issues > 0:
                print(f"   🔴 CRITICAL: Found {broken_visuals} BROKEN VISUALS and {blank_visuals} BLANK VISUALS!")
                # Return True to indicate visual analysis was performed (even though issues were found)
                return True
            elif missing_titles > 0:
                self.warnings.append({
                    'category': 'Visual Configuration',
                    'severity': 'Info',
                    'message': f'{missing_titles} visuals missing titles',
                    'recommendation': 'Consider adding descriptive titles for better accessibility'
                })
                print(f"   ℹ️  {missing_titles} visuals without titles")
                print(f"   ✅ All visuals appear valid (no broken visuals)")
                return True
            else:
                print(f"   ✅ All {total_visuals} visuals are healthy!")
                return True

        except Exception as e:
            print(f"   ❌ Enhanced visual check failed: {e}")
            return False

    def _check_visual_integrity(self):
        """
        Check if visuals reference valid fields from dataset schema
        NOTE: Scanner API does NOT provide visual-level metadata.
        This is a placeholder that recommends using Enhanced Mode.
        """
        try:
            # Scanner API limitation: Cannot access visual-level data
            print(f"   ℹ️  Standard mode: Visual-level analysis not available")
            print(f"   💡 Use Enhanced Mode for visual detection (requires JavaScript SDK)")

            # Add informational warning
            self.warnings.append({
                'category': 'Visual Integrity',
                'severity': 'Info',
                'message': 'Visual-level analysis not performed in Standard Mode',
                'description': 'Scanner API does not provide visual-level metadata. Broken visuals cannot be detected in Standard Mode.',
                'recommendation': 'Use Enhanced Mode with JavaScript Embed API to detect broken visuals and rendering errors'
            })
            return True

        except Exception as e:
            print(f"   ❌ Error in visual integrity check: {e}")
            self.warnings.append({
                'category': 'Visual Integrity',
                'severity': 'Warning',
                'message': f'Visual integrity check failed: {str(e)}',
                'recommendation': 'Use Enhanced Mode for visual-level analysis'
            })
            return False

    def _validate_expressions(self):
        """Validate DAX measures and M queries for syntax errors"""
        try:
            if not self.dataset_schema.get('measures'):
                print(f"   ⚠️  No measures available for validation")
                return

            measures = self.dataset_schema['measures']
            print(f"   ✓ Validating {len(measures)} DAX measures")

            invalid_measures = 0
            for measure_name, measure_obj in measures.items():
                expression = measure_obj.get('Expression', measure_obj.get('expression', ''))

                if not expression:
                    invalid_measures += 1
                    self.issues.append({
                        'category': 'Expression Validation',
                        'severity': 'Medium',
                        'page': 'N/A',
                        'visual': 'N/A',
                        'description': f"Measure '{measure_name}' has no expression",
                        'recommendation': 'Remove unused measure or add valid DAX expression'
                    })
                    continue

                # Basic syntax validation
                if self._has_dax_syntax_errors(expression, measure_name):
                    invalid_measures += 1

            if invalid_measures > 0:
                print(f"   ⚠️  Found {invalid_measures} measures with potential issues")
            else:
                print(f"   ✅ All measures appear valid")

        except Exception as e:
            print(f"   ❌ Error validating expressions: {e}")

    def _has_dax_syntax_errors(self, expression, measure_name):
        """Basic DAX syntax validation"""
        try:
            # Check for common syntax errors
            errors = []

            # Unmatched parentheses
            if expression.count('(') != expression.count(')'):
                errors.append('Unmatched parentheses')

            # Unmatched brackets
            if expression.count('[') != expression.count(']'):
                errors.append('Unmatched brackets')

            # Empty CALCULATE
            if re.search(r'CALCULATE\s*\(\s*\)', expression, re.IGNORECASE):
                errors.append('Empty CALCULATE function')

            # Broken table references (table name without quotes containing spaces)
            if re.search(r"'[^']*\s+[^']*'(?!\[)", expression):
                # This is actually valid - table names with spaces in quotes
                pass

            if errors:
                self.issues.append({
                    'category': 'Expression Validation',
                    'severity': 'High',
                    'page': 'N/A',
                    'visual': 'N/A',
                    'description': f"Measure '{measure_name}': {', '.join(errors)}",
                    'recommendation': 'Fix DAX syntax errors in measure definition'
                })
                return True

            return False

        except Exception as e:
            return False

    def _analyze_filters(self):
        """Analyze report, page, and visual-level filters for broken references"""
        try:
            # This would require parsing report definition JSON
            # For MVP, we'll check if we can access pages

            url = f"{self.base_url}/groups/{self.workspace_id}/reports/{self.report_id}/pages"
            headers = {'Authorization': f'Bearer {self.access_token}'}

            response = requests.get(url, headers=headers, timeout=30)

            if response.status_code == 200:
                pages = response.json().get('value', [])
                print(f"   ✓ Analyzed {len(pages)} pages")

                # Check for hidden pages (might indicate broken pages)
                hidden_pages = [p for p in pages if p.get('isHidden', False)]
                if hidden_pages:
                    self.warnings.append({
                        'category': 'Page Configuration',
                        'severity': 'Low',
                        'description': f'{len(hidden_pages)} hidden pages detected',
                        'recommendation': 'Review if hidden pages are intentional'
                    })
                    print(f"   ℹ️  {len(hidden_pages)} hidden pages found")
            else:
                print(f"   ⚠️  Could not retrieve page information")

        except Exception as e:
            print(f"   ⚠️  Filter analysis limited: {e}")

    def _calculate_health_score(self):
        """
        Calculate overall health score (0-100)

        Scoring:
        - Start at 100
        - Deduct 20 points per Critical issue
        - Deduct 10 points per High severity issue
        - Deduct 5 points per Medium severity issue
        - Deduct 2 points per Warning
        """
        score = 100

        for issue in self.issues:
            severity = issue.get('severity', 'Medium')
            if severity == 'Critical':
                score -= 20
            elif severity == 'High':
                score -= 10
            elif severity == 'Medium':
                score -= 5

        for warning in self.warnings:
            score -= 2

        # Floor at 0
        return max(0, score)

    def _get_health_status(self, score):
        """Get health status based on score"""
        if score >= 90:
            return 'healthy'
        elif score >= 70:
            return 'warning'
        else:
            return 'critical'

    def _generate_summary(self, health_score):
        """Generate crash test summary"""
        return {
            'health_score': health_score,
            'status': self._get_health_status(health_score),
            'total_issues': len(self.issues),
            'total_warnings': len(self.warnings),
            'critical_issues': len([i for i in self.issues if i.get('severity') == 'Critical']),
            'high_issues': len([i for i in self.issues if i.get('severity') == 'High']),
            'medium_issues': len([i for i in self.issues if i.get('severity') == 'Medium']),
            'categories': self._get_issue_categories()
        }

    def _get_issue_categories(self):
        """Get breakdown of issues by category"""
        categories = {}

        for issue in self.issues:
            category = issue.get('category', 'Other')
            if category not in categories:
                categories[category] = 0
            categories[category] += 1

        return categories

    def _analyze_lineage_impact(self):
        """
        NEW: Analyze visual lineage to assess impact of broken visuals

        Returns:
            dict: Lineage analysis with impact assessment
        """
        print(f"   🔗 Analyzing visual lineage for impact assessment...")

        lineage_data = {
            'affected_tables': set(),
            'affected_data_sources': set(),
            'field_usage_map': {},  # field -> list of visuals using it
            'broken_field_usage': {},  # broken field -> usage count
            'visual_dependencies': [],  # list of {visual, page, fields, tables}
            'affected_tables_count': 0,
            'affected_sources_count': 0,
            'critical_fields': []  # fields used by many visuals
        }

        # Extract field usage from visual metadata
        for page in self.visual_metadata:
            page_name = page.get('displayName', page.get('name', 'Unknown'))

            for visual in page.get('visuals', []):
                visual_name = visual.get('title', visual.get('name', 'Unnamed'))
                visual_type = visual.get('type', 'unknown')
                fields = visual.get('fields', [])

                # Track visual dependency
                visual_dep = {
                    'page': page_name,
                    'visual_name': visual_name,
                    'visual_type': visual_type,
                    'fields': [],
                    'tables': set(),
                    'is_broken': False
                }

                # Process each field
                for field in fields:
                    field_name = field.get('field', field.get('column', ''))
                    table_name = field.get('table', '')

                    if not field_name:
                        continue

                    # Track field usage
                    full_field_name = f"{table_name}.{field_name}" if table_name else field_name
                    if full_field_name not in lineage_data['field_usage_map']:
                        lineage_data['field_usage_map'][full_field_name] = []
                    lineage_data['field_usage_map'][full_field_name].append({
                        'page': page_name,
                        'visual': visual_name
                    })

                    visual_dep['fields'].append(full_field_name)
                    if table_name:
                        visual_dep['tables'].add(table_name)
                        lineage_data['affected_tables'].add(table_name)

                # Check if this visual is broken
                for issue in self.issues:
                    if issue.get('category') == 'Broken Visual':
                        if issue.get('visual_name') == visual_name or issue.get('visual') == visual_name:
                            visual_dep['is_broken'] = True

                            # Track broken field usage
                            for missing_field in issue.get('missing_fields', []):
                                broken_field = f"{missing_field.get('table', '')}.{missing_field.get('field', '')}"
                                if broken_field not in lineage_data['broken_field_usage']:
                                    lineage_data['broken_field_usage'][broken_field] = 0
                                lineage_data['broken_field_usage'][broken_field] += 1

                # Convert sets to lists before appending
                visual_dep['tables'] = list(visual_dep['tables'])
                lineage_data['visual_dependencies'].append(visual_dep)

        # Identify critical fields (used by 3+ visuals)
        for field, usage_list in lineage_data['field_usage_map'].items():
            if len(usage_list) >= 3:
                lineage_data['critical_fields'].append({
                    'field': field,
                    'usage_count': len(usage_list),
                    'used_in': usage_list
                })

        lineage_data['critical_fields'].sort(key=lambda x: x['usage_count'], reverse=True)

        # Convert sets to lists
        lineage_data['affected_tables_count'] = len(lineage_data['affected_tables'])
        lineage_data['affected_tables'] = list(lineage_data['affected_tables'])
        lineage_data['affected_data_sources'] = list(lineage_data['affected_data_sources'])

        # Calculate impact score
        impact_score = 0
        if lineage_data['broken_field_usage']:
            max_usage = max(lineage_data['broken_field_usage'].values())
            impact_score = min(100, max_usage * 20)
        lineage_data['impact_score'] = impact_score

        print(f"   ✅ Lineage analysis complete:")
        print(f"      - {lineage_data['affected_tables_count']} tables affected")
        print(f"      - {len(lineage_data['critical_fields'])} critical fields")
        if lineage_data['broken_field_usage']:
            print(f"      - {len(lineage_data['broken_field_usage'])} broken fields")
            print(f"      - Impact score: {impact_score}/100")

        return lineage_data

    def _generate_recommendations(self, lineage_analysis=None):
        """Generate prioritized list of recommendations with lineage insights"""
        recommendations = []

        # Add lineage-based recommendations first
        if lineage_analysis and lineage_analysis.get('broken_field_usage'):
            # Sort broken fields by usage (most critical first)
            sorted_broken = sorted(
                lineage_analysis['broken_field_usage'].items(),
                key=lambda x: x[1],
                reverse=True
            )

            for broken_field, usage_count in sorted_broken[:5]:  # Top 5
                recommendations.append({
                    'priority': 'Critical' if usage_count >= 3 else 'High',
                    'category': 'Field Lineage',
                    'issue': f"Field '{broken_field}' is broken and used in {usage_count} visual(s)",
                    'recommendation': f"Fix or replace '{broken_field}' to restore {usage_count} affected visual(s)",
                    'impact': f"{usage_count} visuals affected"
                })

        # Critical issues first
        critical_issues = [i for i in self.issues if i.get('severity') == 'Critical']
        if critical_issues:
            recommendations.append({
                'priority': 'Immediate',
                'title': 'Fix Critical Issues',
                'description': f'{len(critical_issues)} critical issues require immediate attention',
                'actions': [i.get('recommendation', 'Fix this issue') for i in critical_issues[:3]]
            })

        # Dataset health issues
        dataset_issues = [i for i in self.issues if i.get('category') == 'Dataset Health']
        if dataset_issues:
            recommendations.append({
                'priority': 'High',
                'title': 'Resolve Data Connectivity Issues',
                'description': 'Dataset refresh failures detected',
                'actions': [
                    'Check data source connection strings',
                    'Verify gateway status (if using on-premises data)',
                    'Review dataset credentials and permissions',
                    'Check for timeouts in large data loads'
                ]
            })

        # Visual integrity issues
        visual_issues = [i for i in self.issues if i.get('category') == 'Visual Integrity']
        if visual_issues:
            recommendations.append({
                'priority': 'Medium',
                'title': 'Fix Broken Visuals',
                'description': f'{len(visual_issues)} visuals have integrity issues',
                'actions': [
                    'Re-create broken visuals',
                    'Verify all visual fields exist in dataset',
                    'Check for deleted columns or measures',
                    'Update visual configurations'
                ]
            })

        # Expression issues
        expr_issues = [i for i in self.issues if i.get('category') == 'Expression Validation']
        if expr_issues:
            recommendations.append({
                'priority': 'Medium',
                'title': 'Fix DAX Expression Errors',
                'description': f'{len(expr_issues)} measures have syntax issues',
                'actions': [
                    'Review and fix DAX syntax errors',
                    'Remove unused measures',
                    'Validate measure dependencies',
                    'Test measures with sample data'
                ]
            })

        # General recommendations if healthy
        if not recommendations:
            recommendations.append({
                'priority': 'Low',
                'title': 'Maintain Report Health',
                'description': 'Report is healthy. Follow best practices.',
                'actions': [
                    'Schedule regular crash tests',
                    'Monitor dataset refresh status',
                    'Keep documentation up to date',
                    'Review performance optimization opportunities'
                ]
            })

        return recommendations

    def _format_error_reason(self, error_message, detailed_message):
        """
        Format technical Power BI error into human-readable explanation

        Args:
            error_message: Technical error code (e.g., "Missing_References")
            detailed_message: Detailed error text from Power BI

        Returns:
            Human-readable error explanation
        """
        error_map = {
            'Missing_References': 'One or more fields referenced by this visual no longer exist in the dataset',
            'DataTypeConversionFailed': 'Data type mismatch - field cannot be converted to expected type',
            'InvalidExpression': 'Invalid DAX expression or calculation error',
            'QueryTimeout': 'Query execution exceeded time limit',
            'InsufficientMemory': 'Not enough memory to render this visual',
            'ConnectionFailed': 'Unable to connect to data source',
            'AuthenticationFailed': 'Data source authentication failed'
        }

        # Return mapped explanation or use the detailed message
        if error_message in error_map:
            return error_map[error_message]
        elif detailed_message and ('field' in detailed_message.lower() or 'column' in detailed_message.lower()):
            return 'Field reference error - missing or renamed columns'
        elif detailed_message and 'data' in detailed_message.lower():
            return 'Data retrieval or processing error'
        else:
            return detailed_message[:100] if detailed_message else 'Visual rendering failed'

    def _extract_missing_fields(self, detailed_message, error_message):
        """
        Extract specific field names from Power BI error messages and match against dataset schema

        Args:
            detailed_message: Detailed error text from Power BI
            error_message: Technical error code

        Returns:
            List of dicts with missing field information:
            [{'table': 'Sales', 'field': 'Revenue_2023', 'type': 'column'}, ...]
        """
        missing_fields = []

        if not detailed_message:
            return missing_fields

        # Common patterns in Power BI error messages for missing fields
        import re

        # Expanded patterns to catch more error message formats
        patterns = [
            # Explicit table and field patterns
            r"field\s+['\"]([^'\"]+)['\"].*?table\s+['\"]([^'\"]+)['\"]",  # field 'X' in table 'Y'
            r"table\s+['\"]([^'\"]+)['\"].*?field\s+['\"]([^'\"]+)['\"]",  # table 'Y' field 'X'
            r"column\s+['\"]([^'\"]+)['\"].*?(?:in|from)\s+table\s+['\"]([^'\"]+)['\"]",  # column 'X' in table 'Y'

            # Column/field patterns (single group)
            r"\[([^\]]+)\]",  # [FieldName] - common in DAX errors
            r"column\s+['\"]([^'\"]+)['\"]",  # column 'X'
            r"field\s+['\"]([^'\"]+)['\"]",  # field 'X'
            r"measure\s+['\"]([^'\"]+)['\"]",  # measure 'X'

            # Generic quoted field names followed by error indicators
            r"['\"]([^'\"]{3,})['\"].*?(?:doesn't exist|not found|missing|no longer exist|deleted|removed)",
        ]

        for pattern in patterns:
            matches = re.finditer(pattern, detailed_message, re.IGNORECASE)
            for match in matches:
                if len(match.groups()) == 2:
                    # Pattern matched table and field
                    if 'table.*field' in pattern:
                        table_name = match.group(1)
                        field_name = match.group(2)
                    else:
                        field_name = match.group(1)
                        table_name = match.group(2)

                    # Verify against schema if available
                    verified = self._verify_field_missing(table_name, field_name) if self.dataset_schema else None

                    missing_fields.append({
                        'table': table_name,
                        'field': field_name,
                        'type': 'column',
                        'verified': verified
                    })
                elif len(match.groups()) == 1:
                    # Pattern matched field only
                    field_name = match.group(1)

                    # Skip if it looks like a common word or too short
                    if len(field_name) < 3 or field_name.lower() in ['the', 'and', 'or', 'not', 'this', 'that']:
                        continue

                    # Try to find which table this field belongs to (if schema available)
                    if self.dataset_schema:
                        table_info = self._find_field_in_schema(field_name)
                        if table_info:
                            missing_fields.append({
                                'table': table_info['table'],
                                'field': field_name,
                                'type': table_info['type'],
                                'verified': False  # Found in schema, so NOT missing
                            })
                        else:
                            missing_fields.append({
                                'table': 'Unknown',
                                'field': field_name,
                                'type': 'unknown',
                                'verified': True  # Not found in schema, so likely missing
                            })
                    else:
                        # No schema available, just report the field
                        missing_fields.append({
                            'table': 'Unknown',
                            'field': field_name,
                            'type': 'unknown',
                            'verified': None  # Cannot verify without schema
                        })

        # Remove duplicates
        unique_fields = []
        seen = set()
        for field in missing_fields:
            key = f"{field['table']}.{field['field']}"
            if key not in seen:
                seen.add(key)
                unique_fields.append(field)

        # Debug output
        if unique_fields:
            print(f"         🔍 Extracted {len(unique_fields)} field reference(s) from error message")

        return unique_fields

    def _verify_field_missing(self, table_name, field_name):
        """
        Verify if a field is actually missing from the dataset schema

        Returns:
            True if field is confirmed missing, False if it exists
        """
        if not self.dataset_schema:
            return None  # Cannot verify without schema

        # Check in tables/columns
        tables = self.dataset_schema.get('tables', [])
        for table in tables:
            # table can be either a string or a dict - handle both cases
            table_name_str = table.get('name') if isinstance(table, dict) else table
            if not table_name_str:
                continue

            if table_name_str.lower() == table_name.lower():
                # Check columns
                columns = self.dataset_schema.get('columns', {}).get(table_name_str, [])
                # columns is a list of dicts, not strings
                column_names = [c.get('name') if isinstance(c, dict) else c for c in columns]
                if field_name in column_names or field_name.lower() in [c.lower() for c in column_names if c]:
                    return False  # Field exists

        # Check in measures
        measures = self.dataset_schema.get('measures', {})
        if field_name in measures or field_name.lower() in [m.lower() for m in measures.keys()]:
            return False  # Measure exists

        return True  # Field not found in schema

    def _find_field_in_schema(self, field_name):
        """
        Search for a field across all tables in the dataset schema

        Returns:
            {'table': 'TableName', 'type': 'column'|'measure'} or None
        """
        if not self.dataset_schema:
            return None

        # Check measures first
        measures = self.dataset_schema.get('measures', {})
        if field_name in measures or field_name.lower() in [m.lower() for m in measures.keys()]:
            return {'table': 'Measures', 'type': 'measure'}

        # Check columns in each table
        columns_by_table = self.dataset_schema.get('columns', {})
        for table, columns in columns_by_table.items():
            # columns is a list of dicts, not strings
            column_names = [c.get('name') if isinstance(c, dict) else c for c in columns]
            if field_name in column_names or field_name.lower() in [c.lower() for c in column_names if c]:
                return {'table': table, 'type': 'column'}

        return None

    def _analyze_root_cause(self, missing_fields):
        """
        Analyze missing fields to provide root cause explanation with technical details

        Args:
            missing_fields: List of missing field dicts from _extract_missing_fields

        Returns:
            Human-readable root cause explanation with specific technical reason
        """
        if not missing_fields:
            return 'Visual failed to render due to field reference errors. Specific fields could not be identified from the error message.'

        # Separate verified missing from verified exists
        verified_missing = [f for f in missing_fields if f.get('verified') == True]
        verified_exists = [f for f in missing_fields if f.get('verified') == False]
        unverified = [f for f in missing_fields if f.get('verified') is None]

        # Determine the most specific technical reason
        if verified_missing:
            # Fields confirmed deleted/missing
            field_list = ', '.join([f"{f.get('table', 'Unknown')}.{f.get('field', 'Unknown')}" for f in verified_missing])
            if len(verified_missing) == 1:
                f = verified_missing[0]
                field_type = f.get('type', 'field')
                return f"DELETED {field_type.upper()}: '{f.get('table', 'Unknown')}.{f.get('field', 'Unknown')}' no longer exists in the dataset schema. This {field_type} was removed or the table was deleted."
            else:
                return f"DELETED FIELDS ({len(verified_missing)}): {field_list}. These were likely deleted during a recent dataset schema change."
        elif verified_exists:
            # Fields exist in schema but visual still broken (permission/binding issue)
            field_list = ', '.join([f.get('field', 'Unknown') for f in verified_exists[:3]])
            return f"DATA BINDING ERROR: Fields exist in schema ({field_list}) but visual cannot access them. Possible causes: Row-Level Security restriction, incorrect relationship, or data type mismatch."
        elif unverified:
            # Cannot verify (no schema available)
            field_list = ', '.join([f.get('field', 'Unknown') for f in unverified[:3]])
            return f"FIELD REFERENCE ERROR: Visual references {len(unverified)} field(s) ({field_list}) that could not be verified. Dataset schema not available for validation."
        else:
            return 'UNKNOWN ERROR: Visual contains field references, but specific issue could not be determined.'

    def _find_visual_type(self, page, visual_title):
        """
        Find the visual type by matching visual title in the page's visual list

        Args:
            page: Page data dict with visuals list
            visual_title: Title of the visual to find

        Returns:
            Visual type string (e.g., 'clusteredColumnChart') or 'Unknown'
        """
        visuals = page.get('visuals', [])

        # Strategy 1: Try exact title match
        for visual in visuals:
            if visual.get('title', '') == visual_title:
                visual_type = visual.get('type', '')
                if visual_type:
                    return visual_type

        # Strategy 2: Try case-insensitive title match
        for visual in visuals:
            if visual.get('title', '').lower() == visual_title.lower():
                visual_type = visual.get('type', '')
                if visual_type:
                    return visual_type

        # Strategy 3: Try name match (visual name often matches title)
        for visual in visuals:
            if visual.get('name', '') == visual_title:
                visual_type = visual.get('type', '')
                if visual_type:
                    return visual_type

        # Strategy 4: Try partial match (title contains visual name or vice versa)
        for visual in visuals:
            v_title = visual.get('title', '')
            v_name = visual.get('name', '')
            if (v_title and visual_title in v_title) or (v_name and visual_title in v_name):
                visual_type = visual.get('type', '')
                if visual_type:
                    return visual_type

        return 'Unknown'



    def _generate_field_specific_recommendation(self, missing_fields):
        """
        Generate actionable recommendations based on specific missing fields

        Args:
            missing_fields: List of missing field dicts

        Returns:
            Detailed recommendation string
        """
        if not missing_fields:
            return 'Open this visual in Power BI Desktop and verify all fields exist in the dataset. Check for deleted columns, renamed measures, or invalid field references.'

        verified_missing = [f for f in missing_fields if f.get('verified')]

        if verified_missing:
            field_details = []
            for f in verified_missing:
                if f['table'] != 'Unknown':
                    field_details.append(f"'{f['field']}' from table '{f['table']}'")
                else:
                    field_details.append(f"'{f['field']}'")

            recommendation = f"This visual references deleted or renamed fields: {', '.join(field_details)}. "
            recommendation += "To fix: (1) Open the report in Power BI Desktop, (2) Edit this visual, "
            recommendation += "(3) Remove the missing fields from the Fields pane, (4) Replace with valid fields or delete the visual if no longer needed."
            return recommendation
        else:
            return 'The fields referenced by this visual exist in the dataset but may have data type issues or incorrect usage. Check the visual configuration in Power BI Desktop.'
