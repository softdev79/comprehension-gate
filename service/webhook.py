"""Lambda behind a Function URL: verify GitHub's signature, keep only gate events, queue them.

Standard library plus boto3 (in the Lambda runtime). It does no git or model work, so it
answers GitHub within its 10-second limit; the worker does the rest from the FIFO queue,
one message at a time per pull request.
"""
import base64
import hashlib
import hmac
import json
import os

_secret_cache = {}


def verify_signature(secret, body, header):
    """True if X-Hub-Signature-256 is the HMAC-SHA256 of the raw body with the webhook secret."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def route(event_name, payload, bot_login):
    """The job for the worker, or None to ignore. Pure function of the webhook payload."""
    if not isinstance(payload, dict):
        return None
    sender = (payload.get("sender") or {}).get("login", "")
    repo = (payload.get("repository") or {}).get("full_name", "")
    installation = (payload.get("installation") or {}).get("id")
    if not repo or not installation:
        return None
    if sender == bot_login:
        return None  # our own questionnaire commits and comments must not trigger the gate again

    if event_name == "pull_request" and payload.get("action") in ("opened", "reopened", "synchronize"):
        pr = payload.get("pull_request") or {}
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
        if head_repo != repo:
            return None  # fork pull requests are not supported yet
        return {
            "kind": "pull_request", "action": payload["action"], "repo": repo, "installation": installation,
            "pr": pr.get("number"), "base_ref": pr["base"]["ref"], "head_ref": pr["head"]["ref"],
            "head_sha": pr["head"]["sha"], "before": payload.get("before"), "after": payload.get("after"),
            "author": (pr.get("user") or {}).get("login"),
        }

    if event_name == "issue_comment" and payload.get("action") == "created":
        issue = payload.get("issue") or {}
        body = ((payload.get("comment") or {}).get("body") or "").strip()
        if issue.get("pull_request") and body.split()[:1] == ["/sign"]:
            return {
                "kind": "sign", "repo": repo, "installation": installation, "pr": issue.get("number"),
                "commenter": ((payload.get("comment") or {}).get("user") or {}).get("login"),
            }
    return None


def _webhook_secret():
    if "value" not in _secret_cache:
        import boto3

        ssm = boto3.client("ssm")
        _secret_cache["value"] = ssm.get_parameter(Name=os.environ["WEBHOOK_SECRET_PARAM"],
                                                   WithDecryption=True)["Parameter"]["Value"]
    return _secret_cache["value"]


def _response(code, text, event_name="", delivery=""):
    # One log line per delivery: status, event and delivery id only (no payload, no secrets).
    print(json.dumps({"status": code, "result": text, "event": event_name, "delivery": delivery}))
    return {"statusCode": code, "headers": {"Content-Type": "text/plain"}, "body": text}


def handler(event, context, sqs=None, secret=None):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode("utf-8")
    event_name = headers.get("x-github-event", "")
    delivery = headers.get("x-github-delivery", "")
    if not verify_signature(secret if secret is not None else _webhook_secret(), body,
                            headers.get("x-hub-signature-256")):
        return _response(401, "bad signature", event_name, delivery)

    if event_name == "ping":
        return _response(200, "pong", event_name, delivery)
    try:
        payload = json.loads(body)
    except ValueError:
        return _response(400, "bad json", event_name, delivery)
    job = route(event_name, payload, os.environ.get("BOT_LOGIN", ""))
    if job is None:
        return _response(202, "ignored", event_name, delivery)

    if sqs is None:
        import boto3

        sqs = boto3.client("sqs")
    sqs.send_message(
        QueueUrl=os.environ["QUEUE_URL"], MessageBody=json.dumps(job),
        MessageGroupId="%s#%s" % (job["repo"], job["pr"]),       # one job at a time per pull request
        MessageDeduplicationId=hashlib.sha256(delivery.encode() or body).hexdigest(),  # GitHub redeliveries
    )
    return _response(202, "queued %s" % job["kind"], event_name, delivery)
