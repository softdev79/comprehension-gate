"""Claude Code hook: append authorship and activity events to .comprehension/ledger.jsonl.

Reads the hook payload from stdin. Records only file paths, line counts and
timestamps - never prompt text or file contents. Always exits 0 so it can
never block the session.
"""
import json
import os
import sys
import time


def lines_written(tool_name, tool_input):
    """Number of lines the AI wrote in this tool call."""
    if tool_name == "Write":
        texts = [tool_input.get("content", "")]
    elif tool_name == "Edit":
        texts = [tool_input.get("new_string", "")]
    elif tool_name == "MultiEdit":
        texts = [e.get("new_string", "") for e in tool_input.get("edits", [])]
    else:
        return 0
    return sum(len(t.splitlines()) for t in texts if t)


def build_event(payload, now=None):
    """Turn a hook payload into a ledger row, or None if nothing to record."""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    event = {
        "ts": int(now if now is not None else time.time()),
        "session": payload.get("session_id", ""),
        "event": payload.get("hook_event_name", ""),
    }
    if event["event"] == "PostToolUse":
        tool_input = payload.get("tool_input") or {}
        path = tool_input.get("file_path", "")
        if not path:
            return None
        try:
            rel = os.path.relpath(path, root)
        except ValueError:  # different drive on Windows
            return None
        rel = rel.replace("\\", "/")
        if rel.startswith("..") or rel.startswith(".comprehension/"):
            return None
        event["file"] = rel
        event["lines"] = lines_written(payload.get("tool_name", ""), tool_input)
    return event


def main():
    try:
        payload = json.load(sys.stdin)
        event = build_event(payload)
        if event is None:
            return
        root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
        folder = os.path.join(root, ".comprehension")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "ledger.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
