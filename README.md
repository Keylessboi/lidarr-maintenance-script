# Lidarr Queue Maintenance

> **Disclaimer:** This script was generated entirely by **DeepSeek V4 Flash** (via Hermes Agent) — an AI language model. It was not written by a human. Verify before running in production.

[![CI](https://github.com/Keylessboi/lidarr-maintenance-script/actions/workflows/ci.yml/badge.svg)](https://github.com/Keylessboi/lidarr-maintenance-script/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![GitHub last commit](https://img.shields.io/github/last-commit/Keylessboi/lidarr-maintenance-script)](https://github.com/Keylessboi/lidarr-maintenance-script/commits/main)

Automated Lidarr queue cleanup with agentic oversight. Runs daily at 2 AM via cron — handles stuck imports, deletes stale downloads, force-imports borderline matches, and flags problem albums for review.

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Setup](#setup)
- [Configuration Reference](#configuration-reference)
  - [Thresholds](#thresholds)
  - [Action Lists](#action-lists)
  - [All Known Lidarr Import Error Messages](#all-known-lidarr-import-error-messages)
- [Decision Flow](#decision-flow)
- [Customization Examples](#customization-examples)
  - [Change the match threshold for force import](#change-the-match-threshold-for-force-import)
  - [Move "not an upgrade" items to delete instead of import](#move-not-an-upgrade-items-to-delete-instead-of-import)
  - [Flag "has unmatched tracks" for agent review](#flag-has-unmatched-tracks-for-agent-review)
  - [Add a new error pattern](#add-a-new-error-pattern)
  - [Stop force-importing completely](#stop-force-importing-completely)
- [Agentic Oversight](#agentic-oversight)
- [Auto-Deploy](#auto-deploy)
- [Files](#files)
- [License](#license)

---

## Features

| Feature | Description |
|---------|-------------|
| **Force import** | Albums that failed auto-import but should still work — re-attempts via Lidarr's manual import API with `move` mode |
| **Delete + re-search** | Genuinely broken downloads are removed from queue and Lidarr re-searches for a better copy |
| **Agent oversight** | Ambiguous or low-confidence items get flagged with `[AGENT_OVERSIGHT_NEEDED]` for human/AI review |
| **Stalled download cleanup** | qBittorrent/Soulseek/YouTube downloads stuck for N+ days are removed and re-searched |
| **Missing album scan** | Scans the oldest N missing albums for ones that have been repeatedly searched but never grabbed (likely naming/indexer issues) |
| **Unmapped files cleanup** | Deletes orphaned track files that are no longer linked to any album in Lidarr |
| **Tubifarry integration** | Per-download-client config for Slskd2/Soulseek, YouTube, Lucida — handles retrying downloads, lower match thresholds, stale timeout overrides |
| **Config-driven logic** | All error pattern matching is controlled by a `CONFIG` dict at the top of the script — move keywords between lists to change behavior without touching logic |
| **Direct Lidarr API** | No MCP server dependency — talks directly to Lidarr's REST API |

---

## Requirements

- Python 3.10+
- Lidarr instance with API access
- Lidarr API key and URL

---

## Setup

### 1. Configure environment

The script can read credentials from environment variables or from the arr-mcp `.env` file:

```bash
export LIDARR_URL="http://192.168.1.67:8686"
export LIDARR_API_KEY="your-api-key-here"
```

Or leave the arr-mcp `.env` at `/opt/projects/lidarr-mcp/arr-mcp/.env` and the script will pick it up automatically.

### 2. Test it

```bash
python3 lidarr_queue_maintenance.py
```

### 3. Schedule it (via Hermes cron)

```bash
hermes cron create \
  --name daily-lidarr-maintenance \
  --schedule "0 2 * * *" \
  --script lidarr_queue_maintenance.py \
  --prompt "Review the output. If items are flagged for agent oversight, decide what to do with them."
```

Or update an existing cron:

```bash
hermes cron update <job-id> --script lidarr_queue_maintenance.py
```

The `run_maintenance.sh` wrapper auto-pulls from git before running, so updates are synced automatically:

```bash
hermes cron update <job-id> --script run_maintenance.sh
```

---

## Configuration Reference

All behavior is controlled by the `CONFIG` dictionary at the top of `lidarr_queue_maintenance.py`. You do **not** need to modify any logic below it — just edit the dict.

```python
CONFIG = {
    # ── Thresholds ──
    "match_import_min": 30,          # match % >= this → try force import
    "match_oversight_max": 30,       # match % < this → flag for agent oversight
    "stale_download_days": 14,       # downloads stuck this many days → delete + re-search
    "queue_page_size": 500,          # how many queue records to fetch per page
    "missing_album_scan_count": 10,  # how many oldest missing albums to check per run
    "missing_search_threshold": 2,   # searches >= this + zero grabs → flag problematic

    # ── Action: FORCE IMPORT ──
    "import_keywords": [              # items where the files probably exist and just need a nudge
        "Not an upgrade for existing",
        "Album already imported",
        "Failed to import track, Destination already exists",
        "Has unmatched tracks",
        "could not find similar album",
    ],

    # ── Action: IMPORT IF MATCH % >= threshold ──
    "import_if_match_keywords": [     # items rejected by match-scoring (contains % info)
        "Album match",
        "Worst track match",
        "Track match is not close enough",
    ],

    # ── Action: DELETE + RE-SEARCH ──
    "delete_keywords": [              # items where the download was genuinely wrong
        "Has missing tracks",
        "Has fewer tracks than existing release",
        "One or more tracks expected",
    ],

    # ── Action: FLAG FOR AGENT OVERSIGHT ──
    "oversight_keywords": [],         # patterns needing human/AI judgement
}
```

### Thresholds

| Setting | Default | What it does |
|---------|---------|-------------|
| `match_import_min` | `30` | Minimum match percentage (0-100). Items with Album match / Worst track match >= this value get force-imported. Below this → flagged for oversight. |
| `match_oversight_max` | `30` | Matches `match_import_min` by default. Items below this go to the oversight bucket. |
| `stale_download_days` | `14` | Days a download can sit in "downloading" state before it's considered stalled and gets deleted + re-searched. |
| `queue_page_size` | `500` | How many queue records to fetch from Lidarr per API call. Lower = smaller JSON payloads. Higher = fewer pages to fetch. |
| `missing_album_scan_count` | `10` | How many of the oldest missing albums to check per run. Kept low to avoid API rate issues. |
| `missing_search_threshold` | `2` | If a missing album has been searched this many times with zero successful grabs, flag it as a potential naming issue. |

### Tubifarry Integration

[Tubifarry](https://github.com/TypNull/Tubifarry) is a Lidarr plugin that adds download sources beyond traditional indexers. It registers custom download clients in Lidarr — this script handles each one with specific behavior:

| Download Client | Source | Stale days | Match min % | Retrying detection |
|----------------|--------|-----------|-------------|--------------------|
| `Slskd2` | Soulseek (via slskd) | 14 | 20% | `"Some files failed. Retrying download"` → delete after 14d |
| `Youtube` | YouTube audio (via yt-dlp) | 3 | 15% | — |
| `Lucida` | Multi-source web client | 7 | 20% | — |
| Any other client | — | 14 (global default) | 30% (global default) | — |

**Slskd2 / Soulseek**: Soulseek downloads can stall when the peer goes offline. Lidarr reports this with `errorMessage: "Some files failed. Retrying download..."`. The script detects this specific pattern and if the download has been retrying for 14+ days (configurable via `retrying_delete_days`), it removes it from the queue and triggers a new search. Fresh retrying downloads (<14d) are left alone to give the peer time to come back.

**YouTube**: YouTube audio extraction should finish quickly — if a YouTube download is stuck for more than 3 days, it's likely dead. YouTube audio is often lower quality, so the match % threshold for force import is lowered to 15%.

**Lucida**: Multi-source web downloader. 7-day stale threshold with 20% match minimum.

If you add more Tubifarry web clients (DABmusic, T2Tunes, Subsonic, etc.), just add an entry to `client_overrides`:

```python
"DABmusic": {
    "stale_download_days": 7,
    "match_import_min": 25,
},
```

### Client Overrides

You can set different thresholds per download client (**see [Tubifarry Integration](#tubifarry-integration) above for the built-in values**). This is useful because different sources have different behavior:

| Setting | What it does |
|---------|-------------|
| `stale_download_days` | Override the global stale threshold for this client |
| `match_import_min` | Override the match % import threshold (lower for low-quality sources like YouTube) |
| `retrying_message` | If set, the script checks `errorMessage` for this pattern to detect "retrying" state |
| `retrying_delete_days` | Delete if retrying for this many days (separate from stale) |

To add or modify a client, edit the `client_overrides` dict:

```python
"client_overrides": {
    "Slskd2": {
        "stale_download_days": 14,
        "match_import_min": 20,
        "retrying_message": "Some files failed. Retrying download",
        "retrying_delete_days": 14,
    },
    "Youtube": {
        "stale_download_days": 3,
        "match_import_min": 15,
    },
    "MyCustomClient": {
        "stale_download_days": 7,
        "match_import_min": 25,
    },
},
```

If a client is not listed, global thresholds apply and retrying detection is disabled.

### Action Lists

There are four action lists, applied in priority order:

| List | Action taken |
|------|-------------|
| `import_keywords` | Force import via Lidarr's manual import API. If the import API returns no files, falls back to delete + re-search. |
| `import_if_match_keywords` | Extracts the match percentage from the error message. If >= `match_import_min`, force imports. Otherwise, flags for oversight. |
| `delete_keywords` | Deletes the queue item, optionally removing from the download client, and triggers `AlbumSearch` command for a re-search. |
| `oversight_keywords` | No action taken. The item is printed with an `[AGENT_OVERSIGHT_NEEDED]` marker for human/AI review. |

**Priority order matters.** Keywords in `import_keywords` are checked first, then `import_if_match_keywords`, then `delete_keywords`, then `oversight_keywords`. The first match wins. If no list matches, the item goes to "unknown" (also flagged for oversight).

The `sm_str` (stringified status messages) is matched with a simple `if kw in sm_str` check. Substring matching means partial matches work — `"Album match"` will match `"Album match is not close enough: 74.5 % vs 80 %"`.

### All Known Lidarr Import Error Messages

These are all the rejection messages Lidarr can produce during auto-import, sourced from [Lidarr's source code](https://github.com/Lidarr/Lidarr/tree/develop/src/NzbDrone.Core/MediaFiles/TrackImport/Specifications).

| Error message (substring match) | Source file | Default action |
|--------------------------------|-------------|---------------|
| `"Not an upgrade for existing"` | `UpgradeSpecification.cs` — Quality is same or worse than what's on disk | **Import** — files are valid, just not better quality |
| `"Album already imported"` | `AlreadyImportedSpecification.cs` — Album was already imported before | **Import** — already in library, just stuck in queue |
| `"Failed to import track, Destination already exists"` | File system / import engine — destination file exists | **Import** — file is there, just needs queue cleanup |
| `"Has unmatched tracks"` | `NoMissingOrUnmatchedTracksSpecification.cs` — Extra files Lidarr couldn't match | **Import** — extra tracks are usually valid bonus content |
| `"could not find similar album"` | Folder/name resolution — Lidarr couldn't match path to an album | **Import** — agent should verify, but files likely belong |
| `"Album match"` | `CloseAlbumMatchSpecification.cs` — Album-level match score too low | **Import if >= 30%** — threshold is configurable |
| `"Worst track match"` | `CloseAlbumMatchSpecification.cs` — Worst individual track match too low | **Import if >= 30%** — threshold is configurable |
| `"Track match is not close enough"` | `CloseTrackMatchSpecification.cs` — Individual track match too low | **Import if >= 30%** — threshold is configurable |
| `"Has missing tracks"` | `NoMissingOrUnmatchedTracksSpecification.cs` — MusicBrainz has tracks not in this release | **Delete + re-search** — download is incomplete |
| `"Has fewer tracks than existing release"` | `MoreTracksSpecification.cs` — Fewer tracks than what's already imported | **Delete + re-search** — worse than what you have |
| `"One or more tracks expected"` | Generic wrapper (always appears with another reason) | **Delete + re-search** — when no specific reason exists underneath |
| `"Track file on disk contains more tracks than this file contains"` | `SameTracksImportSpecification.cs` | **Undefined** — not seen in wild; add to a list if encountered |
| `"No tracks matched"` | `CloseAlbumMatchSpecification.cs` — Zero tracks matched the album | **Undefined** — not seen in wild; add to a list if encountered |
| `"Album release not requested"` | `ReleaseWantedSpecification.cs` — Release isn't wanted | **Undefined** — not seen in wild; add to a list if encountered |

**Messages not yet assigned to a list** (`"Track file on disk contains more tracks"`, `"No tracks matched"`, `"Album release not requested"`) will appear as "unknown" and be flagged for agent oversight. If you encounter them, add them to the appropriate list in `CONFIG`.

Messages are checked **in order** within each list. If an item's status messages match multiple patterns, the first list wins.

---

## Decision Flow

```
Queue item
├── Has errorMessage matching client retrying_pattern?
│   └── Yes + stale > retrying_delete_days → DELETE + re-search (retrying)
├── Has statusMessages?
│   └── No → skip (active download, no issues)
├── downloading + stale > N days (per-client)?
│   └── Yes → DELETE + re-search (stalled)
├── importFailed?
│   ├── Matches import_keywords? → FORCE IMPORT
│   ├── Matches import_if_match_keywords?
│   │   ├── Match % >= threshold (per-client) → FORCE IMPORT
│   │   └── Match % < threshold → FLAG FOR OVERSIGHT
│   ├── Matches delete_keywords? → DELETE + re-search
│   ├── Matches oversight_keywords? → FLAG FOR OVERSIGHT
│   └── No match → FLAG FOR OVERSIGHT (unknown)
└── No statusMessages → skip
```

After queue processing:

```
Phase 3: Continuously Missing Albums
├── Fetch oldest N missing (by releaseDate)
├── For each: check search history + grab history
│   ├── Searches >= threshold AND grabs == 0?
│   │   └── Yes → print with [AGENT_OVERSIGHT_NEEDED]
│   └── No → skip
└── Report findings
```

---

## Customization Examples

### Change the match threshold for force import

```python
"match_import_min": 50,  # Only force-import if match >= 50%
```

Now items with 30-49% match will go to oversight instead of auto-import.

### Move "not an upgrade" items to delete instead of import

Move `"Not an upgrade for existing"` from `import_keywords` to `delete_keywords`:

```python
"import_keywords": [
    # "Not an upgrade for existing",   <-- remove from here
    "Album already imported",
    "Failed to import track, Destination already exists",
    "Has unmatched tracks",
    "could not find similar album",
],

"delete_keywords": [
    "Not an upgrade for existing",    <-- add here
    "Has missing tracks",
    "Has fewer tracks than existing release",
    "One or more tracks expected",
],
```

### Flag "has unmatched tracks" for agent review

Move it to `oversight_keywords`:

```python
"import_keywords": [
    "Not an upgrade for existing",
    "Album already imported",
    "Failed to import track, Destination already exists",
    # "Has unmatched tracks",  <-- remove from here
    "could not find similar album",
],

"oversight_keywords": [
    "Has unmatched tracks",    <-- add here
],
```

### Add a new error pattern

If you discover a new error message in your queue, just add a substring of it to the right list:

```python
"delete_keywords": [
    "Has missing tracks",
    "Has fewer tracks than existing release",
    "One or more tracks expected",
    "Track file on disk contains more tracks",  # <-- new pattern
],
```

The script uses simple `if kw in sm_str` matching, so a short unique substring is all you need.

### Stop force-importing completely

Empty the import lists:

```python
"import_keywords": [],
"import_if_match_keywords": [],
```

Now everything will either be deleted, flagged for oversight, or reported as unknown. You'll manually review every import failure.

---

## Agentic Oversight

The script outputs `[AGENT_OVERSIGHT_NEEDED]` markers for items that need human (or AI agent) judgement:

```
[AGENT_OVERSIGHT_NEEDED] 3 items need review
[OVERSIGHT] id=699476194 | John Lennon - Anthology (1998) [FLAC]... | match 30.5%
[OVERSIGHT] id=17016522 | Ravi Shankar - Sitar (1989) [FLAC]...   | could not find similar album
[OVERSIGHT] id=400023700 | NIRVANA (1996) Outcesticide IV ...     | no matching album
```

When used with a Hermes cron job, the agent sees these markers and can decide:
- **Low match %** — Check if the album is correct. If yes, try a manual import via the Lidarr API.
- **No matching album** — The folder structure doesn't match any known album. Figure out which album the files belong to and manually trigger an import.
- **Continuously missing** — Album has been searched multiple times with zero grabs. Investigate the name — it might have weird characters, wrong MusicBrainz ID, or be unavailable on any indexer.

---

## Auto-Deploy

The `run_maintenance.sh` wrapper pulls the latest from git before running, so pushing to `main` auto-deploys on the next cron tick:

```bash
hermes cron update <job-id> --script run_maintenance.sh
```

For instant deploys, set up a webhook listener:

```bash
pip install flask
cat > ~/webhook.py << 'EOF'
from flask import Flask, request
import subprocess, os

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    subprocess.run(["git", "-C", os.path.expanduser("~/.hermes/scripts"), "pull"])
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8765)
EOF
```

Then add the server's URL as `WEBHOOK_URL` in the repo's GitHub secrets and enable the deploy workflow.

---

## Files

```
lidarr_queue_maintenance.py   # Main script — the only file you need
run_maintenance.sh            # Wrapper that git-pulls then runs the script
tests/test_script.py          # Smoke tests
.github/workflows/ci.yml      # Lint + syntax check on push
.github/workflows/deploy.yml  # Optional auto-deploy via webhook
.github/ISSUE_TEMPLATE/       # Bug report and feature request templates
.github/PULL_REQUEST_TEMPLATE.md
CONTRIBUTING.md               # Contributing guide
SECURITY.md                   # Security policy
LICENSE                       # MIT
.editorconfig                 # Editor settings
README.md                     # This file
```

---

## License

MIT
