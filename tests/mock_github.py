#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线自测用的假 GitHub API（仅用于验证 ForkSync 的合并逻辑，不联网）。

    python tests/mock_github.py 8799
    FORKSYNC_API_BASE=http://127.0.0.1:8799 FORKSYNC_HOME=./tests/tmp \
        python app.py --port 8771

控制接口（模拟不同上游状态）：
    curl -X POST localhost:8799/_ctl -d '{"behind":5,"ahead":2,"conflict":false}'
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"behind": 3, "ahead": 0, "conflict": False, "merged": 0, "forced": 0,
         "sha": "a" * 40, "workflow_block": False, "scopes": "repo, workflow",
         "files": [], "fork_sha": "b" * 40, "up_sha": "a" * 40}
FORK = "me/proj"
UP = "up/proj"
BRANCH = "master"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == f"/repos/{FORK}":
            return self._send(200, {
                "full_name": FORK, "fork": True, "default_branch": BRANCH,
                "owner": {"login": "me"}, "parent": {"full_name": UP, "default_branch": BRANCH},
            })
        if p == f"/repos/{UP}":
            return self._send(200, {"full_name": UP, "fork": False, "default_branch": BRANCH})
        if p.startswith(f"/repos/{UP}/compare/"):
            return self._send(200, {
                "behind_by": STATE["behind"], "ahead_by": STATE["ahead"],
                "files": STATE.get("files") or [],
                "commits": [{"sha": "b" * 40}] * min(STATE["behind"], 3),
            })
        if p.startswith(f"/repos/{FORK}/git/ref/heads/"):
            return self._send(200, {"ref": f"refs/heads/{BRANCH}", "object": {"sha": STATE["fork_sha"]}})
        if p == f"/repos/{UP}/git/ref/heads/{BRANCH}":
            return self._send(200, {"ref": f"refs/heads/{BRANCH}", "object": {"sha": STATE.get("up_sha") or STATE["sha"]}})
        if p == "/user":
            scopes = STATE.get("scopes")
            hdr = {"X-OAuth-Scopes": scopes} if scopes is not None else None
            return self._send(200, {"login": "tester", "name": "ForkSync Tester"}, hdr)
        if p == "/_state":
            return self._send(200, STATE)
        if p == "/rate_limit":
            return self._send(200, {"resources": {"core": {"limit": 5000, "remaining": 4999, "reset": 0}}})
        return self._send(404, {"message": "Not Found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        p = self.path.split("?")[0]

        if p == "/_ctl":
            STATE.update({k: v for k, v in body.items() if k in STATE})
            return self._send(200, STATE)
        if p == "/_state":
            return self._send(200, STATE)
        if p == f"/repos/{FORK}/merge-upstream":
            if STATE.get("workflow_block"):
                return self._send(422, {"message": "refusing to allow a Personal Access Token to create or "
                                                   "update workflow `.github/workflows/build.yml` without "
                                                   "`workflow` scope"})
            if STATE["conflict"]:
                return self._send(409, {"message": "Merge conflict"})
            if STATE["behind"] == 0:
                return self._send(200, {"message": "Up to date", "merge_type": None})
            STATE["behind"] = 0
            STATE["merged"] += 1
            kind = "merge" if STATE["ahead"] else "fast-forward"
            return self._send(200, {
                "message": f"Successfully fetched and {kind}ed from {UP}",
                "merge_type": kind,
                "base_branch": BRANCH,
            })
        if p == f"/repos/{FORK}/git/refs":
            STATE["forced"] += 1
            return self._send(201, {"ref": body.get("ref"), "object": {"sha": body.get("sha")}})
        return self._send(404, {"message": "Not Found"})

    def do_PATCH(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        p = self.path.split("?")[0]
        if p.startswith(f"/repos/{FORK}/git/refs/heads/"):
            if STATE["behind"] == 0:
                return self._send(422, {"message": "Reference does not exist"})
            STATE["forced"] += 1
            STATE["behind"] = 0
            return self._send(200, {"object": {"sha": STATE["sha"]}})
        return self._send(404, {"message": "Not Found"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    print(f"mock github api on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
