"""
Power BI Visual Metadata Extractor using JavaScript Embed API
Uses headless browser automation to extract visual-level metadata

This module provides a way to extract report pages and visuals that are
NOT available via the Scanner API or standard REST API.
"""

import asyncio
import json
import os
import re
from typing import Dict, List, Optional
from datetime import datetime
from playwright.async_api import async_playwright, Page, Browser, TimeoutError as PlaywrightTimeoutError
import requests


class VisualMetadataExtractor:
    """Extract visual metadata from Power BI reports using JavaScript Embed API"""

    def __init__(self, client_id: str = None, client_secret: str = None, tenant_id: str = None, user_token: str = None):
        """
        Initialize the extractor with either service principal credentials OR user token

        Args:
            client_id: Azure AD app client ID (for service principal auth)
            client_secret: Azure AD app client secret (for service principal auth)
            tenant_id: Azure AD tenant ID (for service principal auth)
            user_token: User's SSO token (for delegated auth) - PREFERRED for SSO scenarios
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self.user_token = user_token
        self.access_token = None

    def get_access_token(self) -> str:
        """Get Power BI access token using service principal"""
        if self.user_token:
            # If user token is provided, use it directly
            return self.user_token

        # Otherwise, use service principal
        url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://analysis.windows.net/powerbi/api/.default"
        }
        response = requests.post(url, data=data)
        response.raise_for_status()
        return response.json()["access_token"]

    def get_report_definition(self, workspace_id: str, report_id: str) -> Dict:
        """
        Get report definition JSON which contains field bindings for all visuals
        This is MUCH more reliable than trying to extract via JavaScript API

        Uses the Export API to download the .pbix file and extract the report definition

        Returns: Dictionary containing the Layout JSON with all visual definitions
        """
        if not self.access_token:
            self.access_token = self.get_access_token()

        # Export report definition (.pbix file)
        url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}/Export"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/zip"
        }

        print(f"   📥 Downloading report .pbix file...")

        # Increase timeout for larger files
        try:
            response = requests.get(url, headers=headers, timeout=60, stream=True)
        except requests.exceptions.Timeout:
            print(f"   ⚠️ Download timed out after 60 seconds")
            return None
        except Exception as e:
            print(f"   ⚠️ Download error: {e}")
            return None

        if response.status_code != 200:
            print(f"   ⚠️ Could not download report: {response.status_code} {response.reason}")
            # Try to get error details
            try:
                error_detail = response.json()
                error_code = error_detail.get('error', {}).get('code', '')

                # Check for known limitations
                if 'PremiumFilesErrors' in error_code:
                    print(f"   ℹ️ Report uses Premium/Fabric storage (Export API not supported)")
                else:
                    print(f"   ⚠️ Error details: {error_detail}")
            except:
                pass
            return None

        # The response is a .pbix file (ZIP format) — classic Layout OR enhanced PBIR
        import zipfile
        import io

        try:
            pbix_data = io.BytesIO(response.content)
            with zipfile.ZipFile(pbix_data, 'r') as zip_ref:
                file_list = zip_ref.namelist()
                print(f"   📦 PBIX contains {len(file_list)} files")

                # --- Classic report Layout (UTF-16 LE or UTF-8) ---
                layout_file = None
                for filename in file_list:
                    fl = filename.lower().replace('\\', '/')
                    if fl.endswith('/layout') or fl == 'layout' or fl.endswith('report/layout'):
                        layout_file = filename
                        break

                if layout_file:
                    print(f"   📄 Found Layout file: {layout_file}")
                    layout_bytes = zip_ref.read(layout_file)
                    layout_text = None
                    for enc in ('utf-16-le', 'utf-8-sig', 'utf-8'):
                        try:
                            layout_text = layout_bytes.decode(enc)
                            print(f"   ✅ Decoded Layout as {enc} ({len(layout_text)} chars)")
                            break
                        except UnicodeDecodeError:
                            continue
                    if layout_text is None:
                        print(f"   ⚠️ Could not decode Layout file")
                    else:
                        layout_json = json.loads(layout_text)
                        if 'sections' in layout_json:
                            print(f"   ✅ Extracted report definition with {len(layout_json['sections'])} page(s)")
                            return layout_json
                        layout_keys = list(layout_json.keys())
                        print(f"   ⚠️ Layout JSON missing 'sections'. Keys: {layout_keys}")
                        if 'diagrams' in layout_keys:
                            print(f"   ℹ️  Model Diagram (relationship view), not a visual report")
                            return {"reportType": "ModelDiagram"}

                # --- Enhanced report PBIR (definition.pbir + page/visual JSON) ---
                pbir_layout = self._extract_pbir_layout_from_zip(zip_ref, file_list)
                if pbir_layout and pbir_layout.get('sections'):
                    print(
                        f"   ✅ Extracted PBIR definition with "
                        f"{len(pbir_layout['sections'])} page(s)"
                    )
                    return pbir_layout

                print(f"   ⚠️ No classic Layout or PBIR visuals found. Sample files: {file_list[:12]}")
                return None

        except zipfile.BadZipFile:
            print(f"   ⚠️ Invalid .pbix file (not a valid ZIP)")
            return None
        except json.JSONDecodeError as e:
            print(f"   ⚠️ Could not parse Layout JSON: {e}")
            return None
        except Exception as e:
            print(f"   ⚠️ Could not parse report definition: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _extract_pbir_layout_from_zip(self, zip_ref, file_list) -> Optional[Dict]:
        """
        Build a classic-like {sections:[{displayName, visualContainers:[{name,config}]}]}
        structure from enhanced PBIR package files inside an exported .pbix/.pbir zip.
        """
        def _read_json(path):
            raw = zip_ref.read(path)
            for enc in ('utf-8-sig', 'utf-8', 'utf-16-le'):
                try:
                    return json.loads(raw.decode(enc))
                except Exception:
                    continue
            return None

        # Collect page folders: .../pages/<pageId>/page.json
        page_dirs = {}
        for name in file_list:
            n = name.replace('\\', '/')
            m = re.search(r'(?i)(?:^|/)(pages/([^/]+))/page\.json$', n)
            if m:
                page_dirs[m.group(2)] = {
                    'page_json': name,
                    'prefix': n[: n.lower().rfind('page.json')],
                }

        if not page_dirs:
            # Also try Report/definition/pages style
            for name in file_list:
                n = name.replace('\\', '/')
                if n.lower().endswith('/page.json') and '/pages/' in n.lower():
                    parts = n.split('/')
                    try:
                        idx = next(i for i, p in enumerate(parts) if p.lower() == 'pages')
                        page_id = parts[idx + 1]
                        page_dirs[page_id] = {
                            'page_json': name,
                            'prefix': '/'.join(parts[: idx + 2]) + '/',
                        }
                    except Exception:
                        continue

        if not page_dirs:
            return None

        print(f"   🧩 PBIR package detected: {len(page_dirs)} page folder(s)")
        sections = []
        for page_id, meta in page_dirs.items():
            page_json = _read_json(meta['page_json']) or {}
            display = (
                page_json.get('displayName')
                or page_json.get('name')
                or page_id
            )
            prefix = meta['prefix'].replace('\\', '/')
            # visuals live under pages/<id>/visuals/<vid>/visual.json
            visual_containers = []
            for name in file_list:
                n = name.replace('\\', '/')
                if not n.lower().startswith(prefix.lower()):
                    continue
                if not n.lower().endswith('/visual.json') and not n.lower().endswith('visual.json'):
                    # some packages use visual.json only
                    if '/visuals/' not in n.lower() or not n.lower().endswith('.json'):
                        continue
                    if not n.lower().endswith('visual.json'):
                        continue
                vjson = _read_json(name)
                if not vjson:
                    continue
                # Derive visual id from path
                vid = n.split('/')[-2] if '/visuals/' in n.lower() else n.split('/')[-1]
                # Normalize to singleVisual shape expected by extract_fields_from_definition
                single = (
                    vjson.get('visual')
                    or vjson.get('singleVisual')
                    or vjson
                )
                if 'visualType' not in single and isinstance(vjson.get('visualType'), str):
                    single = dict(single)
                    single['visualType'] = vjson.get('visualType')
                config = {
                    'name': vid,
                    'singleVisual': single if isinstance(single, dict) else {},
                }
                # Title may live under visualContainerObjects / vcObjects
                title = None
                for key in ('visualContainerObjects', 'vcObjects', 'objects'):
                    blob = single.get(key) if isinstance(single, dict) else None
                    if not isinstance(blob, dict):
                        continue
                    title_arr = blob.get('title')
                    if isinstance(title_arr, list) and title_arr:
                        props = title_arr[0].get('properties') or {}
                        text = props.get('text') or {}
                        if isinstance(text, dict):
                            expr = text.get('expr') or text
                            lit = expr.get('Literal') if isinstance(expr, dict) else None
                            if isinstance(lit, dict) and lit.get('Value'):
                                title = str(lit.get('Value')).strip("'\"")
                if title and isinstance(config['singleVisual'], dict):
                    vc = config['singleVisual'].setdefault('vcObjects', {})
                    if 'title' not in vc:
                        vc['title'] = [{'properties': {'text': {'expr': {'Literal': {'Value': f"'{title}'"}}}}}]

                visual_containers.append({
                    'name': vid,
                    'config': json.dumps(config),
                })

            sections.append({
                'name': page_id,
                'displayName': display,
                'visualContainers': visual_containers,
            })
            print(f"      • Page '{display}': {len(visual_containers)} visual(s)")

        if not sections:
            return None
        return {'sections': sections, 'reportType': 'PBIR'}

    def _walk_field_refs(self, node, depth: int = 0) -> List[Dict]:
        """
        Recursively find Column/Measure/queryRef field bindings in nested PBIR JSON.
        """
        found = []
        if depth > 14 or node is None:
            return found

        if isinstance(node, dict):
            # Standard tabular expression shapes
            for kind in ('Column', 'Measure', 'HierarchyLevel'):
                blob = node.get(kind)
                if isinstance(blob, dict) and blob.get('Property'):
                    expr = blob.get('Expression') or {}
                    src = expr.get('SourceRef') if isinstance(expr, dict) else {}
                    table = ''
                    if isinstance(src, dict):
                        table = src.get('Entity') or src.get('Source') or ''
                    found.append({
                        'name': blob.get('Property'),
                        'displayName': blob.get('Property'),
                        'table': table or '',
                        'type': kind if kind != 'HierarchyLevel' else 'Column',
                    })

            # queryRef style: "Table.Field" or "Sum(Table.Field)"
            qref = node.get('queryRef') or node.get('nativeQueryRef') or node.get('Name')
            if isinstance(qref, str) and '.' in qref:
                # Strip aggregation wrappers like Sum(Table.Col)
                inner = qref
                m = re.match(r'^[A-Za-z]+\((.+)\)$', qref.strip())
                if m:
                    inner = m.group(1)
                if '.' in inner and not inner.strip().startswith('http'):
                    parts = inner.split('.')
                    if len(parts) >= 2:
                        field_name = parts[-1].strip("[]'\" ")
                        table_name = '.'.join(parts[:-1]).strip("[]'\" ")
                        if field_name and len(field_name) < 120:
                            found.append({
                                'name': field_name,
                                'displayName': field_name,
                                'table': table_name,
                                'type': 'Unknown',
                            })

            for v in node.values():
                found.extend(self._walk_field_refs(v, depth + 1))
        elif isinstance(node, list):
            for item in node:
                found.extend(self._walk_field_refs(item, depth + 1))

        # de-dupe while preserving order
        seen = set()
        uniq = []
        for f in found:
            key = (str(f.get('table') or '').lower(), str(f.get('name') or '').lower())
            if key in seen or not f.get('name'):
                continue
            seen.add(key)
            uniq.append(f)
        return uniq

    def extract_fields_from_definition(self, report_definition: Dict) -> Dict:
        """
        Extract field bindings from report definition JSON (Layout file from .pbix)

        This extracts field bindings from multiple locations:
        1. query.queryState.{Role}.projections (PRIMARY - new format)
        2. projections (legacy format)
        3. prototypeQuery (backup method)
        4. deep walk for nested PBIR shapes
        5. filters (field references in filters)

        Returns: Dictionary mapping visual IDs to their metadata including field lists
        """
        visual_fields = {}
        total_visuals = 0
        visuals_with_fields = 0

        try:
            # Navigate through the report definition structure
            sections = report_definition.get('sections', [])

            print(f"   🔍 Parsing {len(sections)} page(s) from Layout JSON...")

            for section_idx, section in enumerate(sections):
                section_name = section.get('displayName', section.get('name', f'Page {section_idx + 1}'))
                visual_containers = section.get('visualContainers', [])

                for container in visual_containers:
                    total_visuals += 1

                    # Get the visual config (can be a string or dict)
                    config = container.get('config', '{}')

                    # Parse the config JSON if it's a string
                    if isinstance(config, str):
                        try:
                            config_json = json.loads(config)
                        except json.JSONDecodeError:
                            continue
                    else:
                        config_json = config

                    # Get visual metadata
                    visual_id = container.get('name', f'visual_{total_visuals}')
                    single_visual = config_json.get('singleVisual', {})
                    visual_type = single_visual.get('visualType', 'unknown')

                    # Get visual title from vcObjects
                    visual_title = None
                    vc_objects = single_visual.get('vcObjects', {})
                    if vc_objects:
                        title_obj = vc_objects.get('title', [{}])[0] if isinstance(vc_objects.get('title'), list) else {}
                        title_props = title_obj.get('properties', {})
                        title_text = title_props.get('text', {})
                        if isinstance(title_text, dict):
                            visual_title = title_text.get('expr', {}).get('Literal', {}).get('Value', None)
                            if visual_title:
                                visual_title = visual_title.strip("'\"")

                    # Initialize field list
                    fields = []
                    field_names_seen = set()  # Prevent duplicates

                    # ============================================================
                    # METHOD 1: Extract from query.queryState.{Role}.projections
                    # This is the PRIMARY method for new PBIR format
                    # ============================================================
                    query = single_visual.get('query', {})
                    query_state = query.get('queryState', {})

                    if query_state:
                        # Iterate through all data roles (Category, Values, Y, X, Series, etc.)
                        for role_name, role_data in query_state.items():
                            if not isinstance(role_data, dict):
                                continue

                            projections = role_data.get('projections', [])
                            for projection in projections:
                                if not isinstance(projection, dict):
                                    continue

                                field_info = projection.get('field', {})
                                query_ref = projection.get('queryRef', '')
                                native_ref = projection.get('nativeQueryRef', '')

                                # Extract Column fields
                                if 'Column' in field_info:
                                    col = field_info['Column']
                                    expr = col.get('Expression', {})
                                    source_ref = expr.get('SourceRef', {})
                                    table_name = source_ref.get('Entity', '')
                                    column_name = col.get('Property', '')

                                    if column_name and column_name not in field_names_seen:
                                        fields.append({
                                            'name': column_name,
                                            'displayName': native_ref or column_name,
                                            'table': table_name,
                                            'type': 'Column',
                                            'role': role_name
                                        })
                                        field_names_seen.add(column_name)

                                # Extract Measure fields
                                elif 'Measure' in field_info:
                                    measure = field_info['Measure']
                                    expr = measure.get('Expression', {})
                                    source_ref = expr.get('SourceRef', {})
                                    table_name = source_ref.get('Entity', '')
                                    measure_name = measure.get('Property', '')

                                    if measure_name and measure_name not in field_names_seen:
                                        fields.append({
                                            'name': measure_name,
                                            'displayName': native_ref or measure_name,
                                            'table': table_name,
                                            'type': 'Measure',
                                            'role': role_name
                                        })
                                        field_names_seen.add(measure_name)

                                # Extract Aggregation fields
                                elif 'Aggregation' in field_info:
                                    agg = field_info['Aggregation']
                                    expr = agg.get('Expression', {})
                                    if 'Column' in expr:
                                        col = expr['Column']
                                        col_expr = col.get('Expression', {})
                                        source_ref = col_expr.get('SourceRef', {})
                                        table_name = source_ref.get('Entity', '')
                                        column_name = col.get('Property', '')

                                        if column_name and column_name not in field_names_seen:
                                            fields.append({
                                                'name': column_name,
                                                'displayName': native_ref or column_name,
                                                'table': table_name,
                                                'type': 'Aggregation',
                                                'role': role_name
                                            })
                                            field_names_seen.add(column_name)

                    # ============================================================
                    # METHOD 2: Extract from legacy "projections" format
                    # For older PBIR-Legacy format
                    # ============================================================
                    legacy_projections = single_visual.get('projections', {})
                    if legacy_projections and not query_state:  # Only use if queryState is empty
                        for role_name, role_items in legacy_projections.items():
                            if not isinstance(role_items, list):
                                continue

                            for item in role_items:
                                if isinstance(item, dict):
                                    query_ref = item.get('queryRef', '')
                                    native_ref = item.get('nativeQueryRef', '')

                                    # Parse queryRef like "TableName.FieldName"
                                    if query_ref and '.' in query_ref:
                                        parts = query_ref.split('.')
                                        if len(parts) >= 2:
                                            field_name = parts[-1]
                                            table_name = '.'.join(parts[:-1])

                                            if field_name and field_name not in field_names_seen:
                                                fields.append({
                                                    'name': field_name,
                                                    'displayName': native_ref or field_name,
                                                    'table': table_name,
                                                    'type': 'Unknown',
                                                    'role': role_name
                                                })
                                                field_names_seen.add(field_name)

                    # ============================================================
                    # METHOD 3: Extract from prototypeQuery (backup)
                    # ============================================================
                    prototype = single_visual.get('prototypeQuery', {})
                    if prototype:
                        for select_item in prototype.get('Select', []):
                            if isinstance(select_item, dict):
                                # Check for Column
                                col = select_item.get('Column', {})
                                if col:
                                    expr = col.get('Expression', {})
                                    source_ref = expr.get('SourceRef', {})
                                    table_name = source_ref.get('Entity', '')
                                    column_name = col.get('Property', '')

                                    if column_name and column_name not in field_names_seen:
                                        fields.append({
                                            'name': column_name,
                                            'displayName': select_item.get('Name', column_name),
                                            'table': table_name,
                                            'type': 'Column'
                                        })
                                        field_names_seen.add(column_name)

                                # Check for Measure
                                measure = select_item.get('Measure', {})
                                if measure:
                                    expr = measure.get('Expression', {})
                                    source_ref = expr.get('SourceRef', {})
                                    table_name = source_ref.get('Entity', '')
                                    measure_name = measure.get('Property', '')

                                    if measure_name and measure_name not in field_names_seen:
                                        fields.append({
                                            'name': measure_name,
                                            'displayName': select_item.get('Name', measure_name),
                                            'table': table_name,
                                            'type': 'Measure'
                                        })
                                        field_names_seen.add(measure_name)

                                # Check for Aggregation in prototypeQuery
                                agg = select_item.get('Aggregation', {})
                                if agg:
                                    expr = agg.get('Expression', {})
                                    if 'Column' in expr:
                                        col = expr['Column']
                                        col_expr = col.get('Expression', {})
                                        source_ref = col_expr.get('SourceRef', {})
                                        table_name = source_ref.get('Entity', '')
                                        column_name = col.get('Property', '')

                                        if column_name and column_name not in field_names_seen:
                                            fields.append({
                                                'name': column_name,
                                                'displayName': select_item.get('Name', column_name),
                                                'table': table_name,
                                                'type': 'Aggregation'
                                            })
                                            field_names_seen.add(column_name)

                    # ============================================================
                    # METHOD 4: Deep walk — catch PBIR / nested queryRef shapes
                    # ============================================================
                    if not fields:
                        for walked in self._walk_field_refs(single_visual):
                            fname = walked.get('name')
                            if not fname or fname in field_names_seen:
                                continue
                            fields.append(walked)
                            field_names_seen.add(fname)

                    # DEBUG: Print extraction details for table visuals
                    if visual_type in ['tableEx', 'pivotTable', 'table']:
                        print(f"\n   🔍 TABLE VISUAL DEBUG: '{visual_title or visual_id}' (type: {visual_type})")
                        print(f"      - Extracted {len(fields)} fields from queryState")
                        print(f"      - queryState roles: {list(query_state.keys()) if query_state else 'None'}")
                        print(f"      - legacy projections: {list(legacy_projections.keys()) if legacy_projections else 'None'}")
                        print(f"      - prototypeQuery.Select items: {len(prototype.get('Select', [])) if prototype else 0}")
                        if len(fields) < 3:
                            print(f"      ⚠️  WARNING: Only {len(fields)} fields extracted, this might be incomplete!")

                    # Keep all real visuals (even without fields) so crash test can
                    # count/page-walk them. Field-less decorations still useful.
                    if fields:
                        visuals_with_fields += 1
                    # Skip pure chrome containers with no type
                    if (not visual_type or visual_type == 'unknown') and not fields and not visual_title:
                        continue
                    visual_fields[visual_id] = {
                        'type': visual_type,
                        'page': section_name,
                        'title': visual_title,
                        'fields': fields
                    }

            print(f"   ✅ Extracted fields from {visuals_with_fields}/{total_visuals} visuals")

        except Exception as e:
            print(f"   ⚠️ Error extracting fields from definition: {e}")
            import traceback
            traceback.print_exc()

        return visual_fields

    # Canvas / broken-visual text patterns shared by multi-frame scan + in-page JS
    CANVAS_ERROR_PATTERNS = [
        "Something's wrong with one or more fields",
        "We are not able to identify the following fields",
        "This visual has exceeded the available resources",
        "Couldn't retrieve the data for this visual",
        "Could not retrieve the data for this visual",
        "Can't display the visual",
        "Cannot display the visual",
        "Couldn't display the visual",
        "Couldn't display this visual",
        "Can't display this visual",
        "Visual has exceeded",
        "fields aren't available",
        "fields are not available",
        "isn't available or may have been deleted",
        "is not available or may have been deleted",
        "Please try again later or contact support",
        "Missing_References",
        "Something went wrong",
        "See details",
        "Couldn't load the data for this visual",
        "couldn't load data for this visual",
    ]

    async def _scan_frames_for_canvas_errors(self, page) -> Dict:
        """
        Scan the main page AND every iframe (including cross-origin Power BI frames)
        for broken-visual canvas banners. Same-origin contentDocument walks miss these.
        """
        patterns = list(self.CANVAS_ERROR_PATTERNS)
        js = """
        (patterns) => {
            const text = ((document.body && (document.body.innerText || document.body.textContent)) || '')
                + ' ' + ((document.documentElement && document.documentElement.innerText) || '');
            const hits = [];
            for (const p of patterns) {
                if (p && text.indexOf(p) !== -1) hits.push(p);
            }
            // Count "See details" style tiles near error icon containers when possible
            let detailButtons = 0;
            try {
                const all = Array.from(document.querySelectorAll('button, a, span, div'));
                for (const el of all) {
                    const t = (el.innerText || el.textContent || '').trim();
                    if (/^see details$/i.test(t)) detailButtons += 1;
                }
            } catch (e) {}
            return { hits, detailButtons, textLen: text.length };
        }
        """
        all_hits = []
        detail_buttons = 0
        frames_scanned = 0
        frame_errors = []

        frames = list(page.frames)
        for fr in frames:
            try:
                data = await fr.evaluate(js, patterns)
                frames_scanned += 1
                hits = data.get('hits') or []
                detail_buttons += int(data.get('detailButtons') or 0)
                for h in hits:
                    if h not in all_hits:
                        all_hits.append(h)
                if hits:
                    frame_errors.append({
                        'url': (fr.url or '')[:180],
                        'hits': hits,
                        'detailButtons': data.get('detailButtons') or 0,
                    })
            except Exception as e:
                # Detached / about:blank / CSP — ignore
                msg = str(e)
                if 'Execution context was destroyed' in msg or 'Target closed' in msg:
                    continue
                continue

        # Promote detail-button density into a signal when banners alone are weak
        if detail_buttons > 0 and 'See details' not in all_hits:
            all_hits.append('See details')

        return {
            'hasErrors': len(all_hits) > 0 or detail_buttons > 0,
            'errorCount': max(len(all_hits), detail_buttons),
            'errors': all_hits,
            'detailButtons': detail_buttons,
            'framesScanned': frames_scanned,
            'frameErrors': frame_errors,
        }

    def _merge_render_scan(self, export_result: Dict, playwright_result: Dict) -> Dict:
        """Keep export field bindings and overlay Playwright runtime errors."""
        export_pages = export_result.get('pages') or []
        pw_pages = playwright_result.get('pages') or []

        def _norm(value):
            return str(value or '').strip().lower()

        by_name = {_norm(p.get('name')): p for p in export_pages if p.get('name')}
        by_display = {
            _norm(p.get('displayName') or p.get('name')): p
            for p in export_pages
            if (p.get('displayName') or p.get('name'))
        }
        used = set()
        merged_pages = []

        for pw in pw_pages:
            key_name = _norm(pw.get('name'))
            key_disp = _norm(pw.get('displayName') or pw.get('name'))
            exp = by_name.get(key_name) or by_display.get(key_disp)
            if not exp:
                merged_pages.append(pw)
                continue

            used.add(id(exp))
            page = dict(exp)
            page['name'] = pw.get('name') or page.get('name')
            page['displayName'] = pw.get('displayName') or page.get('displayName')
            page['hasErrors'] = bool(pw.get('hasErrors'))
            page['errors'] = pw.get('errors') or []
            page['order'] = pw.get('order', page.get('order'))
            page['isActive'] = pw.get('isActive', page.get('isActive'))

            pw_visuals = { _norm(v.get('title') or v.get('name')): v for v in (pw.get('visuals') or []) }
            overlayed = []
            for visual in page.get('visuals') or []:
                match = pw_visuals.get(_norm(visual.get('title') or visual.get('name')))
                if match:
                    visual = dict(visual)
                    if match.get('isBlank'):
                        visual['isBlank'] = True
                    if match.get('type') and not visual.get('type'):
                        visual['type'] = match.get('type')
                    if match.get('layout') and not visual.get('layout'):
                        visual['layout'] = match.get('layout')
                    if match.get('fields') and not visual.get('fields'):
                        visual['fields'] = match.get('fields')
                overlayed.append(visual)
            page['visuals'] = overlayed
            page['visualCount'] = len(overlayed)
            merged_pages.append(page)

        for exp in export_pages:
            if id(exp) not in used:
                merged_pages.append(exp)

        export_result['pages'] = merged_pages
        export_result['totalPages'] = len(merged_pages)
        export_result['totalVisuals'] = sum(len(p.get('visuals') or []) for p in merged_pages)
        export_result['method'] = 'report_definition_export+playwright_render_scan'
        export_result['render_scan_performed'] = True
        export_result['success'] = True
        return export_result

    def get_embed_token(self, workspace_id: str, report_id: str) -> Dict:
        """Get credentials for embedding the report.

        Prefers GenerateToken (TokenType.Embed). If that fails — typical for
        service principals — fall back to the caller's AAD token so Playwright
        can still render the report the same way the signed-in user sees it.
        """
        if not self.access_token:
            self.access_token = self.get_access_token()

        url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}/GenerateToken"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        body = {
            "accessLevel": "View",
            "allowSaveAs": False
        }

        print(f"   🔐 Getting embed token...")
        response = requests.post(url, headers=headers, json=body)

        if response.status_code == 200:
            payload = response.json()
            payload['tokenType'] = 'Embed'
            return payload

        # Only fall back to TokenType.Aad with a real user SSO token — never a service principal.
        if self.user_token and response.status_code in (400, 401, 403):
            print(f"   ⚠️  GenerateToken failed ({response.status_code}); falling back to AAD user-token embed")
            return {
                "token": self.user_token,
                "tokenType": "Aad"
            }

        if response.status_code == 400:
            print(f"   ⚠️  Embed token generation failed (Service Principals cannot generate embed tokens)")
            print(f"   💡 Tip: For visual extraction with SP, use Report Definition Export only")
            raise Exception("Service Principal cannot generate embed tokens. Use Report Definition Export method instead.")

        print(f"   ❌ Error getting embed token: {response.status_code} {response.reason}")
        response.raise_for_status()
        return response.json()

    async def extract_visuals(self, workspace_id: str, report_id: str, timeout: int = 60, detect_render_errors: bool = False) -> Dict:
        """
        Extract visual metadata from a Power BI report

        Default: Report Definition Export (fast, includes field bindings).
        When detect_render_errors=True (Crash Test), also walk rendered pages
        with Playwright so runtime errors like "See details" are captured.

        Args:
            workspace_id: Power BI workspace GUID
            report_id: Power BI report GUID
            timeout: Maximum time to wait for report to render (seconds)
            detect_render_errors: If True, force a Playwright page walk after export
        """
        print(f"\n🔍 Extracting visual metadata for report {report_id}...")
        export_result = None

        def _finalize_playwright(pw_result: Dict) -> Dict:
            if export_result:
                if pw_result.get('success'):
                    return self._merge_render_scan(export_result, pw_result)
                export_result['render_scan_error'] = pw_result.get('error')
                export_result['render_scan_performed'] = False
                print(f"   ⚠️ Render scan failed, keeping export metadata: {pw_result.get('error')}")
                return export_result
            return pw_result

        # TRY NEW APPROACH FIRST: Report Definition Export (faster, includes fields!)
        print(f"   🚀 Trying optimized Report Definition approach...")
        report_def = self.get_report_definition(workspace_id, report_id)

        if report_def:
            # Check if it's a Model Diagram
            if isinstance(report_def, dict) and report_def.get('reportType') == 'ModelDiagram':
                print(f"   ℹ️  This is a Model Diagram (relationship view), not a report with visuals")
                return {
                    "success": False,
                    "reportId": report_id,
                    "workspaceId": workspace_id,
                    "error": "This is a Model Diagram (relationship view). Model diagrams display table relationships, not visual components. Please select a regular Power BI report to view visual lineage.",
                    "reportType": "ModelDiagram"
                }

            # Extract field bindings from definition
            visual_fields_map = self.extract_fields_from_definition(report_def)

            # Build result from report definition
            result = {
                "success": True,
                "reportId": report_id,
                "workspaceId": workspace_id,
                "extractedAt": datetime.utcnow().isoformat() + "Z",
                "pages": [],
                "totalPages": 0,
                "totalVisuals": 0,
                "method": "report_definition_export"  # Indicate which method was used
            }

            # Group visuals by page
            pages_map = {}
            for visual_id, visual_info in visual_fields_map.items():
                page_name = visual_info['page']
                if page_name not in pages_map:
                    pages_map[page_name] = {
                        'name': page_name,
                        'displayName': page_name,
                        'visuals': [],
                        'hasErrors': False,
                        'errors': []
                    }

                # visual_info['fields'] is already a list of dicts with name, displayName, etc.
                pages_map[page_name]['visuals'].append({
                    'name': visual_id,
                    'type': visual_info['type'],
                    'title': visual_info.get('title') or visual_id,  # Use actual title or visual ID
                    'fields': visual_info['fields']  # Already in correct format
                })

            result['pages'] = list(pages_map.values())
            result['totalPages'] = len(result['pages'])
            result['totalVisuals'] = sum(len(p['visuals']) for p in result['pages'])

            print(f"   ✅ Extracted {result['totalVisuals']} visuals from {result['totalPages']} pages using Report Definition")
            if not detect_render_errors:
                return result

            print(f"   🔎 Crash Test: scanning rendered pages for broken visuals...")
            export_result = result

        # Playwright: fallback when export failed, or extra render scan for Crash Test
        if export_result:
            print(f"   🔎 Running Playwright render scan to detect broken visuals...")
        else:
            print(f"   ⚠️ Report Definition approach failed, falling back to Playwright...")

        result = {
            "success": False,
            "reportId": report_id,
            "workspaceId": workspace_id,
            "extractedAt": datetime.utcnow().isoformat() + "Z",
            "pages": [],
            "totalPages": 0,
            "totalVisuals": 0,
            "error": None,
            "method": "playwright_automation"
        }

        try:
            # Get embed token (GenerateToken) or fall back to AAD user token
            print("   🔐 Getting embed token...")
            embed_data = self.get_embed_token(workspace_id, report_id)
            embed_token = embed_data["token"]
            js_token_type = "Aad" if str(embed_data.get("tokenType", "Embed")).lower() == "aad" else "Embed"

            # Get embed URL
            if not self.access_token:
                self.access_token = self.get_access_token()

            headers = {"Authorization": f"Bearer {self.access_token}"}
            url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}"
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            embed_url = response.json()["embedUrl"]

            print(f"   🌐 Embed URL: {embed_url} (tokenType={js_token_type})")
        except Exception as e:
            print(f"   ❌ Error getting embed token: {e}")
            result["error"] = f"Failed to get embed token: {str(e)}"
            return _finalize_playwright(result)
        
        # Launch headless browser with optimized settings
        try:
            async with async_playwright() as p:
                print("   🚀 Launching headless browser with optimized settings...")

                # Launch browser with additional args for stability
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        '--disable-dev-shm-usage',  # Overcome limited resource problems
                        '--disable-gpu',            # Disable GPU hardware acceleration
                        '--no-sandbox',             # Disable sandboxing (for Docker/Linux)
                        '--disable-setuid-sandbox',
                        '--disable-web-security',   # Allow cross-origin requests
                        '--disable-features=IsolateOrigins,site-per-process'
                    ]
                )

                # Create new page with extended timeout
                page = await browser.new_page()

                # Enable console logging for debugging
                page.on("console", lambda msg: print(f"      [Browser Console] {msg.text}"))
                page.on("pageerror", lambda err: print(f"      [Browser Error] {err}"))

                # OPTIMIZATION: Block unnecessary resources to speed up page load
                # NOTE: Don't block CSS completely - Power BI error messages need CSS to render
                await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico}", lambda route: route.abort())
                await page.route("**/*.{woff,woff2,ttf,eot,otf}", lambda route: route.abort())
                # await page.route("**/*.css", lambda route: route.abort())  # DISABLED - blocks error messages
                await page.route("**/analytics*", lambda route: route.abort())
                await page.route("**/telemetry*", lambda route: route.abort())

                # Set viewport size
                await page.set_viewport_size({"width": 1366, "height": 768})
                try:
                    # Create HTML page with embedded report (using Power BI REPORT AUTHORING SDK)
                    html_content = f"""
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <meta charset="UTF-8">
                        <meta name="viewport" content="width=device-width, initial-scale=1.0">
                        <!-- CRITICAL: Load powerbi-client FIRST, then powerbi-report-authoring extends it -->
                        <script src="https://cdn.jsdelivr.net/npm/powerbi-client@latest/dist/powerbi.min.js"></script>
                        <script src="https://cdn.jsdelivr.net/npm/powerbi-report-authoring@latest/dist/powerbi-report-authoring.min.js"></script>
                        <style>
                            body {{ margin: 0; padding: 0; overflow: hidden; }}
                            #reportContainer {{ width: 100%; height: 100vh; }}
                        </style>
                    </head>
                    <body>
                        <div id="reportContainer"></div>
                        <script>
                            console.log('🚀 Power BI Client SDK loaded:', window.powerbi ? 'OK' : 'FAILED');
                            console.log('🚀 Power BI Report Authoring SDK loaded:', window['powerbi-report-authoring'] ? 'OK' : 'Extended');

                            const models = window['powerbi-client'].models;
                            const tokenType = '{js_token_type}' === 'Aad' ? models.TokenType.Aad : models.TokenType.Embed;
                            const config = {{
                                type: 'report',
                                tokenType: tokenType,
                                accessToken: '{embed_token}',
                                embedUrl: '{embed_url}',
                                id: '{report_id}',
                                permissions: models.Permissions.Read,
                                viewMode: models.ViewMode.View,
                                settings: {{
                                    filterPaneEnabled: false,
                                    navContentPaneEnabled: false,
                                    layoutType: models.LayoutType.Master,  // Use master layout for faster load
                                    background: models.BackgroundType.Transparent,
                                    panes: {{
                                        filters: {{ expanded: false, visible: false }},
                                        pageNavigation: {{ visible: false }}
                                    }},
                                    bars: {{
                                        actionBar: {{ visible: false }}
                                    }}
                                }}
                            }};

                            console.log('📊 Embedding report with config:', config.id);
                            const reportContainer = document.getElementById('reportContainer');
                            window.report = powerbi.embed(reportContainer, config);
                            window.reportReady = false;
                            window.reportLoaded = false;

                            window.report.on('loaded', function() {{
                                console.log('✅ Report loaded event fired');
                                window.reportLoaded = true;
                            }});

                            window.report.on('rendered', function() {{
                                console.log('✅ Report rendered event fired');
                                window.reportReady = true;
                            }});

                            // Track all errors that occur with visual-level detail
                            window.reportErrors = [];
                            window.reportError = null;

                            window.report.on('error', function(event) {{
                                console.error('❌ Report error event:', event.detail);
                                window.reportError = event.detail;

                                // Extract visual title from detailed message
                                // Example: "Could not render a report visual titled: SOP Compliance"
                                const detailedMsg = event.detail.detailedMessage || '';
                                const visualTitleMatch = detailedMsg.match(/visual titled:\s*(.+?)(?:\.|,|$)/i);
                                const visualTitle = visualTitleMatch ? visualTitleMatch[1].trim() : 'Unknown Visual';

                                // Store all errors in an array with visual-level detail
                                window.reportErrors.push({{
                                    message: event.detail.message || 'Unknown error',
                                    detailedMessage: detailedMsg,
                                    visualTitle: visualTitle,
                                    timestamp: new Date().toISOString()
                                }});
                            }});

                            window.report.on('pageChanged', function(event) {{
                                console.log('📄 Page changed:', event.detail);
                            }});
                        </script>
                    </body>
                    </html>
                    """

                    # Load the page
                    await page.set_content(html_content)
                    print("   ⏳ Waiting for report to embed and render...")

                    # Step 1: Wait for Power BI SDK to load (5 seconds)
                    print("      ⏳ Step 1/3: Waiting for Power BI SDK...")
                    try:
                        await page.wait_for_function(
                            "typeof window.powerbi !== 'undefined' && typeof window.report !== 'undefined'",
                            timeout=10000  # 10 seconds for SDK load
                        )
                        print("      ✅ Power BI SDK loaded")
                    except Exception as e:
                        print(f"      ❌ SDK load timeout: {e}")
                        result["error"] = "Power BI SDK failed to load"
                        await browser.close()
                        return _finalize_playwright(result)

                    # Step 2: Wait for report to load (30 seconds)
                    print("      ⏳ Step 2/3: Waiting for report to load...")
                    try:
                        await page.wait_for_function(
                            "window.reportLoaded === true || window.reportError !== null",
                            timeout=30000  # 30 seconds for report load
                        )

                        # Check for load errors
                        report_error = await page.evaluate("window.reportError")
                        if report_error:
                            print(f"      ⚠️  Report load error detected: {report_error}")
                            print(f"      ℹ️  Continuing to extract visual metadata (broken visuals expected)...")
                            # DON'T return - Missing_References is a broken visual, not a fatal error
                            # Continue to extract the broken visual metadata
                        else:
                            print("      ✅ Report loaded")
                    except Exception as e:
                        print(f"      ⚠️  Report load timeout (continuing anyway): {e}")
                        # Don't fail - sometimes rendered fires before loaded

                    # Step 3: Wait for report to render (remaining timeout)
                    print("      ⏳ Step 3/3: Waiting for report to render...")
                    try:
                        await page.wait_for_function(
                            "window.reportReady === true || window.reportError !== null",
                            timeout=max(timeout * 1000 - 40000, 20000)  # Remaining time, min 20s
                        )

                        # Final error check
                        report_error = await page.evaluate("window.reportError")
                        if report_error:
                            print(f"      ⚠️  Report render error detected: {report_error}")
                            print(f"      ℹ️  Continuing to extract visual metadata (broken visuals expected)...")
                            # DON'T return - this is exactly what we want to detect!
                            # Continue to extract the broken visual metadata
                        else:
                            print("      ✅ Report rendered successfully")
                    except Exception as e:
                        print(f"      ⚠️  Render timeout (will try extraction anyway): {e}")
                        # Continue - sometimes we can still extract data
                
                    # Extract visual metadata using JavaScript with robust timeout and retry logic
                    print("   📊 Extracting visual metadata...")

                    # Set page timeout for evaluation
                    page.set_default_timeout(timeout * 1000)

                    # Wait for visuals to render and error banners to paint
                    print("   ⏳ Waiting for visuals to fully render (detecting errors)...")
                    await page.wait_for_timeout(8000)

                    # Multi-frame canvas scan (cross-origin Power BI iframes need frame.evaluate)
                    print("   🔍 Checking for visual errors across all browser frames...")
                    page_errors = await self._scan_frames_for_canvas_errors(page)
                    if page_errors.get('hasErrors'):
                        error_list = ', '.join(page_errors.get('errors', [])[:12])
                        print(f"   🔴 ERRORS DETECTED ON CANVAS: {error_list}")
                        print(
                            f"      frames_scanned={page_errors.get('framesScanned')} "
                            f"hits={page_errors.get('errorCount')}"
                        )
                    else:
                        print(
                            f"   ℹ️  No canvas error banners yet "
                            f"(frames_scanned={page_errors.get('framesScanned', 0)})"
                        )

                    skip_field_extraction = bool(export_result)
                    # Inject shared canvas patterns into page-walk JS
                    canvas_patterns_js = json.dumps(self.CANVAS_ERROR_PATTERNS)
                    visuals_data = await page.evaluate(f"""
                        async () => {{
                            const log = (msg) => console.log('    🔍 ' + msg);
                            const skipFieldExtraction = {str(skip_field_extraction).lower()};
                            const canvasPatterns = {canvas_patterns_js};

                            try {{
                                log('Starting visual extraction...');

                                if (!window.report) {{
                                    throw new Error('window.report is not defined');
                                }}

                                const report = window.report;
                                log('Report object found');

                                // CRITICAL FIX: Use shorter timeout with retry logic
                                const getPagesWithRetry = async (maxRetries = 2) => {{
                                    for (let attempt = 1; attempt <= maxRetries; attempt++) {{
                                        try {{
                                            log(`Attempt ${{attempt}}/${{maxRetries}}: Calling getPages()...`);

                                            const getPagesPromise = report.getPages();
                                            const timeoutPromise = new Promise((_, reject) =>
                                                setTimeout(() => reject(new Error(`getPages() timeout (attempt ${{attempt}})`)), 25000)
                                            );

                                            const pages = await Promise.race([getPagesPromise, timeoutPromise]);
                                            log(`✅ Got ${{pages.length}} pages`);
                                            return pages;
                                        }} catch (err) {{
                                            log(`❌ Attempt ${{attempt}} failed: ${{err.message}}`);
                                            if (attempt === maxRetries) throw err;
                                            await new Promise(resolve => setTimeout(resolve, 2000));  // Wait 2s before retry
                                        }}
                                    }}
                                }};

                                const pages = await getPagesWithRetry();
                                const result = [];

                                // Process each page (navigate to it and check for errors)
                                for (let i = 0; i < pages.length; i++) {{
                                    const page = pages[i];
                                    log(`Processing page ${{i+1}}/${{pages.length}}: ${{page.displayName}}`);

                                    try {{
                                        // NAVIGATE TO THE PAGE to render its visuals
                                        log(`  🔄 Navigating to page "${{page.displayName}}"...`);

                                        // Clear previous errors before navigating
                                        const errorsBefore = window.reportErrors.length;

                                        await page.setActive();

                                        // Wait for page to fully render (errors need time to appear)
                                        await new Promise(resolve => setTimeout(resolve, 10000));

                                        // Check if any NEW errors occurred after navigating to this page
                                        const errorsAfter = window.reportErrors.length;
                                        let hasPageErrors = errorsAfter > errorsBefore;
                                        let pageErrors = hasPageErrors ? window.reportErrors.slice(errorsBefore) : [];

                                        // Same-origin canvas text fallback (cross-origin frames handled by Python multi-frame scan)
                                        const collectText = (doc) => {{
                                            let text = '';
                                            try {{ text += (doc.body && (doc.body.innerText || doc.body.textContent)) || ''; }} catch (e) {{}}
                                            try {{
                                                const iframes = doc.querySelectorAll('iframe');
                                                for (const iframe of iframes) {{
                                                    try {{
                                                        const child = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document);
                                                        if (child) text += '\\n' + collectText(child);
                                                    }} catch (e) {{}}
                                                }}
                                            }} catch (e) {{}}
                                            return text;
                                        }};
                                        const pageText = collectText(document);
                                        const canvasHits = canvasPatterns.filter(p => pageText.includes(p));
                                        let detailButtons = 0;
                                        try {{
                                            const nodes = Array.from(document.querySelectorAll('button, a, span, div'));
                                            for (const el of nodes) {{
                                                const t = (el.innerText || el.textContent || '').trim();
                                                if (/^see details$/i.test(t)) detailButtons += 1;
                                            }}
                                        }} catch (e) {{}}
                                        if (canvasHits.length || detailButtons > 0) {{
                                            log(`  🔴 CANVAS ERROR on page "${{page.displayName}}": ${{canvasHits.join(', ') || 'See details'}} (tiles=${{detailButtons}})`);
                                            hasPageErrors = true;
                                            const baseHits = canvasHits.length ? canvasHits : ['See details'];
                                            pageErrors.push({{
                                                visualTitle: 'Canvas error',
                                                message: baseHits[0],
                                                detailedMessage: baseHits.join(' | '),
                                                timestamp: new Date().toISOString()
                                            }});
                                            // One synthetic issue per remaining See-details tile
                                            for (let di = 1; di < detailButtons; di++) {{
                                                pageErrors.push({{
                                                    visualTitle: `Broken visual tile ${{di + 1}}`,
                                                    message: 'See details',
                                                    detailedMessage: `Canvas 'See details' tile #${{di + 1}} on page ${{page.displayName}}`,
                                                    timestamp: new Date().toISOString()
                                                }});
                                            }}
                                        }}

                                        if (hasPageErrors) {{
                                            log(`  🔴 ERROR DETECTED on page "${{page.displayName}}" - ${{pageErrors.length}} error(s)`);
                                            pageErrors.forEach(err => log(`      ❌ ${{err.visualTitle}}: ${{err.message}} - ${{err.detailedMessage}}`));
                                        }} else {{
                                            log(`  ✅ No errors detected on page "${{page.displayName}}"`);
                                        }}

                                        // Get visuals with timeout
                                        const getVisualsPromise = page.getVisuals();
                                        const timeoutPromise = new Promise((_, reject) =>
                                            setTimeout(() => reject(new Error(`getVisuals() timeout for ${{page.displayName}}`)), 20000)
                                        );

                                        const visuals = await Promise.race([getVisualsPromise, timeoutPromise]);
                                        log(`  ✓ Page "${{page.displayName}}": ${{visuals.length}} visuals`);

                                        const pageData = {{
                                            name: page.name,
                                            displayName: page.displayName,
                                            order: page.order,
                                            isActive: page.isActive,
                                            hasErrors: hasPageErrors,  // Flag if page has error messages
                                            errors: pageErrors.map(err => ({{  // NEW: Individual error details
                                                visualTitle: err.visualTitle,
                                                message: err.message,
                                                detailedMessage: err.detailedMessage,
                                                timestamp: err.timestamp
                                            }})),
                                            visuals: await Promise.all(visuals.map(async (v) => {{
                                                // CRITICAL: Check for broken visuals and blank visuals
                                                const visualType = v.type || '';
                                                const visualTitle = v.title || '';
                                                const visualName = v.name || '';

                                                // Detect blank visuals (no type or empty)
                                                const isBlank = !visualType || visualType === '' || visualType === 'unknown';

                                                if (isBlank) {{
                                                    log(`    ⚠️  BLANK/BROKEN VISUAL: ${{visualName}} has no type (likely broken or empty)`);
                                                }}

                                                // Extract field bindings (columns/measures used in visual) using getDataFields API
                                                // Skip when export already provided bindings — Crash Test only needs runtime errors.
                                                let fields = [];
                                                if (skipFieldExtraction) {{
                                                    return {{
                                                        name: visualName,
                                                        type: visualType,
                                                        title: visualTitle,
                                                        fields: fields,
                                                        layout: v.layout || {{}},
                                                        isBlank: isBlank
                                                    }};
                                                }}
                                                try {{
                                                    // CORRECT METHOD: Use getDataFields() for each data role
                                                    // Common data roles: Values, Category, Y, X, Series, Legend, Details, Tooltips, etc.
                                                    const commonRoles = ['Values', 'Category', 'Y', 'X', 'Series', 'Legend', 'Details', 'Tooltips',
                                                                         'Columns', 'Rows', 'Axis', 'Size', 'Color', 'Shape'];

                                                    // Get visual capabilities to find actual data roles
                                                    let dataRoles = [];
                                                    try {{
                                                        const capabilities = await v.getCapabilities();
                                                        if (capabilities && capabilities.dataRoles) {{
                                                            dataRoles = capabilities.dataRoles.map(role => role.name);
                                                            log(`    🔍 Visual has data roles: ${{dataRoles.join(', ')}}`);
                                                        }}
                                                    }} catch (capErr) {{
                                                        // Fallback to common roles if getCapabilities fails
                                                        dataRoles = commonRoles;
                                                    }}

                                                    // Extract fields from each data role
                                                    for (const roleName of dataRoles) {{
                                                        try {{
                                                            const roleFields = await v.getDataFields(roleName);
                                                            if (roleFields && roleFields.length > 0) {{
                                                                log(`    ✅ Found ${{roleFields.length}} field(s) in role '${{roleName}}'`);

                                                                // Process each field with async/await to get display name
                                                                for (let index = 0; index < roleFields.length; index++) {{
                                                                    const field = roleFields[index];

                                                                    // DEBUG: Log the raw field structure
                                                                    log(`      📋 Raw field ${{index}}: ${{JSON.stringify(field).substring(0, 200)}}`);

                                                                    // Extract field information
                                                                    let fieldName = 'Unknown';
                                                                    let displayName = '';
                                                                    let tableName = '';
                                                                    let fieldType = 'Unknown';

                                                                    // CRITICAL: Parse the field object structure
                                                                    // The structure is: {{"$schema": "...", "column": "name", "table": "table"}}

                                                                    if (field.$schema && field.$schema.includes('#column')) {{
                                                                        // Column or aggregated column
                                                                        fieldName = field.column || '';
                                                                        tableName = field.table || '';
                                                                        fieldType = field.aggregationFunction ? 'Aggregation' : 'Column';
                                                                    }} else if (field.$schema && field.$schema.includes('#measure')) {{
                                                                        // Measure
                                                                        fieldName = field.measure || '';
                                                                        tableName = field.table || '';
                                                                        fieldType = 'Measure';
                                                                    }} else if (field.$schema && field.$schema.includes('#hierarchy')) {{
                                                                        // Hierarchy
                                                                        fieldName = field.hierarchy || '';
                                                                        tableName = field.table || '';
                                                                        fieldType = 'Hierarchy';
                                                                    }} else if (typeof field === 'string') {{
                                                                        // Fallback: string representation
                                                                        fieldName = field;
                                                                        fieldType = 'Unknown';
                                                                    }} else {{
                                                                        log(`      ⚠️  Field structure not recognized! Keys: ${{Object.keys(field).join(', ')}}`);
                                                                        continue;
                                                                    }}

                                                                    // BREAKTHROUGH: Get the visual-level display name (alias) using separate API
                                                                    try {{
                                                                        const visualDisplayName = await v.getDataFieldDisplayName(roleName, index);
                                                                        displayName = visualDisplayName || fieldName;
                                                                        log(`      ✅ Got display name from API: "${{visualDisplayName}}"`);
                                                                    }} catch (err) {{
                                                                        // Display name API not available or failed - use field name
                                                                        displayName = fieldName;
                                                                        log(`      ⚠️  Could not get display name, using field name: ${{err.message}}`);
                                                                    }}

                                                                    log(`      ✓ Parsed as ${{fieldType}}: ${{tableName}}.${{fieldName}} (Alias: "${{displayName}}")`);

                                                                    if (fieldName && !fields.find(f => f.name === fieldName && f.role === roleName)) {{
                                                                        fields.push({{
                                                                            name: fieldName,
                                                                            displayName: displayName,
                                                                            table: tableName,
                                                                            type: fieldType,
                                                                            role: roleName
                                                                        }});
                                                                        log(`    📊 Field: ${{tableName}}.${{fieldName}} (Alias: "${{displayName}}", Type: ${{fieldType}}, Role: ${{roleName}})`);
                                                                    }}
                                                                }}
                                                            }}
                                                        }} catch (roleErr) {{
                                                            // Role doesn't exist or has no fields - this is normal, skip silently
                                                        }}
                                                    }}

                                                    if (fields.length > 0) {{
                                                        log(`    ✅ Total extracted fields for ${{visualName}}: ${{fields.length}}`);
                                                    }}
                                                }} catch (err) {{
                                                    // Field extraction failed - not critical
                                                    log(`    ⚠️ Could not extract fields for ${{visualName}}: ${{err.message}}`);
                                                }}

                                                return {{
                                                    name: visualName,
                                                    type: visualType,
                                                    title: visualTitle,
                                                    fields: fields,  // NEW: Field bindings
                                                    layout: v.layout || {{}},
                                                    isBlank: isBlank  // Flag for blank/broken visuals
                                                }};
                                            }})),
                                            visualCount: visuals.length
                                        }};

                                        result.push(pageData);
                                    }} catch (pageError) {{
                                        log(`  ❌ Error processing page "${{page.displayName}}": ${{pageError.message}}`);
                                        // Add page with error marker
                                        result.push({{
                                            name: page.name,
                                            displayName: page.displayName,
                                            order: page.order,
                                            isActive: page.isActive,
                                            visuals: [],
                                            visualCount: 0,
                                            error: pageError.message
                                        }});
                                    }}
                                }}

                                log(`✅ Extraction complete: ${{result.length}} pages processed`);
                                return {{ success: true, pages: result }};

                            }} catch (error) {{
                                console.error('❌ Fatal extraction error:', error);
                                return {{
                                    success: false,
                                    error: error.message || error.toString()
                                }};
                            }}
                        }}
                    """)

                    if not visuals_data.get("success"):
                        print(f"   ❌ Error extracting visuals: {visuals_data.get('error')}")
                        result["error"] = visuals_data.get('error')
                        await browser.close()
                        return _finalize_playwright(result)

                    # Calculate totals
                    pages = visuals_data["pages"]
                    total_visuals = sum(p.get("visualCount") or len(p.get("visuals") or []) for p in pages)

                    print(f"   ✅ Successfully extracted {len(pages)} page(s) with {total_visuals} visual(s)")

                    # Re-scan frames after page walk (errors often paint late / on last page only)
                    try:
                        final_canvas = await self._scan_frames_for_canvas_errors(page)
                        result["canvas_scan"] = final_canvas
                        if final_canvas.get('hasErrors'):
                            print(
                                f"   🔴 Final multi-frame canvas scan: "
                                f"{final_canvas.get('errorCount')} signal(s), "
                                f"{final_canvas.get('detailButtons', 0)} 'See details' control(s)"
                            )
                            # Ensure at least the active/last processed page carries errors
                            if pages:
                                target = pages[-1]
                                # Prefer currently active page if flagged
                                for p in pages:
                                    if p.get('isActive'):
                                        target = p
                                        break
                                if not target.get('hasErrors'):
                                    target['hasErrors'] = True
                                errs = list(target.get('errors') or [])
                                for hit in (final_canvas.get('errors') or []):
                                    errs.append({
                                        'visualTitle': 'Canvas error',
                                        'message': hit,
                                        'detailedMessage': hit,
                                        'timestamp': datetime.utcnow().isoformat() + 'Z',
                                        'source': 'multi_frame_canvas_scan',
                                    })
                                # If many See-details buttons, emit one synthetic issue per button
                                # so Crash Test counts broken tiles closer to what the user sees.
                                detail_n = int(final_canvas.get('detailButtons') or 0)
                                if detail_n > 1:
                                    # Already added one 'See details' pattern — add remaining N-1
                                    for i in range(1, detail_n):
                                        errs.append({
                                            'visualTitle': f'Broken visual tile {i + 1}',
                                            'message': 'See details',
                                            'detailedMessage': (
                                                f"Canvas shows 'See details' error tile #{i + 1} "
                                                f"(multi-frame scan counted {detail_n} tiles)"
                                            ),
                                            'timestamp': datetime.utcnow().isoformat() + 'Z',
                                            'source': 'multi_frame_canvas_scan',
                                        })
                                target['errors'] = errs
                                target['hasErrors'] = True
                                target['canvasDetailButtons'] = detail_n
                    except Exception as canvas_err:
                        print(f"   ⚠️  Final canvas scan failed: {canvas_err}")

                    # Update result
                    result["success"] = True
                    result["pages"] = pages
                    result["totalPages"] = len(pages)
                    result["totalVisuals"] = total_visuals
                    result["error"] = None

                    await browser.close()
                    return _finalize_playwright(result)

                except PlaywrightTimeoutError as e:
                    print(f"   ⏱️ Timeout waiting for report: {e}")
                    result["error"] = f"Timeout: Report took longer than {timeout}s to render"
                    if 'browser' in locals():
                        await browser.close()
                    return _finalize_playwright(result)
                except Exception as e:
                    print(f"   ❌ Error during extraction: {e}")
                    result["error"] = f"Extraction error: {str(e)}"
                    if 'browser' in locals():
                        await browser.close()
                    return _finalize_playwright(result)

        except Exception as e:
            print(f"   ❌ Browser automation error: {e}")
            result["error"] = f"Browser error: {str(e)}"
            return _finalize_playwright(result)


# Example usage
async def main():
    # Load credentials from environment
    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")
    tenant_id = os.getenv("TENANT_ID")
    
    extractor = VisualMetadataExtractor(client_id, client_secret, tenant_id)
    
    # Example: Extract visuals from a report
    workspace_id = "59ed6719-608e-43c7-b38e-7d08934d17b0"  # CQI Team
    report_id = "d0949266-11dc-4af2-be51-3cf3088e742c"  # Example report
    
    result = await extractor.extract_visuals(workspace_id, report_id)
    
    # Print results
    print("\n" + "="*80)
    print("📋 VISUAL METADATA EXTRACTION RESULTS")
    print("="*80)
    print(json.dumps(result, indent=2))
    
    # Save to file
    with open("visual_metadata.json", "w") as f:
        json.dump(result, f, indent=2)
    print("\n✅ Results saved to visual_metadata.json")


if __name__ == "__main__":
    # Note: Requires: pip install playwright
    # Then run: playwright install chromium
    asyncio.run(main())
