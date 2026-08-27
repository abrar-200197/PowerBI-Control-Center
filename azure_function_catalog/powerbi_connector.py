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
        # scheduled | ondemand | viaapi | admin | content_modified | unknown
        'refresh_source': None,
        # Scheduled history type from API when present (Scheduled/OnDemand/ViaApi/...)
        'history_refresh_type': None,
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


def _history_refresh_source_label(entry):
    """Map Power BI history refreshType → UI/source label."""
    if not entry:
        return 'scheduled'
    rt = str(entry.get('refreshType') or entry.get('type') or '').strip().lower()
    if rt in ('scheduled',):
        return 'scheduled'
    if rt in ('ondemand', 'on demand', 'on_demand'):
        return 'ondemand'
    if rt in ('viaapi', 'via api', 'via_api', 'viaenhancedapi'):
        return 'viaapi'
    if rt in ('viaxmlaendpoint', 'via xmla endpoint'):
        return 'viaxmla'
    if 'onedrive' in rt:
        return 'onedrive'
    return rt or 'scheduled'


def pick_best_refresh_from_history(refreshes):
    """
    Choose the best last-refresh timestamp/status from history.

    Power BI returns newest-first. The top entry often has endTime=null while
    status is Unknown/InProgress. In that case walk to the next entries and use
    the newest record that has an endTime (typically the previous completed run).

    NOTE: Microsoft documents that OneDrive refresh history is NOT returned by
    GET .../refreshes. This picker only sees Scheduled / OnDemand / ViaApi / XMLA
    rows that the Scheduled tab would show.

    Returns dict:
      last_refreshed, last_refresh_status, refresh_note, source_index,
      refresh_source, history_refresh_type
    """
    empty = {
        'last_refreshed': None,
        'last_refresh_status': None,
        'refresh_note': 'No refresh history',
        'source_index': None,
        'refresh_source': None,
        'history_refresh_type': None,
    }
    if not refreshes:
        return empty

    latest = refreshes[0] or {}
    latest_status_raw = latest.get('status')
    latest_end = latest.get('endTime') or None
    latest_start = latest.get('startTime') or None
    latest_status = _normalize_refresh_status(latest_status_raw)
    latest_type = latest.get('refreshType') or latest.get('type')

    def _pack(entry, idx, status, note, ts_key='endTime'):
        src = _history_refresh_source_label(entry)
        return {
            'last_refreshed': (entry or {}).get(ts_key),
            'last_refresh_status': status,
            'refresh_note': note,
            'source_index': idx,
            'refresh_source': src,
            'history_refresh_type': (entry or {}).get('refreshType') or (entry or {}).get('type'),
        }

    # Ideal case: newest entry already finished
    if latest_end:
        return _pack(latest, 0, latest_status or 'Completed', None, 'endTime')

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
            return _pack(
                finished,
                finished_idx,
                'InProgress',
                f'Refresh in progress; showing last completed ({finished_status})',
                'endTime',
            )
        return _pack(
            finished,
            finished_idx,
            finished_status,
            f'Latest entry had no endTime; used history item #{finished_idx + 1}',
            'endTime',
        )

    # No finished entry at all — fall back to startTime of newest
    if latest_start:
        return _pack(
            latest,
            0,
            latest_status or 'InProgress',
            'No completed refresh found; showing start time',
            'startTime',
        )

    return {
        'last_refreshed': None,
        'last_refresh_status': latest_status,
        'refresh_note': 'Refresh history present but no timestamps',
        'source_index': 0,
        'refresh_source': _history_refresh_source_label(latest),
        'history_refresh_type': latest_type,
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


def _is_true_refresh_history(src):
    """True API refresh row (scheduled/ondemand/admin) vs content fallback."""
    if not isinstance(src, dict):
        return False
    src_l = str(src.get('refresh_source') or '').lower().replace('-', '_').replace(' ', '')
    if src_l in (
        'scheduled', 'ondemand', 'on_demand', 'viaapi', 'via_api',
        'admin', 'history', 'api',
    ):
        return True
    if src.get('history_refresh_type'):
        return True
    return False


def _is_weak_created_fallback(src):
    """Dataset createdDate stamped as last_refreshed — weaker than report modified."""
    if not isinstance(src, dict):
        return False
    src_l = str(src.get('refresh_source') or '').lower()
    if src_l in ('content_created', 'created'):
        return True
    note = str(src.get('refresh_note') or '').lower()
    if 'created date' in note or 'dataset created' in note:
        return True
    # legacy: content_modified note that only mentions created
    if src_l == 'content_modified' and 'created' in note and 'modified' not in note:
        return True
    return False


def merge_refresh_candidates(*sources, prefer_keys=None):
    """
    Merge multiple refresh info dicts and pick the best last_refreshed.

    Rules:
      1) TRUE history (scheduled / ondemand / viaapi / admin) → latest of those only.
         Content fallbacks never override real history.
      2) Else content fallbacks: prefer report/dataset *modified* over dataset *created*.
         Among same quality, pick the newest timestamp.

    Microsoft does NOT expose OneDrive-tab history via /refreshes.
    """
    prefer_keys = prefer_keys or (
        'refresh_schedule', 'refresh_type', 'refresh_note',
        'dataset_owner', 'dataset_workspace_id', 'is_refreshable',
        'schedule_days', 'schedule_times', 'schedule_summary',
        'history_refresh_type',
    )
    cleaned = [s for s in sources if isinstance(s, dict) and s]
    if not cleaned:
        return _empty_refresh_info()

    with_ts = []
    for src in cleaned:
        dt = parse_refresh_timestamp(src.get('last_refreshed'))
        if dt is not None:
            with_ts.append((src, dt))

    true_hist = [(s, dt) for s, dt in with_ts if _is_true_refresh_history(s)]
    if true_hist:
        pool = true_hist
    else:
        # Content only: drop weak created stamps when any non-created content exists
        strong = [(s, dt) for s, dt in with_ts if not _is_weak_created_fallback(s)]
        pool = strong if strong else with_ts

    best = None
    best_dt = None
    for src, dt in pool:
        if best is None or dt > best_dt:
            best, best_dt = src, dt

    # Base: first source that has any useful structure, else first
    base = dict(cleaned[0])
    for src in cleaned[1:]:
        for k in prefer_keys:
            if not base.get(k) and src.get(k) not in (None, '', 'Unknown', 'N/A'):
                base[k] = src.get(k)

    if best is not None:
        base['last_refreshed'] = best.get('last_refreshed')
        # Prefer status / source labels from the same winner
        if best.get('last_refresh_status'):
            base['last_refresh_status'] = best.get('last_refresh_status')
        if best.get('refresh_source'):
            base['refresh_source'] = best.get('refresh_source')
        if best.get('history_refresh_type'):
            base['history_refresh_type'] = best.get('history_refresh_type')

        note_bits = []
        if best.get('refresh_note'):
            note_bits.append(str(best.get('refresh_note')))
        sources_with_ts = [
            s for s in cleaned if parse_refresh_timestamp(s.get('last_refreshed'))
        ]
        if len(sources_with_ts) > 1:
            labels = []
            for s in sources_with_ts:
                labels.append(str(s.get('refresh_source') or 'history'))
            uniq = []
            for lb in labels:
                if lb not in uniq:
                    uniq.append(lb)
            note_bits.append('latest of ' + ' + '.join(uniq))
        elif any(not parse_refresh_timestamp(s.get('last_refreshed')) for s in cleaned):
            note_bits.append('filled from alternate refresh source')

        # Drop empty / duplicate notes
        seen_n = set()
        clean_notes = []
        for n in note_bits:
            n = (n or '').strip()
            if not n or n in seen_n:
                continue
            seen_n.add(n)
            clean_notes.append(n)
        if clean_notes:
            base['refresh_note'] = '; '.join(clean_notes)
    else:
        # No timestamps anywhere — still take best status label available
        for src in cleaned:
            if src.get('last_refresh_status') and not base.get('last_refresh_status'):
                base['last_refresh_status'] = src.get('last_refresh_status')
            if src.get('refresh_note') and not base.get('refresh_note'):
                base['refresh_note'] = src.get('refresh_note')
            if src.get('refresh_source') and not base.get('refresh_source'):
                base['refresh_source'] = src.get('refresh_source')

        # Clarify OneDrive-only case for the UI (Scheduled tab empty, portal OneDrive has rows)
        status = str(base.get('last_refresh_status') or '').lower()
        if status in ('', 'no history', 'none', 'null') or not base.get('last_refreshed'):
            base['last_refresh_status'] = base.get('last_refresh_status') or 'No History'
            note = str(base.get('refresh_note') or '')
            if 'onedrive' not in note.lower():
                extra = (
                    'Scheduled refresh history empty. '
                    'OneDrive-tab history is not returned by Power BI REST APIs.'
                )
                base['refresh_note'] = f"{note}; {extra}".strip('; ') if note else extra
            if not base.get('refresh_source'):
                base['refresh_source'] = 'none'

    # Content fallbacks must never look like a successful import refresh in the UI.
    src_final = str(base.get('refresh_source') or '').lower().replace('-', '_').replace(' ', '')
    if src_final in (
        'content_modified', 'content_created', 'contentcreated', 'created',
    ) or _is_weak_created_fallback(base):
        st = str(base.get('last_refresh_status') or '').lower()
        if st in ('completed', 'success', 'succeeded', ''):
            base['last_refresh_status'] = 'Unverified'
        note = str(base.get('refresh_note') or '')
        if 'verify in power bi' not in note.lower():
            extra = (
                'Verify in Power BI service (content modified/created is not a confirmed refresh job).'
            )
            base['refresh_note'] = f'{note}; {extra}'.strip('; ') if note else extra

    base['days_since_refresh'] = days_since_refresh(base.get('last_refreshed'))
    return base


def refresh_info_from_admin_last_refresh(last_refresh, *, dataset_workspace_id=None):
    """
    Build a refresh-info dict from Admin refreshables `lastRefresh` object.
    May capture some refreshes not visible on the plain history endpoint.
    """
    if not isinstance(last_refresh, dict) or not last_refresh:
        return None
    picked = pick_best_refresh_from_history([last_refresh])
    if not picked.get('last_refreshed') and not picked.get('last_refresh_status'):
        return None
    return _empty_refresh_info(
        last_refreshed=picked.get('last_refreshed'),
        last_refresh_status=picked.get('last_refresh_status') or 'Completed',
        refresh_type='import',
        refresh_source=picked.get('refresh_source') or 'admin',
        history_refresh_type=picked.get('history_refresh_type') or last_refresh.get('refreshType'),
        refresh_note=picked.get('refresh_note') or 'From admin refreshables lastRefresh',
        dataset_workspace_id=dataset_workspace_id,
    )


def refresh_info_from_content_modified(dataset_info, report_meta=None, *, dataset_workspace_id=None):
    """
    Last-resort timestamp when Scheduled/Admin history is empty.

    Preference (never use create-time when a modified stamp exists):
      1) report modifiedDateTime (often tracks OneDrive .pbix publish/sync)
      2) dataset modifiedDateTime / lastModified
      3) dataset createdDate only if nothing else (weak — labeled content_created)

    Not true OneDrive history (REST does not expose that tab). Never pretends
    this is Scheduled history.
    """
    modified_candidates = []  # (raw_ts, label)
    created_candidates = []

    if isinstance(report_meta, dict):
        for k in (
            'modifiedDateTime', 'modified_date_time', 'modifiedDate',
            'modified_date', 'lastModified',
        ):
            if report_meta.get(k):
                modified_candidates.append((report_meta.get(k), 'report modified'))

    if isinstance(dataset_info, dict):
        for k in ('modifiedDateTime', 'modifiedDate', 'lastModified'):
            if dataset_info.get(k):
                modified_candidates.append((dataset_info.get(k), 'dataset modified'))
        for k in ('createdDate', 'createdDateTime'):
            if dataset_info.get(k):
                created_candidates.append((dataset_info.get(k), 'dataset created'))

    def _newest(cands):
        best_ts, best_dt, best_label = None, None, None
        for raw, label in cands:
            dt = parse_refresh_timestamp(raw)
            if dt is None:
                continue
            if best_dt is None or dt > best_dt:
                best_ts, best_dt, best_label = raw, dt, label
        return best_ts, best_label

    best_ts, label = _newest(modified_candidates)
    used_created = False
    if not best_ts:
        best_ts, label = _newest(created_candidates)
        used_created = bool(best_ts)

    if not best_ts:
        return None

    if used_created:
        source = 'content_created'
        note = (
            'No Scheduled refresh history; using dataset created date '
            '(no report/dataset modified time; OneDrive-tab history is not available via REST API)'
        )
    else:
        source = 'content_modified'
        note = (
            f'No Scheduled refresh history; using latest {label or "content modified"} time '
            '(OneDrive-tab history is not available via REST API)'
        )

    # Never present content fallback as a successful import refresh.
    # UI must not show green "Success" for these rows — users should verify in Power BI.
    warn = (
        'Verify in Power BI service (Scheduled refresh history was not confirmed this run; '
        'date is content modified/created, not a dataset refresh job). '
        'OneDrive-tab history is not available via REST API.'
    )
    full_note = f'{note}; {warn}' if note else warn
    return _empty_refresh_info(
        last_refreshed=best_ts,
        last_refresh_status='Unverified',
        refresh_type='import',
        refresh_source=source,
        refresh_note=full_note,
        dataset_workspace_id=dataset_workspace_id,
        is_refreshable=(dataset_info or {}).get('isRefreshable') if isinstance(dataset_info, dict) else None,
        dataset_owner=(dataset_info or {}).get('configuredBy', 'Unknown') if isinstance(dataset_info, dict) else 'Unknown',
    )


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

        # ---- Parse Scheduled/OnDemand history (OneDrive tab is NOT in this API) ----
        scheduled_side = None
        if history_status == 200:
            picked = pick_best_refresh_from_history(history)
            scheduled_side = {
                'last_refreshed': picked.get('last_refreshed'),
                'last_refresh_status': picked.get('last_refresh_status'),
                'refresh_note': picked.get('refresh_note'),
                'refresh_source': picked.get('refresh_source') or 'scheduled',
                'history_refresh_type': picked.get('history_refresh_type'),
            }
            if picked.get('last_refreshed'):
                print(
                    f"      ✅ Scheduled history: {picked['last_refreshed']} - "
                    f"{picked['last_refresh_status']}"
                    + (f" ({picked['refresh_note']})" if picked.get('refresh_note') else "")
                )
            else:
                print(
                    f"      ⚠️ Scheduled history empty: "
                    f"{picked.get('refresh_note') or 'No usable timestamp'}"
                )
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

        # Content-modified fallback (report/dataset timestamps). Useful when the
        # portal only has OneDrive-tab history which REST does not expose.
        content_side = None
        if not (scheduled_side and scheduled_side.get('last_refreshed')):
            content_side = refresh_info_from_content_modified(
                dataset_info,
                dataset_workspace_id=refresh_info.get('dataset_workspace_id'),
            )

        # Latest of scheduled history + content-modified (and any pre-seeded fields)
        merged = merge_refresh_candidates(
            refresh_info,
            scheduled_side or {},
            content_side or {},
        )
        for k, v in merged.items():
            if v is not None and v != '':
                refresh_info[k] = v
        if refresh_info.get('last_refreshed'):
            print(
                f"      ✅ Selected refresh: {refresh_info.get('last_refreshed')} "
                f"[{refresh_info.get('refresh_source') or '?'}] "
                f"{refresh_info.get('last_refresh_status')}"
            )
        elif refresh_info.get('last_refresh_status') in (None, 'No History'):
            # Explicit OneDrive gap message for UI tooltips
            refresh_info['last_refresh_status'] = 'No History'
            note = str(refresh_info.get('refresh_note') or '')
            if 'onedrive' not in note.lower():
                refresh_info['refresh_note'] = (
                    (note + '; ' if note else '')
                    + 'Scheduled history empty. OneDrive-tab history is not returned by Power BI REST APIs.'
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
        # Don't let "No Schedule" wipe a stronger prior label unless empty
        for k, v in schedule_fields.items():
            if k == 'refresh_schedule' and refresh_info.get('refresh_schedule') not in (
                None, '', 'Unknown', 'N/A',
            ):
                # Keep existing explicit DQ/error labels
                if str(refresh_info.get('refresh_schedule')).startswith(('DirectQuery', 'Not ', 'Error')):
                    continue
            refresh_info[k] = v

        # If still unknown schedule but we know not refreshable
        if refresh_info['refresh_type'] == 'not_refreshable':
            refresh_info['refresh_schedule'] = 'Not Refreshable'

        refresh_info['days_since_refresh'] = days_since_refresh(refresh_info.get('last_refreshed'))
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
