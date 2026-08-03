# powerbi_connector.py - Corrected for Client Credentials Flow
import requests
import json
from config import Config

class PowerBIConnector:
    """Handles all Power BI API interactions using client credentials or user-delegated tokens"""

    def __init__(self, user_token=None):
        """
        Initialize PowerBIConnector

        Args:
            user_token (str, optional): User-delegated access token. If provided, this will be used
                                       instead of service principal authentication.
        """
        self.access_token = None
        self.user_token = user_token  # User-delegated token (takes priority)
        self.base_url = "https://api.powerbi.com/v1.0/myorg"

    def set_user_token(self, token):
        """Set user-delegated access token for API calls"""
        self.user_token = token
        print("🔑 Using user-delegated access token")

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
        # Priority 1: Use user-delegated token if available
        if self.user_token:
            return {
                'Authorization': f'Bearer {self.user_token}',
                'Content-Type': 'application/json'
            }

        # Priority 2: Use service principal token
        if not self.access_token:
            if not self.authenticate():
                raise Exception("Failed to authenticate with Power BI")

        return {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }
    
    def get_workspaces(self):
        """Get list of all workspaces (groups) the service principal has access to"""
        print(f"\n📊 Fetching workspaces...")

        url = f"{self.base_url}/groups"

        try:
            response = requests.get(url, headers=self._get_headers())
            response.raise_for_status()

            workspaces = response.json().get('value', [])
            print(f"✅ Found {len(workspaces)} workspace(s)")

            if workspaces:
                print("\nWorkspaces:")
                for idx, workspace in enumerate(workspaces, 1):
                    print(f"   {idx}. {workspace['name']} (ID: {workspace['id']})")

            return workspaces

        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching workspaces!")
            print(f"   Error: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"   Status Code: {e.response.status_code}")
                try:
                    error_detail = e.response.json()
                    print(f"   Details: {error_detail}")
                except:
                    print(f"   Response: {e.response.text[:200]}")
            return []

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

    def get_report_metadata(self, workspace_id, report_id):
        """Get report metadata - alias for get_report_details"""
        reports = self.get_all_reports(workspace_id)
        report = next((r for r in reports if r['id'] == report_id), None)
        if report:
            return self.get_report_details(workspace_id, report_id, report.get('name', 'Report'))
        return {}

    def get_refresh_schedule(self, workspace_id, dataset_id):
        """Get refresh schedule for a dataset"""
        try:
            url = f"{self.base_url}/groups/{workspace_id}/datasets/{dataset_id}/refreshSchedule"
            response = requests.get(url, headers=self._get_headers())

            if response.status_code == 200:
                return response.json()
            else:
                print(f"⚠️ Failed to get refresh schedule for dataset {dataset_id} in workspace {workspace_id}")
                print(f"   Status Code: {response.status_code}")
                try:
                    error_detail = response.json()
                    print(f"   Error Details: {error_detail}")
                except:
                    print(f"   Response: {response.text[:200]}")
            return None
        except Exception as e:
            print(f"⚠️ Error getting refresh schedule: {str(e)}")
            return None

    def get_refresh_history(self, workspace_id, dataset_id, top=5):
        """Get refresh history for a dataset"""
        try:
            url = f"{self.base_url}/groups/{workspace_id}/datasets/{dataset_id}/refreshes?$top={top}"
            response = requests.get(url, headers=self._get_headers())

            if response.status_code == 200:
                return response.json().get('value', [])
            else:
                print(f"⚠️ Failed to get refresh history for dataset {dataset_id} in workspace {workspace_id}")
                print(f"   Status Code: {response.status_code}")
                try:
                    error_detail = response.json()
                    print(f"   Error Details: {error_detail}")
                except:
                    print(f"   Response: {response.text[:200]}")
            return []
        except Exception as e:
            print(f"⚠️ Error getting refresh history: {str(e)}")
            return []

    def get_dataset_info(self, workspace_id, dataset_id):
        """Get dataset information"""
        try:
            url = f"{self.base_url}/groups/{workspace_id}/datasets/{dataset_id}"
            response = requests.get(url, headers=self._get_headers())

            if response.status_code == 200:
                return response.json()
            else:
                print(f"⚠️ Failed to get dataset info for dataset {dataset_id} in workspace {workspace_id}")
                print(f"   Status Code: {response.status_code}")
                try:
                    error_detail = response.json()
                    print(f"   Error Details: {error_detail}")
                except:
                    print(f"   Response: {response.text[:200]}")
            return None
        except Exception as e:
            print(f"⚠️ Error getting dataset info: {str(e)}")
            return None

    def resolve_dataset_refresh(self, workspace_id, dataset_id, dataset_workspace_id=None, history_top=5):
        """
        Instance wrapper around the shared refresh resolver.
        Uses this connector's auth headers.
        """
        return resolve_dataset_refresh_info(
            headers=self._get_headers(),
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            dataset_workspace_id=dataset_workspace_id,
            history_top=history_top,
            base_url=self.base_url,
        )


# =============================================================================
# Shared refresh resolution (usable from threads without a connector instance)
# =============================================================================

def _empty_refresh_info(**overrides):
    """Canonical refresh payload used across the app."""
    info = {
        'refresh_schedule': 'Unknown',
        'last_refreshed': None,
        'last_refresh_status': None,
        'refresh_type': 'unknown',          # import | directquery | live | not_refreshable | unknown | error
        'dataset_owner': 'Unknown',
        'dataset_workspace_id': None,
        'schedule_days': [],
        'schedule_times': [],
        'schedule_summary': None,
        'refresh_note': None,               # human-readable reason when timestamp missing
        'is_refreshable': None,
    }
    info.update(overrides)
    return info


def _normalize_refresh_status(status):
    """Normalize Power BI refresh status strings."""
    if not status:
        return None
    s = str(status).strip()
    lower = s.lower()
    if lower in ('completed', 'success', 'succeeded'):
        return 'Completed'
    if lower in ('failed', 'failure'):
        return 'Failed'
    if lower in ('unknown', 'inprogress', 'in progress', 'running', 'notstarted', 'not started'):
        # Power BI uses "Unknown" while a refresh is still running
        return 'InProgress'
    if lower in ('cancelled', 'canceled', 'disabled'):
        return s.title() if s.lower() != 'disabled' else 'Disabled'
    return s


def _is_in_progress_status(status):
    normalized = _normalize_refresh_status(status)
    return normalized == 'InProgress'


def pick_best_refresh_from_history(refreshes):
    """
    Choose the best last-refresh timestamp/status from history.

    Power BI returns newest-first. The top entry often has endTime=null while
    status is Unknown/InProgress. In that case walk to the next entries and use
    the newest record that has an endTime (typically the previous completed run).

    Returns dict:
      last_refreshed, last_refresh_status, refresh_note, source_index
    """
    if not refreshes:
        return {
            'last_refreshed': None,
            'last_refresh_status': None,
            'refresh_note': 'No refresh history',
            'source_index': None,
        }

    latest = refreshes[0] or {}
    latest_status_raw = latest.get('status')
    latest_end = latest.get('endTime') or None
    latest_start = latest.get('startTime') or None
    latest_status = _normalize_refresh_status(latest_status_raw)

    # Ideal case: newest entry already finished
    if latest_end:
        return {
            'last_refreshed': latest_end,
            'last_refresh_status': latest_status or 'Completed',
            'refresh_note': None,
            'source_index': 0,
        }

    # Newest has no endTime — walk history for the first finished entry
    finished_idx = None
    finished = None
    for idx, entry in enumerate(refreshes):
        if not entry:
            continue
        end_time = entry.get('endTime')
        if end_time:
            finished_idx = idx
            finished = entry
            break

    if finished:
        finished_status = _normalize_refresh_status(finished.get('status')) or 'Completed'
        if _is_in_progress_status(latest_status_raw):
            # Keep UI honest: a run is in progress, but still show last finished time
            return {
                'last_refreshed': finished.get('endTime'),
                'last_refresh_status': 'InProgress',
                'refresh_note': f'Refresh in progress; showing last completed ({finished_status})',
                'source_index': finished_idx,
            }
        return {
            'last_refreshed': finished.get('endTime'),
            'last_refresh_status': finished_status,
            'refresh_note': f'Latest entry had no endTime; used history item #{finished_idx + 1}',
            'source_index': finished_idx,
        }

    # No finished entry at all — fall back to startTime of newest (in progress / never finished)
    if latest_start:
        return {
            'last_refreshed': latest_start,
            'last_refresh_status': latest_status or 'InProgress',
            'refresh_note': 'No completed refresh found; showing start time',
            'source_index': 0,
        }

    return {
        'last_refreshed': None,
        'last_refresh_status': latest_status,
        'refresh_note': 'Refresh history present but no timestamps',
        'source_index': 0,
    }


def _format_refresh_schedule(schedule_data, is_refreshable, refresh_type):
    """Build schedule display fields from /refreshSchedule payload."""
    if refresh_type in ('directquery', 'live'):
        label = 'DirectQuery/Live'
        return {
            'refresh_schedule': label,
            'schedule_days': [],
            'schedule_times': [],
            'schedule_summary': None,
        }

    if is_refreshable is False:
        return {
            'refresh_schedule': 'Not Refreshable',
            'schedule_days': [],
            'schedule_times': [],
            'schedule_summary': None,
        }

    if not schedule_data:
        return {
            'refresh_schedule': 'No Schedule' if is_refreshable else 'Unknown',
            'schedule_days': [],
            'schedule_times': [],
            'schedule_summary': None,
        }

    enabled = schedule_data.get('enabled', False)
    days = schedule_data.get('days', []) or []
    times = schedule_data.get('times', []) or []

    if enabled and days and times:
        days_str = ', '.join(days)
        times_str = ', '.join(times)
        return {
            'refresh_schedule': f"Scheduled: {days_str} at {times_str}",
            'schedule_days': days,
            'schedule_times': times,
            'schedule_summary': f"({len(days)} days, {len(times)} times/day)",
        }
    if enabled:
        return {
            'refresh_schedule': 'Enabled (incomplete)',
            'schedule_days': days,
            'schedule_times': times,
            'schedule_summary': None,
        }
    return {
        'refresh_schedule': 'No Schedule',
        'schedule_days': [],
        'schedule_times': [],
        'schedule_summary': None,
    }


def _classify_dataset_type(dataset_info, history_status_code=None):
    """
    Classify dataset connectivity / refresh behavior.
    """
    if history_status_code == 415:
        return 'directquery'

    if not dataset_info:
        return 'unknown'

    is_refreshable = dataset_info.get('isRefreshable')
    # Common Power BI markers for live/DQ (field presence varies by API version)
    storage_mode = (
        dataset_info.get('targetStorageMode')
        or dataset_info.get('contentProviderType')
        or ''
    )
    storage_lower = str(storage_mode).lower()

    if 'directquery' in storage_lower or storage_lower in ('direct query',):
        return 'directquery'
    if 'live' in storage_lower or storage_lower in ('liveconnect', 'live connection'):
        return 'live'

    if is_refreshable is False:
        # Non-refreshable import-like models are rare; usually DQ/Live/push
        return 'not_refreshable'

    if is_refreshable is True:
        return 'import'

    return 'unknown'


def parse_refresh_timestamp(value):
    """Parse ISO / epoch refresh timestamps to aware UTC datetime, or None."""
    if value is None or value == '':
        return None
    try:
        from datetime import datetime, timezone
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        s = str(value).strip()
        if not s or s.lower() in {'unknown', 'n/a', 'none', 'null', '-', '—'}:
            return None
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def days_since_refresh(value, as_of=None):
    """Whole days since last refresh; None if timestamp unknown."""
    from datetime import datetime, timezone
    dt = parse_refresh_timestamp(value)
    if not dt:
        return None
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0, (now - dt).days)


def merge_refresh_candidates(*sources, prefer_keys=None):
    """
    Merge multiple refresh info dicts and pick the LATEST last_refreshed.

    Used when catalog/SharePoint ops has a refresh timestamp (OneDrive snapshot)
    but live Power BI history is missing/stale (or the reverse). UI should show
    the newer of both and recompute days_since_refresh from that winner.

    Non-timestamp fields (schedule, type, owner) prefer the first non-empty from
    sources in order, unless that field is tied to the winning timestamp source.
    """
    prefer_keys = prefer_keys or (
        'refresh_schedule', 'refresh_type', 'refresh_note',
        'dataset_owner', 'dataset_workspace_id', 'is_refreshable',
    )
    cleaned = [s for s in sources if isinstance(s, dict) and s]
    if not cleaned:
        return _empty_refresh_info()

    best = None
    best_dt = None
    for src in cleaned:
        ts = src.get('last_refreshed')
        dt = parse_refresh_timestamp(ts)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best = src

    # Base: first source that has any useful structure, else first
    base = dict(cleaned[0])
    for src in cleaned[1:]:
        for k in prefer_keys:
            if not base.get(k) and src.get(k) not in (None, '', 'Unknown', 'N/A'):
                base[k] = src.get(k)

    if best is not None:
        base['last_refreshed'] = best.get('last_refreshed')
        # Prefer status from the same source as the winning timestamp
        if best.get('last_refresh_status'):
            base['last_refresh_status'] = best.get('last_refresh_status')
        note_bits = []
        if best.get('refresh_note'):
            note_bits.append(str(best.get('refresh_note')))
        # Annotate if we picked catalog/ops over empty live (or vice versa)
        others = [s for s in cleaned if s is not best]
        other_has_ts = any(parse_refresh_timestamp(s.get('last_refreshed')) for s in others)
        if other_has_ts:
            note_bits.append('latest of live API + catalog ops')
        elif any(
            not parse_refresh_timestamp(s.get('last_refreshed'))
            for s in others
        ):
            note_bits.append('filled from alternate refresh source')
        if note_bits and not base.get('refresh_note'):
            base['refresh_note'] = '; '.join(note_bits)
        elif note_bits and 'latest of' not in str(base.get('refresh_note') or ''):
            base['refresh_note'] = (
                f"{base.get('refresh_note')}; {note_bits[-1]}"
                if base.get('refresh_note') else note_bits[-1]
            )
    else:
        # No timestamps anywhere — still take best status label available
        for src in cleaned:
            if src.get('last_refresh_status') and not base.get('last_refresh_status'):
                base['last_refresh_status'] = src.get('last_refresh_status')

    base['days_since_refresh'] = days_since_refresh(base.get('last_refreshed'))
    return base


def resolve_dataset_refresh_info(
    headers,
    workspace_id,
    dataset_id,
    dataset_workspace_id=None,
    history_top=5,
    base_url="https://api.powerbi.com/v1.0/myorg",
    timeout=8,
):
    """
    Robust dataset refresh resolution used by /api/reports and other callers.

    Handles:
      - In-progress top history row with null endTime (walk to next finished entry)
      - DirectQuery/Live (HTTP 415) — explicit label, not blank N/A
      - Cross-workspace datasets (try dataset home workspace, then report workspace,
        then workspace-less /datasets/{id} path)
      - Missing history, not refreshable, auth/timeout errors

    Returns a dict from _empty_refresh_info().
    """
    if not dataset_id:
        return _empty_refresh_info(
            refresh_schedule='N/A',
            last_refresh_status='No Dataset',
            refresh_type='unknown',
            refresh_note='Report has no datasetId',
        )

    # Candidate workspaces, de-duplicated, prefer dataset's home workspace first
    candidates = []
    for ws in (dataset_workspace_id, workspace_id):
        if ws and ws not in candidates:
            candidates.append(ws)

    refresh_info = _empty_refresh_info(dataset_workspace_id=dataset_workspace_id or workspace_id)

    try:
        dataset_info = None
        resolved_ws = None
        history = None
        history_status = None

        def _get_dataset(ws_id):
            if ws_id:
                url = f"{base_url}/groups/{ws_id}/datasets/{dataset_id}"
            else:
                url = f"{base_url}/datasets/{dataset_id}"
            return requests.get(url, headers=headers, timeout=timeout)

        def _get_history(ws_id):
            if ws_id:
                url = f"{base_url}/groups/{ws_id}/datasets/{dataset_id}/refreshes?$top={history_top}"
            else:
                url = f"{base_url}/datasets/{dataset_id}/refreshes?$top={history_top}"
            return requests.get(url, headers=headers, timeout=timeout)

        def _get_schedule(ws_id):
            if ws_id:
                url = f"{base_url}/groups/{ws_id}/datasets/{dataset_id}/refreshSchedule"
            else:
                url = f"{base_url}/datasets/{dataset_id}/refreshSchedule"
            return requests.get(url, headers=headers, timeout=timeout)

        # ---- Resolve dataset metadata (try candidate workspaces, then no-group) ----
        attempt_ws = list(candidates) + [None]
        for ws in attempt_ws:
            try:
                resp = _get_dataset(ws)
            except requests.exceptions.RequestException as ex:
                print(f"      ⚠️ Dataset lookup error (ws={ws}): {ex}")
                continue

            if resp.status_code == 200:
                dataset_info = resp.json()
                resolved_ws = ws if ws is not None else dataset_info.get('workspaceId') or workspace_id
                break
            if resp.status_code in (401, 403):
                print(f"      ⚠️ Dataset lookup forbidden (ws={ws}): HTTP {resp.status_code}")
            # 404 → try next candidate

        if dataset_info:
            refresh_info['dataset_owner'] = dataset_info.get('configuredBy', 'Unknown')
            refresh_info['is_refreshable'] = dataset_info.get('isRefreshable')
            refresh_info['dataset_workspace_id'] = resolved_ws
        else:
            # Still try history against candidates; dataset GET may fail while refreshes work
            resolved_ws = candidates[0] if candidates else workspace_id
            refresh_info['dataset_workspace_id'] = resolved_ws

        # ---- Refresh history (prefer resolved workspace, then fallbacks) ----
        history_ws_attempts = []
        for ws in [resolved_ws] + candidates + [None]:
            if ws not in history_ws_attempts:
                history_ws_attempts.append(ws)

        for ws in history_ws_attempts:
            try:
                resp = _get_history(ws)
            except requests.exceptions.RequestException as ex:
                print(f"      ⚠️ Refresh history error (ws={ws}): {ex}")
                history_status = 'error'
                continue

            history_status = resp.status_code
            print(f"      Refresh API status: {resp.status_code} (ws={str(ws)[:8] if ws else 'none'})")

            if resp.status_code == 200:
                history = resp.json().get('value', []) or []
                resolved_ws = ws if ws is not None else resolved_ws
                refresh_info['dataset_workspace_id'] = resolved_ws
                break

            if resp.status_code == 415:
                # DirectQuery / Live — not applicable for scheduled refresh history
                history = []
                resolved_ws = ws if ws is not None else resolved_ws
                refresh_info['dataset_workspace_id'] = resolved_ws
                break

            if resp.status_code in (401, 403):
                refresh_info['refresh_note'] = f'Access denied to refresh history (HTTP {resp.status_code})'
                # Keep trying other workspace scopes; may succeed elsewhere
                continue

            # 404 / other — try next

        # ---- Classify type ----
        refresh_type = _classify_dataset_type(dataset_info, history_status_code=history_status)
        refresh_info['refresh_type'] = refresh_type

        # DirectQuery / Live path
        if refresh_type in ('directquery', 'live') or history_status == 415:
            label = 'DirectQuery/Live'
            refresh_info['refresh_type'] = 'directquery' if refresh_type != 'live' else 'live'
            refresh_info['refresh_schedule'] = label
            refresh_info['last_refreshed'] = None
            refresh_info['last_refresh_status'] = label
            refresh_info['refresh_note'] = 'Live connection — no import refresh history'
            refresh_info['is_refreshable'] = False
            print(f"      ℹ️ {label} dataset (no import refresh)")
            return refresh_info

        # ---- Parse history with endTime fallback ----
        if history_status == 200:
            picked = pick_best_refresh_from_history(history)
            refresh_info['last_refreshed'] = picked['last_refreshed']
            refresh_info['last_refresh_status'] = picked['last_refresh_status']
            refresh_info['refresh_note'] = picked['refresh_note']
            if picked['last_refreshed']:
                print(
                    f"      ✅ Last refresh: {picked['last_refreshed']} - "
                    f"{picked['last_refresh_status']}"
                    + (f" ({picked['refresh_note']})" if picked['refresh_note'] else "")
                )
            else:
                print(f"      ⚠️ {picked['refresh_note'] or 'No usable refresh timestamp'}")
                if not refresh_info['last_refresh_status']:
                    if refresh_info.get('is_refreshable') is False:
                        refresh_info['last_refresh_status'] = 'Not Refreshable'
                    else:
                        refresh_info['last_refresh_status'] = 'No History'
        elif history_status in (401, 403):
            refresh_info['last_refresh_status'] = 'Access Denied'
            refresh_info['refresh_type'] = 'error'
            if not refresh_info.get('refresh_note'):
                refresh_info['refresh_note'] = 'No permission to read refresh history'
        elif history_status == 'error':
            refresh_info['last_refresh_status'] = 'Error'
            refresh_info['refresh_type'] = 'error'
            refresh_info['refresh_note'] = refresh_info.get('refresh_note') or 'Failed to query refresh history'
        else:
            # 404 / unknown
            if refresh_info.get('is_refreshable') is False:
                refresh_info['last_refresh_status'] = 'Not Refreshable'
                refresh_info['refresh_type'] = 'not_refreshable'
                refresh_info['refresh_note'] = 'Dataset is not refreshable'
            else:
                refresh_info['last_refresh_status'] = 'No History'
                refresh_info['refresh_note'] = (
                    f'Could not load refresh history'
                    + (f' (HTTP {history_status})' if history_status else '')
                )

        # ---- Schedule (only meaningful for refreshable import models) ----
        is_refreshable = refresh_info.get('is_refreshable')
        schedule_data = None
        if is_refreshable and refresh_type == 'import':
            for ws in history_ws_attempts:
                try:
                    sresp = _get_schedule(ws)
                except requests.exceptions.RequestException:
                    continue
                if sresp.status_code == 200:
                    schedule_data = sresp.json()
                    break
                if sresp.status_code == 404:
                    schedule_data = None
                    break

        schedule_fields = _format_refresh_schedule(schedule_data, is_refreshable, refresh_info['refresh_type'])
        refresh_info.update(schedule_fields)

        # If still unknown schedule but we know not refreshable
        if refresh_info['refresh_type'] == 'not_refreshable':
            refresh_info['refresh_schedule'] = 'Not Refreshable'

        return refresh_info

    except Exception as e:
        print(f"      ❌ Error resolving refresh for dataset {str(dataset_id)[:8]}: {e}")
        return _empty_refresh_info(
            refresh_schedule='Error',
            last_refresh_status='Error',
            refresh_type='error',
            refresh_note=str(e),
            dataset_workspace_id=dataset_workspace_id or workspace_id,
        )


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
