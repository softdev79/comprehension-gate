"""Minimal GitHub App client: app JWT, installation tokens and the few REST calls the gate makes.

Uses urllib from the standard library. Signing the app JWT needs PyJWT with the
cryptography backend, imported only where it is used (the worker image has it).
"""
import json
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
USER_AGENT = "comprehension-gate"


class GitHubError(RuntimeError):
    def __init__(self, status, message):
        super().__init__("GitHub API %s: %s" % (status, message))
        self.status = status


def app_jwt(app_id, private_key_pem, now=None):
    import jwt  # PyJWT[crypto]

    now = int(now or time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": str(app_id)}  # GitHub allows at most 10 minutes
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def _request(method, url, token, body=None, auth_scheme="token"):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "%s %s" % (auth_scheme, token),
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise GitHubError(exc.code, detail)


def installation_token(app_id, private_key_pem, installation_id):
    """A one-hour token scoped to the repositories the App is installed on."""
    url = "%s/app/installations/%s/access_tokens" % (API, int(installation_id))
    return _request("POST", url, app_jwt(app_id, private_key_pem), {}, auth_scheme="Bearer")["token"]


class Repo:
    """REST calls for one repository with an installation token."""

    def __init__(self, full_name, token):
        if full_name.count("/") != 1:
            raise ValueError("repository must be owner/name")
        self.full_name = full_name
        self.token = token

    def _url(self, path):
        return "%s/repos/%s%s" % (API, self.full_name, path)

    def pull(self, number):
        return _request("GET", self._url("/pulls/%d" % int(number)), self.token)

    def comment(self, number, body):
        return _request("POST", self._url("/issues/%d/comments" % int(number)), self.token, {"body": body})

    def status(self, sha, state, context, description, target_url=None):
        body = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        return _request("POST", self._url("/statuses/%s" % sha), self.token, body)

    def clone_url(self):
        return "https://x-access-token:%s@github.com/%s.git" % (self.token, self.full_name)
