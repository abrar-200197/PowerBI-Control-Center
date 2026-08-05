"""
Power BI Admin Workspace Scanner API client.
Flow: modified workspaces → getInfo (batched) → poll status → fetch results.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from catalog_service import catalog_config as config
from .auth import PowerBIAuth

logger = logging.getLogger(__name__)


class ScannerClient:
    """Tenant-wide metadata via Admin Scanner API."""

    def __init__(self, auth: Optional[PowerBIAuth] = None):
        self.auth = auth or PowerBIAuth()
        self.base = config.ADMIN_API_BASE

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        params: Optional[dict] = None,
    ) -> requests.Response:
        last_err: Optional[Exception] = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=self.auth.headers(),
                    json=json_body,
                    params=params,
                    timeout=config.HTTP_TIMEOUT_SEC,
                )
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", config.RETRY_BASE_DELAY_SEC * attempt))
                    logger.warning("429 rate limited; sleeping %.1fs (attempt %s)", retry_after, attempt)
                    time.sleep(retry_after)
                    continue
                if resp.status_code in (401, 403) and attempt == 1:
                    self.auth.get_token(force_refresh=True)
                    continue
                if resp.status_code >= 500:
                    delay = config.RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                    logger.warning("Server %s; retry in %.1fs", resp.status_code, delay)
                    time.sleep(delay)
                    continue
                return resp
            except requests.RequestException as exc:
                last_err = exc
                delay = config.RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                logger.warning("Request error: %s; retry in %.1fs", exc, delay)
                time.sleep(delay)
        raise RuntimeError(f"Request failed after {config.MAX_RETRIES} retries: {last_err}")

    def get_modified_workspaces(
        self,
        modified_since: Optional[datetime] = None,
        exclude_personal: bool = True,
        exclude_inactive: bool = True,
    ) -> List[Dict[str, str]]:
        """Return list of {id, ...} workspaces modified since timestamp (or all)."""
        url = f"{self.base}/workspaces/modified"
        params: Dict[str, Any] = {
            "excludePersonalWorkspaces": str(exclude_personal).lower(),
            "excludeInActiveWorkspaces": str(exclude_inactive).lower(),
        }
        if modified_since is not None:
            if modified_since.tzinfo is None:
                modified_since = modified_since.replace(tzinfo=timezone.utc)
            params["modifiedSince"] = modified_since.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            )

        logger.info("Fetching modified workspaces (modifiedSince=%s)...", params.get("modifiedSince", "ALL"))
        resp = self._request("GET", url, params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"get_modified_workspaces failed: {resp.status_code} {resp.text[:500]}")
        workspaces = resp.json() or []
        logger.info("Found %s workspaces to scan", len(workspaces))
        return workspaces

    def start_scan(self, workspace_ids: List[str]) -> str:
        """POST getInfo; returns scanId."""
        url = f"{self.base}/workspaces/getInfo"
        params = {
            "datasetSchema": str(config.SCAN_DATASET_SCHEMA).lower(),
            "datasetExpressions": str(config.SCAN_DATASET_EXPRESSIONS).lower(),
            "lineage": str(config.SCAN_LINEAGE).lower(),
            "datasourceDetails": str(config.SCAN_DATASOURCE_DETAILS).lower(),
            "getArtifactUsers": str(config.SCAN_GET_ARTIFACT_USERS).lower(),
        }
        body = {"workspaces": workspace_ids}
        logger.info("Starting scan for %s workspaces...", len(workspace_ids))
        resp = self._request("POST", url, json_body=body, params=params)
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"start_scan failed: {resp.status_code} {resp.text[:500]}")
        data = resp.json()
        scan_id = data.get("id") or data.get("scanId")
        if not scan_id:
            raise RuntimeError(f"No scan id in response: {data}")
        logger.info("Scan started: %s (status=%s)", scan_id, data.get("status"))
        return scan_id

    def wait_for_scan(self, scan_id: str) -> str:
        """Poll until Succeeded/Failed. Returns final status."""
        url = f"{self.base}/workspaces/scanStatus/{scan_id}"
        deadline = time.time() + config.SCAN_POLL_MAX_WAIT_SEC
        while time.time() < deadline:
            resp = self._request("GET", url)
            if resp.status_code != 200:
                raise RuntimeError(f"scanStatus failed: {resp.status_code} {resp.text[:500]}")
            status = (resp.json() or {}).get("status", "Unknown")
            logger.info("Scan %s status: %s", scan_id, status)
            if status in ("Succeeded", "Failed", "PartialySucceeded", "PartiallySucceeded"):
                return status
            time.sleep(config.SCAN_POLL_INTERVAL_SEC)
        raise TimeoutError(f"Scan {scan_id} did not finish within {config.SCAN_POLL_MAX_WAIT_SEC}s")

    def get_scan_result(self, scan_id: str) -> Dict[str, Any]:
        url = f"{self.base}/workspaces/scanResult/{scan_id}"
        resp = self._request("GET", url)
        if resp.status_code != 200:
            raise RuntimeError(f"scanResult failed: {resp.status_code} {resp.text[:500]}")
        return resp.json() or {}

    def scan_workspace_ids(self, workspace_ids: List[str]) -> List[Dict[str, Any]]:
        """Scan IDs in batches; return merged workspace objects from all results."""
        if not workspace_ids:
            return []

        all_workspaces: List[Dict[str, Any]] = []
        batch_size = config.WORKSPACE_BATCH_SIZE

        for i in range(0, len(workspace_ids), batch_size):
            batch = workspace_ids[i : i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (len(workspace_ids) + batch_size - 1) // batch_size
            logger.info("=== Batch %s/%s (%s workspaces) ===", batch_num, total_batches, len(batch))

            scan_id = self.start_scan(batch)
            status = self.wait_for_scan(scan_id)
            if status == "Failed":
                logger.error("Scan %s failed; skipping batch", scan_id)
                continue
            if status in ("PartialySucceeded", "PartiallySucceeded"):
                logger.warning("Scan %s partially succeeded; ingesting available data", scan_id)

            result = self.get_scan_result(scan_id)
            wss = result.get("workspaces") or []
            logger.info("Batch %s returned %s workspaces", batch_num, len(wss))
            all_workspaces.extend(wss)

        return all_workspaces
