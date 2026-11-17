"""Questionnaire assembly, the answers.md format, and hashing."""
import hashlib
import json
import re

HEADING = re.compile(r"^## ([FQ]\d+)\b")


def build_facts(evidence):
    """Measured facts the owner must confirm or dispute. Built from evidence, not by the model."""
    attr = evidence["attribution"]
    eff = evidence["effort"]
    if attr["status"] == "unknown":
        authorship = (
            "No authorship ledger was found, so the AI/human split is unknown (%d lines added). "
            "State what share of this change was AI-generated and which tools were used."
            % attr["total_added"]
        )
    else:
        authorship = (
            "Estimated %s%% AI-written (%d of %d added lines, from the Claude Code ledger). "
            "Confirm, or correct it and name any other AI tools used."
            % (attr["ai_percent"], attr["ai_lines"], attr["total_added"])
        )
    if eff["ai_session_active_minutes"] is None:
        time_fact = "%d commits spanning %s minutes; no AI-session timing was recorded." % (
            eff["commits"], eff["commit_span_minutes"])
    else:
        time_fact = "%s active minutes across %d AI sessions; %d commits spanning %s minutes." % (
            eff["ai_session_active_minutes"], eff["ai_sessions"], eff["commits"], eff["commit_span_minutes"])
    deps = ", ".join("%s %s" % (v["name"], v["version"]) for v in evidence["versions"][:40])
    return [
        {"id": "F1", "title": "AI vs human authorship", "detected": authorship},
        {"id": "F2", "title": "Time taken",
         "detected": time_fact + " Confirm, or give the real total including thinking and review time."},
        {"id": "F3", "title": "Libraries and framework versions",
         "detected": "Declared dependencies: %s. Confirm these are the versions this code was written "
                     "and tested against, and flag any this change adds or upgrades." % (deps or "none detected")},
    ]


def assemble(pr, risk, evidence, generated):
    questions = []
    for i, q in enumerate(generated["questions"], 1):
        questions.append({"id": "Q%d" % i, "area": q["area"], "question": q["question"], "code_ref": q["code_ref"]})
    return {
        "version": 1,
        "pr": pr,
        "head_sha": evidence["head_sha"],
        "risk": risk,
        "change_summary": generated["change_summary"],
        "facts": build_facts(evidence),
        "questions": questions,
    }


def offline_questions(evidence, count):
    """Template questions for trying the flow without an API key. Not a substitute for the real ones."""
    files = [f["path"] for f in evidence["files"]] or ["(no files)"]
    templates = [
        ("business_logic", "What business need does the change in this file serve, and who relies on it?"),
        ("failure_modes", "Name one input or condition that would make this code fail, and what the user would see."),
        ("software_principles", "Which design choice here would you defend in review, and what alternative did you reject?"),
        ("testing", "Which behaviour in this file is not covered by a test, and why is that acceptable?"),
        ("operations", "If this change misbehaves in production, how would you detect it and roll it back?"),
    ]
    questions = []
    for i in range(count):
        area, text = templates[i % len(templates)]
        questions.append({"area": area, "question": text, "code_ref": files[i % len(files)]})
    return {"change_summary": "Offline mode: template questions, not derived from the code.", "questions": questions}


def sha256_of(questionnaire):
    canonical = json.dumps(questionnaire, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _quote(text):
    return "\n".join("> " + line for line in text.splitlines() or [""])


def render_answers_template(questionnaire):
    out = [
        "# Comprehension questionnaire - PR #%s" % questionnaire["pr"],
        "",
        "> Risk tier: **%s**. %s" % (questionnaire["risk"], questionnaire["change_summary"]),
        "> Write your answer under each heading, in your own words. Do not edit the headings or the quoted text.",
        "> Push this file to have it graded, then comment `/sign` on the pull request once it passes.",
        "",
        "## Part 1 - Measured facts (confirm or correct)",
        "",
    ]
    for fact in questionnaire["facts"]:
        out += ["## %s - %s" % (fact["id"], fact["title"]), "", _quote(fact["detected"]), "", "", ""]
    out += ["## Part 2 - Questions about the code", ""]
    for q in questionnaire["questions"]:
        out += ["## %s - %s" % (q["id"], q["area"].replace("_", " ")), "",
                _quote(q["question"]), _quote("Code: " + q["code_ref"]), "", "", ""]
    return "\n".join(out)


def parse_answers(text):
    """{id: answer}. An answer is every non-quoted line under an F/Q heading."""
    answers, current = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            m = HEADING.match(line)
            current = m.group(1) if m else None
            if current:
                answers[current] = []
            continue
        if current and not line.lstrip().startswith(">"):
            answers[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in answers.items()}
