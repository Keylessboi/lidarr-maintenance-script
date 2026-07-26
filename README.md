# Lidarr Queue Maintenance

> **Disclaimer:** This script was generated entirely by **DeepSeek V4 Flash** (via Hermes Agent) — an AI language model. It was not written by a human. Verify before running in production.

Automated Lidarr queue cleanup with agentic oversight. Runs daily at 2 AM via cron — handles stuck imports, deletes stale downloads, force-imports borderline matches, and flags problem albums for review.

## Features

| Feature | Description |
|---------|-------------|
| **Force import** | Albums with match >= 30%, unmatched tracks, "not an upgrade", "already imported", or "destination exists" |
| **Delete + re-search** | Albums with missing tracks, fewer tracks, generic import failures, or stalled downloads (>14 days) |
| **Agent oversight** | Albums with match < 30%, "could not find similar album", or unknown errors get flagged for human (or AI agent) review |
| **Stalled download cleanup** | qBittorrent/Soulseek downloads stuck for 14+ days are removed and re-searched |
| **Missing album scan** | Checks the oldest 100 missing albums for ones that have been repeatedly searched but never grabbed — likely naming/indexer issues |
| **Direct Lidarr API** | No MCP server dependency — talks directly to Lidarr's REST API |

## Requirements

- Python 3.10+
- Lidarr instance with API access
- Lidarr API key and URL

## Setup

### 1. Configure environment

The script can read credentials from environment variables or from the arr-mcp `.env` file:

```bash
export LIDARR_URL="http://192.168.1.67:8686"
export LIDARR_API_KEY="your-api-key-here"
```

Or just leave the arr-mcp `.env` at `/opt/projects/lidarr-mcp/arr-mcp/.env` and the script will pick it up automatically.

### 2. Test it

```bash
python3 lidarr_queue_maintenance.py
```

### 3. Schedule it (via Hermes cron)

```bash
hermes cron create \\
  --name daily-lidarr-maintenance \\
  --schedule "0 2 * * *" \\
  --script lidarr_queue_maintenance.py \\
  --prompt "Review the output. If items are flagged for agent oversight, decide what to do with them."
```

Or add it to the existing cron:

```bash
hermes cron update <job-id> \\
  --script lidarr_queue_maintenance.py
```

### 4. Auto-deploy (optional)

Set up a webhook so the server pulls updates automatically after each push:

```bash
# On the server, create a simple webhook listener
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

Then add the server's URL as a `WEBHOOK_URL` secret in the GitHub repo settings.

## How It Works

### Decision Logic

When the script runs, it fetches the entire Lidarr queue and classifies each item:

```
Queue item
├── downloading + stale (>14d)? → DELETE + re-search
├── importFailed?
│   ├── "Has missing tracks" → DELETE + re-search
│   ├── "Has fewer tracks" → DELETE + re-search
│   ├── "Album match" or "Worst track match" ≥ 30% → FORCE IMPORT
│   ├── "Has unmatched tracks" → FORCE IMPORT
│   ├── "Not an upgrade" / "Already imported" / "Dest exists" → FORCE IMPORT
│   ├── "Could not find similar album" → FLAG FOR REVIEW
│   ├── Match < 30% → FLAG FOR REVIEW
│   ├── "Generic import failure" → DELETE + re-search
│   └── Unknown → FLAG FOR REVIEW
└── No status messages → skip (active download)
```

After queue processing, it scans the oldest 100 missing albums for ones that have been searched 2+ times with zero grabs — these are likely naming or indexer issues.

### Agentic Oversight

The script outputs structured results including `[AGENT_OVERSIGHT_NEEDED]` markers for edge cases. When used with a Hermes cron job, the agent reviews these and decides whether to investigate further, try a manual import, or delete+re-search.

## Files

```
lidarr_queue_maintenance.py   # Main script — the only file you need
tests/test_script.py          # Smoke tests
.github/workflows/ci.yml      # Lint + syntax check on push
.github/workflows/deploy.yml  # Optional auto-deploy via webhook
LICENSE                       # MIT
README.md                     # This file
```

## License

MIT
