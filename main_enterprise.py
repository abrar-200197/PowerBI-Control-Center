from powerbi_admin_client import PowerBIAdminClient
from ai_generator import AIDocGenerator
from document_creator import PowerBIDocumentCreator as DocumentGenerator
import time
import os
from datetime import datetime

def display_workspaces(workspaces, show_reports=False):
    """Display workspaces in a formatted table"""
    print("\n" + "="*100)
    if show_reports:
        print(f"{'No.':<5} {'Workspace Name':<45} {'Type':<15} {'ID':<35}")
    else:
        print(f"{'No.':<5} {'Workspace Name':<50} {'Type':<20} {'Reports':<15}")
    print("="*100)
    
    for idx, ws in enumerate(workspaces, 1):
        ws_name = ws.get('name', 'Unknown')[:48] if show_reports else ws.get('name', 'Unknown')[:48]
        ws_type = ws.get('type', 'Unknown')
        ws_id = ws.get('id', 'N/A')[:33] if show_reports else 'TBD'
        
        if show_reports:
            print(f"{idx:<5} {ws_name:<45} {ws_type:<15} {ws_id:<35}")
        else:
            print(f"{idx:<5} {ws_name:<50} {ws_type:<20} {'TBD':<15}")
    
    print("="*100)

def display_reports(reports, workspace_name):
    """Display reports from a workspace in a formatted table"""
    print("\n" + "="*120)
    print(f"📊 Reports in Workspace: '{workspace_name}'")
    print("="*120)
    print(f"{'No.':<5} {'Report Name':<50} {'Report ID':<40} {'Dataset':<25}")
    print("="*120)
    
    for idx, report in enumerate(reports, 1):
        report_name = report.get('name', 'Unknown')[:48]
        report_id = report.get('id', 'N/A')[:38]
        dataset_id = report.get('datasetId', 'N/A')[:23]
        
        print(f"{idx:<5} {report_name:<50} {report_id:<40} {dataset_id:<25}")
    
    print("="*120)

def search_workspaces(workspaces, search_term):
    """Search workspaces by name"""
    search_term = search_term.lower()
    matching = [
        ws for ws in workspaces 
        if search_term in ws.get('name', '').lower()
    ]
    return matching

def search_reports(reports, search_term):
    """Search reports by name"""
    search_term = search_term.lower()
    matching = [
        report for report in reports 
        if search_term in report.get('name', '').lower()
    ]
    return matching

def filter_workspaces(workspaces):
    """
    Filter out personal workspaces and return only organizational workspaces
    """
    org_workspaces = [
        ws for ws in workspaces 
        if ws.get('type') not in ['PersonalGroup', 'Personal']
    ]
    
    print(f"\n📊 Workspace Summary:")
    print(f"   Total workspaces found: {len(workspaces)}")
    print(f"   Personal workspaces (excluded): {len(workspaces) - len(org_workspaces)}")
    print(f"   Organizational workspaces: {len(org_workspaces)}")
    
    return org_workspaces

def select_workspace_mode(workspaces, admin_client):
    """
    Let user choose how to select workspaces
    Returns: (mode, selected_workspaces, selected_reports)
    """
    while True:
        print("\n" + "="*100)
        print("🎯 WORKSPACE & REPORT SELECTION")
        print("="*100)
        print("\nChoose your documentation scope:")
        print("\n  1️⃣  Generate documentation for ALL reports in ALL workspaces")
        print("  2️⃣  Generate documentation for ALL reports in SPECIFIC workspace(s)")
        print("  3️⃣  Generate documentation for SPECIFIC report(s) from a workspace")
        print("  4️⃣  Search and select workspace/reports")
        print("  5️⃣  Exit")
        print("\n" + "="*100)
        
        choice = input("\n👉 Enter your choice (1-5): ").strip()
        
        if choice == '1':
            # All workspaces, all reports
            print(f"\n✅ Selected: ALL {len(workspaces)} workspace(s), ALL reports")
            confirm = input("   ⚠️  This may take a long time. Confirm? (y/n): ").strip().lower()
            if confirm == 'y':
                return ('all_workspaces', workspaces, None)
        
        elif choice == '2':
            # Specific workspaces, all reports in them
            selected_workspaces = select_specific_workspaces(workspaces)
            if selected_workspaces:
                return ('specific_workspaces', selected_workspaces, None)
        
        elif choice == '3':
            # Specific reports from a workspace
            selected_items = select_specific_reports(workspaces, admin_client)
            if selected_items:
                return ('specific_reports', None, selected_items)
        
        elif choice == '4':
            # Search functionality
            search_result = search_and_select(workspaces, admin_client)
            if search_result:
                mode, selection = search_result
                if mode == 'workspaces':
                    return ('specific_workspaces', selection, None)
                elif mode == 'reports':
                    return ('specific_reports', None, selection)
        
        elif choice == '5':
            print("\n👋 Exiting...")
            return (None, None, None)
        
        else:
            print("\n❌ Invalid choice. Please enter 1-5.")

def select_specific_workspaces(workspaces):
    """
    Let user select specific workspaces with full listing
    """
    print("\n" + "="*100)
    print("📋 AVAILABLE WORKSPACES")
    display_workspaces(workspaces)
    
    print("\n💡 Instructions:")
    print("   - Enter workspace numbers separated by commas (e.g., 1,3,5)")
    print("   - Enter range with dash (e.g., 1-5)")
    print("   - Enter 'all' to select all workspaces")
    print("   - Enter 'back' to return to main menu")
    
    while True:
        selection = input("\n👉 Enter workspace number(s): ").strip().lower()
        
        if selection == 'back':
            return None
        
        if selection == 'all':
            print(f"\n✅ Selected ALL {len(workspaces)} workspace(s)")
            confirm = input("   Confirm? (y/n): ").strip().lower()
            if confirm == 'y':
                return workspaces
        
        try:
            indices = []
            
            # Handle ranges (e.g., "1-5")
            if '-' in selection:
                parts = selection.split(',')
                for part in parts:
                    if '-' in part:
                        start, end = part.split('-')
                        indices.extend(range(int(start.strip()), int(end.strip()) + 1))
                    else:
                        indices.append(int(part.strip()))
            else:
                # Parse comma-separated numbers
                indices = [int(x.strip()) for x in selection.split(',')]
            
            # Validate indices
            if all(1 <= idx <= len(workspaces) for idx in indices):
                selected = [workspaces[idx-1] for idx in sorted(set(indices))]
                
                print(f"\n✅ Selected {len(selected)} workspace(s):")
                for ws in selected:
                    print(f"   - {ws.get('name')}")
                
                confirm = input("\n   Confirm selection? (y/n): ").strip().lower()
                if confirm == 'y':
                    return selected
            else:
                print(f"\n❌ Invalid number(s). Please enter numbers between 1 and {len(workspaces)}")
        
        except ValueError:
            print("\n❌ Invalid format. Use comma-separated numbers or ranges (e.g., 1,3,5 or 1-5)")

def select_specific_reports(workspaces, admin_client):
    """
    Let user select specific reports from a workspace
    Returns: list of dicts with workspace and report info
    """
    print("\n" + "="*100)
    print("📋 STEP 1: SELECT WORKSPACE")
    display_workspaces(workspaces)
    
    print("\n💡 Instructions:")
    print("   - Enter workspace number")
    print("   - Enter 'back' to return to main menu")
    
    while True:
        try:
            ws_num = input("\n👉 Enter workspace number: ").strip().lower()
            
            if ws_num == 'back':
                return None
            
            ws_idx = int(ws_num)
            
            if 1 <= ws_idx <= len(workspaces):
                selected_workspace = workspaces[ws_idx - 1]
                break
            else:
                print(f"❌ Invalid number. Enter between 1 and {len(workspaces)}")
        except ValueError:
            print("❌ Please enter a valid number")
    
    # Fetch reports from selected workspace
    workspace_id = selected_workspace.get('id')
    workspace_name = selected_workspace.get('name')
    
    print(f"\n🔍 Fetching reports from '{workspace_name}'...")
    reports = admin_client.get_workspace_reports(workspace_id)
    
    if not reports:
        print(f"\n❌ No reports found in workspace '{workspace_name}'")
        return None
    
    # Display reports
    print("\n" + "="*100)
    print("📋 STEP 2: SELECT REPORT(S)")
    display_reports(reports, workspace_name)
    
    print("\n💡 Instructions:")
    print("   - Enter report numbers separated by commas (e.g., 1,3,5)")
    print("   - Enter range with dash (e.g., 1-3)")
    print("   - Enter 'all' to select all reports")
    print("   - Enter 'back' to return to workspace selection")
    
    while True:
        selection = input("\n👉 Enter report number(s): ").strip().lower()
        
        if selection == 'back':
            return select_specific_reports(workspaces, admin_client)
        
        if selection == 'all':
            print(f"\n✅ Selected ALL {len(reports)} report(s)")
            confirm = input("   Confirm? (y/n): ").strip().lower()
            if confirm == 'y':
                return [{'workspace': selected_workspace, 'reports': reports}]
        
        try:
            indices = []
            
            # Handle ranges
            if '-' in selection:
                parts = selection.split(',')
                for part in parts:
                    if '-' in part:
                        start, end = part.split('-')
                        indices.extend(range(int(start.strip()), int(end.strip()) + 1))
                    else:
                        indices.append(int(part.strip()))
            else:
                indices = [int(x.strip()) for x in selection.split(',')]
            
            # Validate indices
            if all(1 <= idx <= len(reports) for idx in indices):
                selected_reports = [reports[idx-1] for idx in sorted(set(indices))]
                
                print(f"\n✅ Selected {len(selected_reports)} report(s):")
                for report in selected_reports:
                    print(f"   - {report.get('name')}")
                
                confirm = input("\n   Confirm selection? (y/n): ").strip().lower()
                if confirm == 'y':
                    return [{'workspace': selected_workspace, 'reports': selected_reports}]
            else:
                print(f"\n❌ Invalid number(s). Enter numbers between 1 and {len(reports)}")
        
        except ValueError:
            print("\n❌ Invalid format. Use comma-separated numbers or ranges")

def search_and_select(workspaces, admin_client):
    """
    Search functionality for workspaces and reports
    """
    while True:
        print("\n" + "="*100)
        print("🔍 SEARCH WORKSPACES & REPORTS")
        print("="*100)
        print("\nOptions:")
        print("  1️⃣  Search workspaces")
        print("  2️⃣  Search reports (across all workspaces)")
        print("  3️⃣  Back to main menu")
        
        choice = input("\n👉 Enter choice (1-3): ").strip()
        
        if choice == '1':
            result = search_workspaces_interactive(workspaces)
            if result:
                return ('workspaces', result)
        
        elif choice == '2':
            result = search_reports_interactive(workspaces, admin_client)
            if result:
                return ('reports', result)
        
        elif choice == '3':
            return None
        
        else:
            print("❌ Invalid choice")

def search_workspaces_interactive(workspaces):
    """Interactive workspace search"""
    while True:
        search_term = input("\n🔍 Enter workspace name to search (or 'back'): ").strip()
        
        if search_term.lower() == 'back':
            return None
        
        if not search_term:
            print("❌ Please enter a search term")
            continue
        
        matches = search_workspaces(workspaces, search_term)
        
        if not matches:
            print(f"\n❌ No workspaces found matching '{search_term}'")
            retry = input("   Try another search? (y/n): ").strip().lower()
            if retry != 'y':
                return None
            continue
        
        print(f"\n✅ Found {len(matches)} matching workspace(s):")
        display_workspaces(matches)
        
        print("\n💡 Enter numbers to select, or 'search' to search again")
        selection = input("👉 Enter workspace number(s) or 'search': ").strip().lower()
        
        if selection == 'search':
            continue
        
        try:
            indices = [int(x.strip()) for x in selection.split(',')]
            
            if all(1 <= idx <= len(matches) for idx in indices):
                selected = [matches[idx-1] for idx in indices]
                
                print(f"\n✅ Selected {len(selected)} workspace(s):")
                for ws in selected:
                    print(f"   - {ws.get('name')}")
                
                confirm = input("\n   Confirm? (y/n): ").strip().lower()
                if confirm == 'y':
                    return selected
            else:
                print(f"❌ Invalid numbers")
        except ValueError:
            print("❌ Invalid format")

def search_reports_interactive(workspaces, admin_client):
    """Interactive report search across all workspaces"""
    search_term = input("\n🔍 Enter report name to search (or 'back'): ").strip()
    
    if search_term.lower() == 'back':
        return None
    
    if not search_term:
        print("❌ Please enter a search term")
        return None
    
    print(f"\n⏳ Searching for reports matching '{search_term}'...")
    print("   (This may take a moment as we scan all workspaces)")
    
    matching_reports = []
    
    for ws in workspaces:
        try:
            ws_id = ws.get('id')
            ws_name = ws.get('name')
            reports = admin_client.get_workspace_reports(ws_id)
            
            for report in reports:
                if search_term.lower() in report.get('name', '').lower():
                    matching_reports.append({
                        'workspace': ws,
                        'report': report
                    })
        except:
            continue
    
    if not matching_reports:
        print(f"\n❌ No reports found matching '{search_term}'")
        return None
    
    # Display matching reports
    print(f"\n✅ Found {len(matching_reports)} matching report(s):")
    print("\n" + "="*120)
    print(f"{'No.':<5} {'Report Name':<45} {'Workspace':<45} {'Dataset ID':<25}")
    print("="*120)
    
    for idx, item in enumerate(matching_reports, 1):
        report = item['report']
        workspace = item['workspace']
        report_name = report.get('name', 'Unknown')[:43]
        ws_name = workspace.get('name', 'Unknown')[:43]
        dataset_id = report.get('datasetId', 'N/A')[:23]
        
        print(f"{idx:<5} {report_name:<45} {ws_name:<45} {dataset_id:<25}")
    
    print("="*120)
    
    selection = input("\n👉 Enter report number(s) to document (comma-separated): ").strip()
    
    try:
        indices = [int(x.strip()) for x in selection.split(',')]
        
        if all(1 <= idx <= len(matching_reports) for idx in indices):
            selected = [matching_reports[idx-1] for idx in indices]
            
            # Group by workspace
            workspace_reports_map = {}
            for item in selected:
                ws_id = item['workspace']['id']
                if ws_id not in workspace_reports_map:
                    workspace_reports_map[ws_id] = {
                        'workspace': item['workspace'],
                        'reports': []
                    }
                workspace_reports_map[ws_id]['reports'].append(item['report'])
            
            result = list(workspace_reports_map.values())
            
            print(f"\n✅ Selected {len(selected)} report(s) from {len(result)} workspace(s)")
            confirm = input("   Confirm? (y/n): ").strip().lower()
            if confirm == 'y':
                return result
    except ValueError:
        print("❌ Invalid format")
    
    return None

def process_workspaces(admin_client, ai_doc, workspaces_to_process, mode, selected_reports=None):
    """
    Process selected workspaces and generate documentation
    """
    print("\n" + "="*100)
    print("🚀 STARTING DOCUMENTATION GENERATION")
    print("="*100)
    
    stats = {
        'total_workspaces': 0,
        'total_reports': 0,
        'total_pages': 0,
        'successful_docs': 0,
        'failed_docs': 0,
        'skipped_reports': 0
    }
    
    # If specific reports mode, use different processing
    if mode == 'specific_reports' and selected_reports:
        for item in selected_reports:
            workspace = item['workspace']
            reports = item['reports']
            
            workspace_name = workspace.get('name')
            workspace_id = workspace.get('id')
            
            stats['total_workspaces'] += 1
            
            print(f"\n{'─'*100}")
            print(f"📁 Workspace: {workspace_name}")
            print(f"{'─'*100}")
            print(f"   📊 Processing {len(reports)} selected report(s)")

            # Interactive mode doesn't have scan data, pass None
            process_reports_in_workspace(
                admin_client, ai_doc, workspace_id, workspace_name,
                reports, stats, datasets=None
            )
    else:
        # Standard workspace processing
        # Step 1: Initiate metadata scan
        print(f"\n📝 Step 1: Scanning {len(workspaces_to_process)} workspace(s) for metadata...")
        workspace_ids = [ws['id'] for ws in workspaces_to_process]
        
        scan_id = admin_client.initiate_workspace_scan(workspace_ids)
        if not scan_id:
            print("❌ Failed to initiate scan")
            return
        
        # Step 2: Wait for scan completion
        print("\n📝 Step 2: Waiting for scan to complete...")
        if not admin_client.wait_for_scan_completion(scan_id):
            print("❌ Scan did not complete successfully")
            return
        
        # Step 3: Get scan results
        print("\n📝 Step 3: Retrieving scan results...")
        scan_results = admin_client.get_scan_result(scan_id)
        
        if not scan_results:
            print("❌ Failed to retrieve scan results")
            return
        
        workspaces_data = scan_results.get('workspaces', [])
        
        # Step 4: Process each workspace
        print(f"\n📝 Step 4: Generating documentation...")
        print("="*100)
        
        for ws_idx, workspace in enumerate(workspaces_data, 1):
            workspace_name = workspace.get('name', 'Unknown Workspace')
            workspace_id = workspace.get('id')
            reports = workspace.get('reports', [])
            datasets = workspace.get('datasets', [])  # Get datasets from scan

            stats['total_workspaces'] += 1

            print(f"\n{'─'*100}")
            print(f"📁 Workspace {ws_idx}/{len(workspaces_data)}: {workspace_name}")
            print(f"{'─'*100}")
            print(f"   📊 Found {len(reports)} report(s)")
            print(f"   📊 Found {len(datasets)} dataset(s) with schema")

            if not reports:
                print("   ⏭️  No reports found, skipping...")
                continue

            process_reports_in_workspace(
                admin_client, ai_doc, workspace_id, workspace_name,
                reports, stats, datasets  # Pass datasets
            )
    
    # Final summary
    print_final_summary(stats)

def process_reports_in_workspace(admin_client, ai_doc, workspace_id, workspace_name, reports, stats, datasets=None):
    """Process individual reports in a workspace"""

    # Create dataset lookup by ID for quick access
    dataset_lookup = {}
    if datasets:
        for ds in datasets:
            dataset_lookup[ds.get('id')] = ds

    for report_idx, report in enumerate(reports, 1):
        try:
            report_name = report.get('name', 'Unknown Report')
            report_id = report.get('id')

            stats['total_reports'] += 1

            print(f"\n   ┌─ Report {report_idx}/{len(reports)}: {report_name}")

            # Get report pages
            print(f"   ├─ 📄 Fetching pages...")
            pages = admin_client.get_report_pages(workspace_id, report_id)
            stats['total_pages'] += len(pages)
            print(f"   │  └─ Found {len(pages)} page(s)")

            # Build metadata
            metadata = {
                'report_name': report_name,
                'report_id': report_id,
                'workspace_name': workspace_name,
                'workspace_id': workspace_id,
                'pages': pages,
                'dataset_id': report.get('datasetId')
            }

            # Get dataset info
            if metadata['dataset_id']:
                print(f"   ├─ 📊 Fetching dataset information...")
                dataset = admin_client.get_dataset_info(workspace_id, metadata['dataset_id'])
                if dataset:
                    metadata['dataset_name'] = dataset.get('name', 'Unknown')
                    print(f"   │  └─ Dataset: {metadata['dataset_name']}")

                    # Get data sources
                    data_sources = admin_client.get_dataset_datasources(
                        workspace_id,
                        metadata['dataset_id']
                    )
                    metadata['data_sources'] = data_sources
                    print(f"   │  └─ Found {len(data_sources)} data source(s)")

                    # Extract scanner data from scan results if available
                    if metadata['dataset_id'] in dataset_lookup:
                        print(f"   ├─ 📊 Extracting dataset schema from scan...")
                        scanned_dataset = dataset_lookup[metadata['dataset_id']]

                        # Extract tables, columns, measures, relationships
                        model_tables = []
                        model_columns = {}
                        measures = []
                        relationships = []
                        expressions = []

                        for table in scanned_dataset.get('tables', []):
                            table_name = table.get('name')
                            if table_name:
                                model_tables.append(table_name)

                                # Extract columns
                                cols = []
                                for col in table.get('columns', []):
                                    col_info = {
                                        'name': col.get('name'),
                                        'dataType': col.get('dataType'),
                                        'columnType': col.get('columnType', 'Data')
                                    }
                                    # Include DAX expression for calculated columns
                                    if col.get('expression'):
                                        col_info['expression'] = col.get('expression')
                                    cols.append(col_info)
                                model_columns[table_name] = cols

                                # Extract measures
                                for measure in table.get('measures', []):
                                    measures.append({
                                        'table': table_name,
                                        'name': measure.get('name'),
                                        'expression': measure.get('expression'),
                                        'description': measure.get('description')
                                    })

                                # Extract M expressions
                                for src in table.get('source', []):
                                    expr = src.get('expression')
                                    if expr:
                                        expressions.append({
                                            'table': table_name,
                                            'expression': expr
                                        })

                        # Extract relationships
                        for rel in scanned_dataset.get('relationships', []):
                            relationships.append({
                                'name': rel.get('name'),
                                'fromTable': rel.get('fromTable'),
                                'fromColumn': rel.get('fromColumn'),
                                'toTable': rel.get('toTable'),
                                'toColumn': rel.get('toColumn'),
                                'type': rel.get('type'),
                                'isActive': rel.get('isActive')
                            })

                        metadata['model_tables'] = model_tables
                        metadata['model_columns'] = model_columns
                        metadata['measures'] = measures
                        metadata['relationships'] = relationships
                        metadata['expressions'] = expressions

                        print(f"   │  └─ ✅ Extracted: {len(model_tables)} tables, "
                              f"{len(measures)} measures, {len(relationships)} relationships")
                    else:
                        # Initialize empty backend metadata if no scan data
                        metadata['model_tables'] = []
                        metadata['model_columns'] = {}
                        metadata['measures'] = []
                        metadata['relationships'] = []
                        metadata['expressions'] = []
                        print(f"   ├─ ℹ️  No scanner data available for this dataset")
            
            # Generate AI content
            print(f"   ├─ 🤖 Generating AI documentation...")
            
            overview = ai_doc.generate_overview(metadata)
            print(f"   │  └─ ✅ Overview generated")
            
            user_guide = ai_doc.generate_user_guide(pages, report_name)
            print(f"   │  └─ ✅ User guide generated")
            
            if metadata.get('data_sources'):
                data_sources_doc = ai_doc.generate_data_sources_doc(
                    metadata['data_sources']
                )
                print(f"   │  └─ ✅ Data sources documented")
            else:
                data_sources_doc = None
            
            migration_proc = ai_doc.generate_migration_steps(report_name)
            print(f"   │  └─ ✅ Migration procedure generated")
            
            technical_notes = ai_doc.generate_technical_details(metadata)
            print(f"   │  └─ ✅ Technical notes generated")
            
            # Generate Word document
            print(f"   ├─ 📝 Creating Word document...")
            doc_creator = DocumentGenerator()
            
            # Prepare JSON payload
            doc_json = {
                'metadata': metadata,
                'overview': overview,
                'pages': pages,
                'user_guide': user_guide,
                'data_sources': metadata.get('data_sources') or data_sources_doc,
                'migration': migration_proc,
                'technical_details': technical_notes
            }
            
            # Save document
            safe_workspace_name = workspace_name.replace(' ', '_').replace('/', '-').replace('\\', '-')
            safe_report_name = report_name.replace(' ', '_').replace('/', '-').replace('\\', '-')
            
            repo_root = os.path.dirname(__file__)
            output_dir = os.path.join(repo_root, 'generated_docs', safe_workspace_name)
            os.makedirs(output_dir, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d")
            filename = f"{safe_report_name}_Documentation_{timestamp}.docx"
            filepath = os.path.join(output_dir, filename)
            
            doc_creator.create_documentation_from_json(doc_json, filepath)
            
            print(f"   └─ ✅ Saved: {filepath}")
            stats['successful_docs'] += 1
            
            time.sleep(1)
            
        except Exception as e:
            print(f"   └─ ❌ Error: {str(e)}")
            stats['failed_docs'] += 1
            continue

def print_final_summary(stats):
    """Print final summary statistics"""
    print("\n" + "="*100)
    print("🎉 DOCUMENTATION GENERATION COMPLETE!")
    print("="*100)
    print(f"\n📊 Summary Statistics:")
    print(f"   ├─ Workspaces processed:     {stats['total_workspaces']}")
    print(f"   ├─ Reports processed:        {stats['total_reports']}")
    print(f"   ├─ Total pages documented:   {stats['total_pages']}")
    print(f"   ├─ Successfully generated:   {stats['successful_docs']}")
    print(f"   ├─ Failed:                   {stats['failed_docs']}")
    print(f"   └─ Skipped:                  {stats['skipped_reports']}")
    
    repo_root = os.path.dirname(__file__)
    print(f"\n📁 Documents saved in: {os.path.join(repo_root, 'generated_docs')}")
    print("="*100)
    
    # Show folder structure
    print("\n📂 Output Directory Structure:")
    generated_root = os.path.join(repo_root, 'generated_docs')
    for root, dirs, files in os.walk(generated_root):
        level = root.replace(generated_root, '').count(os.sep)
        indent = ' ' * 2 * level
        folder_name = os.path.basename(root) if os.path.basename(root) else 'generated_docs'
        print(f'{indent}├─ {folder_name}/')
        sub_indent = ' ' * 2 * (level + 1)
        for file in files[:3]:
            print(f'{sub_indent}├─ {file}')
        if len(files) > 3:
            print(f'{sub_indent}└─ ... and {len(files) - 3} more file(s)')

def main():
    print("\n" + "="*100)
    print("🏢 ENTERPRISE POWER BI DOCUMENTATION GENERATOR")
    print("="*100)
    print(f"📅 Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*100)
    
    # Initialize clients
    admin_client = PowerBIAdminClient()
    ai_doc = AIDocGenerator()
    
    # Step 1: Authenticate
    print("\n📝 Step 1: Authenticating with Power BI Admin API...")
    if not admin_client.authenticate():
        print("\n❌ Authentication failed!")
        print("   Please check:")
        print("   - Your credentials in .env file")
        print("   - Service Principal has Power BI Administrator role")
        print("   - Admin API settings are enabled in Power BI tenant")
        return
    
    # Step 2: Get all workspaces
    print("\n📝 Step 2: Discovering workspaces in organization...")
    all_workspaces = admin_client.get_all_workspaces()
    
    if not all_workspaces:
        print("\n❌ No workspaces found or insufficient permissions.")
        print("   Please verify Service Principal has proper admin access.")
        return
    
    # Step 3: Filter out personal workspaces
    print("\n📝 Step 3: Filtering workspaces...")
    org_workspaces = filter_workspaces(all_workspaces)
    
    if not org_workspaces:
        print("\n❌ No organizational workspaces found after filtering.")
        return
    
    # Step 4: Let user select workspace mode
    mode, selected_workspaces, selected_reports = select_workspace_mode(org_workspaces, admin_client)
    
    if mode is None:
        print("\n👋 Goodbye!")
        return
    
    # Step 5: Process based on selection
    if mode == 'all_workspaces':
        process_workspaces(admin_client, ai_doc, org_workspaces, mode)
    
    elif mode == 'specific_workspaces':
        process_workspaces(admin_client, ai_doc, selected_workspaces, mode)
    
    elif mode == 'specific_reports':
        process_workspaces(admin_client, ai_doc, None, mode, selected_reports)
    
    print(f"\n📅 Completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("\n" + "="*100)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Process interrupted by user")
        print("👋 Exiting gracefully...")
    except Exception as e:
        print(f"\n\n❌ Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()
