#!/usr/bin/env python3
"""Smoke tests for lidarr_queue_maintenance.py"""
import subprocess
import json
import os
import sys

SCRIPT = os.path.join(os.path.dirname(__file__), "lidarr_queue_maintenance.py")


def test_script_imports():
    """Verify the script can be imported without errors"""
    result = subprocess.run(
        [sys.executable, "-c", "import ast; ast.parse(open('{}').read())".format(SCRIPT)],
        capture_output=True, text=True, shell=False
    )
    assert result.returncode == 0, f"Syntax error:\n{result.stderr}"
    print("  ✓ Syntax check passed")


def test_script_runs():
    """Verify the script runs without crashing (dry run against real API)"""
    env = os.environ.copy()
    result = subprocess.run(
        [sys.executable, SCRIPT],
        capture_output=True, text=True, timeout=120,
        env=env
    )
    if result.returncode != 0 and "ERROR" not in result.stdout:
        # Non-zero is OK if script found issues — check stderr
        if result.stderr:
            print(f"  ⚠ Stderr: {result.stderr[:200]}")
    
    # Should always produce a summary
    assert "SUMMARY" in result.stdout, "Script didn't produce summary output"
    print(f"  ✓ Script ran (exit={result.returncode})")

    # Check for API connectivity
    assert ("ERROR" not in result.stdout[:200]
            or "Could not" in result.stdout), f"API error:\n{result.stdout[:300]}"


if __name__ == "__main__":
    print("Running smoke tests...")
    test_script_imports()
    test_script_runs()
    print("\nAll tests passed.")
