"""Optional hosted PR/MR creation after a human-approved push."""

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from process_runner import StageError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HostedPullRequests:
    def __init__(self, provider, api, repository, token):
        parsed = urlsplit(api)
        if (provider not in {"github", "gitlab"} or parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not token or "\r" in token or "\n" in token):
            raise ValueError("Configure a supported PR provider, HTTPS API origin and token")
        if not repository or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."}
                                     for part in repository.split("/")):
            raise ValueError("Invalid configured PR repository")
        if provider == "github" and repository.count("/") != 1:
            raise ValueError("GitHub repository must use owner/name")
        self.provider, self.api, self.repository, self.token = provider, api.rstrip("/"), repository, token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    @classmethod
    def from_env(cls):
        if provider := os.environ.get("UNITY_PR_PROVIDER"):
            return cls(provider, os.environ.get("UNITY_PR_API_URL", "https://api.github.com" if provider == "github" else ""),
                       os.environ.get("UNITY_PR_REPOSITORY", ""), os.environ.get("UNITY_PR_TOKEN", ""))
        return None

    def request(self, path, payload=None):
        headers = {"Accept": "application/json", "User-Agent": "UnityWorker"}
        if self.provider == "github":
            headers["Authorization"] = "Bearer " + self.token
        else:
            headers["PRIVATE-TOKEN"] = self.token
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(Request(self.api + path, headers=headers, data=data), timeout=20) as response:
                body = response.read(1_000_001)
                if len(body) > 1_000_000:
                    raise ValueError("PR response too large")
                return json.loads(body)
        except HTTPError as error:
            raise StageError("pr_failed", f"{self.provider} API returned HTTP {error.code}") from error
        except (URLError, OSError, ValueError) as error:
            raise StageError("pr_failed", f"{self.provider} API is unavailable or returned invalid data") from error

    def create(self, task):
        head, base = task["task_branch"], task["base_branch"]
        title = f"Unity Worker task {task['task_id']}"
        if self.provider == "github":
            owner = self.repository.split("/")[0]
            endpoint = f"/repos/{self.repository}/pulls"
            query = {"state": "all", "head": f"{owner}:{head}", "base": base}
            payload = {"head": head, "base": base, "title": title}
            id_key, url_key = "number", "html_url"
        else:
            endpoint = f"/projects/{quote(self.repository, safe='')}/merge_requests"
            query = {"scope": "all", "state": "all", "source_branch": head, "target_branch": base}
            payload = {"source_branch": head, "target_branch": base, "title": title}
            id_key, url_key = "iid", "web_url"
        matches = self.request(endpoint + "?" + urlencode(query))
        if not isinstance(matches, list):
            raise StageError("pr_failed", "PR lookup returned invalid data")
        record = matches[0] if matches else self.request(endpoint, payload)
        if not isinstance(record, dict) or not isinstance(record.get(id_key), int):
            raise StageError("pr_failed", "PR creation returned invalid data")
        url = record.get(url_key)
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise StageError("pr_failed", "PR response did not contain a safe URL")
        return {"provider": self.provider, "id": record[id_key], "url": url, "state": record.get("state")}
