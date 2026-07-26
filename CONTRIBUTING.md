# Contributing

PRs and ideas welcome. This is a small script, so keep it simple.

## Setup

```bash
git clone https://github.com/Keylessboi/lidarr-maintenance-script
cd lidarr-maintenance-script
```

## Testing

```bash
# Syntax check
python3 -m py_compile lidarr_queue_maintenance.py

# Run against your Lidarr instance
export LIDARR_URL="http://your-lidarr:8686"
export LIDARR_API_KEY="your-api-key"
python3 lidarr_queue_maintenance.py
```

## Guidelines

- Keep the decision logic in `lidarr_queue_maintenance.py` self-documenting
- If adding a new condition to the classification logic, add it in the correct priority order
- Update the decision table in README.md if you change how items are classified
- The script talks directly to Lidarr's REST API — no MCP dependency
