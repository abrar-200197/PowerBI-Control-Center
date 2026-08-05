"""
Report Usage Tracker - Views Count Feature
Tracks Power BI report views using Activity Events API with 30-day lookback
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading


class ReportUsageTracker:
    """
    Tracks Power BI report usage (view counts) using Activity Events API
    
    Features:
    - 30-day lookback period (Power BI API limit)
    - Parallel fetching (10 workers for performance)
    - Tracks last viewed timestamp and user
    - 1-hour caching to reduce API calls
    - Handles continuation tokens for pagination
    """
    
    def __init__(self, service_principal_token):
        """
        Initialize usage tracker
        
        Args:
            service_principal_token: Power BI Admin access token (Service Principal)
        """
        self.token = service_principal_token
        self.base_url = "https://api.powerbi.com/v1.0/myorg/admin/activityevents"
        self.headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json'
        }
        self.cache = {}  # In-memory cache: {workspace_id: {data, timestamp}}
        self.cache_duration = timedelta(hours=1)
    
    def get_report_usage(self, workspace_id):
        """
        Get report usage metrics for a workspace
        
        Args:
            workspace_id: Power BI workspace GUID
            
        Returns:
            dict: {
                'success': bool,
                'workspace_id': str,
                'days_analyzed': int,
                'report_views': {report_id: view_count},
                'last_viewed': {report_id: {timestamp, user}}
            }
        """
        # Check cache first
        cache_key = workspace_id
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if datetime.now(timezone.utc) - cached['timestamp'] < self.cache_duration:
                print(f"   💾 Using cached usage data (age: {(datetime.now(timezone.utc) - cached['timestamp']).seconds // 60} min)")
                return cached['data']
        
        print(f"\n📊 Fetching report usage for workspace: {workspace_id}")
        print(f"   Lookback period: 30 days")
        
        # Get current date from Power BI API (handles system date issues)
        current_date = self._get_powerbi_server_date()
        
        # Prepare date range (last 30 days)
        days_back = 30
        dates_to_fetch = []
        
        for day_offset in range(days_back):
            target_date = current_date - timedelta(days=day_offset)
            dates_to_fetch.append(target_date.strftime('%Y-%m-%d'))
        
        print(f"   📅 Fetching: {dates_to_fetch[0]} to {dates_to_fetch[-1]}")
        
        # Aggregate results
        report_views = {}
        last_viewed = {}
        results_lock = threading.Lock()
        
        # Parallel fetching (10 workers). Pattern aligned with PowerBI-Crash-Test:
        # plain ISO times, $filter=ViewReport, continuationUri pagination.
        def _ingest_activity(activity, day_views, day_last_viewed):
            """Count one ViewReport if it belongs to this workspace (or no ws filter)."""
            act = (activity.get('Activity') or '').strip()
            if act and act != 'ViewReport':
                return
            # Prefer workspace match when present; accept if field missing (some payloads)
            ws = activity.get('WorkspaceId') or activity.get('WorkSpaceId')
            if ws and str(ws).lower() != str(workspace_id).lower():
                return
            report_id = activity.get('ReportId') or activity.get('ArtifactId')
            if not report_id:
                return
            report_id = str(report_id)
            day_views[report_id] = day_views.get(report_id, 0) + 1
            creation_time = activity.get('CreationTime') or ''
            user_key = (
                activity.get('UserId')
                or activity.get('UserKey')
                or activity.get('UserEmail')
                or 'Unknown'
            )
            if creation_time:
                prev = day_last_viewed.get(report_id)
                if not prev or creation_time > (prev.get('timestamp') or ''):
                    day_last_viewed[report_id] = {
                        'timestamp': creation_time,
                        'user': user_key,
                    }

        def fetch_day_usage(date_str):
            """Fetch usage for a single day"""
            try:
                # No trailing Z — matches working crash-test Activity Events calls
                start_dt = f"{date_str}T00:00:00"
                end_dt = f"{date_str}T23:59:59"
                url = (
                    f"{self.base_url}"
                    f"?startDateTime='{start_dt}'&endDateTime='{end_dt}'"
                    f"&$filter=Activity eq 'ViewReport'"
                )

                day_views = {}
                day_last_viewed = {}
                pages = 0
                max_pages = 100

                while url and pages < max_pages:
                    pages += 1
                    response = requests.get(url, headers=self.headers, timeout=60)

                    if response.status_code == 429:
                        import time as _time
                        wait = float(response.headers.get('Retry-After', 5))
                        _time.sleep(wait)
                        pages -= 1
                        continue

                    if response.status_code != 200:
                        print(f"      {date_str}: Status {response.status_code} {(response.text or '')[:120]}")
                        if pages == 1:
                            return {'views': {}, 'last_viewed': {}, 'success': False}
                        break

                    data = response.json() or {}
                    for activity in data.get('activityEventEntities') or []:
                        _ingest_activity(activity, day_views, day_last_viewed)

                    cont_uri = data.get('continuationUri')
                    cont_tok = data.get('continuationToken')
                    if cont_uri:
                        url = cont_uri
                    elif cont_tok:
                        tok = str(cont_tok).strip().strip("'")
                        url = f"{self.base_url}?continuationToken='{tok}'"
                    else:
                        url = None

                return {
                    'views': day_views,
                    'last_viewed': day_last_viewed,
                    'success': True,
                    'pages': pages,
                }

            except Exception as e:
                print(f"      Error on {date_str}: {str(e)}")
                return {'views': {}, 'last_viewed': {}, 'success': False}

        # Fetch all days in parallel (10 workers)
        print(f"   🚀 Fetching {len(dates_to_fetch)} days in parallel (10 workers)...")

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_day_usage, date): date for date in dates_to_fetch}

            completed = 0
            for future in as_completed(futures):
                result = future.result()
                completed += 1

                if result['success']:
                    # Aggregate views
                    with results_lock:
                        for report_id, count in result['views'].items():
                            report_views[report_id] = report_views.get(report_id, 0) + count

                        # Update last viewed (keep most recent)
                        for report_id, info in result['last_viewed'].items():
                            if (report_id not in last_viewed or
                                info['timestamp'] > last_viewed[report_id]['timestamp']):
                                last_viewed[report_id] = info

                # Progress indicator
                if completed % 5 == 0:
                    print(f"      Progress: {completed}/{len(dates_to_fetch)} days")

        print(f"\n   ✅ Usage tracking complete")
        print(f"      Total reports with views: {len(report_views)}")
        print(f"      Total views: {sum(report_views.values())}")

        result = {
            'success': True,
            'workspace_id': workspace_id,
            'days_analyzed': days_back,
            'report_views': report_views,
            'last_viewed': last_viewed,
            'note': f'Activity data for last {days_back} days (Power BI API limit)'
        }

        # Cache the result
        self.cache[cache_key] = {
            'data': result,
            'timestamp': datetime.now(timezone.utc)
        }

        return result

    def _get_powerbi_server_date(self):
        """
        Get current date from Power BI API server
        (Handles system date discrepancies)
        """
        try:
            # Make a lightweight API call to get server date from headers
            response = requests.get(
                "https://api.powerbi.com/v1.0/myorg/groups",
                headers=self.headers,
                timeout=10
            )

            if response.status_code == 200 and 'Date' in response.headers:
                server_date_str = response.headers['Date']
                # Parse: "Tue, 08 Jul 2026 12:00:00 GMT"
                from email.utils import parsedate_to_datetime
                server_date = parsedate_to_datetime(server_date_str)
                print(f"   📅 Server date: {server_date.strftime('%Y-%m-%d')}")
                return server_date

        except Exception as e:
            print(f"   ⚠️ Could not get server date: {e}")

        # Fallback to system date
        return datetime.now(timezone.utc)


# ============================================================================
# FLASK ROUTE INTEGRATION
# ============================================================================

def create_usage_tracker_route(app, scanner_service):
    """
    Add usage tracking route to Flask app

    Args:
        app: Flask application instance
        scanner_service: PowerBIScanner instance for getting service principal token
    """

    @app.route('/api/report-usage/<workspace_id>')
    def get_report_usage(workspace_id):
        """
        API endpoint to get report usage metrics

        Query Parameters:
            workspace_id: Power BI workspace GUID

        Returns:
            JSON with view counts and last viewed info
        """
        try:
            # Get service principal token (Activity Events API requires admin token)
            service_token = scanner_service.get_access_token()

            if not service_token:
                return {
                    'success': False,
                    'error': 'Unable to obtain service principal token'
                }, 500

            # Create tracker and fetch usage
            tracker = ReportUsageTracker(service_token)
            result = tracker.get_report_usage(workspace_id)

            return result

        except Exception as e:
            print(f"❌ Error in usage tracking: {str(e)}")
            import traceback
            traceback.print_exc()

            return {
                'success': False,
                'error': str(e)
            }, 500
