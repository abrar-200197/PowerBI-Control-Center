"""
================================================================================
POWER BI DOCUMENTATION GENERATOR - MAIN SCRIPT
================================================================================

PURPOSE:
This script automatically generates documentation for Power BI reports and datasets.
It connects to Power BI, fetches report metadata, uses AI to analyze it, and
creates professional Word documents.

WHAT THIS SCRIPT DOES:
1. Connects to Power BI workspace using your credentials
2. Fetches report and dataset information
3. Uses OpenAI to generate intelligent descriptions
4. Creates formatted Word documents with all the information
5. Tracks which reports have been documented to avoid duplicates

REQUIREMENTS:
- Power BI credentials (Client ID, Secret, Tenant ID)
- OpenAI API key for AI-generated descriptions
- Python packages: requests, openai, python-docx, python-dotenv

================================================================================
"""

# ============================================================================
# STEP 1: IMPORT REQUIRED LIBRARIES
# ============================================================================
# These are pre-built code packages that provide functionality we need

import os          # For working with files and folders on your computer
import sys         # For system-level operations like exiting the program
import json        # For reading and writing JSON data files
from datetime import datetime  # For working with dates and times
from dotenv import load_dotenv  # For loading configuration from .env file

# ============================================================================
# STEP 2: DEFINE CONFIGURATION CONSTANTS
# ============================================================================
# These are fixed values used throughout the program

# The file where we track which reports have been documented
INDEX_FILE = "doc_index.json"

# The format we use for storing dates and times (Year-Month-Day Hour:Minute:Second)
DATE_FMT = "%Y-%m-%dT%H:%M:%S"

# ============================================================================
# STEP 3: IMPORT OUR CUSTOM MODULES
# ============================================================================
# These are other Python files in this project that contain specialized functions

try:
    # Import functions from ai_generator.py:
    # - PowerBIDataFetcher: Connects to Power BI and gets report data
    # - generate_complete_documentation: Uses AI to create documentation
    from ai_generator import PowerBIDataFetcher, generate_complete_documentation

    # Import function from document_creator.py:
    # - create_document_from_json_file: Creates Word documents from JSON data
    from document_creator import create_document_from_json_file

except ImportError as e:
    # If the files don't exist or can't be loaded, show an error message
    print(f"❌ Error importing modules: {e}")
    print("\nMake sure these files exist:")
    print("  - ai_generator.py")
    print("  - document_creator.py")
    sys.exit(1)  # Exit the program with error code 1

# ============================================================================
# STEP 4: LOAD ENVIRONMENT VARIABLES
# ============================================================================
# This reads your configuration from the .env file (passwords, API keys, etc.)
# The .env file keeps sensitive information separate from the code
load_dotenv()



# ============================================================================
# MAIN CLASS: PowerBIDocumentationPipeline
# ============================================================================
# This is the main "brain" of the program that coordinates all the steps
# Think of it as a factory assembly line that processes Power BI reports

class PowerBIDocumentationPipeline:
    """
    WHAT THIS CLASS DOES:
    This class manages the entire process of generating documentation for
    Power BI reports. It handles:
    - Connecting to Power BI
    - Fetching report information
    - Generating AI documentation
    - Creating Word documents
    - Tracking what's been documented
    """

    # ========================================================================
    # INITIALIZATION METHOD
    # ========================================================================
    # This runs when you create a new instance of the class
    # It loads all your settings from the .env file

    def __init__(self):
        """
        WHAT THIS DOES:
        Sets up the documentation generator by loading all configuration
        settings from your .env file (like passwords and API keys)

        CONFIGURATION LOADED:
        - Power BI credentials (to access your workspace)
        - OpenAI API key (for AI-generated descriptions)
        - Author name (who created the documentation)
        - Output folder (where to save generated documents)
        - History settings (how much refresh history to include)
        """

        # Load Power BI authentication credentials from .env file
        # CLIENT_ID: Your Power BI application ID (like a username for apps)
        self.client_id = os.getenv('CLIENT_ID')

        # CLIENT_SECRET: Your Power BI application password (keep this secret!)
        self.client_secret = os.getenv('CLIENT_SECRET')

        # TENANT_ID: Your organization's Microsoft/Azure ID
        self.tenant_id = os.getenv('TENANT_ID')

        # WORKSPACE_ID: The specific Power BI workspace to document
        self.workspace_id = os.getenv('WORKSPACE_ID')

        # OPENAI_API_KEY: Your OpenAI API key for AI-generated content
        self.openai_api_key = os.getenv('OPENAI_API_KEY')

        # AUTHOR_NAME: Who is creating this documentation (defaults to 'System Generated')
        self.author = os.getenv('AUTHOR_NAME', 'System Generated')

        # OUTPUT_FOLDER: Where to save the generated documents (defaults to 'generated_docs')
        self.output_folder = os.getenv('OUTPUT_FOLDER', 'generated_docs')

        # HISTORY_TOP: How many refresh history records to retrieve (defaults to 20)
        # This controls how much historical data is included in the documentation
        try:
            # Try to convert the setting to a number
            self.history_top = int(os.getenv('HISTORY_TOP', '20'))
        except ValueError:
            # If it's not a valid number, use the default of 20
            self.history_top = 20

        # Validate that all required settings are present
        # This will check if you forgot to set any required values in .env
        self.validate_config()

    # ========================================================================
    # INDEX MANAGEMENT METHODS
    # ========================================================================
    # These methods manage the "index" file that tracks which reports have
    # been documented and when. This prevents re-generating documentation
    # for reports that haven't changed.

    def load_index(self):
        """
        WHAT THIS DOES:
        Loads the index file that tracks which reports have been documented.

        THE INDEX FILE CONTAINS:
        - Report IDs
        - Report names
        - When each report was last modified
        - When documentation was last generated

        RETURNS:
        A dictionary (like a phonebook) with report information
        If the file doesn't exist, returns an empty dictionary
        """

        # Check if the index file exists on your computer
        if os.path.exists(INDEX_FILE):
            try:
                # Open the file and read its contents
                with open(INDEX_FILE, "r", encoding="utf-8") as f:
                    # Load the JSON data and convert it to a Python dictionary
                    return json.load(f)
            except Exception:
                # If there's any error reading the file, return empty dictionary
                return {}

        # If file doesn't exist, return empty dictionary
        return {}

    def save_index(self, index):
        """
        WHAT THIS DOES:
        Saves the index information back to the file.

        PARAMETERS:
        - index: A dictionary containing all the report tracking information

        This is called after generating documentation to remember what was done.
        """

        # Open the index file for writing (creates it if it doesn't exist)
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            # Convert the dictionary to JSON format and save it
            # indent=2 makes it human-readable with nice formatting
            json.dump(index, f, indent=2)

    # ========================================================================
    # CONFIGURATION & SETUP METHODS
    # ========================================================================
    # These methods check that everything is set up correctly before starting

    def validate_config(self):
        """
        WHAT THIS DOES:
        Checks that all required settings are present in your .env file.
        If anything is missing, the program will stop and tell you what's needed.

        REQUIRED SETTINGS:
        - CLIENT_ID: Power BI application ID
        - CLIENT_SECRET: Power BI application password
        - TENANT_ID: Your organization's Microsoft ID
        - WORKSPACE_ID: The Power BI workspace to document
        - OPENAI_API_KEY: Your OpenAI API key for AI features

        If any of these are missing, the program exits with an error message.
        """

        print("\n🔍 Validating configuration...")

        # Create a dictionary of all required settings and their current values
        required = {
            'CLIENT_ID': self.client_id,
            'CLIENT_SECRET': self.client_secret,
            'TENANT_ID': self.tenant_id,
            'WORKSPACE_ID': self.workspace_id,
            'OPENAI_API_KEY': self.openai_api_key
        }

        # Find which settings are missing (empty or not set)
        # This creates a list of setting names that don't have values
        missing = [key for key, value in required.items() if not value]

        # If any settings are missing, show error and exit
        if missing:
            print(f"❌ Missing configuration: {', '.join(missing)}")
            print("\nPlease check your .env file")
            sys.exit(1)  # Exit the program with error code

        # If we get here, all settings are present
        print("✅ Configuration valid")

    def ensure_output_folder(self):
        """
        WHAT THIS DOES:
        Makes sure the output folder exists where documents will be saved.
        If the folder doesn't exist, it creates it.

        EXAMPLE:
        If OUTPUT_FOLDER is set to "generated_docs" and that folder doesn't
        exist, this will create it automatically.
        """

        # Check if the output folder exists
        if not os.path.exists(self.output_folder):
            # Create the folder (and any parent folders if needed)
            os.makedirs(self.output_folder)
            print(f"📁 Created output folder: {self.output_folder}")

    # ========================================================================
    # POWER BI DATA FETCHING METHODS
    # ========================================================================
    # These methods connect to Power BI and retrieve report/dataset information

    def get_all_reports(self):
        """
        WHAT THIS DOES:
        Connects to your Power BI workspace and gets a list of all reports.

        THE PROCESS:
        1. Creates a connection to Power BI using your credentials
        2. Authenticates (logs in) to Power BI
        3. Retrieves the list of all reports in your workspace
        4. For each report, ensures it has a dataset ID (needed for documentation)
        5. Returns the complete list of reports

        WHY WE NEED DATASET ID:
        Sometimes Power BI doesn't include the dataset ID in the initial list.
        We need this ID to get the data model information, so we fetch it
        separately if it's missing.

        RETURNS:
        A list of reports, where each report is a dictionary containing:
        - id: The report's unique identifier
        - name: The report's display name
        - datasetId: The ID of the dataset (data model) used by the report
        - modifiedDateTime: When the report was last updated
        """

        print("\n📊 Fetching reports from workspace...")

        # Create a PowerBIDataFetcher object to handle API communication
        # This is like creating a messenger that talks to Power BI for us
        fetcher = PowerBIDataFetcher(
            self.client_id,      # Your app ID
            self.client_secret,  # Your app password
            self.tenant_id,      # Your organization ID
            self.workspace_id    # Which workspace to access
        )

        # Authenticate with Power BI (log in)
        fetcher.get_access_token()

        # Get the list of all reports in the workspace
        reports = fetcher.get_all_reports_in_workspace()

        # Check each report to make sure it has a dataset ID
        # If missing, fetch the detailed information to get it
        for rpt in reports:
            if not rpt.get('datasetId'):
                # Fetch detailed information for this specific report
                details = fetcher.get_report_details(rpt.get('id'))
                if details and details.get('datasetId'):
                    # Add the dataset ID to the report information
                    rpt['datasetId'] = details.get('datasetId')
                    print(f"   ⚙️  Filled datasetId for report '{rpt.get('name')}'")

        return reports

    def get_all_datasets(self):
        """
        WHAT THIS DOES:
        Gets a list of all datasets (also called semantic models) in the workspace.

        WHAT IS A DATASET?
        A dataset is the data model that contains:
        - Tables and columns
        - Relationships between tables
        - Measures and calculations
        - Data refresh schedules

        This is useful when you want to document just the data model without
        a specific report.

        RETURNS:
        A list of datasets with their IDs and names
        """

        print("\n📊 Fetching datasets from workspace...")

        # Create a connection to Power BI
        fetcher = PowerBIDataFetcher(
            self.client_id,
            self.client_secret,
            self.tenant_id,
            self.workspace_id
        )

        # Log in to Power BI
        fetcher.get_access_token()

        # Get and return the list of all datasets
        return fetcher.get_all_datasets_in_workspace()

    # ========================================================================
    # DOCUMENTATION GENERATION METHODS
    # ========================================================================
    # These methods handle the actual creation of documentation

    def generate_single_report_documentation(self, report_id, dataset_id, report_name, semantic_only=False):
        """
        WHAT THIS DOES:
        Generates complete documentation for a single Power BI report.
        This is the main workhorse function that creates your documentation.

        THE TWO-STEP PROCESS:
        Step 1: Generate AI documentation and save as JSON
                - Fetches all metadata from Power BI (tables, columns, measures, etc.)
                - Uses OpenAI to generate intelligent descriptions
                - Saves everything to a JSON file

        Step 2: Create Word document from JSON
                - Reads the JSON file
                - Formats it into a professional Word document
                - Adds tables, formatting, and structure

        PARAMETERS:
        - report_id: The unique ID of the report (can be None for dataset-only docs)
        - dataset_id: The unique ID of the dataset/semantic model
        - report_name: The display name of the report (used for file naming)
        - semantic_only: If True, only documents the data model (not report pages)

        RETURNS:
        A dictionary with the results:
        - report_name: Name of the report
        - report_id: ID of the report
        - json_file: Path to the generated JSON file
        - docx_file: Path to the generated Word document
        - status: 'Success' or error message
        """

        print("\n" + "=" * 70)
        print(f"📄 Processing: {report_name} {'(Semantic Model Only)' if semantic_only else ''}")
        print("=" * 70)

        try:
            # ================================================================
            # STEP 1: GENERATE AI DOCUMENTATION AND SAVE AS JSON
            # ================================================================
            print("\n🤖 Step 1: Generating AI documentation...")

            # Check if we should use OpenAI for AI-generated descriptions
            # This can be turned off in .env by setting USE_OPENAI=false
            # Useful for testing or if you want to save API costs
            use_ai = os.getenv('USE_OPENAI', 'true').lower() in ('1','true','yes')

            # Call the AI documentation generator
            # This function does the heavy lifting:
            # 1. Connects to Power BI
            # 2. Fetches all metadata (tables, columns, measures, relationships, etc.)
            # 3. Sends metadata to OpenAI for intelligent descriptions
            # 4. Combines everything into a structured format
            docs = generate_complete_documentation(
                client_id=self.client_id,          # Power BI app ID
                client_secret=self.client_secret,  # Power BI app password
                tenant_id=self.tenant_id,          # Organization ID
                group_id=self.workspace_id,        # Workspace ID
                report_id=report_id,               # Report to document
                dataset_id=dataset_id,             # Dataset to document
                openai_api_key=self.openai_api_key,  # OpenAI API key
                semantic_only=semantic_only,       # Data model only?
                history_top=self.history_top,      # How many refresh records
                use_ai=use_ai                      # Use AI or not
            )

            # ================================================================
            # CREATE SAFE FILENAME
            # ================================================================
            # Report names might contain special characters that aren't allowed
            # in filenames (like / \ : * ? " < > |), so we clean them up

            # Remove any characters that aren't letters, numbers, spaces, hyphens, or underscores
            safe_name = "".join(c for c in report_name if c.isalnum() or c in (' ', '-', '_')).strip()

            # Replace spaces with underscores for cleaner filenames
            # Example: "Sales Report 2024" becomes "Sales_Report_2024"
            safe_name = safe_name.replace(' ', '_')

            # Add a suffix if this is semantic-only documentation
            suffix = '_semantic' if semantic_only else ''

            # Create the full path for the JSON file
            # Example: "generated_docs/Sales_Report_2024_documentation.json"
            json_filename = os.path.join(self.output_folder, f"{safe_name}{suffix}_documentation.json")

            # ================================================================
            # SAVE THE DOCUMENTATION AS JSON FILE
            # ================================================================
            # Open the file for writing (creates it if it doesn't exist)
            with open(json_filename, 'w', encoding='utf-8') as f:
                # Convert the documentation dictionary to JSON format and save it
                # indent=2: Makes it human-readable with nice formatting
                # ensure_ascii=False: Allows special characters (like é, ñ, etc.)
                json.dump(docs, f, indent=2, ensure_ascii=False)

            print(f"✅ JSON saved (with full metadata): {json_filename}")

            # ================================================================
            # STEP 2: CREATE WORD DOCUMENT FROM JSON
            # ================================================================
            print("\n📝 Step 2: Creating Word document...")

            # Create the full path for the Word document
            # Example: "generated_docs/Sales_Report_2024_Documentation.docx"
            docx_filename = os.path.join(self.output_folder, f"{safe_name}{suffix}_Documentation.docx")

            # Check if we should export refresh history to CSV
            # This creates a separate CSV file with refresh history data
            export_csv = os.getenv('EXPORT_REFRESH_CSV', 'false').lower() in ('1','true','yes')

            # Call the document creator function
            # This reads the JSON file and creates a formatted Word document
            create_document_from_json_file(
                json_filename=json_filename,    # The JSON file we just created
                output_filename=docx_filename,  # Where to save the Word doc
                author=self.author,             # Who created this document
                use_ai=use_ai,                  # Whether AI was used
                export_csv=export_csv           # Export refresh history to CSV?
            )

            # ================================================================
            # SUCCESS! SHOW SUMMARY
            # ================================================================
            print(f"\n✅ Documentation complete for: {report_name}")
            print(f"   📁 JSON: {json_filename}")
            print(f"   📄 DOCX: {docx_filename}")

            # Return a success result with all the file paths
            return {
                'report_name': report_name,
                'report_id': report_id,
                'json_file': json_filename,
                'docx_file': docx_filename,
                'status': 'Success'
            }

        except Exception as e:
            # ================================================================
            # ERROR HANDLING
            # ================================================================
            # If anything goes wrong, catch the error and return failure status

            print(f"\n❌ Error processing {report_name}: {e}")

            # Print detailed error information for debugging
            import traceback
            traceback.print_exc()

            # Return a failure result
            return {
                'report_name': report_name,
                'report_id': report_id,
                'status': f'Failed: {str(e)}'
            }

    def export_metadata(self, report_id, dataset_id, report_name, semantic_only=False):
        """
        WHAT THIS DOES:
        Exports just the raw metadata (without AI descriptions) to a JSON file.

        USE CASE:
        This is useful when you want to:
        - Share raw data with other systems
        - Analyze metadata programmatically
        - Skip the AI generation step to save costs
        - Create custom documentation formats

        DIFFERENCE FROM generate_single_report_documentation:
        - This only exports raw metadata (no AI descriptions)
        - No Word document is created
        - Faster and cheaper (no OpenAI API calls)

        PARAMETERS:
        - report_id: The report's unique ID
        - dataset_id: The dataset's unique ID
        - report_name: Display name (used for filename)
        - semantic_only: If True, only export dataset metadata

        RETURNS:
        The path to the created JSON file, or error information if failed
        """
        try:
            print(f"\n📄 Exporting metadata for: {report_name}")

            # Create a connection to Power BI
            fetcher = PowerBIDataFetcher(
                self.client_id,
                self.client_secret,
                self.tenant_id,
                self.workspace_id
            )

            # Fetch the metadata based on what type of documentation we want
            if semantic_only:
                # Only get dataset/semantic model metadata (no report pages)
                metadata = fetcher.get_complete_metadata(dataset_id, report_id=None, history_top=self.history_top)
            else:
                # Get both report and dataset metadata
                metadata = fetcher.get_complete_metadata(dataset_id, report_id, history_top=self.history_top)

            # Create a safe filename (remove special characters)
            safe_name = "".join(c for c in report_name if c.isalnum() or c in (' ', '-', '_')).strip()
            safe_name = safe_name.replace(' ', '_')
            suffix = '_semantic' if semantic_only else ''

            # Create the full file path
            json_filename = os.path.join(self.output_folder, f"{safe_name}{suffix}_metadata.json")

            # Save the metadata to a JSON file
            with open(json_filename, 'w', encoding='utf-8') as f:
                json.dump({
                    'metadata': metadata,
                    'relationships': metadata.get('relationships', [])
                }, f, indent=2, ensure_ascii=False)

            print(f"✅ Metadata JSON saved: {json_filename}")
            return json_filename

        except Exception as e:
            # If anything goes wrong, show the error
            print(f"\n❌ Error exporting metadata for {report_name}: {e}")
            import traceback
            traceback.print_exc()

            # Return error information
            return {
                'report_name': report_name,
                'report_id': report_id,
                'status': f'Failed: {str(e)}'
            }

    # ========================================================================
    # WORKSPACE-WIDE DOCUMENTATION METHODS
    # ========================================================================
    # These methods handle generating documentation for all reports at once

    def generate_workspace_documentation(self):
        """
        WHAT THIS DOES:
        Generates documentation for ALL reports in the workspace automatically.

        THE SMART PROCESS:
        1. Gets a list of all reports in the workspace
        2. Checks which reports have changed since last documentation
        3. Only generates documentation for new or modified reports
        4. Skips reports that haven't changed (saves time and API costs)
        5. Tracks everything in the index file

        CHANGE DETECTION:
        The system remembers when each report was last documented and compares
        the report's "modifiedDateTime" to see if it needs updating.

        RETURNS:
        A list of results showing which reports were documented successfully
        and which ones failed or were skipped.
        """

        # Show header with configuration information
        print("\n" + "=" * 70)
        print("🚀 POWER BI WORKSPACE DOCUMENTATION GENERATOR")
        print("=" * 70)
        print(f"\nWorkspace ID: {self.workspace_id}")
        print(f"Output Folder: {self.output_folder}")
        print(f"Author: {self.author}")
        print("=" * 70)

        # Make sure the output folder exists
        self.ensure_output_folder()

        # Get all reports from the Power BI workspace
        reports = self.get_all_reports()

        # If no reports found, exit early
        if not reports:
            print("\n⚠️  No reports found in workspace!")
            return []

        print(f"\n✅ Found {len(reports)} report(s)")

        # Load the index file that tracks which reports have been documented
        index = self.load_index()

        # Create a list to store results for each report
        results = []

        # ================================================================
        # PROCESS EACH REPORT
        # ================================================================
        # Loop through each report and decide if it needs documentation
        # enumerate gives us both the index number and the report data

        for idx, report in enumerate(reports, 1):
            # Extract information about this report
            report_name = report['name']              # Display name
            report_id = report['id']                  # Unique identifier
            dataset_id = report.get('datasetId')      # Associated dataset ID
            modified = report.get('modifiedDateTime') or ''  # When last modified

            # ============================================================
            # ENSURE DATASET ID IS PRESENT
            # ============================================================
            # Sometimes the dataset ID is missing from the initial report list
            # We need it to generate documentation, so fetch it if missing

            if not dataset_id:
                print(f"   ⚙️  Dataset ID missing for {report_name}, fetching details...")

                # Create a temporary connection to Power BI
                fetcher = PowerBIDataFetcher(
                    self.client_id,
                    self.client_secret,
                    self.tenant_id,
                    self.workspace_id
                )
                fetcher.get_access_token()

                # Fetch detailed information for this report
                details = fetcher.get_report_details(report_id)

                # If we got the dataset ID, add it to the report
                if details and details.get('datasetId'):
                    dataset_id = details.get('datasetId')
                    report['datasetId'] = dataset_id
                    print(f"   ✅ Dataset ID obtained: {dataset_id}")

            # If we still don't have a dataset ID, skip this report
            if not dataset_id:
                results.append({
                    'report_name': report_name,
                    'report_id': report_id,
                    'status': 'Skipped - No dataset ID'
                })
                continue  # Move to the next report

            # ============================================================
            # CHECK IF REPORT NEEDS DOCUMENTATION UPDATE
            # ============================================================
            # Compare the report's modification date with our index

            needs_update = False

            # If this report has never been documented before
            if report_id not in index:
                needs_update = True
            else:
                # Check if the report has been modified since last documentation
                last_mod = index[report_id].get('last_modified')
                if last_mod != modified:
                    needs_update = True

            # Show progress information
            print(f"\n{'#' * 70}")
            print(f"Report {idx} of {len(reports)}: {report_name}")
            print(f"Changed since last doc? {'YES' if needs_update else 'NO'}")
            print(f"{'#' * 70}")

            # If no update needed, skip this report
            if not needs_update:
                results.append({
                    'report_name': report_name,
                    'report_id': report_id,
                    'status': 'Skipped - No changes'
                })
                continue  # Move to the next report

            # ============================================================
            # GENERATE DOCUMENTATION FOR THIS REPORT
            # ============================================================
            result = self.generate_single_report_documentation(
                report_id=report_id,
                dataset_id=dataset_id,
                report_name=report_name
            )

            # ============================================================
            # UPDATE THE INDEX
            # ============================================================
            # Record that we've documented this report
            index[report_id] = {
                "report_name": report_name,
                "last_modified": modified,
                "last_doc_generated": datetime.now().strftime(DATE_FMT)
            }

            # Add the result to our results list
            results.append(result)

        # Save the updated index back to the file
        self.save_index(index)

        # Print a summary of what was done
        self.print_summary(results)

        return results

    # ========================================================================
    # SUMMARY AND REPORTING METHODS
    # ========================================================================

    def print_summary(self, results):
        """
        WHAT THIS DOES:
        Prints a nice summary of what was accomplished.
        Shows which reports were successful and which failed.

        PARAMETERS:
        - results: A list of result dictionaries from documentation generation

        THE SUMMARY INCLUDES:
        - Total number of reports processed
        - How many succeeded
        - How many failed
        - File paths for successful documents
        - Error messages for failed reports
        """

        # Separate successful and failed results
        # A report is successful if its status is exactly 'Success'
        successful = [r for r in results if r['status'] == 'Success']
        failed = [r for r in results if r['status'] != 'Success']

        # Print the header
        print("\n\n" + "=" * 70)
        print("📊 DOCUMENTATION GENERATION SUMMARY")
        print("=" * 70)

        # Print statistics
        print(f"\nTotal Reports: {len(results)}")
        print(f"✅ Successful: {len(successful)}")
        print(f"❌ Failed: {len(failed)}")

        # Show details of successful reports
        if successful:
            print(f"\n✅ Successfully Generated:")
            print("-" * 70)
            for r in successful:
                print(f"\n📄 {r['report_name']}")
                print(f"   JSON: {r['json_file']}")
                print(f"   DOCX: {r['docx_file']}")

        # Show details of failed reports
        if failed:
            print(f"\n❌ Failed Reports:")
            print("-" * 70)
            for r in failed:
                print(f"\n📄 {r['report_name']}")
                print(f"   Status: {r['status']}")

        # Show where all files were saved
        print(f"\n📁 All files saved to: {os.path.abspath(self.output_folder)}")
        print("\n" + "=" * 70)
        print("✅ PROCESS COMPLETE!")
        print("=" * 70 + "\n")



# ============================================================================
# INTERACTIVE & BATCH MODE FUNCTIONS
# ============================================================================
# These functions provide different ways to run the documentation generator

def interactive_mode():
    """
    WHAT THIS DOES:
    Runs the program in interactive mode where you can choose what to document.

    INTERACTIVE MODE FEATURES:
    - Shows you a menu of options
    - Lets you choose to document all reports or just one
    - Lets you select a specific report from a list
    - Lets you document just a dataset (without a report)

    HOW TO USE:
    Just run the program without any command-line arguments and this mode
    will start automatically. Follow the on-screen prompts.
    """

    # Show the header
    print("\n" + "=" * 70)
    print("🎯 POWER BI DOCUMENTATION GENERATOR - Interactive Mode")
    print("=" * 70)

    # Create the documentation pipeline (loads configuration)
    pipeline = PowerBIDocumentationPipeline()

    # Show the menu of options
    print("\nWhat would you like to do?")
    print("\n1. Generate documentation for ALL reports in workspace")
    print("2. Generate documentation for a SPECIFIC report")
    print("3. Generate documentation for a SPECIFIC dataset (semantic model)")
    print("4. Exit")

    # Get the user's choice
    choice = input("\nEnter your choice (1-4): ").strip()

    # ========================================================================
    # OPTION 1: DOCUMENT ALL REPORTS
    # ========================================================================
    if choice == '1':
        print("\n🚀 Starting workspace-wide documentation generation...")
        # Call the workspace documentation function
        # This will process all reports in the workspace
        pipeline.generate_workspace_documentation()

    # ========================================================================
    # OPTION 2: DOCUMENT A SPECIFIC REPORT
    # ========================================================================
    elif choice == '2':
        # Get all reports from the workspace
        reports = pipeline.get_all_reports()

        # If no reports found, exit
        if not reports:
            print("\n⚠️  No reports found!")
            return

        # Show a numbered list of all reports
        print(f"\n📊 Available Reports:")
        print("-" * 70)
        for idx, r in enumerate(reports, 1):
            print(f"{idx}. {r['name']}")

        try:
            # Ask the user to select a report by number
            selection = int(input(f"\nSelect report (1-{len(reports)}): ").strip())

            # Check if the selection is valid
            if 1 <= selection <= len(reports):
                # Get the selected report (subtract 1 because lists start at 0)
                selected = reports[selection - 1]

                # ========================================================
                # ENSURE DATASET ID IS PRESENT
                # ========================================================
                # If the selected report doesn't have a dataset ID, try to fetch it
                if not selected.get('datasetId'):
                    print("   ⚙️ Attempting to fetch missing dataset id…")

                    # Create a connection to Power BI
                    fetcher = PowerBIDataFetcher(
                        pipeline.client_id,
                        pipeline.client_secret,
                        pipeline.tenant_id,
                        pipeline.workspace_id,
                    )
                    fetcher.get_access_token()

                    # Fetch detailed report information
                    details = fetcher.get_report_details(selected.get('id'))

                    # If we got the dataset ID, add it
                    if details and details.get('datasetId'):
                        selected['datasetId'] = details.get('datasetId')
                        print("   ✅ Dataset ID retrieved from report details")

                # If we still don't have a dataset ID, we can't continue
                if not selected.get('datasetId'):
                    print(f"\n❌ Error: Report has no dataset ID")
                    return

                # Make sure the output folder exists
                pipeline.ensure_output_folder()

                # ========================================================
                # ASK HOW MANY REFRESH HISTORY ROWS TO FETCH
                # ========================================================
                # Give the user option to customize how much history to include
                try:
                    top_input = input("Enter number of refresh history rows to fetch (enter for default): ").strip()
                    if top_input:
                        pipeline.history_top = int(top_input)
                except ValueError:
                    print("   ⚠️ Invalid number, using default")

                # ========================================================
                # GENERATE THE DOCUMENTATION
                # ========================================================
                result = pipeline.generate_single_report_documentation(
                    report_id=selected['id'],
                    dataset_id=selected['datasetId'],
                    report_name=selected['name']
                )

                # Show the result
                if result['status'] == 'Success':
                    print(f"\n✅ Documentation generated successfully!")
                    print(f"📁 JSON: {result['json_file']}")
                    print(f"📄 DOCX: {result['docx_file']}")
                else:
                    print(f"\n❌ Failed: {result['status']}")
            else:
                print("\n❌ Invalid selection")
        except ValueError:
            # If the user didn't enter a valid number
            print("\n❌ Invalid input")

    # ========================================================================
    # OPTION 3: DOCUMENT A SPECIFIC DATASET (SEMANTIC MODEL)
    # ========================================================================
    elif choice == '3':
        """
        This option documents just the data model (dataset/semantic model)
        without any specific report. Useful when you want to document:
        - The tables and columns
        - Relationships between tables
        - Measures and calculations
        - Refresh history
        """

        # Get all datasets from the workspace
        datasets = pipeline.get_all_datasets()

        # If no datasets found, exit
        if not datasets:
            print("\n⚠️  No datasets found!")
            return

        # Show a numbered list of all datasets
        print(f"\n📊 Available Datasets: (semantic models)")
        print("-" * 70)
        for idx, d in enumerate(datasets, 1):
            # Use the dataset name if available, otherwise use its ID
            name = d.get('name') or d.get('id')
            print(f"{idx}. {name}")

        try:
            # Ask the user to select a dataset by number
            sel = int(input(f"\nSelect dataset (1-{len(datasets)}): ").strip())

            # Check if the selection is valid
            if 1 <= sel <= len(datasets):
                # Get the selected dataset
                ds = datasets[sel - 1]
                ds_id = ds.get('id')
                ds_name = ds.get('name', ds_id)

                # Make sure the output folder exists
                pipeline.ensure_output_folder()

                # Ask how many refresh history rows to fetch
                try:
                    top_input = input("Enter number of refresh history rows to fetch (enter for default): ").strip()
                    if top_input:
                        pipeline.history_top = int(top_input)
                except ValueError:
                    print("   ⚠️ Invalid number, using default")

                # Generate documentation for the dataset only
                # Note: report_id is None because we're not documenting a report
                # semantic_only=True means only document the data model
                result = pipeline.generate_single_report_documentation(
                    report_id=None,
                    dataset_id=ds_id,
                    report_name=ds_name,
                    semantic_only=True
                )

                # Show the result
                if result['status'] == 'Success':
                    print(f"\n✅ Semantic documentation generated successfully!")
                    print(f"📁 JSON: {result['json_file']}")
                    print(f"📄 DOCX: {result['docx_file']}")
                else:
                    print(f"\n❌ Failed: {result['status']}")
            else:
                print("\n❌ Invalid selection")
        except ValueError:
            # If the user didn't enter a valid number
            print("\n❌ Invalid input")

    # ========================================================================
    # OPTION 4: EXIT
    # ========================================================================
    elif choice == '4':
        print("\n👋 Goodbye!")
        return

    # ========================================================================
    # INVALID CHOICE
    # ========================================================================
    else:
        print("\n❌ Invalid choice")



def batch_mode():
    """
    WHAT THIS DOES:
    Runs the program in batch mode - automatically processes all reports.

    BATCH MODE FEATURES:
    - No user interaction required
    - Processes all reports in the workspace
    - Only updates reports that have changed
    - Perfect for automation (scheduled tasks, CI/CD pipelines)

    HOW TO USE:
    Run the program with the --batch flag:
    python main.py --batch

    USE CASES:
    - Scheduled nightly documentation updates
    - Automated documentation after deployments
    - Continuous integration pipelines
    """

    # Show the header
    print("\n" + "=" * 70)
    print("🚀 POWER BI DOCUMENTATION GENERATOR - Batch Mode")
    print("=" * 70)

    # Create the pipeline and run workspace documentation
    pipeline = PowerBIDocumentationPipeline()
    pipeline.generate_workspace_documentation()



# ============================================================================
# MAIN PROGRAM ENTRY POINT
# ============================================================================
# This is where the program starts when you run it

if __name__ == "__main__":
    """
    WHAT THIS DOES:
    This is the main entry point of the program. It handles command-line
    arguments and decides which mode to run in.

    COMMAND-LINE ARGUMENTS:
    You can control how the program runs by passing arguments:

    --batch
        Run in batch mode (process all reports automatically)
        Example: python main.py --batch

    --interactive
        Run in interactive mode (show menu)
        Example: python main.py --interactive

    --report-id <ID>
        Process a specific report by ID
        Example: python main.py --report-id abc123 --dataset-id def456

    --dataset-id <ID>
        Specify the dataset ID (required with --report-id)
        Or use alone to document just a dataset
        Example: python main.py --dataset-id def456

    --semantic
        Document only the semantic model (not report pages)
        Example: python main.py --report-id abc123 --dataset-id def456 --semantic

    --history-top <NUMBER>
        How many refresh history rows to fetch
        Example: python main.py --batch --history-top 50

    If no arguments are provided, interactive mode starts automatically.
    """

    # Import the argument parser library
    import argparse

    # Create an argument parser to handle command-line arguments
    parser = argparse.ArgumentParser(description='Power BI Documentation Generator')

    # Define all the command-line arguments we accept
    parser.add_argument('--batch', action='store_true', help='Run in batch mode (all reports)')
    parser.add_argument('--interactive', action='store_true', help='Run in interactive mode')
    parser.add_argument('--report-id', type=str, help='Specific report ID to process')
    parser.add_argument('--dataset-id', type=str, help='Dataset ID for the report or a standalone semantic model')
    parser.add_argument('--semantic', action='store_true', help='Generate using semantic model only (ignore report)')
    parser.add_argument('--history-top', type=int, help='Number of refresh history rows to fetch')

    # Parse the arguments that were passed when running the program
    args = parser.parse_args()

    try:
        # ====================================================================
        # HANDLE HISTORY-TOP OVERRIDE
        # ====================================================================
        # If the user specified how many history rows to fetch, use that
        if args.history_top:
            # Set it as an environment variable so the pipeline picks it up
            os.environ['HISTORY_TOP'] = str(args.history_top)

        # ====================================================================
        # BATCH MODE
        # ====================================================================
        # If --batch flag was provided, run in batch mode
        if args.batch:
            batch_mode()

        # ====================================================================
        # DATASET-ONLY MODE
        # ====================================================================
        # If only dataset ID was provided (no report ID), document just the dataset
        elif args.dataset_id and not args.report_id:
            pipeline = PowerBIDocumentationPipeline()
            pipeline.ensure_output_folder()

            # Generate documentation for the dataset only
            result = pipeline.generate_single_report_documentation(
                report_id=None,  # No report
                dataset_id=args.dataset_id,
                report_name=f"Dataset_{args.dataset_id[:8]}",  # Use first 8 chars of ID as name
                semantic_only=True  # Only document the data model
            )
            print(f"\nResult: {result['status']}")

        # ====================================================================
        # SPECIFIC REPORT MODE
        # ====================================================================
        # If both report ID and dataset ID were provided, document that specific report
        elif args.report_id and args.dataset_id:
            pipeline = PowerBIDocumentationPipeline()
            pipeline.ensure_output_folder()

            # Generate documentation for the specific report
            result = pipeline.generate_single_report_documentation(
                report_id=args.report_id,
                dataset_id=args.dataset_id,
                report_name=f"Report_{args.report_id[:8]}",  # Use first 8 chars of ID as name
                semantic_only=args.semantic  # Use --semantic flag if provided
            )
            print(f"\nResult: {result['status']}")

        # ====================================================================
        # INTERACTIVE MODE (DEFAULT)
        # ====================================================================
        # If no specific mode was requested, run in interactive mode
        else:
            interactive_mode()

    # ========================================================================
    # ERROR HANDLING
    # ========================================================================
    except KeyboardInterrupt:
        # If the user presses Ctrl+C, exit gracefully
        print("\n\n⚠️  Process interrupted by user")

    except Exception as e:
        # If any unexpected error occurs, show it
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()  # Show detailed error information
