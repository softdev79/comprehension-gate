"""The AWS service: webhook verification and routing, and the worker's full PR flow against a
local bare git remote, with fake GitHub, DynamoDB and model. No AWS, network or key needed."""
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gate import agents, questionnaire as qn  # noqa: E402
from service import webhook, worker  # noqa: E402

SECRET = "s3cret"
BOT = "gate-app[bot]"


def sign(body):
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def pr_payload(action="opened", sender="alice", head_repo="o/r", **extra):
    payload = {
        "action": action, "sender": {"login": sender}, "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
        "pull_request": {"number": 5, "user": {"login": "alice"},
                         "base": {"ref": "main"}, "head": {"ref": "feat", "sha": "abc", "repo": {"full_name": head_repo}}},
    }
    payload.update(extra)
    return payload


class FakeSQS:
    def __init__(self):
        self.sent = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)


class WebhookTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"QUEUE_URL": "q", "BOT_LOGIN": BOT})
        p.start()
        self.addCleanup(p.stop)

    def call(self, event_name, payload, signature=None, delivery="d1"):
        body = json.dumps(payload).encode()
        sqs = FakeSQS()
        resp = webhook.handler({"headers": {"X-GitHub-Event": event_name, "X-GitHub-Delivery": delivery,
                                            "X-Hub-Signature-256": signature or sign(body)},
                                "body": body.decode()}, None, sqs=sqs, secret=SECRET)
        return resp, sqs.sent

    def test_signature(self):
        self.assertTrue(webhook.verify_signature(SECRET, b"x", sign(b"x")))
        for bad in (None, "", "sha1=abc", sign(b"y")):
            self.assertFalse(webhook.verify_signature(SECRET, b"x", bad))
        self.assertFalse(webhook.verify_signature("", b"x", sign(b"x")))
        resp, sent = self.call("pull_request", pr_payload(), signature="sha256=" + "0" * 64)
        self.assertEqual((resp["statusCode"], sent), (401, []))

    def test_pull_request_is_queued_per_pr(self):
        resp, sent = self.call("pull_request", pr_payload("synchronize", before="b", after="a"))
        self.assertEqual(resp["statusCode"], 202)
        job = json.loads(sent[0]["MessageBody"])
        self.assertEqual((job["kind"], job["pr"], job["head_ref"], job["before"]), ("pull_request", 5, "feat", "b"))
        self.assertEqual(sent[0]["MessageGroupId"], "o/r#5")
        self.assertEqual(sent[0]["MessageDeduplicationId"], hashlib.sha256(b"d1").hexdigest())

    def test_ignored_events(self):
        for event_name, payload in (
            ("pull_request", pr_payload(sender=BOT)),            # our own push
            ("pull_request", pr_payload(head_repo="fork/r")),    # fork
            ("pull_request", pr_payload(action="closed")),
            ("issue_comment", {"action": "created", "sender": {"login": "a"}, "repository": {"full_name": "o/r"},
                               "installation": {"id": 7}, "issue": {"number": 5, "pull_request": {}},
                               "comment": {"body": "looks good /sign", "user": {"login": "a"}}}),
            ("push", {"repository": {"full_name": "o/r"}, "installation": {"id": 7}}),
        ):
            resp, sent = self.call(event_name, payload)
            self.assertEqual((resp["body"], sent), ("ignored", []), event_name)
        self.assertEqual(self.call("ping", {})[0]["body"], "pong")

    def test_sign_comment(self):
        payload = {"action": "created", "sender": {"login": "alice"}, "repository": {"full_name": "o/r"},
                   "installation": {"id": 7}, "issue": {"number": 5, "pull_request": {"url": "x"}},
                   "comment": {"body": " /sign \n", "user": {"login": "alice"}}}
        job = json.loads(self.call("issue_comment", payload)[1][0]["MessageBody"])
        self.assertEqual((job["kind"], job["commenter"], job["pr"]), ("sign", "alice", 5))


# ---------------------------------------------------------------------------- worker

def git(cwd, *args):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout.decode().strip()


class FakeGitHub:
    def __init__(self, remote, author="alice"):
        self.remote, self.author = remote, author
        self.token = "tok"
        self.comments, self.statuses = [], []

    def clone_url(self):
        return self.remote

    def comment(self, pr, body):
        self.comments.append(body)

    def status(self, sha, state, context, description, target_url=None):
        self.statuses.append((sha, state, context))

    def pull(self, pr):
        return {"user": {"login": self.author}, "head": {"sha": git(self.remote, "rev-parse", "refs/heads/feat")}}


class FakeState:
    def __init__(self):
        self.items = {}

    def get(self, repo, pr):
        return dict(self.items.get((repo, pr), {}))

    def update(self, repo, pr, **fields):
        self.items.setdefault((repo, pr), {}).update(fields)


def fill_answers(text):
    out, in_item = [], False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        out.append(line)
        if line.startswith("## "):
            in_item = bool(re.match(r"## [FQ]\d", line))
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if in_item and line.startswith(">") and not nxt.startswith(">"):
            out += ["", "A specific answer written for the test."]
    return "\n".join(out) + "\n"


class WorkerFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", self.remote)
        self.dev = os.path.join(self.tmp, "dev")
        git(self.tmp, "clone", "-q", self.remote, self.dev)
        for k, v in (("user.email", "a@example.com"), ("user.name", "alice"), ("commit.gpgsign", "false")):
            git(self.dev, "config", k, v)
        self.write("app.py", "print('hi')\n")
        # base policy: low risk needs 60%; the PR will try to lower it to 0
        self.write("comprehension-gate.json", json.dumps({"pass_percent": {"low": 60}}))
        git(self.dev, "add", ".")
        git(self.dev, "commit", "-q", "-m", "base")
        git(self.dev, "push", "-q", "origin", "HEAD:refs/heads/main")
        git(self.dev, "checkout", "-q", "-b", "feat")
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.write("comprehension-gate.json", json.dumps({"pass_percent": {"low": 0}}))
        git(self.dev, "commit", "-qam", "feature")
        git(self.dev, "push", "-q", "origin", "feat")

        self.gh, self.state = FakeGitHub(self.remote), FakeState()
        self.scores = 4
        patches = [
            mock.patch.object(agents, "generate_questions",
                              lambda diff, data, risk, count, **k: qn.offline_questions(data, count)),
            mock.patch.object(agents, "grade_answers", self.fake_grade),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_grade(self, diff, data, questionnaire, answers, **k):
        return {"results": [{"id": q["id"], "score": self.scores, "feedback": "", "follow_up": ""}
                            for q in questionnaire["questions"]], "overall_comment": ""}

    def write(self, path, text):
        with open(os.path.join(self.dev, path), "w", newline="\n") as f:
            f.write(text)

    def job(self, action, before=None):
        return {"kind": "pull_request", "action": action, "repo": "o/r", "pr": 5, "base_ref": "main",
                "head_ref": "feat", "head_sha": git(self.remote, "rev-parse", "refs/heads/feat"),
                "before": before, "after": git(self.remote, "rev-parse", "refs/heads/feat")}

    def push_answers(self):
        before = git(self.remote, "rev-parse", "refs/heads/feat")
        git(self.dev, "pull", "-q", "--ff-only", "origin", "feat")
        path = os.path.join(self.dev, ".comprehension", "pr-5", "answers.md")
        with open(path, encoding="utf-8") as f:
            filled = fill_answers(f.read())
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(filled)
        git(self.dev, "commit", "-qam", "answers")
        git(self.dev, "push", "-q", "origin", "feat")
        return before

    def test_generate_grade_sign(self):
        worker.process(self.job("opened"), self.gh, self.state, scratch=self.tmp)
        record = self.state.get("o/r", 5)
        self.assertEqual(len(record["questionnaire_sha"]), 64)
        bot_commit = git(self.remote, "log", "-1", "--format=%an %s", "refs/heads/feat")
        self.assertIn("questionnaire for PR #5", bot_commit)
        self.assertIn(record["questionnaire_sha"], self.gh.comments[0])
        ready = self.gh.comments[0]
        self.assertIn("https://github.com/o/r/edit/feat/.comprehension/pr-5/answers.md", ready)
        q = json.loads(git(self.remote, "show", "refs/heads/feat:.comprehension/pr-5/questionnaire.json"))
        for item in q["questions"]:
            self.assertIn("**%s**" % item["id"], ready)
        self.assertEqual(self.gh.statuses[-1][1:], ("pending", "comprehension-gate"))

        # A /sign before grading is refused.
        worker.process({"kind": "sign", "repo": "o/r", "pr": 5, "commenter": "alice"}, self.gh, self.state)
        self.assertIn("Cannot sign yet", self.gh.comments[-1])

        # Answers-only push -> graded with the base branch's 60% threshold, not the PR's 0%.
        self.scores = 1
        before = self.push_answers()
        worker.process(self.job("synchronize", before), self.gh, self.state, scratch=self.tmp)
        self.assertIn("needs 60%", self.gh.comments[-1])
        self.assertFalse(self.state.get("o/r", 5)["grade_passed"])
        self.assertEqual(self.gh.statuses[-1][1:], ("failure", "comprehension-gate"))

        self.scores = 4
        with open(os.path.join(self.dev, ".comprehension", "pr-5", "answers.md"), "a") as f:
            f.write("\n")
        git(self.dev, "add", ".comprehension")
        before = git(self.remote, "rev-parse", "refs/heads/feat")
        git(self.dev, "commit", "-qm", "better answers")
        git(self.dev, "push", "-q", "origin", "feat")
        worker.process(self.job("synchronize", before), self.gh, self.state, scratch=self.tmp)
        self.assertTrue(self.state.get("o/r", 5)["grade_passed"])
        self.assertEqual(self.gh.statuses[-1][1:], ("pending", "comprehension-gate"))

        # Someone else cannot sign; the author can.
        worker.process({"kind": "sign", "repo": "o/r", "pr": 5, "commenter": "mallory"}, self.gh, self.state)
        self.assertIn("Only the pull request author", self.gh.comments[-1])
        worker.process({"kind": "sign", "repo": "o/r", "pr": 5, "commenter": "alice"}, self.gh, self.state)
        self.assertEqual(self.gh.statuses[-1][1:], ("success", "comprehension-gate"))
        self.assertEqual(self.state.get("o/r", 5)["signed_by"], "alice")

    def test_tampered_questionnaire_is_not_graded(self):
        worker.process(self.job("opened"), self.gh, self.state, scratch=self.tmp)
        git(self.dev, "pull", "-q", "--ff-only", "origin", "feat")
        qpath = os.path.join(self.dev, ".comprehension", "pr-5", "questionnaire.json")
        with open(qpath, encoding="utf-8") as f:
            q = json.load(f)
        q["questions"] = q["questions"][:1]  # drop the hard questions
        with open(qpath, "w", encoding="utf-8") as f:
            json.dump(q, f)
        apath = os.path.join(self.dev, ".comprehension", "pr-5", "answers.md")
        with open(apath, encoding="utf-8") as f:
            filled = fill_answers(f.read())
        with open(apath, "w", encoding="utf-8", newline="\n") as f:
            f.write(filled)
        before = git(self.remote, "rev-parse", "refs/heads/feat")
        git(self.dev, "commit", "-qam", "edit")
        git(self.dev, "push", "-q", "origin", "feat")
        worker.process(self.job("synchronize", before), self.gh, self.state, scratch=self.tmp)
        self.assertEqual(self.gh.statuses[-1][1:], ("error", "comprehension-gate"))
        self.assertFalse(self.state.get("o/r", 5).get("grade_passed"))

    def test_comment_neutralises_mentions_and_html_comments(self):
        q = {"risk": "low", "facts": [], "questions": [
            {"id": "Q1", "area": "security", "code_ref": "a.py:1", "question": "Ping @admin <!-- x --> now"}]}
        text = worker.questionnaire_comment({"repo": "o/r", "pr": 5, "head_ref": "feature/x y"}, q, "d" * 64)
        self.assertNotIn("@admin", text)
        self.assertEqual(text.count("<!--"), 1)  # only the gate's own hash marker
        self.assertIn("/edit/feature/x%20y/", text)

    def test_code_push_regenerates_and_resets_grade(self):
        worker.process(self.job("opened"), self.gh, self.state, scratch=self.tmp)
        self.state.update("o/r", 5, grade_passed=True)
        git(self.dev, "pull", "-q", "--ff-only", "origin", "feat")
        self.write("app.py", "def add(a, b):\n    return b + a\n")
        before = git(self.remote, "rev-parse", "refs/heads/feat")
        git(self.dev, "commit", "-qam", "code")
        git(self.dev, "push", "-q", "origin", "feat")
        worker.process(self.job("synchronize", before), self.gh, self.state, scratch=self.tmp)
        self.assertFalse(self.state.get("o/r", 5)["grade_passed"])
        self.assertEqual(len(self.gh.comments), 2)  # a second questionnaire comment


if __name__ == "__main__":
    unittest.main()
