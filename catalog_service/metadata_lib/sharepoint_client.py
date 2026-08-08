"""
Microsoft Graph client for SharePoint document library uploads.
Uses app-only client credentials (Sites.Selected or Sites.ReadWrite.All).
"""

from __future__ import annotations

import logging
import mimetypes
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import msal
import requests

from catalog_service import catalog_config as config

logger = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
# Simple upload limit ~4MB; use upload session above this
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
CHUNK_SIZE = 5 * 1024 * 1024  # 5MB chunks for large JSON


class GraphAuth:
    def __init__(
        self,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        self.tenant_id = tenant_id or config.SHAREPOINT_TENANT_ID or config.TENANT_ID
        self.client_id = client_id or config.SHAREPOINT_CLIENT_ID or config.CLIENT_ID
        self.client_secret = client_secret or config.SHAREPOINT_CLIENT_SECRET or config.CLIENT_SECRET
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._app: Optional[msal.ConfidentialClientApplication] = None

    def _app_client(self) -> msal.ConfidentialClientApplication:
        if self._app is None:
            authority = f"https://login.microsoftonline.com/{self.tenant_id}"
            self._app = msal.ConfidentialClientApplication(
                self.client_id,
                authority=authority,
                client_credential=self.client_secret,
            )
        return self._app

    def get_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._token and time.time() < self._expires_at - 300:
            return self._token
        if not all([self.tenant_id, self.client_id, self.client_secret]):
            raise RuntimeError(
                "SharePoint Graph credentials missing. Set SHAREPOINT_TENANT_ID, "
                "SHAREPOINT_CLIENT_ID, SHAREPOINT_CLIENT_SECRET (or TENANT_ID/CLIENT_ID/CLIENT_SECRET)."
            )
        result = self._app_client().acquire_token_for_client(scopes=GRAPH_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(
                f"Graph auth failed: {result.get('error')} — {result.get('error_description')}"
            )
        self._token = result["access_token"]
        self._expires_at = time.time() + int(result.get("expires_in", 3600))
        return self._token

    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}


class SharePointClient:
    """Upload files into a SharePoint document library folder via Graph."""

    def __init__(self, auth: Optional[GraphAuth] = None):
        self.auth = auth or GraphAuth()
        self.site_hostname = config.SHAREPOINT_SITE_HOSTNAME
        self.site_path = config.SHAREPOINT_SITE_PATH  # e.g. /sites/BARetailAnalytics
        self.folder_path = (config.SHAREPOINT_FOLDER_PATH or "").strip("/")
        self.drive_name = config.SHAREPOINT_DRIVE_NAME or "Documents"
        self._site_id: Optional[str] = None
        self._drive_id: Optional[str] = None

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict] = None,
        json_body: Any = None,
        data: Any = None,
        params: Optional[dict] = None,
        timeout: int = 300,
    ) -> requests.Response:
        last_err = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                hdrs = {**self.auth.headers(), **(headers or {})}
                resp = requests.request(
                    method,
                    url,
                    headers=hdrs,
                    json=json_body,
                    data=data,
                    params=params,
                    timeout=timeout,
                )
                if resp.status_code == 401 and attempt == 1:
                    self.auth.get_token(force_refresh=True)
                    continue
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", config.RETRY_BASE_DELAY_SEC * attempt))
                    logger.warning("Graph 429; sleeping %.1fs", wait)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    time.sleep(config.RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)))
                    continue
                return resp
            except requests.RequestException as exc:
                last_err = exc
                time.sleep(config.RETRY_BASE_DELAY_SEC * attempt)
        raise RuntimeError(f"Graph request failed: {last_err}")

    def resolve_site_and_drive(self) -> None:
        if not self.site_hostname or not self.site_path:
            raise RuntimeError(
                "Set SHAREPOINT_SITE_HOSTNAME and SHAREPOINT_SITE_PATH "
                "(example: hostname=ashleyfurniture.sharepoint.com path=/sites/BARetailAnalytics)"
            )
        # GET /sites/{hostname}:{server-relative-path}
        url = f"{GRAPH}/sites/{self.site_hostname}:{self.site_path}"
        logger.info("Resolving SharePoint site %s:%s ...", self.site_hostname, self.site_path)
        resp = self._request("GET", url)
        if resp.status_code != 200:
            raise RuntimeError(f"Resolve site failed {resp.status_code}: {resp.text[:500]}")
        self._site_id = resp.json()["id"]
        logger.info("Site id resolved")

        # Prefer named document library
        drives_url = f"{GRAPH}/sites/{self._site_id}/drives"
        resp = self._request("GET", drives_url)
        if resp.status_code != 200:
            raise RuntimeError(f"List drives failed {resp.status_code}: {resp.text[:500]}")
        drives = resp.json().get("value") or []
        drive = None
        for d in drives:
            if (d.get("name") or "").lower() == (self.drive_name or "Documents").lower():
                drive = d
                break
        if drive is None and drives:
            drive = drives[0]
            logger.warning("Drive %r not found; using %r", self.drive_name, drive.get("name"))
        if not drive:
            raise RuntimeError("No document libraries found on site")
        self._drive_id = drive["id"]
        logger.info("Using drive: %s", drive.get("name"))

    def ensure_folder(self, folder_path: str) -> None:
        """Create folder path under drive root if missing (best-effort)."""
        folder_path = folder_path.strip("/")
        if not folder_path:
            return
        parts = folder_path.split("/")
        current = ""
        for part in parts:
            parent = current
            current = f"{current}/{part}" if current else part
            # try get
            enc = quote(current)
            url = f"{GRAPH}/drives/{self._drive_id}/root:/{enc}"
            resp = self._request("GET", url)
            if resp.status_code == 200:
                continue
            # create under parent
            if parent:
                create_url = f"{GRAPH}/drives/{self._drive_id}/root:/{quote(parent)}:/children"
            else:
                create_url = f"{GRAPH}/drives/{self._drive_id}/root/children"
            body = {
                "name": part,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail",
            }
            resp = self._request("POST", create_url, json_body=body)
            if resp.status_code in (200, 201):
                logger.info("Created folder %s", current)
            elif resp.status_code == 409:
                pass
            else:
                # conflictBehavior fail may 409; try rename ignore
                logger.warning("ensure_folder %s: %s %s", current, resp.status_code, resp.text[:200])

    def list_children(self, folder_path: str = "") -> List[Dict[str, Any]]:
        """
        List drive items directly under folder_path (relative to drive root).
        Returns list of Graph driveItem dicts (files + folders).
        """
        if self._drive_id is None:
            self.resolve_site_and_drive()
        folder_path = (folder_path or "").replace("\\", "/").strip("/")
        items: List[Dict[str, Any]] = []
        if folder_path:
            url = f"{GRAPH}/drives/{self._drive_id}/root:/{quote(folder_path)}:/children"
        else:
            url = f"{GRAPH}/drives/{self._drive_id}/root/children"
        # Graph pages @odata.nextLink
        while url:
            resp = self._request("GET", url, params={"$top": "200"})
            if resp.status_code != 200:
                raise RuntimeError(
                    f"list_children failed {resp.status_code} path={folder_path!r}: {resp.text[:400]}"
                )
            body = resp.json() or {}
            items.extend(body.get("value") or [])
            url = body.get("@odata.nextLink")
        return items

    def list_folders(self, folder_path: str = "") -> List[Dict[str, Any]]:
        """Return only child folders under folder_path."""
        return [i for i in self.list_children(folder_path) if i.get("folder") is not None]

    def list_files_recursive(
        self,
        folder_path: str = "",
        *,
        max_depth: int = 8,
        _depth: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Recursively list files under folder_path.
        Each item includes relativePath (path from folder_path root, posix).
        """
        if _depth > max_depth:
            return []
        folder_path = (folder_path or "").replace("\\", "/").strip("/")
        out: List[Dict[str, Any]] = []
        try:
            children = self.list_children(folder_path)
        except Exception as exc:
            logger.warning("list_files_recursive failed at %s: %s", folder_path, exc)
            return out
        for it in children:
            name = it.get("name") or ""
            if not name:
                continue
            rel = f"{folder_path}/{name}" if folder_path else name
            if it.get("folder") is not None:
                out.extend(
                    self.list_files_recursive(rel, max_depth=max_depth, _depth=_depth + 1)
                )
            elif it.get("file") is not None:
                row = dict(it)
                row["relativePath"] = rel
                out.append(row)
        return out

    def latest_child_folder(self, folder_path: str) -> Optional[Dict[str, Any]]:
        """
        Pick the newest child folder under folder_path by createdDateTime
        (fallback lastModifiedDateTime, then name).
        """
        folders = self.list_folders(folder_path)
        if not folders:
            return None

        def _key(item: Dict[str, Any]):
            return (
                item.get("createdDateTime")
                or item.get("lastModifiedDateTime")
                or item.get("name")
                or ""
            )

        return max(folders, key=_key)

    def upload_file(self, local_path: Path, remote_relative: str) -> Dict[str, Any]:
        """
        Upload local file to drive path remote_relative (e.g. 'PowerBI Reports MetaData/latest/impact_index.json').
        Returns Graph driveItem JSON.
        """
        if self._drive_id is None:
            self.resolve_site_and_drive()

        local_path = Path(local_path)
        if not local_path.is_file():
            raise FileNotFoundError(local_path)

        remote_relative = remote_relative.replace("\\", "/").strip("/")
        parent = "/".join(remote_relative.split("/")[:-1])
        if parent:
            self.ensure_folder(parent)

        size = local_path.stat().st_size
        logger.info("Uploading %s (%.1f MB) -> %s", local_path.name, size / 1024 / 1024, remote_relative)

        if size <= SIMPLE_UPLOAD_MAX:
            return self._upload_simple(local_path, remote_relative)
        return self._upload_session(local_path, remote_relative, size)

    def _upload_simple(self, local_path: Path, remote_relative: str) -> Dict[str, Any]:
        url = f"{GRAPH}/drives/{self._drive_id}/root:/{quote(remote_relative)}:/content"
        data = local_path.read_bytes()
        content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        resp = self._request(
            "PUT",
            url,
            headers={"Content-Type": content_type},
            data=data,
            timeout=600,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Simple upload failed {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def _upload_session(self, local_path: Path, remote_relative: str, size: int) -> Dict[str, Any]:
        session_url = f"{GRAPH}/drives/{self._drive_id}/root:/{quote(remote_relative)}:/createUploadSession"
        body = {
            "item": {
                "@microsoft.graph.conflictBehavior": "replace",
                "name": Path(remote_relative).name,
            }
        }
        resp = self._request("POST", session_url, json_body=body)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"createUploadSession failed {resp.status_code}: {resp.text[:500]}")
        upload_url = resp.json()["uploadUrl"]

        result = None
        with open(local_path, "rb") as f:
            start = 0
            while start < size:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                end = start + len(chunk) - 1
                headers = {
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                }
                # upload URL is pre-authenticated — do not send bearer
                for attempt in range(1, config.MAX_RETRIES + 1):
                    r = requests.put(upload_url, headers=headers, data=chunk, timeout=600)
                    if r.status_code in (200, 201, 202):
                        if r.status_code in (200, 201) and r.content:
                            try:
                                result = r.json()
                            except Exception:
                                result = {"status": "ok"}
                        break
                    if r.status_code == 429:
                        time.sleep(float(r.headers.get("Retry-After", 5)))
                        continue
                    if attempt == config.MAX_RETRIES:
                        raise RuntimeError(f"Chunk upload failed {r.status_code}: {r.text[:400]}")
                    time.sleep(config.RETRY_BASE_DELAY_SEC * attempt)
                start = end + 1
                logger.info("  uploaded %s / %s bytes", min(start, size), size)

        return result or {"name": local_path.name, "size": size}

    def list_folder(self, remote_folder: Optional[str] = None) -> List[Dict[str, Any]]:
        """List files (not subfolders) directly under remote_folder."""
        if self._drive_id is None:
            self.resolve_site_and_drive()
        base = (remote_folder if remote_folder is not None else self.folder_path).strip("/")
        if base:
            url = f"{GRAPH}/drives/{self._drive_id}/root:/{quote(base)}:/children"
        else:
            url = f"{GRAPH}/drives/{self._drive_id}/root/children"
        items: List[Dict[str, Any]] = []
        while url:
            resp = self._request("GET", url, params={"$select": "id,name,size,file,folder,lastModifiedDateTime,webUrl"})
            if resp.status_code == 404:
                return []
            if resp.status_code != 200:
                raise RuntimeError(f"list_folder failed {resp.status_code}: {resp.text[:400]}")
            payload = resp.json()
            for it in payload.get("value") or []:
                if it.get("file"):
                    items.append(it)
            url = payload.get("@odata.nextLink")
        return items

    def get_item_meta(self, remote_relative: str) -> Dict[str, Any]:
        """Return Graph driveItem metadata for a path (includes size + downloadUrl)."""
        if self._drive_id is None:
            self.resolve_site_and_drive()
        remote_relative = remote_relative.replace("\\", "/").strip("/")
        # Do not $select — Graph only returns @microsoft.graph.downloadUrl on full item GETs.
        url = f"{GRAPH}/drives/{self._drive_id}/root:/{quote(remote_relative)}"
        resp = self._request("GET", url)
        if resp.status_code != 200:
            raise RuntimeError(f"get_item_meta failed {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def download_file(
        self,
        remote_relative: str,
        *,
        max_attempts: int = 3,
        chunk_size: int = 8 * 1024 * 1024,
        timeout: int = 1800,
    ) -> bytes:
        """
        Download a drive file with size verification.

        Large SharePoint files (~300MB+) often truncate on a single stream
        through corporate proxies (no Content-Length, connection drops at
        ~130–290MB). Strategy:
          1) Prefer Graph /content with Bearer + HTTP Range (8MB slices)
          2) Fall back to pre-auth downloadUrl + Range if Graph ranges fail
          3) Verify assembled byte length against driveItem.size
        """
        if self._drive_id is None:
            self.resolve_site_and_drive()
        remote_relative = remote_relative.replace("\\", "/").strip("/")

        last_err: Optional[Exception] = None
        # Try Graph bearer ranges first, then pre-auth CDN ranges.
        modes = ("graph", "preauth")
        for attempt in range(1, max_attempts + 1):
            for mode in modes:
                try:
                    return self._download_file_ranged(
                        remote_relative,
                        mode=mode,
                        chunk_size=chunk_size,
                        timeout=timeout,
                    )
                except Exception as exc:
                    last_err = exc
                    logger.warning(
                        "Download mode=%s attempt=%s/%s failed for %s: %s",
                        mode, attempt, max_attempts, remote_relative, exc,
                    )
                    time.sleep(min(4, attempt * 1.5))
                    try:
                        self.auth.get_token(force_refresh=True)
                    except Exception:
                        pass

        raise RuntimeError(f"download_file failed for {remote_relative}: {last_err}")

    def _download_file_ranged(
        self,
        remote_relative: str,
        *,
        mode: str,
        chunk_size: int,
        timeout: int,
    ) -> bytes:
        meta = self.get_item_meta(remote_relative)
        expected = int(meta.get("size") or 0)
        if expected <= 0:
            raise IOError(f"driveItem size missing/zero for {remote_relative}")

        graph_url = f"{GRAPH}/drives/{self._drive_id}/root:/{quote(remote_relative)}:/content"
        download_url = meta.get("@microsoft.graph.downloadUrl")
        if mode == "preauth":
            if not download_url:
                raise IOError("No @microsoft.graph.downloadUrl on driveItem")
            content_url = download_url
            use_preauth = True
        else:
            content_url = graph_url
            use_preauth = False

        # Small files: single GET
        if expected <= chunk_size:
            data = self._download_range(
                content_url,
                start=0,
                end=expected - 1,
                use_preauth=use_preauth,
                timeout=min(timeout, 300),
            )
            if len(data) != expected:
                raise IOError(
                    f"Short download for {remote_relative}: got {len(data)}, expected {expected}"
                )
            logger.info(
                "Downloaded %s (%.1f MB, mode=%s, single-shot)",
                remote_relative, expected / (1024 * 1024), mode,
            )
            return data

        buf = bytearray(expected)
        offset = 0
        range_idx = 0
        t0 = time.time()
        while offset < expected:
            end = min(offset + chunk_size, expected) - 1
            range_idx += 1
            need = end - offset + 1
            piece = None
            range_err: Optional[Exception] = None
            for r_try in range(1, 4):
                try:
                    # Refresh token / pre-auth URL on retries and periodically.
                    if r_try > 1 or (mode == "preauth" and range_idx > 1 and range_idx % 6 == 1):
                        if mode == "preauth":
                            meta = self.get_item_meta(remote_relative)
                            new_size = int(meta.get("size") or 0)
                            if new_size and new_size != expected:
                                raise IOError(
                                    f"Remote size changed during download ({expected} -> {new_size})"
                                )
                            download_url = meta.get("@microsoft.graph.downloadUrl")
                            if not download_url:
                                raise IOError("downloadUrl missing on refresh")
                            content_url = download_url
                        else:
                            self.auth.get_token(force_refresh=(r_try > 1))

                    piece = self._download_range(
                        content_url,
                        start=offset,
                        end=end,
                        use_preauth=use_preauth,
                        timeout=120,
                    )
                    if len(piece) == expected and offset == 0:
                        # Server ignored Range and returned the full object.
                        logger.info(
                            "Downloaded %s (%.1f MB, mode=%s, full-body)",
                            remote_relative, expected / (1024 * 1024), mode,
                        )
                        return piece
                    if len(piece) != need:
                        raise IOError(
                            f"Range {offset}-{end}: got {len(piece)} bytes, want {need}"
                        )
                    break
                except Exception as exc:
                    range_err = exc
                    logger.warning(
                        "Range %s-%s mode=%s try %s/3 failed for %s: %s",
                        offset, end, mode, r_try, remote_relative, exc,
                    )
                    time.sleep(min(5, r_try * 1.5))
                    piece = None
            if piece is None:
                raise IOError(
                    f"Failed range {offset}-{end} mode={mode}: {range_err}"
                )

            buf[offset: offset + need] = piece
            offset += need
            if range_idx == 1 or range_idx % 4 == 0 or offset >= expected:
                elapsed = max(0.1, time.time() - t0)
                mb_done = offset / (1024 * 1024)
                mb_total = expected / (1024 * 1024)
                rate = mb_done / elapsed
                logger.info(
                    "Download progress %s: %.1f / %.1f MB (%.1f MB/s, range #%s, mode=%s)",
                    remote_relative, mb_done, mb_total, rate, range_idx, mode,
                )

        data = bytes(buf)
        if len(data) != expected:
            raise IOError(
                f"Assembled size mismatch for {remote_relative}: "
                f"got {len(data)}, expected {expected}"
            )
        logger.info(
            "Downloaded %s (%.1f MB verified, %s ranges, mode=%s, %.1fs)",
            remote_relative,
            expected / (1024 * 1024),
            range_idx,
            mode,
            time.time() - t0,
        )
        return data

    def _download_range(
        self,
        content_url: str,
        *,
        start: int,
        end: int,
        use_preauth: bool,
        timeout: int,
    ) -> bytes:
        """GET bytes start-end inclusive. Pre-auth URLs must not send Authorization."""
        headers: Dict[str, str] = {
            "Range": f"bytes={start}-{end}",
            "Accept-Encoding": "identity",  # avoid compressed/truncated streams
        }
        if not use_preauth:
            headers.update(self.auth.headers())

        # Connect timeout short; read timeout per-chunk so stalls fail fast.
        connect_to = 30
        read_to = max(30, int(timeout))
        resp = requests.get(
            content_url,
            headers=headers,
            stream=True,
            timeout=(connect_to, read_to),
        )
        try:
            if resp.status_code not in (200, 206):
                raise RuntimeError(
                    f"range GET HTTP {resp.status_code}: {resp.text[:240]}"
                )
            need = end - start + 1
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    buf.extend(chunk)
                    # If server ignored Range and is streaming the full file,
                    # stop once we have more than this slice (caller handles).
                    if resp.status_code == 200 and len(buf) > need * 2:
                        # Keep reading? No — for full-body only on first slice.
                        # Caller checks full size when offset==0.
                        pass
            return bytes(buf)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def delete_path(self, remote_relative: str) -> bool:
        """Delete a file or folder by drive-relative path. Returns True if deleted/missing."""
        if self._drive_id is None:
            self.resolve_site_and_drive()
        remote_relative = remote_relative.replace("\\", "/").strip("/")
        if not remote_relative:
            raise ValueError("Refusing to delete drive root")
        url = f"{GRAPH}/drives/{self._drive_id}/root:/{quote(remote_relative)}"
        resp = self._request("DELETE", url)
        if resp.status_code in (200, 204, 404):
            return True
        logger.warning("delete_path %s: %s %s", remote_relative, resp.status_code, resp.text[:200])
        return False

    def clear_folder(
        self,
        remote_folder: Optional[str] = None,
        *,
        names: Optional[List[str]] = None,
        extensions: Optional[List[str]] = None,
    ) -> List[str]:
        """
        Delete files in remote_folder.
        - If names provided: delete only those filenames when present.
        - Else delete all files (optionally filter by extensions, e.g. ['.json']).
        Does not delete subfolders.
        """
        if self._drive_id is None:
            self.resolve_site_and_drive()
        base = (remote_folder if remote_folder is not None else self.folder_path).strip("/")
        deleted: List[str] = []
        items = self.list_folder(base)
        want = {n.lower() for n in (names or [])} if names else None
        exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in (extensions or [])} or None

        for it in items:
            name = it.get("name") or ""
            if want is not None and name.lower() not in want:
                continue
            if exts is not None and not any(name.lower().endswith(e) for e in exts):
                continue
            remote = f"{base}/{name}" if base else name
            if self.delete_path(remote):
                deleted.append(remote)
                logger.info("Deleted SharePoint file %s", remote)
        return deleted

    def replace_directory(
        self,
        local_dir: Path,
        remote_folder: Optional[str] = None,
        names: Optional[List[str]] = None,
        *,
        clean_first: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Optionally clear remote folder JSON (or named set), then upload.
        Use clean_first=True on fresh publishes so stale UI files cannot linger.
        """
        base = (remote_folder if remote_folder is not None else self.folder_path).strip("/")
        if clean_first:
            if names:
                self.clear_folder(base, names=names)
            else:
                self.clear_folder(base, extensions=[".json"])
        return self.upload_directory(local_dir, remote_folder=base, names=names)

    def upload_directory(
        self,
        local_dir: Path,
        remote_folder: Optional[str] = None,
        names: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Upload selected (or all) files from local_dir into SharePoint folder.
        remote_folder defaults to SHAREPOINT_FOLDER_PATH.
        """
        if self._drive_id is None:
            self.resolve_site_and_drive()

        base = (remote_folder if remote_folder is not None else self.folder_path).strip("/")
        local_dir = Path(local_dir)
        files = []
        if names:
            for n in names:
                p = local_dir / n
                if p.is_file():
                    files.append(p)
                else:
                    logger.warning("Skip missing file: %s", p)
        else:
            files = sorted([p for p in local_dir.iterdir() if p.is_file()])

        results = []
        for p in files:
            remote = f"{base}/{p.name}" if base else p.name
            item = self.upload_file(p, remote)
            results.append({"local": str(p), "remote": remote, "webUrl": item.get("webUrl"), "id": item.get("id")})
            logger.info("OK %s -> %s", p.name, item.get("webUrl") or remote)
        return results
