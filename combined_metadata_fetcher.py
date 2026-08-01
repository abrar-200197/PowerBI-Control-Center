"""
Combined Metadata Fetcher
Combines Scanner API (for datasets) + JavaScript Embed API (for visuals)

This module provides the BEST of both worlds:
1. Scanner API → Dataset schema, DAX, M queries, relationships
2. JavaScript Embed API → Report pages, visuals, layouts

Author: Power BI Control Center
Date: June 3, 2026
"""

import asyncio
import json
from typing import Dict, List, Optional
from datetime import datetime

# Import existing modules
from scanner_connector import PowerBIScanner
from visual_metadata_extractor import VisualMetadataExtractor


class CombinedMetadataFetcher:
    """
    Combines Scanner API and JavaScript Embed API for complete metadata
    """
    
    def __init__(self, client_id: str, client_secret: str, tenant_id: str):
        """
        Initialize the combined fetcher
        
        Args:
            client_id: Azure AD application client ID
            client_secret: Azure AD application client secret
            tenant_id: Azure AD tenant ID
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id

        # Initialize both connectors
        # NOTE: PowerBIScanner reads credentials from environment variables (.env file)
        self.scanner = PowerBIScanner()  # Reads from .env
        self.visual_extractor = VisualMetadataExtractor(client_id, client_secret, tenant_id)
    
    async def get_complete_metadata(
        self, 
        workspace_id: str, 
        report_id: str, 
        dataset_id: str,
        include_visuals: bool = True,
        visual_timeout: int = 60
    ) -> Dict:
        """
        Get complete metadata combining Scanner API and JavaScript Embed API
        
        Args:
            workspace_id: Power BI workspace GUID
            report_id: Power BI report GUID
            dataset_id: Power BI dataset GUID
            include_visuals: Whether to extract visual metadata (requires browser automation)
            visual_timeout: Timeout for visual extraction in seconds
        
        Returns:
            {
                "metadata_version": "2.0",
                "timestamp": "2026-06-03T15:30:00Z",
                "report": {
                    "id": "...",
                    "name": "...",
                    "workspaceId": "..."
                },
                "dataset": {
                    "id": "...",
                    "name": "...",
                    "tables": [...],
                    "measures": [...],
                    "relationships": [...]
                },
                "pages": [
                    {
                        "name": "ReportSection1",
                        "displayName": "Overview",
                        "visuals": [...]
                    }
                ],
                "sources": {
                    "scanner_api": True,
                    "visual_api": True
                },
                "errors": []
            }
        """
        print("\n" + "="*80)
        print("🔄 COMBINED METADATA EXTRACTION")
        print("="*80)
        print(f"   Report ID: {report_id}")
        print(f"   Dataset ID: {dataset_id}")
        print(f"   Workspace ID: {workspace_id}")
        print(f"   Include Visuals: {include_visuals}")
        print("="*80)
        
        result = {
            "metadata_version": "2.0",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "report": {
                "id": report_id,
                "workspaceId": workspace_id
            },
            "dataset": {
                "id": dataset_id
            },
            "pages": [],
            "sources": {
                "scanner_api": False,
                "visual_api": False
            },
            "errors": []
        }
        
        # PHASE 1: Scanner API - Get dataset metadata
        print("\n📊 PHASE 1: Scanner API (Dataset Metadata)")
        print("-" * 80)
        
        try:
            scanner_data = self.scanner.run_scan([workspace_id])
            
            if scanner_data and 'workspaces' in scanner_data:
                workspace_data = scanner_data['workspaces'][0]
                
                # Extract dataset metadata
                datasets = workspace_data.get('datasets', [])
                target_dataset = next((d for d in datasets if d.get('id') == dataset_id), None)
                
                if target_dataset:
                    print(f"   ✅ Dataset found: {target_dataset.get('name', 'Unknown')}")
                    result["dataset"]["name"] = target_dataset.get('name')
                    result["dataset"]["tables"] = target_dataset.get('tables', [])
                    result["dataset"]["relationships"] = target_dataset.get('relationships', [])
                    result["dataset"]["expressions"] = target_dataset.get('expressions', [])
                    result["dataset"]["roles"] = target_dataset.get('roles', [])
                    result["dataset"]["datasourceUsages"] = target_dataset.get('datasourceUsages', [])
                    
                    # Count measures across all tables
                    total_measures = sum(len(t.get('measures', [])) for t in result["dataset"]["tables"])
                    total_columns = sum(len(t.get('columns', [])) for t in result["dataset"]["tables"])
                    
                    print(f"   📊 Tables: {len(result['dataset']['tables'])}")
                    print(f"   📏 Columns: {total_columns}")
                    print(f"   📐 Measures: {total_measures}")
                    print(f"   🔗 Relationships: {len(result['dataset'].get('relationships', []))}")
                    print(f"   📝 Expressions: {len(result['dataset'].get('expressions', []))}")
                    
                    result["sources"]["scanner_api"] = True
                else:
                    print(f"   ⚠️  Dataset {dataset_id} not found in scan results")
                    result["errors"].append(f"Dataset {dataset_id} not found in Scanner API results")
                
                # Extract report metadata
                reports = workspace_data.get('reports', [])
                target_report = next((r for r in reports if r.get('id') == report_id), None)
                
                if target_report:
                    result["report"]["name"] = target_report.get('name')
                    result["report"]["datasetId"] = target_report.get('datasetId')
                    result["report"]["modifiedDateTime"] = target_report.get('modifiedDateTime')
                    result["report"]["reportType"] = target_report.get('reportType')
                    print(f"   ✅ Report found: {target_report.get('name', 'Unknown')}")
        
        except Exception as e:
            print(f"   ❌ Scanner API error: {e}")
            result["errors"].append(f"Scanner API error: {str(e)}")

        # PHASE 2: JavaScript Embed API - Get visual metadata
        if include_visuals:
            print("\n🎨 PHASE 2: JavaScript Embed API (Visual Metadata)")
            print("-" * 80)

            try:
                visual_data = await self.visual_extractor.extract_visuals(
                    workspace_id,
                    report_id,
                    timeout=visual_timeout
                )

                if visual_data.get("success"):
                    result["pages"] = visual_data.get("pages", [])
                    result["sources"]["visual_api"] = True

                    print(f"   ✅ Extracted {visual_data.get('totalPages', 0)} pages")
                    print(f"   ✅ Extracted {visual_data.get('totalVisuals', 0)} visuals")

                    # Print visual summary by page
                    for page in result["pages"]:
                        visual_types = {}
                        for visual in page.get("visuals", []):
                            vtype = visual.get("type", "unknown")
                            visual_types[vtype] = visual_types.get(vtype, 0) + 1

                        visual_summary = ", ".join([f"{count} {vtype}" for vtype, count in visual_types.items()])
                        print(f"      📄 {page.get('displayName', 'Unknown')}: {visual_summary}")
                else:
                    error_msg = visual_data.get("error", "Unknown error")
                    print(f"   ❌ Visual extraction failed: {error_msg}")
                    result["errors"].append(f"Visual API error: {error_msg}")

            except Exception as e:
                print(f"   ❌ Visual extraction error: {e}")
                result["errors"].append(f"Visual API error: {str(e)}")
        else:
            print("\n⏭️  PHASE 2: Skipped (include_visuals=False)")

        # Summary
        print("\n" + "="*80)
        print("✅ METADATA EXTRACTION COMPLETE")
        print("="*80)
        print(f"   Scanner API: {'✅ Success' if result['sources']['scanner_api'] else '❌ Failed'}")
        print(f"   Visual API: {'✅ Success' if result['sources']['visual_api'] else ('⏭️  Skipped' if not include_visuals else '❌ Failed')}")
        print(f"   Dataset Tables: {len(result['dataset'].get('tables', []))}")
        print(f"   Report Pages: {len(result['pages'])}")

        if result["errors"]:
            print(f"   ⚠️  Errors: {len(result['errors'])}")
            for error in result["errors"]:
                print(f"      - {error}")
        else:
            print("   ✅ No errors")

        print("="*80)

        return result

    async def get_workspace_complete_metadata(
        self,
        workspace_id: str,
        include_visuals: bool = True,
        visual_timeout: int = 60,
        max_reports: Optional[int] = None
    ) -> List[Dict]:
        """
        Get complete metadata for all reports in a workspace

        Args:
            workspace_id: Power BI workspace GUID
            include_visuals: Whether to extract visual metadata
            visual_timeout: Timeout for each report's visual extraction
            max_reports: Maximum number of reports to process (None = all)

        Returns:
            List of metadata dictionaries, one per report
        """
        print("\n" + "="*80)
        print("🌐 WORKSPACE METADATA EXTRACTION")
        print("="*80)
        print(f"   Workspace ID: {workspace_id}")
        print(f"   Include Visuals: {include_visuals}")
        print(f"   Max Reports: {max_reports or 'All'}")
        print("="*80)

        results = []

        # Get workspace scan
        try:
            scanner_data = self.scanner.run_scan([workspace_id])

            if not scanner_data or 'workspaces' not in scanner_data:
                print("   ❌ Failed to scan workspace")
                return results

            workspace_data = scanner_data['workspaces'][0]
            reports = workspace_data.get('reports', [])

            if max_reports:
                reports = reports[:max_reports]

            print(f"\n   📊 Found {len(reports)} report(s) to process")

            for idx, report in enumerate(reports, 1):
                report_id = report.get('id')
                report_name = report.get('name', 'Unknown')
                dataset_id = report.get('datasetId')

                print(f"\n   [{idx}/{len(reports)}] Processing: {report_name}")

                if not dataset_id:
                    print(f"      ⚠️  Skipping - No dataset ID")
                    continue

                metadata = await self.get_complete_metadata(
                    workspace_id,
                    report_id,
                    dataset_id,
                    include_visuals=include_visuals,
                    visual_timeout=visual_timeout
                )

                results.append(metadata)

        except Exception as e:
            print(f"   ❌ Workspace scan error: {e}")

        print("\n" + "="*80)
        print(f"✅ WORKSPACE SCAN COMPLETE - Processed {len(results)} report(s)")
        print("="*80)

        return results


# Async test function
async def test_combined_fetcher():
    """Test the combined metadata fetcher"""
    import os
    from dotenv import load_dotenv

    load_dotenv()

    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")
    tenant_id = os.getenv("TENANT_ID")

    fetcher = CombinedMetadataFetcher(client_id, client_secret, tenant_id)

    # Test with a single report
    workspace_id = "59ed6719-608e-43c7-b38e-7d08934d17b0"  # CQI Team
    report_id = "d0949266-11dc-4af2-be51-3cf3088e742c"
    dataset_id = "your-dataset-id-here"  # You'll need the actual dataset ID

    metadata = await fetcher.get_complete_metadata(
        workspace_id,
        report_id,
        dataset_id,
        include_visuals=True,
        visual_timeout=90
    )

    # Save results
    output_file = "combined_metadata.json"
    with open(output_file, "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"\n💾 Results saved to: {output_file}")

    return metadata


if __name__ == "__main__":
    # Run the test
    asyncio.run(test_combined_fetcher())
