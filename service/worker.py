"""Lambda (container image) that does the gate's work for one queued GitHub event.

    pull_request opened/reopened, or a push that changes code -> generate a questionnaire,
        commit it to the PR branch, post its hash, set comprehension-gate to pending
    a push that changes only .comprehension/pr-<n>/answers.md  -> grade it, post the result
    "/sign" from the PR author on a passed head commit           -> release the gate

The trusted state (questionnaire hash, grade per head commit, sign-off) lives in DynamoDB,
not in the PR branch. The repository's comprehension-gate.json is read from the base branch,
so a pull request cannot loosen its own policy.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gate import cli, evidence as ev, questionnaire as qn  # noqa: E402
from service import github  # noqa: E402

GATE = "comprehension-gate"
GRADED = "comprehension-gate/graded"
_cache = {}


# ---------------------------------------------------------------------------- settings and state

def _ssm(name):
    if name not in _cache:
        import boto3

        _cache[name] = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    return _cache[name]


def configure_model():
    """Model settings come from the Lambda environment; the API key from SSM."""
    if not os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_API_KEY_PARAM"):
        os.environ["OPENAI_API_KEY"] = _ssm(os.environ["OPENAI_API_KEY_PARAM"])


class State:
    """One DynamoDB item per pull request, keyed 'owner/repo#number'."""

    def __init__(self, table=None):
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
        self.table = table

    def get(self, repo, pr):
        return self.table.get_item(Key={"pk": "%s#%s" % (repo, pr)}).get("Item") or {}

    def update(self, repo, pr, **fields):
        fields["updated_at"] = int(time.time())
        names = {"#k%d" % i: k for i, k in enumerate(fields)}
        values = {":v%d" % i: v for i, v in enumerate(fields.values())}
        self.table.update_item(
            Key={"pk": "%s#%s" % (repo, pr)},
            UpdateExpression="SET " + ", ".join("#k%d = :v%d" % (i, i) for i in range(len(fields))),
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)


# ---------------------------------------------------------------------------- git

class GitError(RuntimeError):
    pass


def _git(repo_dir, *args, secret=""):
    proc = subprocess.run(["git", "-c", "core.hooksPath=/dev/null"] + list(args), cwd=repo_dir,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(message.replace(secret, "***") if secret else message)
    return proc.stdout.decode("utf-8", "replace")


def checkout(gh_repo, job, parent):
    """Blobless clone of the PR branch, detached at the event's head commit, with the base fetched."""
    work = tempfile.mkdtemp(dir=parent)
    token = gh_repo.token
    _git(parent, "clone", "-q", "--filter=blob:none", "--no-tags", "--branch", job["head_ref"],
         gh_repo.clone_url(), work, secret=token)
    _git(work, "fetch", "-q", "--no-tags", "origin",
         "+refs/heads/%s:refs/remotes/origin/%s" % (job["base_ref"], job["base_ref"]), secret=token)
    if job.get("head_sha"):
        _git(work, "checkout", "-q", "--detach", job["head_sha"], secret=token)
    return work


def use_base_config(work, base_ref):
    """Replace comprehension-gate.json in the working tree with the base branch's version (or none)."""
    path = os.path.join(work, "comprehension-gate.json")
    try:
        text = _git(work, "show", "origin/%s:comprehension-gate.json" % base_ref)
    except GitError:
        text = None
    if os.path.exists(path):
        os.remove(path)
    if text is not None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def decide(work, job):
    """'generate', 'grade' or None for a pull_request job."""
    if job["action"] != "synchronize":
        return "generate"
    try:
        changed = _git(work, "diff", "--name-only", job["before"], job["after"]).split()
    except (GitError, TypeError):
        return "generate"  # history rewritten or unknown: treat as a code change
    if any(not p.startswith(ev.GATE_DIR + "/") for p in changed):
        return "generate"
    if "%s/pr-%s/answers.md" % (ev.GATE_DIR, job["pr"]) in changed:
        return "grade"
    return None


# ---------------------------------------------------------------------------- jobs

def do_generate(gh_repo, state, job, work):
    code = cli.main(["--repo", work, "generate", "--pr", str(job["pr"]),
                     "--base", "origin/" + job["base_ref"], "--head", "HEAD"])
    if code != 0:
        raise RuntimeError("questionnaire generation failed (exit %d)" % code)
    if not _git(work, "status", "--porcelain", "--", ev.GATE_DIR).strip():
        print("no questionnaire written (no code changes)")
        return
    with open(os.path.join(work, ev.GATE_DIR, "pr-%s" % job["pr"], "questionnaire.json"), encoding="utf-8") as f:
        questionnaire = json.load(f)
    digest = qn.sha256_of(questionnaire)

    bot = os.environ.get("BOT_LOGIN", "comprehension-gate[bot]")
    _git(work, "add", "--", ev.GATE_DIR)
    _git(work, "-c", "user.name=%s" % bot, "-c", "user.email=%s@users.noreply.github.com" % bot,
         "commit", "-q", "-m", "comprehension-gate: questionnaire for PR #%s" % job["pr"])
    try:
        _git(work, "push", "-q", "origin", "HEAD:refs/heads/%s" % job["head_ref"], secret=gh_repo.token)
    except GitError as exc:
        print("push rejected, a newer push will be handled by its own event: %s" % exc)
        return
    new_sha = _git(work, "rev-parse", "HEAD").strip()
    state.update(job["repo"], job["pr"], questionnaire_sha=digest, questionnaire_commit=new_sha,
                 risk=questionnaire["risk"], grade_head_sha="", grade_passed=False, signed_sha="")
    gh_repo.comment(job["pr"], questionnaire_comment(job, questionnaire, digest))
    gh_repo.status(new_sha, "pending", GATE, "Waiting for the owner's answers")


def _inline(text, limit=600):
    """Model-written text for a comment: one line, capped, no @-mentions or HTML comments."""
    text = " ".join(str(text).split())[:limit]
    return text.replace("@", "@​").replace("<!--", "&lt;!--")


def questionnaire_comment(job, questionnaire, digest):
    """The 'questionnaire ready' comment: the questions inline, plus a link to answer them in the browser."""
    path = "%s/pr-%s/answers.md" % (ev.GATE_DIR, job["pr"])
    edit = "https://github.com/%s/edit/%s/%s" % (job["repo"], urllib.parse.quote(job["head_ref"], safe="/"), path)
    lines = [
        "### Comprehension gate: questionnaire ready (%s risk, %d questions)"
        % (questionnaire["risk"], len(questionnaire["questions"])), "",
        "Before this pull request can merge, its author answers these questions about the change in "
        "their own words. **[Answer them in the browser](%s)** (write under each question in `%s`, "
        "then commit to this branch), or edit the file locally and push." % (edit, path), "",
    ]
    for n, q in enumerate(questionnaire["questions"], 1):
        lines.append("%d. **%s** - %s, `%s`<br>%s" % (
            n, q["id"], q["area"].replace("_", " "), _inline(q["code_ref"], 120).replace("`", "'"),
            _inline(q["question"])))
    lines += [
        "", "The file also asks you to confirm %d measured facts (authorship, time, versions)."
        % len(questionnaire.get("facts", [])),
        "Answers are graded automatically when you push them. Once they pass, the author comments `/sign`.", "",
        "<!-- comprehension-gate questionnaire-sha256: %s -->" % digest,
    ]
    return "\n".join(lines)


def do_grade(gh_repo, state, job, work, out_parent):
    expected = state.get(job["repo"], job["pr"]).get("questionnaire_sha")
    if not expected:
        gh_repo.comment(job["pr"], "Comprehension gate: no questionnaire was generated by the gate for this pull "
                                   "request yet. Push a code change or reopen the pull request.")
        return
    out = tempfile.mkdtemp(dir=out_parent)
    code = cli.main(["--repo", work, "grade", "--pr", str(job["pr"]), "--base", "origin/" + job["base_ref"],
                     "--head", "HEAD", "--expect-questionnaire-sha", expected, "--out", out])
    sha = job["head_sha"]
    if code == 2:
        gh_repo.status(sha, "error", GATE, "Grading failed - see the gate's comment")
        gh_repo.comment(job["pr"], "Comprehension gate could not grade this push: the questionnaire does not "
                                   "match the one the gate generated, or grading failed. Restore "
                                   "`questionnaire.json` and push your answers again.")
        return
    with open(os.path.join(out, "grade.json"), encoding="utf-8") as f:
        decision = json.load(f)["decision"]
    with open(os.path.join(out, "result.md"), encoding="utf-8") as f:
        gh_repo.comment(job["pr"], f.read())
    percent = "%.1f" % decision["percent"]
    state.update(job["repo"], job["pr"], grade_head_sha=sha, grade_passed=bool(decision["passed"]),
                 grade_percent=percent)
    if decision["passed"]:
        gh_repo.status(sha, "success", GRADED, "Score %s%%" % percent)
        gh_repo.status(sha, "pending", GATE, "Passed grading; waiting for /sign")
    else:
        gh_repo.status(sha, "failure", GRADED, "Score %s%% - see follow-up questions" % percent)
        gh_repo.status(sha, "failure", GATE, "Answers did not pass yet")


def do_sign(gh_repo, state, job):
    pr = gh_repo.pull(job["pr"])
    author = (pr.get("user") or {}).get("login")
    head = (pr.get("head") or {}).get("sha")
    if job.get("commenter") != author:
        gh_repo.comment(job["pr"], "Only the pull request author (@%s) can sign the comprehension "
                                   "questionnaire." % author)
        return
    record = state.get(job["repo"], job["pr"])
    if not (record.get("grade_passed") and record.get("grade_head_sha") == head):
        gh_repo.comment(job["pr"], "Cannot sign yet: the answers on the latest commit have not passed grading.")
        return
    state.update(job["repo"], job["pr"], signed_sha=head, signed_by=author, signed_at=int(time.time()))
    gh_repo.status(head, "success", GATE, "Signed by @%s" % author)
    gh_repo.comment(job["pr"], "Comprehension gate released: signed by @%s for commit `%s`." % (author, head))


# ---------------------------------------------------------------------------- entry point

def process(job, gh_repo, state, scratch="/tmp"):
    if job["kind"] == "sign":
        return do_sign(gh_repo, state, job)
    work = None
    try:
        try:
            work = checkout(gh_repo, job, scratch)
        except GitError as exc:
            print("checkout failed (branch moved or deleted?): %s" % exc)
            return
        what = decide(work, job)
        print("job %s#%s %s -> %s" % (job["repo"], job["pr"], job.get("action"), what))
        if what is None:
            return
        use_base_config(work, job["base_ref"])
        if what == "generate":
            do_generate(gh_repo, state, job, work)
        else:
            do_grade(gh_repo, state, job, work, scratch)
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


def handler(event, context):
    os.environ.setdefault("HOME", "/tmp")  # git needs a writable home
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    configure_model()
    state = State()
    private_key = _ssm(os.environ["APP_PRIVATE_KEY_PARAM"])
    failures = []
    for record in event.get("Records", []):
        job = json.loads(record["body"])
        try:
            token = github.installation_token(os.environ["APP_ID"], private_key, job["installation"])
            gh_repo = github.Repo(job["repo"], token)
        except Exception as exc:  # infrastructure problem: let SQS retry, then the DLQ
            print("cannot get an installation token: %s" % exc)
            failures.append({"itemIdentifier": record["messageId"]})
            continue
        try:
            process(job, gh_repo, state)
        except Exception as exc:  # gate problem (model refusal, bad diff): report it, do not retry
            print("job failed: %s" % str(exc).replace(token, "***"))
            try:
                if job.get("head_sha"):
                    gh_repo.status(job["head_sha"], "error", GATE, "The gate failed - see its logs")
            except Exception:
                pass
    return {"batchItemFailures": failures}
