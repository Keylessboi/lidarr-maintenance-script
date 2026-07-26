#!/usr/bin/env python3
"""
Find and add secondary/featured releases of a MusicBrainz artist to Lidarr.

Finds all releases where a specified artist appears but is not the primary
artist credit (featured appearances, collaborations, compilations) and adds
them to Lidarr.

Requires: pip install requests musicbrainzngs

Based on: https://github.com/jasonpatrickellykrause/lidarrscripts
"""
import sys, argparse, musicbrainzngs, requests, time, os, json
from typing import List, Dict, Optional

musicbrainzngs.set_useragent("LidarrSecondaryReleases", "1.0", "https://github.com/Keylessboi/lidarr-maintenance-script")

API_KEY = os.environ.get("LIDARR_API_KEY", "")
BASE_URL = os.environ.get("LIDARR_URL", "")


def get_artist_releases(artist_mbid: str) -> List[Dict]:
    try:
        result = musicbrainzngs.get_artist_by_id(artist_mbid, includes=["release-rels", "releases"])
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
        print(f"MusicBrainz error: {e}")
        return []


def is_secondary(artist_mbid: str, release_id: str) -> bool:
    try:
        result = musicbrainzngs.get_release_by_id(release_id, includes=["artist-credits"])
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
    r = requests.get(f"{BASE_URL}/api/v1/{path}", headers={"X-Api-Key": API_KEY}, timeout=30)
    r.raise_for_status()
    return r.json()


def lidarr_post(path, data):
    r = requests.post(f"{BASE_URL}/api/v1/{path}", headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"}, json=data, timeout=30)
    return r.ok


def main():
    parser = argparse.ArgumentParser(description="Add secondary releases of a MusicBrainz artist to Lidarr")
    parser.add_argument("artist_mbid", help="MusicBrainz artist ID")
    parser.add_argument("--max", type=int, default=None, help="Max releases to add")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--quality-profile", type=int, help="Quality profile ID (default: first)")
    parser.add_argument("--metadata-profile", type=int, help="Metadata profile ID (default: first)")
    parser.add_argument("--root-folder", help="Root folder (default: first)")
    args = parser.parse_args()

    if not API_KEY or not BASE_URL:
        print("Set LIDARR_URL and LIDARR_API_KEY environment variables.")
        sys.exit(1)

    print(f"Fetching releases for {args.artist_mbid}...")
    releases = get_artist_releases(args.artist_mbid)
    print(f"Found {len(releases)} total releases.")

    secondary = []
    for r in releases:
        rid = r.get("id")
        if rid and is_secondary(args.artist_mbid, rid):
            secondary.append(r)
    print(f"Secondary releases: {len(secondary)}")

    if args.max and len(secondary) > args.max:
        secondary = secondary[:args.max]

    if not secondary:
        print("No secondary releases to add.")
        return

    root = args.root_folder or lidarr_get("rootfolder")[0]["path"]
    qp = args.quality_profile or lidarr_get("qualityprofile")[0]["id"]
    mp = args.metadata_profile or lidarr_get("metadataprofile")[0]["id"]

    added = 0
    for r in secondary:
        rid = r["id"]
        title = r.get("title", "?")
        print(f"  [{rid}] {title[:60]}", end="")
        if args.dry_run:
            print(" (would add)")
            added += 1
            continue

        try:
            search = lidarr_get(f"search?term=lidarr:{rid}")
            if not search:
                print(" → not found in Lidarr")
                continue
            result = search[0]
            album = result.get("album") or result
            artist = result.get("artist") or {}

            payload = {
                "foreignAlbumId": rid,
                "title": album.get("title", title),
                "monitored": True,
                "anyReleaseOk": True,
                "profileId": qp,
                "artist": artist,
                "addOptions": {"searchForNewAlbum": False},
            }
            ok = requests.post(
                f"{BASE_URL}/api/v1/album",
                headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"},
                json=payload, timeout=30
            ).ok
            print(f" → {'added' if ok else 'exists'}")
            added += 1
        except Exception as e:
            print(f" → error: {e}")

    print(f"\nAdded: {added}")


if __name__ == "__main__":
    main()
