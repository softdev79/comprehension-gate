"""Read-only tools the agents may call while they study a pull request.

Every tool reads from git objects at two fixed commits (the merge base and the head),
never from the working tree, the network or the environment, and nothing here writes.
The model's arguments are untrusted: paths are confined to the repository, git is run
with an argument list (never a shell), and every result is size-capped. A tool that
gets bad arguments returns an error string for the model instead of raising.

Standard library only, Python 3.9+.
"""
import posixpath
import re
import subprocess

from . import evidence as ev

MAX_RESULT_CHARS = 8000
MAX_READ_LINES = 300
MAX_GREP_MATCHES = 60
GIT_TIMEOUT_SECONDS = 20
_PATH_OK = re.compile(r"^[A-Za-z0-9_.@+\-/ ]+$")


class ToolError(ValueError):
    """Bad arguments from the model; reported back to it, never raised to the caller."""


def _clip(text, limit=MAX_RESULT_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated: %d more characters; ask for a narrower range]" % (len(text) - limit)


def clean_path(path):
    """A repository-relative POSIX path, or ToolError. Rejects absolute paths, '..' and odd characters."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError("path must be a non-empty string")
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ToolError("path must be relative to the repository root")
    if not _PATH_OK.match(path):
        raise ToolError("path contains characters that are not allowed")
    norm = posixpath.normpath(path)
    if norm == "." or norm.startswith("../") or norm == ".." or "/../" in "/" + path + "/":
        raise ToolError("path must stay inside the repository")
    if norm == ev.GATE_DIR or norm.startswith(ev.GATE_DIR + "/"):
        raise ToolError("the gate's own files are not available")
    return norm


def _int(value, name, default, low, high):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ToolError("%s must be an integer" % name)
    try:
        number = int(value)
    except ValueError:
        raise ToolError("%s must be an integer" % name)
    return max(low, min(high, number))


class RepoTools:
    """Tool implementations bound to one repository and one change."""

    def __init__(self, repo, base, head):
        self.repo = repo
        self.head = self._git(["rev-parse", "--verify", "%s^{commit}" % head]).strip()
        self.base = self._git(["merge-base", base, self.head]).strip()  # what the diff is relative to
        self.changed = ev.changed_files(self.base, self.head, repo)

    # -- plumbing

    def _git(self, args, ok_codes=(0,)):
        proc = subprocess.run(["git", "-c", "core.quotePath=false"] + list(args), cwd=self.repo,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=GIT_TIMEOUT_SECONDS)
        if proc.returncode not in ok_codes:
            lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise ToolError(lines[-1] if lines else "git failed")
        return proc.stdout.decode("utf-8", "replace")

    def _commit(self, ref):
        if ref in (None, "head"):
            return self.head
        if ref == "base":
            return self.base
        raise ToolError("ref must be 'head' or 'base'")

    def _tree_files(self, commit):
        out = self._git(["ls-tree", "-r", "--name-only", "-z", commit])
        return [p for p in out.split("\0") if p and not p.startswith(ev.GATE_DIR + "/")]

    # -- tools

    def list_changed_files(self):
        lines = ["%s (+%d -%d)" % (f["path"], f["added"], f["deleted"]) for f in self.changed]
        return _clip("\n".join(lines) or "(no changed files)")

    def read_file(self, path, start_line=None, end_line=None, ref="head"):
        path = clean_path(path)
        commit = self._commit(ref)
        kind = self._git(["cat-file", "-t", "%s:%s" % (commit, path)], ok_codes=(0, 128)).strip()
        if kind != "blob":
            raise ToolError("%s does not exist at %s" % (path, ref or "head"))
        text = self._git(["cat-file", "blob", "%s:%s" % (commit, path)])
        if "\0" in text[:4000]:
            return "%s is a binary file" % path
        lines = text.splitlines()
        start = _int(start_line, "start_line", 1, 1, max(1, len(lines)))
        end = _int(end_line, "end_line", start + MAX_READ_LINES - 1, start, len(lines) or 1)
        end = min(end, start + MAX_READ_LINES - 1)
        body = "\n".join("%5d  %s" % (n, lines[n - 1]) for n in range(start, min(end, len(lines)) + 1))
        header = "%s @ %s, lines %d-%d of %d\n" % (path, ref or "head", start, min(end, len(lines)), len(lines))
        return _clip(header + body)

    def grep(self, pattern, path_glob=None, ref="head"):
        if not isinstance(pattern, str) or not pattern or len(pattern) > 200:
            raise ToolError("pattern must be a non-empty string of at most 200 characters")
        commit = self._commit(ref)
        args = ["grep", "-n", "-I", "-E", "--max-count", "20", "-e", pattern, commit, "--"]
        if path_glob:
            if not isinstance(path_glob, str) or not _PATH_OK.match(path_glob.replace("*", "").replace("?", "") or "x"):
                raise ToolError("path_glob may contain only path characters and * or ?")
            if path_glob.startswith(":") or ".." in path_glob or path_glob.startswith("/"):
                raise ToolError("path_glob must be a plain relative glob")
            args.append(path_glob)
        else:
            args.append(".")
        args.append(":(exclude)" + ev.GATE_DIR)
        out = self._git(args, ok_codes=(0, 1))
        prefix = commit + ":"
        matches = [line[len(prefix):] if line.startswith(prefix) else line for line in out.splitlines()]
        if not matches:
            return "no matches"
        more = len(matches) - MAX_GREP_MATCHES
        text = "\n".join(m[:300] for m in matches[:MAX_GREP_MATCHES])
        return _clip(text + ("\n... %d more matches; narrow the pattern or path_glob" % more if more > 0 else ""))

    def list_files(self, directory="", ref="head"):
        commit = self._commit(ref)
        files = self._tree_files(commit)
        if directory not in (None, "", "."):
            prefix = clean_path(directory).rstrip("/") + "/"
            files = [f for f in files if f.startswith(prefix)]
        if not files:
            return "no files"
        shown = files[:400]
        return _clip("\n".join(shown) + ("\n... %d more" % (len(files) - 400) if len(files) > 400 else ""))

    def find_tests(self, path):
        path = clean_path(path)
        stem = posixpath.splitext(posixpath.basename(path))[0].lower()
        if not stem:
            raise ToolError("path has no file name")
        found = []
        for f in self._tree_files(self.head):
            name = posixpath.basename(f).lower()
            in_test_place = "test" in name or "/tests/" in "/" + f.lower() or "spec" in name
            if in_test_place and stem in name and f != path:
                found.append(f)
        if not found:
            mentions = self.grep(re.escape(stem), "*test*")
            return "no test file named after %s; files under test paths mentioning it:\n%s" % (stem, mentions)
        return _clip("\n".join(found[:50]))

    def git_log(self, path, max_count=10):
        path = clean_path(path)
        count = _int(max_count, "max_count", 10, 1, 30)
        out = self._git(["log", "-n", str(count), "--format=%h %ad %s", "--date=short", self.head, "--", path])
        return _clip(out.strip() or "no history for %s" % path)

    # -- dispatch

    def call(self, name, arguments):
        """Run a tool by name with model-supplied arguments. Always returns a string."""
        spec = SPECS.get(name)
        if spec is None:
            return "error: unknown tool %r" % name
        if not isinstance(arguments, dict):
            return "error: arguments must be a JSON object"
        allowed = set(spec["parameters"]["properties"])
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            return "error: unexpected argument(s) %s" % ", ".join(unknown)
        missing = [k for k in spec["parameters"].get("required", []) if k not in arguments]
        if missing:
            return "error: missing argument(s) %s" % ", ".join(missing)
        try:
            return getattr(self, name)(**arguments)
        except ToolError as exc:
            return "error: %s" % exc
        except subprocess.TimeoutExpired:
            return "error: the tool timed out; narrow the request"


def _spec(description, properties, required=()):
    return {"description": description, "parameters": {
        "type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}


_REF = {"type": "string", "enum": ["head", "base"],
        "description": "'head' = the pull request version (default), 'base' = before the change"}

SPECS = {
    "list_changed_files": _spec("Changed files with added/deleted line counts.", {}),
    "read_file": _spec(
        "Read numbered lines of a file at the head or base commit (at most %d lines per call)." % MAX_READ_LINES,
        {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}, "ref": _REF},
        ["path"]),
    "grep": _spec(
        "Search file contents with an extended regular expression. Returns path:line:text.",
        {"pattern": {"type": "string"}, "path_glob": {"type": "string", "description": "e.g. 'src/*.py'"}, "ref": _REF},
        ["pattern"]),
    "list_files": _spec("List files in the repository, optionally under one directory.",
                        {"directory": {"type": "string"}, "ref": _REF}),
    "find_tests": _spec("Find test files that are likely to cover a source file.", {"path": {"type": "string"}}, ["path"]),
    "git_log": _spec("Recent commits that touched a file (hash, date, subject).",
                     {"path": {"type": "string"}, "max_count": {"type": "integer"}}, ["path"]),
}


def openai_tool_specs():
    return [{"type": "function", "function": dict(name=name, **spec)} for name, spec in SPECS.items()]
