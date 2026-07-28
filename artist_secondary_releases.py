#!/usr/bin/env python3
"""
Find and add secondary/featured releases of artists to Lidarr.

Finds all releases where an artist appears but is not the primary
artist credit (featured appearances, collaborations, compilations)
and adds them to Lidarr.

WARNING: Running --all-artists on a large library will add THOUSANDS of
compilation tracks and take HOURS due to MusicBrainz API rate limiting
(~1s/call, ~26 calls per artist). For 500 artists, expect ~4-6 hours.
Most users want to opt out of this — target individual artists by their
MusicBrainz ID instead, or use --dry-run --max-per-artist 1 to preview.

Requires: pip install requests musicbrainzngs

Based on: https://github.com/jasonpatrickellykrause/lidarrscripts
"""
import sys, argparse, musicbrainzngs, requests, time, os
from typing import List, Dict

musicbrainzngs.set_useragent(
    "LidarrSecondaryReleases", "1.0",
    "https://github.com/Keylessboi/lidarr-maintenance-script"
)

API_KEY = os.environ.get("LIDARR_API_KEY", "")
BASE_URL = os.environ.get("LIDARR_URL", "")
MB_SLEEP = 1.0  # seconds between MusicBrainz API calls (rate limiting)


def get_artist_releases(artist_mbid: str) -> List[Dict]:
    """Fetch all releases for a MusicBrainz artist."""
    try:
        result = musicbrainzngs.get_artist_by_id(
            artist_mbid, includes=["release-rels", "releases"]
        )
        artist = result["artist"]
        releases = []
        if "release-rel-list" in artist:
            for rel in artist["release-rel-list"]:
                if "release" in rel:
                    r = rel["release"]
                    releases.append({"id": r.get("id"), "title": r.get("title")})
        if "release-list" in artist:
            for r in artist["release-list"]:
                if r.get("id") not in [x["id"] for x in releases]:
                    releases.append({"id": r.get("id"), "title": r.get("title")})
        return releases
    except Exception as e:
        print(f"    MusicBrainz error: {e}")
        return []


def is_secondary(artist_mbid: str, release_id: str) -> bool:
    """Check if the artist is NOT the primary artist on a release."""
    try:
        result = musicbrainzngs.get_release_by_id(
            release_id, includes=["artist-credits"]
        )
        release = result["release"]
        if "artist-credit" not in release or not release["artist-credit"]:
            return False
        first = release["artist-credit"][0]
        if isinstance(first, dict) and "artist" in first:
            return first["artist"].get("id") != artist_mbid
        return False
    except:
        return False


def lidarr_get(path):
    r = requests.get(
        f"{BASE_URL}/api/v1/{path}",
        headers={"X-Api-Key": API_KEY}, timeout=30
    )
    r.raise_for_status()
    return r.json()


def process_artist(mbid: str, name: str, root: str, qp: int,
                   max_per: int, dry_run: bool, depth: int = 0) -> dict:
    """Process a single artist: find secondary releases and add them."""
    prefix = "  " * depth
    print(f"{prefix}{name} ({mbid})...", end="", flush=True)

    releases = get_artist_releases(mbid)
    if not releases:
        print(" no releases found")
        return {"added": 0, "skipped": 0, "total": 0}

    # Filter for secondary releases
    secondary = []
    for r in releases:
        rid = r.get("id")
        if rid and is_secondary(mbid, rid):
            secondary.append(r)
        time.sleep(MB_SLEEP)

    if not secondary:
        print(f" {len(releases)} releases, 0 secondary")
        return {"added": 0, "skipped": 0, "total": len(releases)}

    if max_per and len(secondary) > max_per:
        secondary = secondary[:max_per]

    print(f" {len(releases)} releases, {len(secondary)} secondary", end="")

    if dry_run:
        print(f" (would add {len(secondary)})")
        return {"added": len(secondary), "skipped": 0, "total": len(releases)}

    added = 0
    skipped = 0
    exists = 0
    for r in secondary:
        rid = r["id"]
        try:
            search = lidarr_get(f"search?term=lidarr:{rid}")
            if not search:
                skipped += 1
                continue
            result = search[0]
            album = result.get("album") or result
            artist = result.get("artist") or {}
            payload = {
                "foreignAlbumId": rid,
                "title": album.get("title", r.get("title", "?")),
                "monitored": False,
                "anyReleaseOk": True,
                "profileId": qp,
                "artist": artist,
                "addOptions": {"searchForNewAlbum": False},
            }
            resp = requests.post(
                f"{BASE_URL}/api/v1/album",
                headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"},
                json=payload, timeout=30
            )
            if resp.ok:
                added += 1
            elif resp.status_code == 400:
                exists += 1  # Already in Lidarr
            else:
                skipped += 1
        except Exception:
            skipped += 1

    print(f" → +{added} added, {exists} already exist, {skipped} skipped")
    return {"added": added, "skipped": skipped, "exists": exists, "total": len(releases)}


def main():
    parser = argparse.ArgumentParser(
        description="Add secondary releases of artists to Lidarr"
    )
    parser.add_argument(
        "artist_mbid", nargs="?",
        help="MusicBrainz artist ID (omit with --all-artists)"
    )
    parser.add_argument(
        "--all-artists", action="store_true",
        help="Run on ALL artists in Lidarr. "
             "WARNING: This adds compilation tracks for every artist. "
             "Use --dry-run first."
    )
    parser.add_argument(
        "--max-per-artist", type=int, default=10,
        help="Max secondary releases to add per artist (default: 10, 0=unlimited)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview only — don't add anything"
    )
    parser.add_argument(
        "--quality-profile", type=int,
        help="Quality profile ID (default: first)"
    )
    parser.add_argument(
        "--root-folder",
        help="Root folder (default: first)"
    )
    args = parser.parse_args()

    if not API_KEY or not BASE_URL:
        print("Set LIDARR_URL and LIDARR_API_KEY env vars.")
        sys.exit(1)

    if not args.all_artists and not args.artist_mbid:
        parser.error("Provide an artist MBID or use --all-artists")

    root = args.root_folder or lidarr_get("rootfolder")[0]["path"]
    qp = args.quality_profile or lidarr_get("qualityprofile")[0]["id"]

    # Single artist mode
    if args.artist_mbid:
        result = process_artist(
            args.artist_mbid, args.artist_mbid,
            root, qp, args.max_per_artist, args.dry_run
        )
        print(f"\nAdded: {result['added']} | Skipped: {result['skipped']}")
        return

    # --all-artists mode
    if args.dry_run:
        print("\n⚠️  DRY RUN — no changes will be made")
    else:
        print("\n⚠️  Adding secondary releases for ALL artists.")
        print("   Albums are added UNMONITORED (not searched).")
        print("   Press Ctrl+C within 5s to abort...")
        time.sleep(5)

    artists = lidarr_get("artist")
    artist_count = len(artists)
    # Estimate: each artist does ~25+1 MB API calls at MB_SLEEP sec each + overhead
    est_releases_per_artist = 26  # ~25 releases + 1 secondary check per release
    est_seconds = artist_count * (est_releases_per_artist * MB_SLEEP + 2)
    est_hours = est_seconds / 3600
    print(f"Processing {artist_count} artists from Lidarr...")
    print(f"  Estimated time: ~{est_hours:.1f} hours ({est_seconds:.0f}s)")
    print(f"  Due to MusicBrainz API rate limiting (~{MB_SLEEP}s between calls)")
    print()

    totals = {"added": 0, "skipped": 0, "exists": 0}
    for i, artist in enumerate(artists, 1):
        mbid = artist.get("foreignArtistId")
        name = artist.get("artistName", "?")
        if not mbid:
            continue

        print(f"[{i}/{len(artists)}] ", end="")
        result = process_artist(
            mbid, name, root, qp,
            args.max_per_artist, args.dry_run
        )
        totals["added"] += result["added"]
        totals["skipped"] += result["skipped"]
        totals["exists"] += result.get("exists", 0)
        time.sleep(MB_SLEEP)

    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    print(f"  Artists processed: {len(artists)}")
    print(f"  Secondary releases found: {totals['added'] + totals['exists'] + totals['skipped']}")
    print(f"  Added: {totals['added']}")
    print(f"  Already in library: {totals['exists']}")
    print(f"  Failed: {totals['skipped']}")
    if args.dry_run:
        print(f"  (dry run — nothing was actually added)")
    print(f"\n  Note: Secondary releases were added UNMONITORED.")
    print(f"  They won't be searched/downloaded unless you manually monitor them.")


if __name__ == "__main__":
    main()
