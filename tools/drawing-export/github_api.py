"""GitHub client for Stage B. stdlib only.

Unlike Onshape, GitHub is NOT quota-constrained here (5,000 req/hr authenticated),
so this side can retry freely. The SQLite manifest is the seam: Stage B re-runs
against PDFs Stage A already paid for, and a GitHub outage never costs Onshape quota.

Matching note: we LIST all issues and match client-side rather than using the
search API. Search has indexing lag -- a freshly created issue is not immediately
findable -- which in a create-then-search flow produces duplicates. Listing is
exact and immediate.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, owner, repo, token=None):
        self.owner, self.repo, self.token = owner, repo, token
        self._issues = None

    def _request(self, method, path, body=None):
        url = path if path.startswith("http") else API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "frc1250-drawing-export")
        if data:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            raise GitHubError("{} {} -> {}: {}".format(method, url, e.code, e.read()[:200]))

    def issues(self, refresh=False):
        """All issues, open and closed. ~1 call per 100. Cached per process."""
        if self._issues is not None and not refresh:
            return self._issues
        out, page = [], 1
        while True:
            batch, _ = self._request(
                "GET",
                "/repos/{}/{}/issues?state=all&per_page=100&page={}".format(
                    self.owner, self.repo, page
                ),
            )
            # Pull requests come back from the issues endpoint too; exclude them.
            out.extend(i for i in batch if "pull_request" not in i)
            if len(batch) < 100:
                break
            page += 1
        self._issues = out
        return out

    def find_by_identifier(self, ident):
        """Issues whose title or body contains `ident` as a whole token.

        Returns a list -- 0, 1, or many. Callers must handle ambiguity; silently
        commenting on the wrong issue is worse than commenting on none.
        """
        needle = ident.lower()
        hits = []
        for i in self.issues():
            hay = (i["title"] or "").lower() + "\n" + (i.get("body") or "").lower()
            if needle in hay:
                hits.append(i)
        return hits

    def comment(self, number, text):
        r, _ = self._request(
            "POST",
            "/repos/{}/{}/issues/{}/comments".format(self.owner, self.repo, number),
            {"body": text},
        )
        return r["html_url"]

    def create_issue(self, title, body):
        r, _ = self._request(
            "POST", "/repos/{}/{}/issues".format(self.owner, self.repo),
            {"title": title, "body": body},
        )
        return r["number"], r["html_url"]
