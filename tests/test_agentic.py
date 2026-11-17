"""Read-only tools, the tool-calling loop and the agentic agents, against a real git repo
and a scripted fake model. No SDK, key or network needed."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gate import agent_loop, agents, llm, tools  # noqa: E402


def git(repo, *args):
    subprocess.run(["git"] + list(args), cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write(repo, path, text):
    full = os.path.join(repo, *path.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", newline="\n") as f:
        f.write(text)


def make_repo():
    """base: src/calc.py; head (branch tip): calc.py gains divide(), a test file, a ledger."""
    repo = tempfile.mkdtemp()
    git(repo, "init", "-q")
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(repo, "config", key, value)
    write(repo, "src/calc.py", "def add(a, b):\n    return a + b\n")
    write(repo, "README.md", "calc\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "base")
    write(repo, "src/calc.py", "def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n"
                               "    if b == 0:\n        raise ZeroDivisionError('b')\n    return a / b\n")
    write(repo, "tests/test_calc.py", "from src.calc import divide\n\n\ndef test_divide():\n    assert divide(4, 2) == 2\n")
    write(repo, ".comprehension/ledger.jsonl", '{"ts": 1, "secret": "ledger"}\n')
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "add divide")
    return repo


class ToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo()
        cls.t = tools.RepoTools(cls.repo, "base", "HEAD")

    def test_changed_files_exclude_gate_dir(self):
        out = self.t.list_changed_files()
        self.assertIn("src/calc.py (+6 -0)", out)
        self.assertIn("tests/test_calc.py", out)
        self.assertNotIn(".comprehension", out)

    def test_read_file_head_base_and_ranges(self):
        head = self.t.read_file("src/calc.py", 5, 6)
        self.assertIn("lines 5-6 of 8", head)
        self.assertIn("def divide", head)
        base = self.t.read_file("./src/calc.py", ref="base")
        self.assertIn("of 2", base)
        self.assertNotIn("divide", base)
        self.assertIn("does not exist", self.t.call("read_file", {"path": "src/nope.py"}))

    def test_paths_cannot_escape_or_reach_gate_files(self):
        for bad in ("../outside.txt", "/etc/passwd", "C:/Windows/win.ini", "src/../../x", "..",
                    ".comprehension/ledger.jsonl", "src/$(whoami).py", "", "a\nb"):
            result = self.t.call("read_file", {"path": bad})
            self.assertTrue(result.startswith("error:"), (bad, result))
        self.assertTrue(self.t.call("read_file", {"path": "src/calc.py", "ref": "HEAD~1"}).startswith("error:"))

    def test_grep_with_glob_and_options_are_not_injectable(self):
        self.assertIn("src/calc.py:5:def divide", self.t.grep("def divide"))
        self.assertIn("tests/test_calc.py", self.t.grep("divide", "tests/*"))
        self.assertNotIn("src/calc.py", self.t.grep("divide", "tests/*"))
        self.assertEqual(self.t.grep("--output=/tmp/x"), "no matches")  # taken as a pattern, not an option
        self.assertNotIn("secret", self.t.grep("secret"))
        for bad_glob in (":(top)*", "../*", "/etc/*"):
            self.assertTrue(self.t.call("grep", {"pattern": "x", "path_glob": bad_glob}).startswith("error:"))

    def test_find_tests_list_files_and_log(self):
        self.assertIn("tests/test_calc.py", self.t.find_tests("src/calc.py"))
        files = self.t.list_files()
        self.assertIn("README.md", files)
        self.assertNotIn(".comprehension", files)
        self.assertEqual(self.t.list_files("tests"), "tests/test_calc.py")
        self.assertIn("add divide", self.t.git_log("src/calc.py"))

    def test_dispatch_rejects_unknown_tools_and_arguments(self):
        self.assertTrue(self.t.call("run_shell", {"cmd": "ls"}).startswith("error: unknown tool"))
        self.assertTrue(self.t.call("read_file", {"path": "src/calc.py", "mode": "w"}).startswith("error: unexpected"))
        self.assertTrue(self.t.call("read_file", {}).startswith("error: missing"))
        self.assertTrue(self.t.call("read_file", ["src/calc.py"]).startswith("error:"))

    def test_results_are_capped(self):
        self.assertLessEqual(len(tools._clip("x" * 50000)), tools.MAX_RESULT_CHARS + 100)
        write(self.repo, "big.txt", "line\n" * 2000)
        git(self.repo, "add", "big.txt")
        git(self.repo, "commit", "-q", "-m", "big")
        big = tools.RepoTools(self.repo, "base", "HEAD").read_file("big.txt", 1, 5000)
        self.assertIn("lines 1-%d of 2000" % tools.MAX_READ_LINES, big)


# ---------------------------------------------------------------------------- fake model

def call(name, args, cid=None):
    raw = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(id=cid or "c-" + name, function=SimpleNamespace(name=name, arguments=raw))


def turn(*calls, content="", finish="tool_calls", refusal=None):
    message = SimpleNamespace(content=content, refusal=refusal, tool_calls=list(calls) or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])


class FakeClient:
    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))  # snapshot, messages keep growing
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False}


class StubTools:
    def __init__(self):
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, args))
        return "result of %s" % name


class Base(unittest.TestCase):
    def setUp(self):
        env = {"GATE_PROVIDER": "openai", "GATE_REASONING_EFFORT": "high", "GATE_AGENT_MAX_TURNS": "4",
               "GATE_AGENTIC": "1", "MODEL_ID": "openai.gpt-oss-120b"}
        for p in (mock.patch.dict(os.environ, env), mock.patch.object(llm, "load_dotenv", lambda path=None: None)):
            p.start()
            self.addCleanup(p.stop)

    def run_loop(self, client, check=None, stub=None):
        return agent_loop.run("sys", "user", stub or StubTools(), "submit", "Submit.", SCHEMA,
                              check=check, client=client)


class LoopTests(Base):
    def test_tools_then_submit(self):
        stub = StubTools()
        client = FakeClient(turn(call("read_file", {"path": "a.py"}), call("grep", {"pattern": "x"})),
                            turn(call("submit", {"n": 3})))
        self.assertEqual(self.run_loop(client, stub=stub), {"n": 3})
        self.assertEqual([c[0] for c in stub.calls], ["read_file", "grep"])
        second = client.requests[1]["messages"]
        self.assertEqual(second[2]["role"], "assistant")
        self.assertEqual([m["tool_call_id"] for m in second[3:]], ["c-read_file", "c-grep"])
        self.assertEqual(second[3]["content"], "result of read_file")
        names = [t["function"]["name"] for t in client.requests[0]["tools"]]
        self.assertIn("submit", names)
        self.assertNotIn("run_shell", names)
        self.assertEqual(client.requests[0]["reasoning_effort"], "high")

    def test_invalid_submit_goes_back_as_tool_error(self):
        client = FakeClient(turn(call("submit", {"n": "three"})), turn(call("submit", "{bad json")),
                            turn(call("submit", {"n": 3})))
        self.assertEqual(self.run_loop(client), {"n": 3})
        errors = [m["content"] for m in client.requests[2]["messages"] if m["role"] == "tool"]
        self.assertTrue(all(e.startswith("Rejected") for e in errors), errors)

    def test_semantic_check_failures_are_retried_then_fatal(self):
        client = FakeClient(*[turn(call("submit", {"n": 1})) for _ in range(3)])
        with self.assertRaisesRegex(RuntimeError, "did not pass validation: n must be even"):
            self.run_loop(client, check=lambda r: [] if r["n"] % 2 == 0 else ["n must be even"])

    def test_turn_limit_forces_submit(self):
        script = [turn(call("list_files", {}, cid="c%d" % i)) for i in range(4)] + [turn(call("submit", {"n": 2}))]
        client = FakeClient(*script)
        self.assertEqual(self.run_loop(client), {"n": 2})
        self.assertNotIn("tool_choice", client.requests[3])
        self.assertEqual(client.requests[4]["tool_choice"]["function"]["name"], "submit")

    def test_no_submit_at_all_fails(self):
        client = FakeClient(*[turn(call("list_files", {}, cid="c%d" % i)) for i in range(5)])
        with self.assertRaisesRegex(RuntimeError, "within 4 turns"):
            self.run_loop(client)

    def test_json_answer_in_content_is_accepted(self):
        client = FakeClient(turn(content="Done. {\"n\": 7}", finish="stop"))
        self.assertEqual(self.run_loop(client), {"n": 7})

    def test_tool_budget(self):
        stub = StubTools()
        with mock.patch.object(agent_loop, "MAX_TOOL_CALLS", 1):
            client = FakeClient(turn(call("grep", {"pattern": "a"}, cid="1"), call("grep", {"pattern": "b"}, cid="2")),
                                turn(call("submit", {"n": 0})))
            self.run_loop(client, stub=stub)
        self.assertEqual(len(stub.calls), 1)
        self.assertIn("budget", client.requests[1]["messages"][-1]["content"])

    def test_refusal_and_truncation(self):
        with self.assertRaisesRegex(RuntimeError, "declined"):
            self.run_loop(FakeClient(turn(refusal="no", finish="stop")))
        with self.assertRaisesRegex(RuntimeError, "cut off"):
            self.run_loop(FakeClient(turn(finish="length")))

    def test_drops_reasoning_effort_on_bad_request(self):
        class BadRequest(Exception):
            status_code = 400
        client = FakeClient(BadRequest("effort"), turn(call("submit", {"n": 0})))
        self.run_loop(client)
        self.assertNotIn("reasoning_effort", client.requests[1])


class AgentTests(Base):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo()

    def test_question_agent_must_anchor_to_changed_files(self):
        bad = {"change_summary": "s", "questions": [
            {"area": "failure_modes", "question": "What if b is 0?", "code_ref": "README.md:1"}]}
        good = {"change_summary": "s", "questions": [
            {"area": "failure_modes", "question": "What if b is 0?", "code_ref": "src/calc.py:5-8"}]}
        client = FakeClient(turn(call("read_file", {"path": "src/calc.py"})),
                            turn(call("submit_questionnaire", bad)), turn(call("submit_questionnaire", good)))
        with mock.patch.object(llm, "make_openai_client", lambda: client):
            out = agents.generate_questions("diff", {"files": []}, "low", 1, repo=self.repo, base="base")
        self.assertEqual(out, good)
        tool_results = [m["content"] for m in client.requests[1]["messages"] if m["role"] == "tool"]
        self.assertIn("def divide", tool_results[0])  # the real tool ran against the repo
        rejection = [m["content"] for m in client.requests[2]["messages"] if m["role"] == "tool"][-1]
        self.assertIn("not a changed file", rejection)

    def test_grader_must_grade_each_id_once(self):
        q = {"questions": [{"id": "Q1", "area": "testing", "question": "?", "code_ref": "src/calc.py:5"},
                           {"id": "Q2", "area": "testing", "question": "?", "code_ref": "src/calc.py:6"}]}
        r = lambda i, s: {"id": i, "score": s, "feedback": "", "follow_up": ""}  # noqa: E731
        bad = {"results": [r("Q1", 4), r("Q1", 4), r("Q9", 4)], "overall_comment": ""}
        good = {"results": [r("Q1", 4), r("Q2", 2)], "overall_comment": ""}
        client = FakeClient(turn(call("submit_grades", bad)), turn(call("submit_grades", good)))
        with mock.patch.object(llm, "make_openai_client", lambda: client):
            out = agents.grade_answers("diff", {}, q, {"Q1": "a", "Q2": "b"}, repo=self.repo, base="base")
        self.assertEqual(out, good)

    def test_agentic_can_be_switched_off(self):
        with mock.patch.dict(os.environ, {"GATE_AGENTIC": "0"}):
            self.assertFalse(agents.agentic())
        with mock.patch.dict(os.environ, {"GATE_PROVIDER": "anthropic"}):
            self.assertFalse(agents.agentic())
        self.assertTrue(agents.agentic())

    def test_code_ref_path(self):
        self.assertEqual(agents.code_ref_path("src/a.py:10-20"), "src/a.py")
        self.assertEqual(agents.code_ref_path("src/a.py:3, 9"), "src/a.py")
        self.assertEqual(agents.code_ref_path("src/a.py"), "src/a.py")


if __name__ == "__main__":
    unittest.main()
