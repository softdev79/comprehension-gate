"""Deterministic pass/fail rules. The model scores answers; only this file decides the gate."""
import fnmatch
import json
import os

MAX_SCORE = 4

DEFAULTS = {
    # questions asked, and percentage needed to pass, per risk tier
    "questions": {"low": 3, "medium": 6, "high": 10},
    "pass_percent": {"low": 60, "medium": 70, "high": 80},
    "high_risk_paths": [
        "*auth*", "*login*", "*payment*", "*billing*", "*crypto*", "*secret*",
        "*security*", "*migration*", "*.sql", "*permission*", ".github/workflows/*",
    ],
    "high_risk_lines": 400,
    "medium_risk_lines": 50,
    "medium_risk_files": 5,
}


def load_config(repo="."):
    """Defaults overridden by comprehension-gate.json in the repo root, if present."""
    cfg = json.loads(json.dumps(DEFAULTS))
    path = os.path.join(repo, "comprehension-gate.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for key, value in json.load(f).items():
                if isinstance(cfg.get(key), dict) and isinstance(value, dict):
                    cfg[key].update(value)
                else:
                    cfg[key] = value
    return cfg


def risk_tier(files, cfg):
    added = sum(f["added"] for f in files)
    for f in files:
        path = f["path"].lower()
        if any(fnmatch.fnmatch(path, pattern) for pattern in cfg["high_risk_paths"]):
            return "high"
    if added >= cfg["high_risk_lines"]:
        return "high"
    if added >= cfg["medium_risk_lines"] or len(files) >= cfg["medium_risk_files"]:
        return "medium"
    return "low"


def decide(questionnaire, answers, grade, cfg):
    """Return {passed, percent, threshold, reasons}. Any reason means the gate stays closed."""
    reasons = []
    risk = questionnaire["risk"]
    threshold = cfg["pass_percent"][risk]
    fact_ids = [f["id"] for f in questionnaire["facts"]]
    question_ids = [q["id"] for q in questionnaire["questions"]]

    for item_id in fact_ids + question_ids:
        if not answers.get(item_id, "").strip():
            reasons.append("%s has no answer" % item_id)

    scores = {}
    for result in grade.get("results", []):
        score = result.get("score")
        if result.get("id") in question_ids and isinstance(score, int) and 0 <= score <= MAX_SCORE:
            scores[result["id"]] = score
    for qid in question_ids:
        if qid not in scores:
            reasons.append("%s was not graded" % qid)
        elif scores[qid] == 0:
            reasons.append("%s scored 0 - every question needs a substantive answer" % qid)

    percent = 0.0
    if question_ids:
        percent = round(100.0 * sum(scores.values()) / (MAX_SCORE * len(question_ids)), 1)
    if percent < threshold:
        reasons.append("score %.1f%% is below the %d%% needed for %s-risk changes" % (percent, threshold, risk))

    return {"passed": not reasons, "percent": percent, "threshold": threshold, "risk": risk, "reasons": reasons}
