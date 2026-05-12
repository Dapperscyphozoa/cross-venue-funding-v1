"""GitHub-backed state persistence.

Writes JSON state to a file in a GitHub repo and reads it on boot.
Free (uses existing GitHub token), no infra cost. Slight latency per write.

Usage:
    from engine.github_state import GithubState
    gs = GithubState("Dapperscyphozoa", "multica", "engine_state/cvf.json")
    state = gs.load()  # dict
    state["positions"] = {...}
    gs.save(state)
"""
from __future__ import annotations
import base64
import json
import os
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Optional


class GithubState:
    """Tiny KV-over-GitHub-repo client. One file per engine."""

    def __init__(self, owner: str, repo: str, path: str,
                  branch: str = "main", token: Optional[str] = None):
        self.owner = owner
        self.repo = repo
        self.path = path
        self.branch = branch
        self.token = token or os.environ.get("GITHUB_TOKEN") or ""
        self._sha: Optional[str] = None
        self._lock = threading.Lock()
        self._base = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"

    def _hdrs(self) -> dict:
        h = {"Accept": "application/vnd.github+json",
             "User-Agent": "multica-state/1.0"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def load(self, default: Any = None) -> Any:
        """Read state from GitHub. Returns default if file missing."""
        if default is None:
            default = {}
        try:
            req = urllib.request.Request(f"{self._base}?ref={self.branch}", headers=self._hdrs())
            with urllib.request.urlopen(req, timeout=12) as r:
                body = json.loads(r.read())
            self._sha = body.get("sha")
            content_b64 = body.get("content", "").replace("\n", "")
            if not content_b64: return default
            raw = base64.b64decode(content_b64).decode("utf-8")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return default   # file doesn't exist yet
            print(f"[github_state] load HTTP {e.code}: {e.read().decode()[:200]}", flush=True)
            return default
        except Exception as e:
            print(f"[github_state] load err: {e}", flush=True)
            return default

    def save(self, state: Any, message: Optional[str] = None) -> bool:
        """Write state to GitHub. Returns True on success."""
        if not self.token:
            print(f"[github_state] no GITHUB_TOKEN — skipping save", flush=True)
            return False
        with self._lock:
            try:
                # If no sha yet, try a quick load to get it (idempotent)
                if self._sha is None:
                    self.load()
                payload = {
                    "message": message or f"state: update {self.path} @ {int(time.time())}",
                    "content": base64.b64encode(
                        json.dumps(state, separators=(",", ":"), default=str).encode("utf-8")
                    ).decode("ascii"),
                    "branch": self.branch,
                }
                if self._sha:
                    payload["sha"] = self._sha
                req = urllib.request.Request(
                    self._base, data=json.dumps(payload).encode(),
                    method="PUT", headers=self._hdrs())
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = json.loads(r.read())
                self._sha = body.get("content", {}).get("sha")
                return True
            except urllib.error.HTTPError as e:
                err_body = e.read().decode()[:300]
                if e.code == 409:
                    # sha conflict — reload and retry once
                    self._sha = None
                    self.load()
                    try:
                        if self._sha:
                            payload["sha"] = self._sha
                        req = urllib.request.Request(self._base, data=json.dumps(payload).encode(),
                                                      method="PUT", headers=self._hdrs())
                        with urllib.request.urlopen(req, timeout=15) as r:
                            body = json.loads(r.read())
                        self._sha = body.get("content", {}).get("sha")
                        return True
                    except Exception as e2:
                        print(f"[github_state] retry failed: {e2}", flush=True)
                        return False
                print(f"[github_state] save HTTP {e.code}: {err_body}", flush=True)
                return False
            except Exception as e:
                print(f"[github_state] save err: {e}", flush=True)
                return False
