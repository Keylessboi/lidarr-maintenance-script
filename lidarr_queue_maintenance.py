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

# ── CONFIG ──  Change these to tweak behavior without touching the logic below.
CONFIG = {
    # ── Global Thresholds ──
    "match_import_min": 30,          # match % >= this → try force import
    "match_oversight_max": 30,       # match % < this → flag for agent oversight
    "stale_download_days": 14,       # days before a stalled download is deleted + re-searched
    "queue_page_size": 500,          # how many queue records to fetch at once
    "missing_album_scan_count": 10,  # how many oldest missing albums to check (kept low to avoid timeouts)
    "missing_search_threshold": 2,   # searches >= this + zero grabs → flag problematic

    # ── Per-Download-Client Overrides ──
    # These override the global thresholds for specific download clients.
    # Add entries for any download client name seen in your Lidarr queue.
    # Possible clients: Slskd2 (Soulseek), Youtube, Lucida, qBittorrent, etc.
    "client_overrides": {
        "Slskd2": {
            "stale_download_days": 14,      # Slskd retrying — peer may be offline for days
            "match_import_min": 20,          # Lower match threshold for Soulseek downloads
            "retrying_message": "Some files failed. Retrying download",  # Slskd retry pattern
            "retrying_delete_days": 14,      # Delete if retrying for this many days
        },
        "Youtube": {
            "stale_download_days": 3,        # YouTube downloads should finish quickly
            "match_import_min": 15,          # Lower quality expected from YT audio extraction
        },
        "Lucida": {
            "stale_download_days": 7,
            "match_import_min": 20,
        },
    },

    # ── Action Lists ──
    # Move a keyword between lists to change its action.
    # Priority order: import > import_if_match > delete > oversight

    "import_keywords": [
        "Not an upgrade for existing",           # UpgradeSpecification — quality not better, but files are valid
        "Album already imported",                # AlreadyImportedSpecification — was imported, just stuck in queue
        "Failed to import track, Destination already exists",  # File system — dest file exists, clean up queue
        "Has unmatched tracks",                  # NoMissingOrUnmatchedTracksSpecification — extra files, valid bonus content
        "could not find similar album",          # Folder/name mismatch, agent should match manually → import
    ],

    "import_if_match_keywords": [
        "Album match",                           # CloseAlbumMatchSpecification
        "Worst track match",                     # CloseAlbumMatchSpecification
        "Track match is not close enough",        # CloseTrackMatchSpecification
    ],

    "delete_keywords": [
        "Has missing tracks",                    # NoMissingOrUnmatchedTracksSpecification
        "Has fewer tracks than existing release", # MoreTracksSpecification
        "One or more tracks expected",           # Generic wrapper (no specific reason underneath)
    ],

    "oversight_keywords": [],
}
# ── END CONFIG ──


API_KEY = os.environ.get("LIDARR_API_KEY", "")
BASE_URL = os.environ.get("LIDARR_URL", "")
if not API_KEY or not BASE_URL:
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


def get_client_config(download_client):
    """Get effective config for a download client (global defaults + client overrides)."""
    cfg = {}
    glob = CONFIG
    overrides = glob.get("client_overrides", {}).get(download_client, {})
    cfg["stale_download_days"] = overrides.get("stale_download_days", glob["stale_download_days"])
    cfg["match_import_min"] = overrides.get("match_import_min", glob["match_import_min"])
    cfg["retrying_message"] = overrides.get("retrying_message", None)
    cfg["retrying_delete_days"] = overrides.get("retrying_delete_days", glob["stale_download_days"])
    return cfg


def classify_record(record, now, utc):
    """
    Classify a single queue record into an action bucket.
    Returns (action_bucket, action_data_tuple) or None to skip.
    """
    record_id = record.get("id")
    title = record.get("title", "Unknown")
    tracked_state = record.get("trackedDownloadState")
    status_messages = record.get("statusMessages", [])
    added_str = record.get("added")
    download_id = record.get("downloadId", "")
    album_id = record.get("albumId")
    download_client = record.get("downloadClient", "")

    # Per-client config
    cc = get_client_config(download_client)

    if not status_messages:
        # Even without status messages, check for retrying
        error_msg = record.get("errorMessage", "")
        if cc["retrying_message"] and cc["retrying_message"] in error_msg and added_str:
            try:
                added_dt = datetime.strptime(str(added_str), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=utc)
                if (now - added_dt) > timedelta(days=cc["retrying_delete_days"]):
                    return ("delete", (record_id, title,
                        f"retrying >{cc['retrying_delete_days']}d ({download_client})", album_id))
            except:
                pass
        return None

    sm_str = str(status_messages)
    sm_lower = sm_str.lower()
    flat = flatten_messages(status_messages)
    primary_reason = flat[0] if flat else ""

    # Stale download check (uses per-client stale_download_days)
    is_stale = False
    retrying = False
    if added_str and tracked_state == "downloading":
        try:
            added_dt = datetime.strptime(str(added_str), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=utc)
            is_stale = (now - added_dt) > timedelta(days=cc["stale_download_days"])
        except:
            pass

    # Check for retrying pattern (e.g. Slskd "Some files failed. Retrying download...")
    error_msg = record.get("errorMessage", "")
    if cc["retrying_message"] and cc["retrying_message"] in error_msg and added_str:
        try:
            added_dt = datetime.strptime(str(added_str), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=utc)
            retrying = (now - added_dt) > timedelta(days=cc["retrying_delete_days"])
        except:
            pass

    if tracked_state == "downloading" and (is_stale or retrying):
        reason = f"stalled >{cc['stale_download_days']}d" if is_stale else f"retrying >{cc['retrying_delete_days']}d"
        client_tag = f" ({download_client})" if download_client else ""
        return ("delete", (record_id, title, f"{reason}{client_tag}", album_id))

    if tracked_state != "importFailed":
        return None

    # Check oversight keywords first (they take priority)
    for kw in CONFIG["oversight_keywords"]:
        if kw.lower() in sm_lower:
            return ("skip", (record_id, title, kw))

    # Check import keywords
    for kw in CONFIG["import_keywords"]:
        if kw in sm_str:
            return ("import", (record_id, download_id, title, kw, album_id))

    # Check import-if-match keywords (uses per-client match_import_min)
    for kw in CONFIG["import_if_match_keywords"]:
        if kw in sm_str and download_id:
            match_pct = parse_match_pct(status_messages)
            if match_pct is not None and match_pct >= cc["match_import_min"]:
                return ("import", (record_id, download_id, title, f"match {match_pct}%", album_id))
            else:
                return ("skip", (record_id, title, f"match {match_pct}%"))

    # Check delete keywords
    for kw in CONFIG["delete_keywords"]:
        if kw in sm_str:
            return ("delete", (record_id, title, kw, album_id))

    # Unknown
    return ("unknown", (record_id, title, primary_reason[:100] if primary_reason else "no details"))


def main():
    cfg = CONFIG
    print(f"Lidarr Queue Maintenance — {datetime.now().isoformat()}", flush=True)
    print(f"Target: {BASE_URL}", flush=True)
    print(f"Config: match_import_min={cfg['match_import_min']}%"
          f" | stale_days={cfg['stale_download_days']}"
          f" | missing_scan={cfg['missing_album_scan_count']}", flush=True)
    if cfg["client_overrides"]:
        clients = ", ".join(cfg["client_overrides"].keys())
        print(f"Clients with overrides: {clients}", flush=True)
    print(flush=True)

    resp = api_get("queue", params={
        "pageSize": cfg["queue_page_size"],
        "page": 1,
        "sortDirection": "ascending",
        "sortKey": "status",
        "includeUnknownArtistItems": True,
    })

    if "error" in resp:
        print(f"ERROR fetching queue: {resp['error']}", flush=True)
        sys.exit(1)

    records = resp.get("records", [])
    total = resp.get("totalRecords", 0)
    print(f"Queue total: {total}", flush=True)
    print(flush=True)

    now = datetime.now(timezone.utc)
    utc = timezone.utc

    action_buckets = {"import": [], "delete": [], "skip": [], "unknown": []}

    for record in records:
        result = classify_record(record, now, utc)
        if result is None:
            continue
        bucket, data = result
        action_buckets[bucket].append(data)

    action_import = action_buckets["import"]
    action_delete = action_buckets["delete"]
    action_skip = action_buckets["skip"]
    action_unknown = action_buckets["unknown"]

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
                delete_queue_item(rid, remove_from_client=True, album_id=album_id)
                results["import_failed"].append(f"{title[:55]} ({reason})")
        else:
            delete_queue_item(rid, remove_from_client=True, album_id=album_id)
            results["import_failed"].append(f"{title[:55]} (no downloadId)")

    print(f"\nLOW MATCH / OVERSIGHT: {len(action_skip)}")
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
    print(f"Skipped (oversight): {len(action_skip)}")
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

    missing_resp = api_get("wanted/missing", params={
        "pageSize": cfg["missing_album_scan_count"],
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

            src = api_get("history", params={"pageSize": 1, "albumId": aid, "eventType": 8})
            grabs = api_get("history", params={"pageSize": 1, "albumId": aid, "eventType": 1})

            s_count = src.get("totalRecords", 0) if isinstance(src, dict) else 0
            g_count = grabs.get("totalRecords", 0) if isinstance(grabs, dict) else 0

            if s_count >= cfg["missing_search_threshold"] and g_count == 0:
                problem_albums.append((aid, artist, title, album_type, s_count))

        if problem_albums:
            print(f"\n  Found {len(problem_albums)} albums searched {cfg['missing_search_threshold']}+ times with zero grabs:")
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
    total_oversight = len(action_unknown) + len(action_skip)
    if total_oversight:
        print(f"\n[AGENT_OVERSIGHT_NEEDED] {total_oversight} items need review")
        for rid, title, reason in (action_unknown + action_skip)[:10]:
            print(f"[OVERSIGHT] id={rid} | {title[:50]} | {reason[:80]}")


if __name__ == "__main__":
    main()
