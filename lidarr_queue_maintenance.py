#!/usr/bin/env python3
"""
Lidarr Queue Maintenance Script
Runs the heavy lifting (API calls) to save tokens.
Outputs structured results for agentic oversight on edge cases.
"""
import json, re, os, sys, time
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# ── CONFIG ──  Change these to tweak behavior without touching the logic below.
CONFIG = {
    # ── Global Thresholds ──
    "match_import_min": 30,          # match % >= this → try force import
    "match_oversight_max": 30,       # match % < this → flag for agent oversight
    "stale_download_days": 14,       # days before a stalled download is deleted + re-searched
    # Records per queue PAGE. The queue is paginated - see fetch_queue() - so
    # this is a page size and not a ceiling on how much of the queue is seen.
    # Kept at 500 because Lidarr serialises the whole queue to answer one
    # request (~46s per 1,000 records on a 2,500-record queue).
    "queue_page_size": 500,
    "missing_album_scan_count": 10,  # how many oldest missing albums to check (kept low to avoid timeouts)
    "missing_search_threshold": 2,   # searches >= this + zero grabs → flag problematic

    # ── Per-run action caps ──
    # Every delete fires an AlbumSearch, and Lidarr's command queue is what a
    # mass search wedges (docs/doctor-log.md, 2026-09-17: 216 commands stuck in
    # "started", ProcessMonitoredDownloads wedged 13.5 hours). While this script
    # only ever saw its first 500 records, the number of matching records was
    # accidentally bounded by that window. It now sees the whole queue, so bound
    # it deliberately instead. Leftovers are next run's work, oldest first.
    "max_deletes_per_run": 50,
    "max_imports_per_run": 500,

    # ── Unmapped Files Cleaner ──
    # Deletes orphaned track files that aren't linked to any album in Lidarr.
    "unmapped_files_cleaner_enabled": True,   # set False to skip
    "unmapped_files_dry_run": False,          # True = report only, don't delete

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
    # Priority order (judge_messages): oversight > clear > import > import_if_match > delete

    # Removed from the queue and the client, with no import and no re-search.
    # The library already holds files at least this good, so importing would
    # REPLACE them with these (Lidarr deletes the existing track files on a
    # manual import) - a downgrade, or the same files again. Harmless while
    # force-import was a no-op; not once it really imports.
    "clear_keywords": [
        "Not an upgrade for existing",           # UpgradeSpecification — existing files are equal or better
        "Album already imported",                # AlreadyImportedSpecification — this download was imported before
    ],

    "import_keywords": [
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

# Client statuses that mean a download is NOT progressing, and so may be
# treated as stale. Lidarr reports trackedDownloadState "downloading" for
# anything the client has not finished, which includes a torrent merely queued
# behind a download-slot limit - that is a backlog, not a fault. See
# classify_record().
STALE_CLIENT_STATUSES = ("downloading", "paused")


# DRY_RUN=1 classifies and resolves everything but sends no POST or DELETE,
# so a change can be checked against the live queue before it is trusted.
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

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


# A queue page is served by serialising the entire queue, which is slow: one
# pageSize=1000 request against a 2,500-record queue measured ~46s on this
# library. 30s was therefore not a timeout but a guaranteed failure - it is why
# the nightly run died on "ERROR fetching queue: timed out" for weeks.
API_TIMEOUT = 300


def api_get(path, params=None, timeout=API_TIMEOUT):
    url = f"{BASE_URL}/api/v1/{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = Request(url, headers=HEADERS, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        return {"error": str(e), "status": e.code}
    except Exception as e:
        return {"error": str(e)}


def api_post(path, data):
    if DRY_RUN:
        return "DRY_RUN"
    url = f"{BASE_URL}/api/v1/{path}"
    body = json.dumps(data).encode()
    req = Request(url, data=body, headers=HEADERS, method="POST")
    try:
        with urlopen(req, timeout=API_TIMEOUT) as resp:
            return resp.read().decode()
    except HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return str(e)


def api_delete(path):
    if DRY_RUN:
        return "DRY_RUN"
    url = f"{BASE_URL}/api/v1/{path}"
    req = Request(url, headers=HEADERS, method="DELETE")
    try:
        with urlopen(req, timeout=120) as resp:
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


def try_manual_import(download_id, artist_id=None, album_id=None):
    """Force-import a failed download with Lidarr's ManualImport command.

    Returns (outcome, reason), outcome being:
      True  - imported
      False - nothing importable, or the command failed: the download is bad
      None  - refused, because importing could land files somewhere wrong;
              leave the download alone and flag it

    Until 2026-10-01 this POSTed the items back to /manualimport, which only
    re-evaluates them (see run_manual_import()). Every "imported" item the
    summary reported was still in the queue the next night.

    Identification is pinned to the queue record's artist, and the files must
    all land on the album that was grabbed. A low match score is the case this
    path exists for, and also the case where Lidarr may have picked a
    different album by the same artist.
    """
    params = {"downloadId": download_id, "filterExistingFiles": True}
    if artist_id:
        params["artistId"] = artist_id
    items = api_get("manualimport", params=params)
    if not isinstance(items, list) or not items:
        return False, "no files to import"
    files = importable_files(items)
    if not files:
        return False, "no file maps to an album"
    albums = {f["albumId"] for f in files}
    if len(albums) != 1:
        return None, f"files span {len(albums)} albums"
    if album_id and albums != {album_id}:
        return None, f"files match albumId={albums.pop()}, grabbed albumId={album_id}"
    if run_manual_import(files):
        return True, ""
    return False, "ManualImport command failed"


# How long to wait for one ManualImport command. Lidarr runs disk-access
# commands one at a time, and a FLAC album moved onto the NAS takes a while.
MANUAL_IMPORT_WAIT = 600

# "found multiple artists: [7138333@deezer][Chris Travis], [ba7c...][Chris Travis]"
MULTIPLE_ARTISTS_RE = re.compile(r"found multiple artists: (.*)")
ARTIST_TOKEN_RE = re.compile(r"\[([^\]]+)\]\[([^\]]*)\]")


def parse_multiple_artists(messages):
    """Return [(foreignArtistId, name), ...] from a "found multiple artists"
    warning, or [] when the record does not carry one."""
    for msg in messages:
        m = MULTIPLE_ARTISTS_RE.search(msg)
        if m:
            return ARTIST_TOKEN_RE.findall(m.group(1))
    return []


def importable_files(items):
    """ManualImport command entries for the manualimport items Lidarr mapped to
    an artist, album, release and tracks. Unmapped items (cover art, cue files,
    bonus tracks) are left out rather than imported blind."""
    files = []
    for item in items:
        artist = item.get("artist") or {}
        album = item.get("album") or {}
        track_ids = [t["id"] for t in item.get("tracks") or [] if t.get("id")]
        if not (artist.get("id") and album.get("id") and item.get("albumReleaseId") and track_ids):
            continue
        files.append({
            "path": item["path"],
            "artistId": artist["id"],
            "albumId": album["id"],
            "albumReleaseId": item["albumReleaseId"],
            "trackIds": track_ids,
            "quality": item.get("quality"),
            "releaseGroup": item.get("releaseGroup"),
            "indexerFlags": item.get("indexerFlags", 0),
            "downloadId": item.get("downloadId"),
            "disableReleaseSwitching": item.get("disableReleaseSwitching", False),
        })
    return files


def run_manual_import(files):
    """Import files with Lidarr's ManualImport command and wait for it to end.

    This is the call the Manual Import dialog makes. POST /manualimport is NOT
    an import: it is UpdateItems, which re-evaluates the items and hands them
    back (Lidarr.Api.V1/ManualImport/ManualImportController.cs) - which is why
    try_manual_import() above reports success without importing anything.
    importMode "auto" moves soulseek/usenet files and copies torrent files, so
    seeding survives.
    """
    if DRY_RUN:
        return True
    resp = api_post("command", {
        "name": "ManualImport",
        "files": files,
        "importMode": "auto",
        "replaceExistingFiles": False,
    })
    try:
        command_id = json.loads(resp).get("id")
    except (ValueError, AttributeError):
        return False
    if not command_id:
        return False
    deadline = time.monotonic() + MANUAL_IMPORT_WAIT
    while time.monotonic() < deadline:
        status = api_get(f"command/{command_id}").get("status")
        if status == "completed":
            return True
        if status in ("failed", "aborted", "cancelled", "orphaned"):
            return False
        time.sleep(2)
    return False


def resolve_multiple_artists(download_id, candidates, artists_by_fid, cc):
    """Work out which of several same-named library artists a download is for.

    Lidarr will not guess when a release title's artist name matches more than
    one library artist - typically the same person added twice, once from
    MusicBrainz and once from another metadata source (7138333@deezer). It
    leaves the finished download in the queue as "downloading" with a warning,
    with no albumId, indefinitely.

    Asking manualimport for the download WITH an explicit artistId removes the
    ambiguity, so ask once per candidate and keep the one whose files map to a
    single album and pass the same keyword rules as any other failed import.
    Ties go to a clean match, then more files mapped, then the artist that
    already holds files, then the one added first.

    Returns (files, artist, reason); files is None when nothing qualified.
    """
    best = None
    tried = []
    for fid, name in candidates:
        artist = artists_by_fid.get(fid)
        if not artist:
            tried.append(f"{fid} not in library")
            continue
        items = api_get("manualimport", params={
            "downloadId": download_id,
            "artistId": artist["id"],
            "filterExistingFiles": True,
        })
        if not isinstance(items, list) or not items:
            tried.append(f"artistId={artist['id']} no files")
            continue
        files = importable_files(items)
        if not files:
            tried.append(f"artistId={artist['id']} no album match")
            continue
        if len({f["albumId"] for f in files}) != 1:
            tried.append(f"artistId={artist['id']} spans several albums")
            continue
        rejections = [r.get("reason", "") for item in items for r in item.get("rejections") or []]
        verdict, why = judge_messages([{"messages": rejections}], cc)
        if rejections and verdict != "import":
            tried.append(f"artistId={artist['id']} {why[:40]}")
            continue
        stats = artist.get("statistics") or {}
        rank = (not rejections, len(files), stats.get("trackFileCount") or 0,
                stats.get("albumCount") or 0, -artist["id"])
        if best is None or rank > best[0]:
            best = (rank, files, artist, "clean match" if not rejections else why)
    if best is None:
        return None, None, "; ".join(tried) or "no candidate artist resolved"
    return best[1], best[2], best[3]


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


def clean_unmapped_files(cfg):
    """Find and delete unmapped track files (orphaned files not linked to any album).

    Corresponds to UnmappedFilesCleaner from RandomNinjaAtk/arr-scripts.
    """
    dry_run = cfg.get("unmapped_files_dry_run", False)
    try:
        unmapped = api_get("trackFile", params={"unmapped": True})
    except Exception as e:
        print(f"  Error fetching unmapped files: {e}")
        return

    if not unmapped or isinstance(unmapped, dict) and unmapped.get("error"):
        print(f"  Could not fetch unmapped files: {unmapped}")
        return

    if not isinstance(unmapped, list) or not unmapped:
        print("  No unmapped files found.")
        return

    print(f"  Found {len(unmapped)} unmapped file(s).")
    freed_bytes = 0
    deleted_count = 0

    for f in unmapped:
        file_id = f.get("id", "?")
        path = f.get("path", "")
        size = f.get("size", 0) or 0
        dirname = os.path.dirname(path) if path else ""

        print(f"    [{file_id}] {path or '?'[:80]}", end="")
        if dry_run:
            print(" (dry run — would delete)")
            freed_bytes += size
            deleted_count += 1
        else:
            # Delete the Lidarr track file entry
            try:
                api_delete(f"trackFile/{file_id}")
                # Delete the actual file/directory
                if dirname and os.path.isdir(dirname):
                    import shutil
                    shutil.rmtree(dirname, ignore_errors=True)
                    print(" → deleted dir + Lidarr entry")
                else:
                    print(" → Lidarr entry removed (dir gone)")
                freed_bytes += size
                deleted_count += 1
            except Exception as e:
                print(f" → FAILED: {e}")

    print(f"\n  Cleaned: {deleted_count} files"
          f" ({freed_bytes / (1024**3):.1f} GB freed)"
          if not dry_run else f"  Would clean: {deleted_count} files"
          f" ({freed_bytes / (1024**3):.1f} GB)")


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
    artist_id = record.get("artistId")
    download_client = record.get("downloadClient", "")

    # Per-client config
    cc = get_client_config(download_client)

    # ── Several library artists share this release's artist name ──
    #
    # Checked before the stale rules on purpose. Lidarr parks these as
    # "downloading" with a warning although the client has finished, and they
    # carry no albumId - so the stale rule would delete a good download and
    # have nothing to re-search. See resolve_multiple_artists().
    candidates = parse_multiple_artists(flatten_messages(status_messages))
    if candidates:
        return ("resolve_artist", (record_id, download_id, title, candidates, download_client))

    # ── Stale / retrying checks ──
    #
    # THESE COME FIRST, AND THAT ORDER IS THE FIX.
    #
    # They used to sit BELOW an early `if not status_messages: return None`, so
    # they could only fire on a record that ALSO carried import-failure
    # messages - never on a stalled download, which is the one case they exist
    # for. Lidarr emits no status messages at all for a download that is merely
    # in progress or waiting for a download slot, and that is most of a queue
    # this size.
    #
    # Being stale is deliberately NOT age alone. Lidarr reports
    # trackedDownloadState "downloading" for anything the client has not
    # finished, which includes a torrent simply QUEUED behind the slot limit -
    # the normal state of a backlog, not a fault. Treating those as stale would
    # discard working grabs and fire a re-search for every one of them
    # (measured 2026-09-21: 1,289 records, against 40 genuinely stalled).
    # So a record with no status messages must have a client status that says
    # the download is not progressing; see STALE_CLIENT_STATUSES.
    client_status = record.get("status", "")
    error_msg = record.get("errorMessage", "")
    is_stale = False
    retrying = False

    if added_str and tracked_state == "downloading":
        if status_messages or client_status in STALE_CLIENT_STATUSES:
            try:
                added_dt = datetime.strptime(str(added_str), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=utc)
                is_stale = (now - added_dt) > timedelta(days=cc["stale_download_days"])
            except:
                pass

    # Check for retrying pattern (e.g. Slskd "Some files failed. Retrying download...")
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

    if not status_messages:
        return None

    if tracked_state != "importFailed":
        return None

    verdict, reason = judge_messages(status_messages, cc, bool(download_id))
    if verdict == "import":
        return ("import", (record_id, download_id, title, reason, album_id, artist_id))
    if verdict == "delete":
        return ("delete", (record_id, title, reason, album_id))
    return (verdict, (record_id, title, reason))


def judge_messages(status_messages, cc, has_download_id=True):
    """Map Lidarr's import-failure messages to (verdict, reason), where verdict
    is import, skip, delete or unknown. Shared by queue records and by the
    rejections manualimport returns when a duplicate artist is resolved."""
    sm_str = str(status_messages)
    sm_lower = sm_str.lower()
    flat = flatten_messages(status_messages)
    primary_reason = flat[0] if flat else ""

    # Check oversight keywords first (they take priority)
    for kw in CONFIG["oversight_keywords"]:
        if kw.lower() in sm_lower:
            return ("skip", kw)

    # Check import keywords
    # Check clear keywords (before import: importing these would downgrade)
    for kw in CONFIG.get("clear_keywords", []):
        if kw in sm_str:
            return ("clear", kw)

    for kw in CONFIG["import_keywords"]:
        if kw in sm_str:
            return ("import", kw)

    # Check import-if-match keywords (uses per-client match_import_min)
    for kw in CONFIG["import_if_match_keywords"]:
        if kw in sm_str and has_download_id:
            match_pct = parse_match_pct(status_messages)
            if match_pct is not None and match_pct >= cc["match_import_min"]:
                return ("import", f"match {match_pct}%")
            else:
                return ("skip", f"match {match_pct}%")

    # Check delete keywords
    for kw in CONFIG["delete_keywords"]:
        if kw in sm_str:
            return ("delete", kw)

    # Unknown
    return ("unknown", primary_reason[:100] if primary_reason else "no details")


def fetch_queue(cfg):
    """Fetch the WHOLE queue, one page at a time.

    A single request returns at most pageSize records and says nothing about
    the rest, so the caller silently works on a PREFIX of the queue. With
    sortKey=status that prefix was always the same alphabetical block
    ('completed' first), which is how this script spent weeks examining 500 of
    2,587 records and never once looking at the other 2,000 - including every
    stalled download it exists to clear.

    Returns (records, error). No sortKey is sent: pages are fetched in the
    server's own stable order and the caller sorts once it has everything, so
    the per-run caps apply to the oldest work rather than to whichever page
    happened to come back first.
    """
    records = []
    page = 1
    total = 0
    while True:
        resp = api_get("queue", params={
            "page": page,
            "pageSize": cfg["queue_page_size"],
            "includeUnknownArtistItems": True,
        })
        if "error" in resp:
            return None, resp["error"]
        batch = resp.get("records", []) or []
        total = resp.get("totalRecords", 0)
        records.extend(batch)
        if not batch or len(records) >= total:
            break
        page += 1
        if page > 40:  # hard stop: 40 x pageSize records, no infinite loop
            break
    return records, None


def main():
    cfg = CONFIG
    print(f"Lidarr Queue Maintenance — {datetime.now().isoformat()}", flush=True)
    print(f"Target: {BASE_URL}", flush=True)
    if DRY_RUN:
        print("DRY RUN: no deletes, imports or searches will be sent", flush=True)
    print(f"Config: match_import_min={cfg['match_import_min']}%"
          f" | stale_days={cfg['stale_download_days']}"
          f" | missing_scan={cfg['missing_album_scan_count']}", flush=True)
    if cfg["client_overrides"]:
        clients = ", ".join(cfg["client_overrides"].keys())
        print(f"Clients with overrides: {clients}", flush=True)
    print(flush=True)

    records, err = fetch_queue(cfg)
    if err:
        print(f"ERROR fetching queue: {err}", flush=True)
        sys.exit(1)

    total = len(records)
    print(f"Queue total: {total} records fetched (all pages)", flush=True)

    # Oldest first, so the caps below spend themselves on the work that has been
    # waiting longest. ISO-8601 strings sort correctly as text.
    records.sort(key=lambda r: str(r.get("added") or ""))
    print(flush=True)

    now = datetime.now(timezone.utc)
    utc = timezone.utc

    action_buckets = {"import": [], "delete": [], "skip": [], "unknown": [], "resolve_artist": [],
                      "clear": []}

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
    action_resolve = action_buckets["resolve_artist"]
    action_clear = action_buckets["clear"]

    # Caps are applied AFTER classification, so the summary still reports how
    # much was matched and the log shows what was deferred. Whatever is left is
    # simply next run's work.
    deferred_delete = max(0, len(action_delete) - cfg["max_deletes_per_run"])
    deferred_import = max(0, len(action_import) - cfg["max_imports_per_run"])
    action_delete = action_delete[:cfg["max_deletes_per_run"]]
    action_import = action_import[:cfg["max_imports_per_run"]]
    deferred_resolve = max(0, len(action_resolve) - cfg["max_imports_per_run"])
    action_resolve = action_resolve[:cfg["max_imports_per_run"]]
    if deferred_delete:
        print(f"NOTE: {deferred_delete} further delete(s) deferred to the next run "
              f"(max_deletes_per_run={cfg['max_deletes_per_run']})", flush=True)
    if deferred_import:
        print(f"NOTE: {deferred_import} further import(s) deferred to the next run "
              f"(max_imports_per_run={cfg['max_imports_per_run']})", flush=True)
    if deferred_resolve:
        print(f"NOTE: {deferred_resolve} further multiple-artist item(s) deferred to the next run "
              f"(max_imports_per_run={cfg['max_imports_per_run']})", flush=True)

    # === EXECUTE ===
    results = {"imported": [], "import_failed": [], "deleted": [], "skipped": [], "unknown": [],
               "artist_resolved": [], "artist_unresolved": [], "cleared": [],
               "import_refused": [], "import_failed_deferred": []}

    print(f"PHASE 1: Deleting {len(action_delete)} items...", flush=True)
    for i, (rid, title, reason, album_id) in enumerate(action_delete, 1):
        delete_queue_item(rid, remove_from_client=True, album_id=album_id)
        results["deleted"].append(title[:60])
        if i % 50 == 0:
            print(f"  {i}/{len(action_delete)} deleted...", flush=True)

    # No re-search: the library already has these at equal or better quality.
    print(f"\nPHASE 1b: Clearing {len(action_clear)} items already covered by the library...", flush=True)
    for rid, title, reason in action_clear:
        delete_queue_item(rid, remove_from_client=True, album_id=None)
        results["cleared"].append(f"{title[:55]} ({reason})")

    # A failed import falls back to delete + re-search, and every re-search is
    # an AlbumSearch on Lidarr's command queue - so those fallbacks share the
    # delete cap with PHASE 1 instead of adding up to max_imports_per_run more.
    delete_budget = max(0, cfg["max_deletes_per_run"] - len(action_delete))

    print(f"\nPHASE 2: Importing {len(action_import)} items...", flush=True)
    for i, (rid, did, title, reason, album_id, artist_id) in enumerate(action_import, 1):
        if did:
            outcome, why = try_manual_import(did, artist_id=artist_id, album_id=album_id)
        else:
            outcome, why = False, "no downloadId"
        if outcome:
            results["imported"].append(f"{title[:55]} ({reason})")
        elif outcome is None:
            results["import_refused"].append(f"[{rid}] {title[:45]} | {why}")
            action_skip.append((rid, title, f"import refused: {why}"))
        elif delete_budget:
            delete_budget -= 1
            delete_queue_item(rid, remove_from_client=True, album_id=album_id)
            results["import_failed"].append(f"{title[:55]} ({reason}; {why})")
        else:
            results["import_failed_deferred"].append(f"{title[:55]} ({why})")
        if i % 25 == 0:
            print(f"  {i}/{len(action_import)} processed...", flush=True)

    # Resolving each download is the workaround; merging the duplicate artists
    # in Lidarr is the fix, and that is not a call for a script to make. Count
    # the queue items per duplicate set so the oversight report can say so.
    duplicate_artists = {}
    print(f"\nPHASE 2b: Resolving {len(action_resolve)} multiple-artist items...", flush=True)
    if action_resolve:
        library = api_get("artist")
        artists_by_fid = ({a.get("foreignArtistId"): a for a in library}
                          if isinstance(library, list) else {})
        if not artists_by_fid:
            print(f"  Could not fetch the artist list: {library}", flush=True)
        for rid, did, title, candidates, client in action_resolve:
            key = tuple(sorted(candidates))
            duplicate_artists[key] = duplicate_artists.get(key, 0) + 1
            if not artists_by_fid:
                continue
            if not did:
                action_skip.append((rid, title, "multiple artists, no downloadId"))
                continue
            files, artist, why = resolve_multiple_artists(
                did, candidates, artists_by_fid, get_client_config(client))
            if files and run_manual_import(files):
                results["artist_resolved"].append(
                    f"{title[:45]} -> {artist.get('artistName')} [{artist.get('foreignArtistId')}] ({why})")
                continue
            reason = f"ManualImport failed ({why})" if files else why
            results["artist_unresolved"].append(f"[{rid}] {title[:45]} | {reason[:90]}")
            action_skip.append((rid, title, f"multiple artists: {reason}"))
    else:
        artists_by_fid = {}

    print(f"\nLOW MATCH / OVERSIGHT: {len(action_skip)}")
    for rid, title, reason in action_skip[:5]:
        print(f"  ? {title[:55]} — {reason}")

    # === OUTPUT SUMMARY ===
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Deleted & re-searched: {len(results['deleted'])}")
    print(f"Cleared (library already has it): {len(results['cleared'])}")
    print(f"Imported: {len(results['imported'])}")
    if results["import_refused"]:
        print(f"Import refused (wrong or ambiguous album, left alone): {len(results['import_refused'])}")
    if results["import_failed_deferred"]:
        print(f"Import failed, delete deferred (delete cap reached): {len(results['import_failed_deferred'])}")
    print(f"Imported after resolving duplicate artists: {len(results['artist_resolved'])}"
          f" ({len(results['artist_unresolved'])} left for oversight)")
    if results["import_failed"]:
        print(f"Import failed (deleted instead): {len(results['import_failed'])}")
    print(f"Skipped (oversight): {len(action_skip)}")
    print(f"Unknown/edge cases: {len(action_unknown)}")

    if results["imported"]:
        print("\n--- Imported ---")
        for item in results["imported"][:15]:
            print(f"  + {item}")

    if results["artist_resolved"]:
        print("\n--- Imported after resolving duplicate artists ---")
        for item in results["artist_resolved"][:15]:
            print(f"  + {item}")

    if results["artist_unresolved"]:
        print(f"\n--- Duplicate artists, not resolved ({len(results['artist_unresolved'])}) ---")
        for item in results["artist_unresolved"][:15]:
            print(f"  ? {item}")

    if results["import_failed"]:
        print("\n--- Import Failed (deleted) ---")
        for item in results["import_failed"][:10]:
            print(f"  ~ {item}")

    if results["import_refused"]:
        print("\n--- Import Refused (left in queue) ---")
        for item in results["import_refused"][:10]:
            print(f"  ? {item}")

    if results["cleared"]:
        print("\n--- Cleared ---")
        for item in results["cleared"][:10]:
            print(f"  = {item}")

    if results["deleted"]:
        print("\n--- Deleted ---")
        for item in results["deleted"][:10]:
            print(f"  - {item}")

    if action_unknown:
        print("\n--- UNKNOWN — Agent Review Needed ---")
        for rid, title, reason in action_unknown[:15]:
            print(f"  ? [{rid}] {title[:55]}")
            print(f"    Reason: {reason[:100]}")

    # Count what actually happened, not what was attempted: action_import is
    # the capped work list, and an entry in it can still fail and be deleted
    # instead. A live run on 2026-09-21 reported "500 imported" while the
    # summary above it correctly said 499 imported and 1 deleted.
    imported = len(results["imported"]) + len(results["artist_resolved"])
    deleted = len(results["deleted"]) + len(results["import_failed"])
    print(f"\nDone. {deleted} deleted, {len(results['cleared'])} cleared and {imported} imported "
          f"out of a {total}-record queue.")

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

    # === PHASE 4: Clean up unmapped track files ===
    if cfg["unmapped_files_cleaner_enabled"]:
        print(f"\n{'='*60}")
        print("PHASE 4: Cleaning up unmapped track files...")
        print(f"{'='*60}")
        clean_unmapped_files(dict(cfg, unmapped_files_dry_run=True) if DRY_RUN else cfg)

    if duplicate_artists:
        print(f"\n[AGENT_OVERSIGHT_NEEDED] {len(duplicate_artists)} duplicate artist name(s) "
              f"block auto-import; merge or remove the spare artist in Lidarr")
        for key, count in sorted(duplicate_artists.items(), key=lambda kv: -kv[1]):
            parts = []
            for fid, name in key:
                a = artists_by_fid.get(fid) or {}
                files = (a.get("statistics") or {}).get("trackFileCount") or 0
                parts.append(f"artistId={a.get('id', '?')} {fid} ({files} files)")
            print(f"[OVERSIGHT] duplicate artist '{key[0][1]}' | {' vs '.join(parts)} | {count} queue item(s)")

    # Signal edge cases for agent oversight
    total_oversight = len(action_unknown) + len(action_skip)
    if total_oversight:
        print(f"\n[AGENT_OVERSIGHT_NEEDED] {total_oversight} items need review")
        for rid, title, reason in (action_unknown + action_skip)[:10]:
            print(f"[OVERSIGHT] id={rid} | {title[:50]} | {reason[:80]}")


if __name__ == "__main__":
    main()
