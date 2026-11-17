"""Command line entry point used by the workflows and by the /comprehension-status command.

Exit codes: 0 = ok / gate passed, 1 = gate not passed, 2 = error.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys

from . import evidence as ev
from . import policy
from . import questionnaire as qn


def pr_dir(repo, pr):
    return os.path.join(repo, ev.GATE_DIR, "pr-%s" % pr)


def _write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _github_output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for key, value in values.items():
                f.write("%s=%s\n" % (key, value))


def cmd_evidence(args):
    data = ev.collect(args.base, args.head, args.repo)
    data["risk"] = policy.risk_tier(data["files"], policy.load_config(args.repo))
    print(json.dumps(data, indent=2))
    return 0


def cmd_generate(args):
    cfg = policy.load_config(args.repo)
    data = ev.collect(args.base, args.head, args.repo)
    if not data["files"]:
        print("No code changes to ask about.")
        _github_output(questionnaire_sha256="", risk="none")
        return 0
    risk = policy.risk_tier(data["files"], cfg)
    count = cfg["questions"][risk]
    if args.offline:
        generated = qn.offline_questions(data, count)
    else:
        from . import agents
        generated = agents.generate_questions(ev.diff_text(args.base, args.head, args.repo), data, risk, count,
                                             repo=args.repo, base=args.base, head=args.head)
    generated["questions"] = generated["questions"][:count]
    questionnaire = qn.assemble(args.pr, risk, data, generated)

    folder = pr_dir(args.repo, args.pr)
    answers_path = os.path.join(folder, "answers.md")
    if os.path.exists(answers_path):  # code changed after answers were written: keep the old ones for reference
        shutil.copyfile(answers_path, os.path.join(folder, "answers.previous.md"))
    _write(os.path.join(folder, "questionnaire.json"), json.dumps(questionnaire, indent=2) + "\n")
    _write(answers_path, qn.render_answers_template(questionnaire) + "\n")

    digest = qn.sha256_of(questionnaire)
    _github_output(questionnaire_sha256=digest, risk=risk)
    print("Wrote %s (%d questions, %s risk, sha256 %s)" % (answers_path, len(questionnaire["questions"]), risk, digest))
    return 0


def render_result(questionnaire, grade, decision):
    lines = [
        "## Comprehension gate: %s" % ("passed" if decision["passed"] else "not passed yet"),
        "",
        "Score **%.1f%%** (needs %d%% for %s-risk changes)." % (decision["percent"], decision["threshold"], decision["risk"]),
        "",
        "| Question | Score | Feedback |",
        "|---|---|---|",
    ]
    for result in grade.get("results", []):
        feedback = " ".join(str(result.get("feedback", "")).split()).replace("|", "\\|")
        lines.append("| %s | %s/%d | %s |" % (result.get("id"), result.get("score"), policy.MAX_SCORE, feedback))
    follow_ups = [(r["id"], r["follow_up"]) for r in grade.get("results", []) if str(r.get("follow_up", "")).strip()]
    if not decision["passed"]:
        lines += ["", "**Why the gate is still closed**", ""] + ["- " + r for r in decision["reasons"]]
        if follow_ups:
            lines += ["", "**Follow-up questions** - extend your answers in `answers.md` and push again", ""]
            lines += ["- **%s:** %s" % (qid, " ".join(text.split())) for qid, text in follow_ups]
    else:
        lines += ["", "The pull request author can now comment `/sign` to sign off and release the gate."]
    if grade.get("overall_comment"):
        lines += ["", "_%s_" % " ".join(grade["overall_comment"].split())]
    return "\n".join(lines) + "\n"


def cmd_grade(args):
    cfg = policy.load_config(args.repo)
    folder = pr_dir(args.repo, args.pr)
    questionnaire = _read_json(os.path.join(folder, "questionnaire.json"))
    digest = qn.sha256_of(questionnaire)
    if args.expect_questionnaire_sha and digest != args.expect_questionnaire_sha:
        print("questionnaire.json does not match the one the gate generated (expected %s, found %s)."
              % (args.expect_questionnaire_sha, digest), file=sys.stderr)
        return 2
    with open(os.path.join(folder, "answers.md"), encoding="utf-8") as f:
        answers = qn.parse_answers(f.read())

    data = ev.collect(args.base, args.head, args.repo)
    missing = [i["id"] for i in questionnaire["facts"] + questionnaire["questions"] if not answers.get(i["id"])]
    if missing:  # do not spend a model call on an incomplete questionnaire
        grade = {"results": [], "overall_comment": "Unanswered: %s." % ", ".join(missing)}
    else:
        from . import agents
        grade = agents.grade_answers(ev.diff_text(args.base, args.head, args.repo), data, questionnaire, answers,
                                     repo=args.repo, base=args.base, head=args.head)
    decision = policy.decide(questionnaire, answers, grade, cfg)

    _write(os.path.join(args.out, "grade.json"), json.dumps(
        {"questionnaire_sha256": digest, "head_sha": data["head_sha"], "grade": grade, "decision": decision}, indent=2) + "\n")
    _write(os.path.join(args.out, "result.md"), render_result(questionnaire, grade, decision))
    _github_output(passed=str(decision["passed"]).lower(), percent=decision["percent"])
    print("passed=%s percent=%s" % (decision["passed"], decision["percent"]))
    return 0 if decision["passed"] else 1


def cmd_predicate(args):
    """Write the attestation predicate and print the subject digest (sha256 of answers.md)."""
    folder = pr_dir(args.repo, args.pr)
    questionnaire = _read_json(os.path.join(folder, "questionnaire.json"))
    with open(os.path.join(folder, "answers.md"), "rb") as f:
        answers_digest = hashlib.sha256(f.read()).hexdigest()
    predicate = {
        "pr": args.pr,
        "head_sha": args.head_sha,
        "signer": args.signer,
        "risk": questionnaire["risk"],
        "questionnaire_sha256": qn.sha256_of(questionnaire),
        "answers_sha256": answers_digest,
        "grade_summary": args.grade_summary,
    }
    _write(args.out, json.dumps(predicate, indent=2) + "\n")
    _github_output(subject_digest="sha256:" + answers_digest)
    print("sha256:" + answers_digest)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="comprehension-gate")
    parser.add_argument("--repo", default=".", help="path to the repository being checked")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("evidence", help="print measured evidence for a change (no model call)")
    p.add_argument("--base", required=True)
    p.add_argument("--head", default="HEAD")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("generate", help="write questionnaire.json and answers.md for a pull request")
    p.add_argument("--pr", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--head", default="HEAD")
    p.add_argument("--offline", action="store_true", help="use template questions instead of calling the model")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("grade", help="grade answers.md and apply the policy")
    p.add_argument("--pr", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--head", default="HEAD")
    p.add_argument("--expect-questionnaire-sha", default="")
    p.add_argument("--out", required=True, help="directory for grade.json and result.md (outside the repo)")
    p.set_defaults(func=cmd_grade)

    p = sub.add_parser("predicate", help="write the sign-off attestation predicate")
    p.add_argument("--pr", required=True)
    p.add_argument("--head-sha", required=True)
    p.add_argument("--signer", required=True)
    p.add_argument("--grade-summary", default="")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_predicate)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # workflows read the exit code; keep the message on stderr
        print("comprehension-gate error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
