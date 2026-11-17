import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

from gate import cli, evidence, policy, questionnaire  # noqa: E402
import record_authorship  # noqa: E402


def git(repo, *args):
    subprocess.run(["git"] + list(args), cwd=repo, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class HookTests(unittest.TestCase):
    def test_counts_lines_per_tool(self):
        self.assertEqual(record_authorship.lines_written("Write", {"content": "a\nb\nc\n"}), 3)
        self.assertEqual(record_authorship.lines_written("Edit", {"new_string": "x\ny"}), 2)
        self.assertEqual(record_authorship.lines_written(
            "MultiEdit", {"edits": [{"new_string": "x"}, {"new_string": "y\nz"}]}), 3)
        self.assertEqual(record_authorship.lines_written("Bash", {"command": "ls"}), 0)

    def test_event_uses_relative_path_and_skips_gate_files(self):
        root = tempfile.mkdtemp()
        os.environ["CLAUDE_PROJECT_DIR"] = root
        try:
            payload = {"hook_event_name": "PostToolUse", "tool_name": "Write", "session_id": "s1",
                       "tool_input": {"file_path": os.path.join(root, "src", "app.py"), "content": "a\nb"}}
            event = record_authorship.build_event(payload, now=100)
            self.assertEqual((event["file"], event["lines"], event["ts"]), ("src/app.py", 2, 100))
            payload["tool_input"]["file_path"] = os.path.join(root, ".comprehension", "pr-1", "answers.md")
            self.assertIsNone(record_authorship.build_event(payload, now=100))
        finally:
            del os.environ["CLAUDE_PROJECT_DIR"]


class EvidenceTests(unittest.TestCase):
    def test_attribution_caps_ai_lines_and_handles_missing_ledger(self):
        files = [{"path": "a.py", "added": 10, "deleted": 0}, {"path": "b.py", "added": 10, "deleted": 0}]
        ledger = [{"ts": 1, "event": "PostToolUse", "file": "a.py", "lines": 50}]
        result = evidence.attribution(files, ledger)
        self.assertEqual((result["ai_lines"], result["human_lines"], result["ai_percent"]), (10, 10, 50.0))
        unknown = evidence.attribution(files, [])
        self.assertEqual(unknown["status"], "unknown")
        self.assertIsNone(unknown["ai_percent"])

    def test_active_minutes_ignores_idle_gaps(self):
        ledger = [{"ts": 0, "session": "s"}, {"ts": 120, "session": "s"},
                  {"ts": 120 + 3600, "session": "s"}, {"ts": 180 + 3600, "session": "s"}]
        self.assertEqual(evidence.active_minutes(ledger), 3.0)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = policy.load_config(tempfile.mkdtemp())
        self.q = {"risk": "medium", "facts": [{"id": "F1"}], "questions": [{"id": "Q1"}, {"id": "Q2"}]}
        self.answers = {"F1": "confirmed", "Q1": "because", "Q2": "because"}

    def grade(self, *scores):
        return {"results": [{"id": "Q%d" % (i + 1), "score": s} for i, s in enumerate(scores)]}

    def test_risk_tiers(self):
        self.assertEqual(policy.risk_tier([{"path": "README.md", "added": 3}], self.cfg), "low")
        self.assertEqual(policy.risk_tier([{"path": "util.py", "added": 80}], self.cfg), "medium")
        self.assertEqual(policy.risk_tier([{"path": "src/auth/token.py", "added": 1}], self.cfg), "high")

    def test_passes_at_threshold(self):
        self.assertTrue(policy.decide(self.q, self.answers, self.grade(3, 3), self.cfg)["passed"])

    def test_fails_below_threshold_on_zero_and_on_missing(self):
        self.assertFalse(policy.decide(self.q, self.answers, self.grade(2, 2), self.cfg)["passed"])
        self.assertFalse(policy.decide(self.q, self.answers, self.grade(4, 0), self.cfg)["passed"])
        self.assertFalse(policy.decide(self.q, self.answers, self.grade(4), self.cfg)["passed"])
        blank = dict(self.answers, F1=" ")
        self.assertFalse(policy.decide(self.q, blank, self.grade(4, 4), self.cfg)["passed"])

    def test_model_cannot_pass_by_grading_unknown_ids_or_bad_scores(self):
        grade = {"results": [{"id": "Q1", "score": 99}, {"id": "Q2", "score": 4}, {"id": "Q9", "score": 4}]}
        self.assertFalse(policy.decide(self.q, self.answers, grade, self.cfg)["passed"])


class QuestionnaireTests(unittest.TestCase):
    def test_template_round_trip(self):
        q = {"pr": "7", "risk": "low", "change_summary": "s",
             "facts": [{"id": "F1", "title": "t", "detected": "d"}],
             "questions": [{"id": "Q1", "area": "business_logic", "question": "why?", "code_ref": "a.py:1-3"}]}
        template = questionnaire.render_answers_template(q)
        self.assertEqual(questionnaire.parse_answers(template), {"F1": "", "Q1": ""})
        filled = template.replace("> Code: a.py:1-3\n", "> Code: a.py:1-3\n\nIt guards the empty case.\nSecond line.\n")
        self.assertEqual(questionnaire.parse_answers(filled)["Q1"], "It guards the empty case.\nSecond line.")

    def test_hash_is_stable_and_sensitive(self):
        a = {"questions": [{"id": "Q1"}], "risk": "low"}
        b = {"risk": "low", "questions": [{"id": "Q1"}]}
        self.assertEqual(questionnaire.sha256_of(a), questionnaire.sha256_of(b))
        self.assertNotEqual(questionnaire.sha256_of(a), questionnaire.sha256_of({"questions": [], "risk": "low"}))


class EndToEndOfflineTests(unittest.TestCase):
    """Real git repo, offline questions, no model call."""

    def test_generate_then_incomplete_answers_fail(self):
        repo = tempfile.mkdtemp()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "t")
        git(repo, "config", "commit.gpgsign", "false")
        with open(os.path.join(repo, "app.py"), "w") as f:
            f.write("print('hi')\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "base")
        git(repo, "branch", "base")
        os.makedirs(os.path.join(repo, ".comprehension"))
        with open(os.path.join(repo, ".comprehension", "ledger.jsonl"), "w") as f:
            f.write(json.dumps({"ts": 10, "session": "s", "event": "PostToolUse", "file": "app.py", "lines": 2}) + "\n")
        with open(os.path.join(repo, "app.py"), "a") as f:
            f.write("def add(a, b):\n    return a + b\n\n\nprint(add(1, 2))\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "feature")

        data = evidence.collect("base", "HEAD", repo)
        self.assertEqual([f["path"] for f in data["files"]], ["app.py"])  # ledger file is excluded
        self.assertEqual(data["attribution"]["ai_lines"], 2)

        self.assertEqual(cli.main(["--repo", repo, "generate", "--pr", "1", "--base", "base", "--offline"]), 0)
        folder = os.path.join(repo, ".comprehension", "pr-1")
        with open(os.path.join(folder, "questionnaire.json")) as f:
            q = json.load(f)
        self.assertEqual((q["risk"], len(q["questions"]), len(q["facts"])), ("low", 3, 3))

        out = tempfile.mkdtemp()
        wrong = cli.main(["--repo", repo, "grade", "--pr", "1", "--base", "base",
                          "--expect-questionnaire-sha", "0" * 64, "--out", out])
        self.assertEqual(wrong, 2)  # tampered questionnaire is rejected
        code = cli.main(["--repo", repo, "grade", "--pr", "1", "--base", "base", "--out", out])
        self.assertEqual(code, 1)  # blank answers: gate stays closed, without a model call
        with open(os.path.join(out, "result.md")) as f:
            self.assertIn("not passed yet", f.read())


if __name__ == "__main__":
    unittest.main()
