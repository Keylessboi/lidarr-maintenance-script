#!/usr/bin/env python3
"""Classification and pagination tests for lidarr_queue_maintenance.py.

Pure logic only: no Lidarr instance, no network, no mutations. Run with
`python3 tests/test_classification.py`.

These exist because the bug they cover was silent. The stale-download rule sat
below an early return, so it could never fire on the records it was written for,
and every nightly run still reported success.
"""
import importlib.util
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "lidarr_queue_maintenance.py")

spec = importlib.util.spec_from_file_location("lq", SCRIPT)
lq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lq)

NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
UTC = timezone.utc
FAILURES = []


def record(**over):
    r = {
        "id": 1,
        "title": "Test Album [FLAC 16bit]",
        "trackedDownloadState": "downloading",
        "statusMessages": [],
        "added": (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "downloadId": "abc",
        "albumId": 42,
        "downloadClient": "qBittorrent",
        "status": "downloading",
    }
    r.update(over)
    return r


def check(name, rec, expected):
    res = lq.classify_record(rec, NOW, UTC)
    got = res[0] if res else None
    if got != expected:
        FAILURES.append("%s: expected %r, got %r" % (name, expected, got))
        print("  FAIL %s: expected %r, got %r" % (name, expected, got))
    else:
        print("  ok   %-44s -> %s" % (name, got))


def test_stale():
    # The regression itself: a stalled download carries NO status messages,
    # and the stale rule has to be reachable for it.
    check("stalled, no messages, paused", record(status="paused"), "delete")
    check("stalled, no messages, downloading", record(status="downloading"), "delete")
    check("young paused, not stale", record(
        status="paused", added=(NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")), None)


def test_queued_is_not_stale():
    # A torrent waiting for a download slot is a backlog, not a fault. Deleting
    # these was the whole danger of the naive fix (1,289 records, each one
    # firing an AlbumSearch).
    check("queued is not stale", record(status="queued"), None)
    check("delayed is not stale", record(status="delay"), None)


def test_retrying():
    retry = "Some files failed. Retrying download"
    old = record(status="queued", downloadClient="Slskd2", errorMessage=retry)
    young = record(status="queued", downloadClient="Slskd2", errorMessage=retry,
                   added=(NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    check("slskd retrying past its window", old, "delete")
    check("slskd retrying within its window", young, None)


def test_import_and_keywords():
    failed = record(trackedDownloadState="importFailed", status="completed")
    failed["statusMessages"] = [{"messages": ["Has missing tracks"]}]
    check("importFailed + delete keyword", failed, "delete")
    usable = record(trackedDownloadState="importFailed", status="completed")
    usable["statusMessages"] = [{"messages": ["Has unmatched tracks"]}]
    check("importFailed + import keyword", usable, "import")


def test_pagination():
    pages = {
        1: {"records": [{"id": 1}, {"id": 2}], "totalRecords": 5},
        2: {"records": [{"id": 3}, {"id": 4}], "totalRecords": 5},
        3: {"records": [{"id": 5}], "totalRecords": 5},
    }
    seen = []

    def fake(path, params=None, timeout=None):
        seen.append(params["page"])
        return pages[params["page"]]

    real = lq.api_get
    lq.api_get = fake
    try:
        recs, err = lq.fetch_queue({"queue_page_size": 2})
    finally:
        lq.api_get = real
    if err is not None or len(recs) != 5 or seen != [1, 2, 3]:
        FAILURES.append("pagination: err=%r n=%d pages=%r" % (err, len(recs), seen))
        print("  FAIL pagination: err=%r n=%d pages=%r" % (err, len(recs), seen))
    else:
        print("  ok   %-44s -> walked pages %r, got 5" % ("pagination", seen))

    # An empty page ends the walk even when totalRecords overstates the queue.
    lq.api_get = lambda path, params=None, timeout=None: {"records": [], "totalRecords": 99}
    try:
        recs, err = lq.fetch_queue({"queue_page_size": 2})
    finally:
        lq.api_get = real
    if recs != [] or err is not None:
        FAILURES.append("pagination empty page: recs=%r err=%r" % (recs, err))
        print("  FAIL pagination stops on an empty page")
    else:
        print("  ok   %-44s -> stops on an empty page" % "pagination")

    # An API error must surface, not be read as "the queue is empty".
    lq.api_get = lambda path, params=None, timeout=None: {"error": "boom"}
    try:
        recs, err = lq.fetch_queue({"queue_page_size": 2})
    finally:
        lq.api_get = real
    if recs is not None or err != "boom":
        FAILURES.append("pagination error propagation: recs=%r err=%r" % (recs, err))
        print("  FAIL pagination error propagation")
    else:
        print("  ok   %-44s -> surfaces the API error" % "pagination")


if __name__ == "__main__":
    for fn in (test_stale, test_queued_is_not_stale, test_retrying,
               test_import_and_keywords, test_pagination):
        fn()
    if FAILURES:
        print("\n%d failure(s):" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
        raise SystemExit(1)
    print("\nAll tests passed.")