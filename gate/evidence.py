"""Deterministic evidence collectors. No model calls here - everything is measured."""
import json
import os
import re
import subprocess

GATE_DIR = ".comprehension"
LEDGER = os.path.join(GATE_DIR, "ledger.jsonl")
EXCLUDE = ":(exclude)" + GATE_DIR
IDLE_GAP_SECONDS = 10 * 60  # gaps longer than this do not count as active time


def _git(args, repo="."):
    out = subprocess.run(
        ["git"] + list(args), cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return out.stdout.decode("utf-8", errors="replace")


def diff_text(base, head, repo="."):
    """Unified diff of the change, excluding the gate's own files."""
    return _git(["diff", "%s...%s" % (base, head), "--", ".", EXCLUDE], repo)


def changed_files(base, head, repo="."):
    """[{path, added, deleted}] for the change, excluding the gate's own files."""
    files = []
    out = _git(["diff", "--numstat", "%s...%s" % (base, head), "--", ".", EXCLUDE], repo)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added = int(parts[0]) if parts[0].isdigit() else 0  # "-" for binary files
        deleted = int(parts[1]) if parts[1].isdigit() else 0
        files.append({"path": parts[2], "added": added, "deleted": deleted})
    return files


def read_ledger(repo="."):
    """Rows written by hooks/record_authorship.py. Malformed lines are skipped."""
    rows = []
    path = os.path.join(repo, LEDGER)
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("ts"), int):
                rows.append(row)
    return rows


def attribution(files, ledger):
    """Estimate AI-written vs human-written added lines.

    AI lines per file = lines the hook saw Claude write, capped at the lines
    actually added in the diff (repeated edits to the same lines over-count).
    With no ledger at all the split is unknown, not 0% AI.
    """
    ai_by_file = {}
    for row in ledger:
        if row.get("event") == "PostToolUse" and row.get("file"):
            ai_by_file[row["file"]] = ai_by_file.get(row["file"], 0) + int(row.get("lines", 0))
    total = sum(f["added"] for f in files)
    if not ledger:
        return {"status": "unknown", "total_added": total, "ai_lines": None,
                "human_lines": None, "ai_percent": None, "per_file": []}
    per_file = []
    for f in files:
        ai = min(ai_by_file.get(f["path"], 0), f["added"])
        per_file.append({"path": f["path"], "added": f["added"], "ai_lines": ai})
    ai_total = sum(p["ai_lines"] for p in per_file)
    return {
        "status": "estimated",
        "total_added": total,
        "ai_lines": ai_total,
        "human_lines": total - ai_total,
        "ai_percent": round(100.0 * ai_total / total, 1) if total else 0.0,
        "per_file": per_file,
    }


def active_minutes(ledger):
    """Active AI-session time: sum of gaps between consecutive events, ignoring idle gaps."""
    by_session = {}
    for row in ledger:
        by_session.setdefault(row.get("session", ""), []).append(row["ts"])
    seconds = 0
    for stamps in by_session.values():
        stamps.sort()
        for prev, cur in zip(stamps, stamps[1:]):
            if cur - prev <= IDLE_GAP_SECONDS:
                seconds += cur - prev
    return round(seconds / 60.0, 1)


def effort(base, head, ledger, repo="."):
    stamps = [int(s) for s in _git(["log", "--format=%at", "%s..%s" % (base, head)], repo).split()]
    return {
        "ai_session_active_minutes": active_minutes(ledger) if ledger else None,
        "ai_sessions": len({r.get("session", "") for r in ledger}) if ledger else None,
        "commits": len(stamps),
        "commit_span_minutes": round((max(stamps) - min(stamps)) / 60.0, 1) if stamps else 0.0,
    }


def versions(repo="."):
    """Declared dependency versions from manifests in the repo root.

    Scaffold-level parsing. Replace with a Syft SBOM for full coverage.
    """
    found = []

    def read(name):
        path = os.path.join(repo, name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    for name in sorted(os.listdir(repo)):
        if re.match(r"requirements.*\.txt$", name):
            for line in read(name).splitlines():
                m = re.match(r"\s*([A-Za-z0-9_.\-\[\]]+)\s*([=<>~!]=?.*)?$", line.split("#")[0])
                if m and m.group(1):
                    found.append({"name": m.group(1), "version": (m.group(2) or "unpinned").strip(), "source": name})
    text = read("package.json")
    if text:
        try:
            pkg = json.loads(text)
        except ValueError:
            pkg = {}
        for section in ("dependencies", "devDependencies"):
            for dep, ver in sorted((pkg.get(section) or {}).items()):
                found.append({"name": dep, "version": str(ver), "source": "package.json"})
    text = read("go.mod")
    if text:
        for m in re.finditer(r"^\s*(?:require\s+)?([\w./\-]+\.[\w./\-]+)\s+(v[\w.\-+]+)", text, re.M):
            found.append({"name": m.group(1), "version": m.group(2), "source": "go.mod"})
    return found


def collect(base, head, repo="."):
    files = changed_files(base, head, repo)
    ledger = read_ledger(repo)
    return {
        "base": base,
        "head_sha": _git(["rev-parse", head], repo).strip(),
        "files": files,
        "attribution": attribution(files, ledger),
        "effort": effort(base, head, ledger, repo),
        "versions": versions(repo),
    }
