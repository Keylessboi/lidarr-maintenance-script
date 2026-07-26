#!/usr/bin/env python3
"""
Lidarr Queue Maintenance Script
Runs the heavy lifting (API calls) to save tokens.
Outputs structured results for agentic oversight on edge cases.
"""
import json, re, os, sys
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API_KEY = os.environ.get("LIDARR_API_KEY", "")
BASE_URL = os.environ.get("LIDARR_URL", "")
if not API_KEY or not BASE_URL:
    # Fallback: read from arr-mcp .env
    env_path = "/opt/projects/lidarr-mcp/arr-mcp/.env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("LIDARR_API_KEY="):
                    API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("LIDARR_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"').strip("'")

HEADERS = {
    "X-Api-Key": API_KEY,
    "Content-Type": "application/json",
}


def api_get(path, params=None):
    url = f"{BASE_URL}/api/v1/{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = Request(url, headers=HEADERS, method="GET")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        return {"error": str(e), "status": e.code}
    except Exception as e:
        return {"error": str(e)}


def api_post(path, data):
    url = f"{BASE_URL}/api/v1/{path}"
    body = json.dumps(data).encode()
    req = Request(url, data=body, headers=HEADERS, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode()
    except HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return str(e)


def api_delete(path):
    url = f"{BASE_URL}/api/v1/{path}"
    req = Request(url, headers=HEADERS, method="DELETE")
    try:
        with urlopen(req, timeout=15) as resp:
            return resp.read().decode()
    except HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return str(e)


def delete_queue_item(record_id, remove_from_client=True, album_id=None):
    """Remove item from Lidarr queue and optionally trigger re-search"""
    api_delete(f"queue/{record_id}?removeFromClient={str(remove_from_client).lower()}&blocklist=false")
    if album_id:
        api_post("command", {"name": "AlbumSearch", "albumIds": [album_id]})


def try_manual_import(download_id):
    """Attempt to force-import via Lidarr manual import API"""
    items = api_get(f"manualimport?downloadId={download_id}")
    if not items or isinstance(items, dict) and items.get("error"):
        return False
    if not isinstance(items, list) or not items:
        return False
    for item in items:
        item["importMode"] = "move"
    api_post("manualimport", items)
    return True


def parse_match_pct(status_messages):
    """Extract match percentage from status messages"""
    for sm in status_messages:
        for msg in sm.get("messages", []):
            if "match" in msg.lower() and "%" in msg:
                m = re.search(r"(\d+\.?\d*)\s*%", msg)
                if m:
                    return float(m.group(1))
    return None


def flatten_messages(status_messages):
    """Get all message strings flattened"""
    msgs = []
    for sm in status_messages:
        msgs.extend(sm.get("messages", []))
    return msgs


def main():
    print(f"Lidarr Queue Maintenance — {datetime.now().isoformat()}")
    print(f"Target: {BASE_URL}")
    print()

    resp = api_get("queue", params={
        "pageSize": 2500,
        "page": 1,
        "sortDirection": "ascending",
        "sortKey": "status",
        "includeUnknownArtistItems": True,
    })

    if "error" in resp:
        print(f"ERROR fetching queue: {resp['error']}")
        sys.exit(1)

    records = resp.get("records", [])
    total = resp.get("totalRecords", 0)
    print(f"Queue total: {total}")
    print()

    now = datetime.now(timezone.utc)
    utc = timezone.utc

    # Action buckets
    action_import = []   # items to try force-import
    action_delete = []   # items to delete-and-research
    action_skip = []     # low match items for agent oversight
    action_unknown = []  # items with unrecognized errors

    for record in records:
        record_id = record.get("id")
        title = record.get("title", "Unknown")
        tracked_state = record.get("trackedDownloadState")
        status_messages = record.get("statusMessages", [])
        added_str = record.get("added")
        download_id = record.get("downloadId", "")
        album_id = record.get("albumId")

        if not status_messages:
            continue

        sm_str = str(status_messages)
        flat = flatten_messages(status_messages)
        primary_reason = flat[0] if flat else ""

        # Stale download check
        is_stale = False
        if added_str and tracked_state == "downloading":
            try:
                added_dt = datetime.strptime(str(added_str), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=utc)
                is_stale = (now - added_dt) > timedelta(days=14)
            except:
                pass

        if tracked_state == "downloading" and is_stale:
            action_delete.append((record_id, title, "stalled >14d", album_id))
            continue

        if tracked_state != "importFailed":
            continue

        # === IMPORT cases (force import, keep files) ===
        if "Not an upgrade for existing" in sm_str:
            action_import.append((record_id, download_id, title, "not an upgrade", album_id))

        elif "Album already imported" in sm_str:
            action_import.append((record_id, download_id, title, "already imported", album_id))

        elif "Failed to import track, Destination already exists" in sm_str:
            action_import.append((record_id, download_id, title, "dest exists", album_id))

        elif "Has unmatched tracks" in sm_str:
            action_import.append((record_id, download_id, title, "unmatched tracks", album_id))

        elif ("Album match" in sm_str or "Worst track match" in sm_str) and download_id:
            match_pct = parse_match_pct(status_messages)
            if match_pct is not None and match_pct >= 30:
                action_import.append((record_id, download_id, title, f"match {match_pct}%", album_id))
            else:
                action_skip.append((record_id, title, f"match {match_pct}%"))

        # === DELETE cases (remove + re-search) ===
        elif "Has missing tracks" in sm_str:
            action_delete.append((record_id, title, "missing tracks", album_id))

        elif "Has fewer tracks than existing release" in sm_str:
            action_delete.append((record_id, title, "fewer tracks", album_id))

        elif "could not find similar album" in sm_str.lower():
            action_skip.append((record_id, title, "no matching album (agent needs to match manually)"))

        elif "One or more tracks expected" in sm_str:
            action_delete.append((record_id, title, "generic import failure", album_id))

        else:
            action_unknown.append((record_id, title, primary_reason[:100] if primary_reason else "no details"))

    # === EXECUTE ===
    results = {"imported": [], "import_failed": [], "deleted": [], "skipped": [], "unknown": []}

    print(f"PHASE 1: Deleting {len(action_delete)} items...")
    for i, (rid, title, reason, album_id) in enumerate(action_delete, 1):
        delete_queue_item(rid, remove_from_client=True, album_id=album_id)
        results["deleted"].append(title[:60])
        if i % 100 == 0:
            print(f"  {i}/{len(action_delete)} deleted...")

    print(f"\nPHASE 2: Importing {len(action_import)} items...")
    for rid, did, title, reason, album_id in action_import:
        if did:
            success = try_manual_import(did)
            if success:
                results["imported"].append(f"{title[:55]} ({reason})")
            else:
                # If import fails, fall back to delete + re-search
                delete_queue_item(rid, remove_from_client=True, album_id=album_id)
                results["import_failed"].append(f"{title[:55]} ({reason})")
        else:
            # No downloadId, just delete + re-search
            delete_queue_item(rid, remove_from_client=True, album_id=album_id)
            results["import_failed"].append(f"{title[:55]} (no downloadId)")

    print(f"\nLOW MATCH (agent oversight needed): {len(action_skip)}")
    for rid, title, reason in action_skip[:5]:
        print(f"  ? {title[:55]} — {reason}")

    # === OUTPUT SUMMARY ===
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Deleted & re-searched: {len(results['deleted'])}")
    print(f"Imported: {len(results['imported'])}")
    if results["import_failed"]:
        print(f"Import failed (deleted instead): {len(results['import_failed'])}")
    print(f"Skipped (low match %): {len(action_skip)}")
    print(f"Unknown/edge cases: {len(action_unknown)}")

    if results["imported"]:
        print(f"\n--- Imported ---")
        for item in results["imported"][:15]:
            print(f"  + {item}")

    if results["import_failed"]:
        print(f"\n--- Import Failed (deleted) ---")
        for item in results["import_failed"][:10]:
            print(f"  ~ {item}")

    if results["deleted"]:
        print(f"\n--- Deleted ---")
        for item in results["deleted"][:10]:
            print(f"  - {item}")

    if action_unknown:
        print(f"\n--- UNKNOWN — Agent Review Needed ---")
        for rid, title, reason in action_unknown[:15]:
            print(f"  ? [{rid}] {title[:55]}")
            print(f"    Reason: {reason[:100]}")

    print(f"\nDone. Queue now has {total - len(action_delete)} items.")

    # === PHASE 3: Find continuously missing albums ===
    print(f"\n{'='*60}")
    print("PHASE 3: Checking for continuously missing albums...")
    print(f"{'='*60}")
    
    # Get oldest missing albums (most likely to have name issues)
    missing_resp = api_get("wanted/missing", params={
        "pageSize": 100,
        "page": 1,
        "sortKey": "releaseDate",
        "sortDirection": "ascending",
    })
    
    if "error" not in missing_resp:
        problem_albums = []
        for album in missing_resp.get("records", []):
            aid = album.get("id")
            artist = album.get("artist", {}).get("artistName", "?")
            title = album.get("title", "?")
            album_type = album.get("albumType", "?")
            
            # Check search and grab history
            src = api_get(f"history", params={"pageSize": 1, "albumId": aid, "eventType": 8})
            grabs = api_get(f"history", params={"pageSize": 1, "albumId": aid, "eventType": 1})
            
            s_count = src.get("totalRecords", 0) if isinstance(src, dict) else 0
            g_count = grabs.get("totalRecords", 0) if isinstance(grabs, dict) else 0
            
            if s_count >= 2 and g_count == 0:
                problem_albums.append((aid, artist, title, album_type, s_count))
        
        if problem_albums:
            print(f"\n  Found {len(problem_albums)} albums searched 2+ times with zero grabs:")
            for aid, artist, title, atype, s in sorted(problem_albums, key=lambda x: -x[4])[:20]:
                print(f"  ! [{aid}] {artist} - {title[:55]}")
                print(f"           Type: {atype} | Searched {s}x | Never grabbed")
            
            if len(problem_albums) > 20:
                print(f"  ... and {len(problem_albums)-20} more")
            
            print(f"\n[AGENT_OVERSIGHT_NEEDED] {len(problem_albums)} albums may have naming issues")
            for aid, artist, title, atype, s in problem_albums[:10]:
                print(f"[OVERSIGHT] albumId={aid} | {artist} - {title[:45]} | {s} failed searches")
        else:
            print("  No continuously missing albums found in this batch.")
    else:
        print(f"  Skipped (could not fetch missing list: {missing_resp.get('error')})")

    # Signal edge cases for agent oversight
    if action_unknown or action_skip:
        total_oversight = len(action_unknown) + len(action_skip)
        print(f"\n[AGENT_OVERSIGHT_NEEDED] {total_oversight} items need review")
        for rid, title, reason in (action_unknown + action_skip)[:10]:
            print(f"[OVERSIGHT] id={rid} | {title[:50]} | {reason[:80]}")


if __name__ == "__main__":
    main()
