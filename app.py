#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ForkSync — GitHub fork 自动同步工具
==================================

纯标准库实现（Python 3.9+），不需要 pip install 任何东西。

核心能力
  * 通过 GitHub 官方 API 的 merge-upstream 服务端快进/合并，无需本地克隆仓库
  * WebUI 管理：用 git 链接添加自己的 fork，批量/单个同步，查看落后提交数
  * 定时任务：按 N 分钟自动把所有已启用的 fork 同步一遍
  * 三种策略：ff（只快进）/ merge（允许合并提交）/ force（强制对齐，丢弃自己的提交）
  * 可选本地 git 模式：需要顺带同步 tag 或其他分支时使用

启动
  python app.py                 # 默认 http://127.0.0.1:8770
  python app.py --port 9000 --open
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
HOME_DIR = Path(os.environ.get("FORKSYNC_HOME") or APP_DIR)   # 配置与日志目录（可用环境变量覆盖）
CONFIG_PATH = HOME_DIR / "config.json"
LOG_DIR = HOME_DIR / "logs"
LOG_PATH = LOG_DIR / "sync.jsonl"
WEB_DIR = APP_DIR / "webui"
API_BASE = (os.environ.get("FORKSYNC_API_BASE") or "https://api.github.com").rstrip("/")
VERSION = "1.0.0"

CST = timezone(timedelta(hours=8))

# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def human_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        delta = (datetime.now(CST) - dt).total_seconds()
    except Exception:
        return iso
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return dt.strftime("%m-%d %H:%M")


class AppError(Exception):
    """可以直接展示给用户的错误。"""

    def __init__(self, message: str, status: int = 400, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.hint = hint


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 配置存储
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "token": "",
    "auto_sync": False,
    "interval_minutes": 30,
    "default_strategy": "ff",
    "projects": [],
    # 安全阀：上游删除文件时怎么办
    #   off   = 不拦，直接同步（旧行为）
    #   warn  = 允许同步，但把被删文件列给你看（默认）
    #   block = 拦下来，必须你手动确认才同步
    "deletion_guard": "warn",
    # 命中这些模式的路径，即使 guard=warn 也要拦（正则，逗号分隔）
    "protected_paths": "",
}


def new_project(full_name: str, upstream: str, branch: str, strategy: str) -> dict:
    return {
        "id": hashlib.sha1(full_name.lower().encode()).hexdigest()[:12],
        "full_name": full_name,
        "upstream": upstream,
        "branch": branch,
        "strategy": strategy,
        "enabled": True,
        "local_path": "",
        "sync_tags": False,
        # 本项目覆盖全局的删除保护："" 表示跟随全局
        "deletion_guard": "",
        "status": "unknown",
        "behind": None,
        "ahead": None,
        "message": "尚未检查",
        "last_check": None,
        "last_sync": None,
        "sync_count": 0,
        "error_count": 0,
        "created_at": now_iso(),
        # 上一次检查时上游会删掉哪些文件（供 UI 展开查看）
        "pending_deletions": [],
        "pending_deletion_count": 0,
        "last_deletion_ack": "",
    }


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        self.load()

    def load(self) -> None:
        with self.lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    backup = self.path.with_suffix(".broken.json")
                    try:
                        shutil.copy2(self.path, backup)
                    except Exception:
                        pass
                    raw = {}
            else:
                raw = {}
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg.update({k: v for k, v in (raw or {}).items() if k in DEFAULT_CONFIG or k in ("token",)})
            if not isinstance(cfg.get("projects"), list):
                cfg["projects"] = []
            self.cfg = cfg
            if not self.path.exists():
                self.save()

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.path, self.cfg)

    # -- 读 ---------------------------------------------------------------- #
    def public_state(self) -> dict:
        with self.lock:
            cfg = json.loads(json.dumps(self.cfg))
        cfg.pop("token", None)
        cfg.pop("projects", None)
        env_token = bool(os.environ.get("FORKSYNC_TOKEN", "").strip())
        token = self.token()
        cfg["token_set"] = bool(token)
        cfg["token_source"] = "env" if env_token else ("file" if token else "")
        cfg["token_hint"] = ("环境变量 FORKSYNC_TOKEN" if env_token
                             else (token[:4] + "…" + token[-4:]) if len(token) > 12
                             else ("已配置" if token else ""))
        return cfg

    def token(self) -> str:
        env = os.environ.get("FORKSYNC_TOKEN", "").strip()
        if env:
            return env
        with self.lock:
            return (self.cfg.get("token") or "").strip()

    def projects(self) -> list:
        with self.lock:
            return json.loads(json.dumps(self.cfg.get("projects") or []))

    def project(self, pid: str) -> dict:
        with self.lock:
            for p in self.cfg.get("projects") or []:
                if p.get("id") == pid:
                    return json.loads(json.dumps(p))
        raise AppError("项目不存在", 404)

    def upsert(self, project: dict) -> dict:
        with self.lock:
            projects = self.cfg.setdefault("projects", [])
            for i, p in enumerate(projects):
                if p.get("id") == project.get("id"):
                    projects[i] = json.loads(json.dumps(project))
                    break
            else:
                projects.append(json.loads(json.dumps(project)))
            self.cfg["projects"] = projects
            self.save()
        return project

    def patch(self, pid: str, changes: dict) -> dict:
        with self.lock:
            for p in self.cfg.get("projects") or []:
                if p.get("id") == pid:
                    p.update(changes)
                    self.save()
                    return json.loads(json.dumps(p))
        raise AppError("项目不存在", 404)

    def remove(self, pid: str) -> None:
        with self.lock:
            before = len(self.cfg.get("projects") or [])
            self.cfg["projects"] = [p for p in (self.cfg.get("projects") or []) if p.get("id") != pid]
            if len(self.cfg["projects"]) == before:
                raise AppError("项目不存在", 404)
            self.save()


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

class Logger:
    def __init__(self, path: Path, keep: int = 2000):
        self.path = path
        self.keep = keep
        self.buf: deque = deque(maxlen=keep)
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._load_tail()

    def _load_tail(self) -> None:
        try:
            if not self.path.exists():
                return
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-self.keep:]
            for line in lines:
                try:
                    self.buf.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass

    def add(self, level: str, message: str, project: dict | None = None, detail: str = "", actor: str = "system") -> dict:
        entry = {
            "t": now_iso(),
            "level": level,          # info | success | warn | error
            "message": message,
            "detail": (detail or "")[:4000],
            "actor": actor,
            "project_id": (project or {}).get("id", ""),
            "project": (project or {}).get("full_name", ""),
        }
        with self.lock:
            self.buf.append(entry)
            try:
                if self.path.exists() and self.path.stat().st_size > 8 * 1024 * 1024:
                    os.replace(self.path, self.path.with_suffix(".1.jsonl"))
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass
        return entry

    def tail(self, limit: int = 200, project_id: str = "", level: str = "") -> list:
        with self.lock:
            items = list(self.buf)
        if project_id:
            items = [i for i in items if i.get("project_id") == project_id]
        if level:
            items = [i for i in items if i.get("level") == level]
        return items[-limit:][::-1]


# --------------------------------------------------------------------------- #
# GitHub API 客户端
# --------------------------------------------------------------------------- #

class GitHubError(AppError):
    def __init__(self, status: int, message: str, hint: str = "", path: str = ""):
        super().__init__(message, status or 502, hint)
        self.gh_status = status
        self.path = path


def is_workflow_scope_error(message: str) -> bool:
    """GitHub 特有的拦截：token 没有 workflow 权限时，不许创建/修改 .github/workflows 里的文件。"""
    low = (message or "").lower()
    return "workflow" in low and ("scope" in low or "refusing to allow" in low)


def split_workflow_paths(message: str) -> list:
    """从 GitHub 的报错里抠出被拦下的 workflow 文件路径。"""
    return re.findall(r"[`'\"]?(\.github/workflows/[\w.\-/]+)", message or "")


def token_scopes(token: str) -> dict:
    """探测 token 类型与权限范围。

    返回 workflow_scope: yes（有权限）/ no（确认没有）/ unknown（fine-grained，无法从响应头判断）
    """
    _, user, headers = gh("GET", "/user", token=token)
    raw = headers.get("x-oauth-scopes", "")
    classic = "x-oauth-scopes" in headers
    scopes = [s.strip() for s in raw.split(",") if s.strip()]
    if not classic:
        workflow = "unknown"
    else:
        workflow = "yes" if "workflow" in scopes else "no"
    return {
        "login": user.get("login"),
        "name": user.get("name"),
        "classic": classic,
        "scopes": scopes,
        "workflow_scope": workflow,
    }


def gh(method: str, path: str, body=None, token: str = "", timeout: int = 45, raw: bool = False):
    """调用 GitHub REST API，返回 (status, data, headers)。"""
    url = path if path.startswith("http") else API_BASE + path
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method=method.upper())
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", f"ForkSync/{VERSION}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            if raw:
                return resp.status, text, headers
            return resp.status, (json.loads(text) if text.strip() else {}), headers
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        try:
            data = json.loads(text) if text.strip() else {}
        except Exception:
            data = {"message": text[:400]}
        msg = str(data.get("message") or "").strip() or f"HTTP {exc.code}"
        hint = ""
        low = msg.lower()
        if is_workflow_scope_error(msg):
            files = split_workflow_paths(msg)
            flist = "、".join(files) if files else ".github/workflows/ 下的文件"
            hint = (
                f"上游这次带上了 workflow 文件（{flist}），而你的 token 没有 workflow 权限，"
                "GitHub 拒绝创建/更新这类文件。两个办法：\n"
                "① 给 token 补上 workflow 权限（classic token 直接勾选 workflow 后重新生成并更新到本工具）；\n"
                "② 或者不要同步这个仓库的 workflow 改动（改用本地 git 模式手动处理）。"
            )
        elif exc.code in (401, 403) and ("bad credentials" in low or exc.code == 401):
            hint = "token 无效或已过期，请到 Settings → Developer settings → Personal access tokens 重新生成"
        elif exc.code == 403 and "rate limit" in low:
            hint = "触发 API 限流。未配置 token 时每小时只有 60 次请求，配置 token 后为 5000 次/小时"
        elif exc.code == 403:
            hint = "token 权限不足。私有仓库需要 repo 权限，公开仓库 public_repo 即可"
        elif exc.code == 404:
            hint = "仓库不存在，或 token 无权访问（私有仓库必须带 token）"
        elif exc.code == 409:
            hint = "上游与你的分支存在冲突提交，需要手动解决或改用 force 策略"
        raise GitHubError(exc.code, msg, hint, path)
    except urllib.error.URLError as exc:
        raise GitHubError(0, f"网络错误：{exc.reason}", "检查代理 / DNS，或稍后重试", path)
    except TimeoutError:
        raise GitHubError(0, "请求 GitHub 超时", "网络较慢，稍后重试", path)


def parse_repo_input(text: str) -> str:
    """把各种 git 链接整理成 owner/repo。"""
    s = (text or "").strip()
    if not s:
        raise AppError("请粘贴 fork 仓库链接，例如 https://github.com/你的名字/仓库")
    s = re.split(r"[?#]", s)[0].strip()
    s = re.sub(r"^git@github\.com:", "", s)
    s = re.sub(r"^ssh://git@github\.com/", "", s)
    s = re.sub(r"^https?://([^@/]+@)?(www\.)?github\.com/", "", s, flags=re.I)
    s = s.strip().strip("/")
    if s.endswith(".git"):
        s = s[:-4]
    parts = [p for p in s.split("/") if p]
    if len(parts) < 2:
        raise AppError(f"无法识别仓库：{text}", hint="支持 https://github.com/owner/repo、git@github.com:owner/repo.git、owner/repo")
    owner, repo = parts[0], parts[1]
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner):
        raise AppError(f"owner 不合法：{owner}")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", repo):
        raise AppError(f"仓库名不合法：{repo}")
    return f"{owner}/{repo}"


# --------------------------------------------------------------------------- #
# 同步引擎
# --------------------------------------------------------------------------- #

SYNC_LOCK = threading.Lock()


def q(seg: str) -> str:
    return urllib.parse.quote(seg, safe="")


def repo_info(full_name: str, token: str) -> dict:
    _, info, _ = gh("GET", f"/repos/{full_name}", token=token)
    return info


def check_project(store: Store, log: Logger, project: dict) -> dict:
    """刷新一个 fork 与上游的差距，返回更新后的 project。"""
    token = store.token()
    full = project["full_name"]
    try:
        info = repo_info(full, token)
    except GitHubError as exc:
        project.update(status="error", message=exc.message, last_check=now_iso())
        project["error_count"] = int(project.get("error_count") or 0) + 1
        store.upsert(project)
        log.add("error", f"{full} 读取仓库信息失败：{exc.message}", project, exc.hint, actor="check")
        raise

    if not info.get("fork"):
        msg = "该仓库不是 fork，没有上游可同步"
        project.update(status="error", message=msg, last_check=now_iso())
        store.upsert(project)
        log.add("error", f"{full} · {msg}", project, actor="check")
        raise AppError(msg, hint="只有 fork 出来的仓库才能从上游合并；如果需要长期跟随，请改用 fork 或 template")

    parent = info.get("parent") or {}
    upstream = parent.get("full_name") or project.get("upstream")
    fork_owner = (info.get("owner") or {}).get("login") or full.split("/")[0]
    branch = project.get("branch") or info.get("default_branch") or "main"
    up_branch = parent.get("default_branch") or branch

    behind = ahead = None
    status = "unknown"
    note = ""
    try:
        _, cmp, _ = gh(
            "GET",
            f"/repos/{upstream}/compare/{q(up_branch)}...{fork_owner}:{q(branch)}",
            token=token,
        )
        behind = int(cmp.get("behind_by") or 0)
        ahead = int(cmp.get("ahead_by") or 0)
        if behind == 0 and ahead == 0:
            status, note = "up-to-date", "与上游一致"
        elif behind == 0:
            status, note = "ahead", f"比上游多 {ahead} 个提交"
        elif ahead == 0:
            status, note = "behind", f"落后上游 {behind} 个提交"
        else:
            status, note = "diverged", f"落后 {behind}，领先 {ahead}"
    except GitHubError as exc:
        status = "unknown"
        note = f"无法比较（分支 {branch} 可能只存在于你的 fork）：{exc.message}"

    project.update(
        upstream=upstream,
        branch=branch,
        behind=behind,
        ahead=ahead,
        status=status,
        message=note,
        last_check=now_iso(),
    )

    # 顺便算一下「同步会删掉哪些文件」，供界面预警；只在确实落后时才算，省 API 额度
    if status in ("behind", "diverged"):
        try:
            chk = incoming_changes(store, project, token)
            if chk["ok"]:
                project["pending_deletions"] = chk["removed"][:200]
                project["pending_deletion_count"] = len(chk["removed"])
                project["pending_renames"] = chk["renamed"][:50]
                if chk["removed"]:
                    extra = f"；将删除 {len(chk['removed'])} 个文件"
                    if chk["truncated"]:
                        extra += "（列表可能被截断）"
                    project["message"] = note + extra
        except Exception:
            pass

    store.upsert(project)
    log.add("info", f"{full} · 检查：{note}", project, actor="check")
    return project


def incoming_changes(store: Store, project: dict, token: str) -> dict:
    """看看「如果现在同步，上游会改动些什么」，重点是删除和重命名。

    必须用 base=fork_sha, head=upstream_sha 这个方向：
    这样 removed 才表示「上游删掉了、同步后你本地也会没有」的文件。
    反方向（现有检查用的 compare）拿不到这个语义。
    """
    full, upstream, branch = project["full_name"], project["upstream"], project["branch"]
    out = {"ok": False, "files": [], "removed": [], "renamed": [],
           "modified": 0, "added": 0, "truncated": False, "total_commits": 0, "error": ""}
    try:
        _, fork_ref, _ = gh("GET", f"/repos/{full}/git/ref/heads/{q(branch)}", token=token)
        _, up_ref, _ = gh("GET", f"/repos/{upstream}/git/ref/heads/{q(branch)}", token=token)
    except GitHubError as exc:
        out["error"] = f"读取分支 sha 失败：{exc.message}"
        return out

    fork_sha = (fork_ref.get("object") or {}).get("sha")
    up_sha = (up_ref.get("object") or {}).get("sha")
    if not fork_sha or not up_sha:
        out["error"] = "拿不到分支 sha"
        return out
    if fork_sha == up_sha:
        out["ok"] = True
        return out

    try:
        _, cmp_, _ = gh("GET", f"/repos/{upstream}/compare/{fork_sha}...{up_sha}", token=token)
    except GitHubError as exc:
        out["error"] = f"比较差异失败：{exc.message}"
        return out

    files = cmp_.get("files") or []
    total = int(cmp_.get("ahead_by") or 0)
    out["total_commits"] = total
    out["ok"] = True
    # compare 接口最多返回 300 个文件；数量对不上就说明被截断，不能假装看全了
    out["truncated"] = bool(total and len(files) >= 300)
    for f in files:
        st = f.get("status")
        name = f.get("filename") or ""
        if st == "removed":
            out["removed"].append(name)
        elif st == "renamed":
            out["renamed"].append(f"{f.get('previous_filename')} → {name}")
        elif st == "added":
            out["added"] += 1
        else:
            out["modified"] += 1
    out["files"] = files
    return out


def match_protected(paths: list, patterns: str) -> list:
    """返回命中了保护规则的路径。patterns 是逗号分隔的正则。"""
    if not patterns.strip():
        return []
    rules = []
    for raw in patterns.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rules.append(re.compile(raw))
        except re.error:
            continue
    hits = []
    for p in paths:
        for rx in rules:
            if rx.search(p):
                hits.append(p)
                break
    return hits


def deletion_guard_mode(store: Store, project: dict) -> str:
    """项目级设置优先于全局。"""
    per = (project.get("deletion_guard") or "").strip().lower()
    if per in ("off", "warn", "block"):
        return per
    glob = str(store.cfg.get("deletion_guard") or "warn").lower()
    return glob if glob in ("off", "warn", "block") else "warn"


def evaluate_deletions(store: Store, project: dict, token: str, force: bool = False) -> dict:
    """同步前的删除检查。返回 {mode, removed, renamed, protected, blocked, detail}。"""
    mode = deletion_guard_mode(store, project)
    chk = incoming_changes(store, project, token)
    result = {
        "mode": mode, "removed": chk["removed"], "renamed": chk["renamed"],
        "protected": [], "blocked": False, "detail": chk, "error": chk["error"],
        "added": chk["added"], "modified": chk["modified"], "truncated": chk["truncated"],
    }
    if not chk["ok"] or mode == "off" or force:
        return result

    protected = match_protected(chk["removed"], str(store.cfg.get("protected_paths") or ""))
    result["protected"] = protected
    if mode == "block" and (chk["removed"] or chk["renamed"]):
        result["blocked"] = True
    if protected:
        # 命中保护名单时，warn 也升级为拦下
        result["blocked"] = True
    return result


def _merge_upstream(store: Store, log: Logger, project: dict, actor: str) -> dict:
    token = store.token()
    full, branch = project["full_name"], project["branch"]
    try:
        status, data, _ = gh("POST", f"/repos/{full}/merge-upstream", {"branch": branch}, token=token)
    except GitHubError as exc:
        if is_workflow_scope_error(exc.message):
            files = split_workflow_paths(exc.message)
            flist = "、".join(files) if files else ".github/workflows/ 下的文件"
            raise AppError(
                f"上游带上了 workflow 文件（{flist}），但 token 缺少 workflow 权限",
                hint=(f"GitHub 规定：token 没有 workflow 权限时，不允许创建或更新 .github/workflows/ 里的文件，"
                      f"所以这次 {branch} 分支无法同步。解决办法：\n"
                      f"① 去 https://github.com/settings/tokens 勾上 workflow（classic token 要重新生成），"
                      f"再把新 token 填回本工具「设置」；\n"
                      f"② 或者改用本地 git 模式：手动 git merge 上游，由你自己决定要不要接受 workflow 改动。"),
            )
        if exc.gh_status == 409:
            raise AppError(
                "同步冲突：上游的改动和你的提交改到了同一处",
                hint="在 GitHub 网页上手动解决冲突，或把策略改成 force（会丢弃你自己的提交）",
            )
        if exc.gh_status == 422:
            raise AppError(
                f"无法合并：{exc.message}",
                hint="确认该分支在上游存在，且此仓库确实是同一 fork 网络中的 fork",
            )
        if exc.gh_status == 404:
            raise AppError("合并失败：仓库或分支不存在（token 是否有该仓库写权限？）")
        raise
    merge_type = (data or {}).get("merge_type") or "fast-forward"
    msg = (data or {}).get("message") or "同步成功"
    return {"merge_type": merge_type, "message": msg}


def _force_align(store: Store, log: Logger, project: dict) -> dict:
    """把 fork 的分支强制指向上游 HEAD，丢弃 fork 上的独有提交。"""
    token = store.token()
    full, upstream, branch = project["full_name"], project["upstream"], project["branch"]
    _, ref, _ = gh("GET", f"/repos/{upstream}/git/ref/heads/{q(branch)}", token=token)
    sha = (ref.get("object") or {}).get("sha")
    if not sha:
        raise AppError("拿不到上游分支的 sha")
    try:
        gh("PATCH", f"/repos/{full}/git/refs/heads/{q(branch)}", {"sha": sha, "force": True}, token=token)
    except GitHubError as exc:
        if exc.gh_status == 422:  # 分支在 fork 上还不存在，创建它
            gh("POST", f"/repos/{full}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}, token=token)
        else:
            raise
    return {"message": f"已强制对齐到上游 {sha[:7]}"}


def _sync_local(store: Store, log: Logger, project: dict, actor: str) -> dict:
    """本地克隆模式：fetch upstream → merge → push，可顺带同步 tag。"""
    git = shutil.which("git")
    if not git:
        raise AppError("未检测到 git，本地模式不可用", hint="安装 Git 后重试，或把策略留空使用 API 模式（无需本地仓库）")
    path = Path(project.get("local_path") or "")
    if not path.exists():
        raise AppError(f"本地路径不存在：{path}")
    token = store.token()
    if not token:
        raise AppError(
            "本地模式推送需要 token",
            hint="本地 git 模式要向 origin 推送，必须配置 token；或清空本地路径改用 API 模式",
        )
    full, upstream, branch = project["full_name"], project["upstream"], project["branch"]
    strategy = project.get("strategy") or "ff"
    lines: list[str] = []

    def run(args: list[str], secret: str = "") -> str:
        proc = subprocess.run(
            [git, "-C", str(path), *args],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if secret:
            out = out.replace(secret, "***")
        shown = "git " + " ".join(args)
        if secret:
            shown = shown.replace(secret, "***")
        lines.append(f"$ {shown}\n{out}")
        if proc.returncode != 0:
            raise AppError(f"git {args[0]} 失败", hint=out[-800:])
        return out

    try:
        run(["rev-parse", "--is-inside-work-tree"])
    except AppError:
        raise AppError(f"{path} 不是 git 工作区", hint="本地路径要指向仓库根目录（含 .git）；先 git clone 再把该目录填到项目设置里")

    remotes = run(["remote"]).split()
    if "upstream" not in remotes:
        run(["remote", "add", "upstream", f"https://github.com/{upstream}.git"])
    else:
        run(["remote", "set-url", "upstream", f"https://github.com/{upstream}.git"])

    auth = f"https://x-access-token:{token}@github.com/{full}.git"
    run(["fetch", "upstream", "--tags", "--prune"])
    run(["fetch", "origin", "--tags", "--prune"])
    try:
        run(["checkout", branch])
    except AppError:
        # 本地还没有这个分支：从上游建一个并跟踪它
        lines.append(f"本地无分支 {branch}，改从 upstream/{branch} 创建")
        run(["checkout", "-B", branch, f"upstream/{branch}"])
    if strategy == "force":
        run(["reset", "--hard", f"upstream/{branch}"])
    elif strategy == "merge":
        run(["merge", "--no-edit", f"upstream/{branch}"])
    else:
        run(["merge", "--ff-only", f"upstream/{branch}"])
    run(["push", auth, f"HEAD:refs/heads/{branch}"], secret=token)
    if project.get("sync_tags"):
        try:
            run(["push", auth, "--tags"], secret=token)
        except AppError:
            lines.append("tags 推送失败（通常是无新 tag），已忽略")
    return {"message": "本地同步完成", "detail": "\n".join(lines)[-4000:]}


def sync_project(store: Store, log: Logger, project: dict, actor: str = "manual",
                 strategy_override: str = "", allow_force: bool = False,
                 allow_deletions: bool = False) -> dict:
    """执行一次同步。返回 {ok, changed, message, project}。"""
    full = project["full_name"]
    strategy = (strategy_override or project.get("strategy") or store.cfg.get("default_strategy") or "ff").lower()

    with SYNC_LOCK:
        try:
            project = check_project(store, log, project)
        except AppError as exc:
            return {"ok": False, "changed": False, "message": exc.message, "hint": exc.hint, "project": store.project(project["id"])}
        status = project.get("status")
        if status == "up-to-date":
            log.add("info", f"{full} 已是最新，跳过", project, actor=actor)
            return {"ok": True, "changed": False, "message": "已是最新，无需同步", "project": project}
        if status == "ahead":
            log.add("info", f"{full} 只领先上游，没有可合并的内容", project, actor=actor)
            return {"ok": True, "changed": False, "message": project.get("message") or "没有可合并的上游提交", "project": project}

        if not store.token() and not project.get("local_path"):
            return {
                "ok": False, "changed": False,
                "message": "尚未配置 GitHub token，无法写入 fork",
                "hint": "右上角「设置」填入 Personal Access Token：公开仓库勾 public_repo，私有仓库勾 repo",
                "project": project,
            }

        # ---- 删除保护：同步前先看这次会不会删掉文件 ----
        token_now = store.token()
        guard = {"mode": deletion_guard_mode(store, project), "removed": [], "renamed": [],
                 "protected": [], "blocked": False, "detail": {}, "error": "",
                 "added": 0, "modified": 0, "truncated": False}
        if token_now:
            guard = evaluate_deletions(store, project, token_now, force=allow_deletions)
            if guard["detail"].get("removed") is not None:
                project["pending_deletions"] = guard["removed"][:200]
                project["pending_deletion_count"] = len(guard["removed"])
                project["pending_renames"] = guard["renamed"][:50]
                store.upsert(project)

            if guard["blocked"]:
                bits = []
                if guard["removed"]:
                    bits.append(f"会删除 {len(guard['removed'])} 个文件")
                if guard["renamed"]:
                    bits.append(f"会重命名 {len(guard['renamed'])} 个文件")
                if guard["protected"]:
                    bits.append(f"其中 {len(guard['protected'])} 个命中保护名单")
                if guard["truncated"]:
                    bits.append("文件列表可能不完整")

                preview = "\n".join("  ✗ " + p for p in guard["removed"][:15])
                if len(guard["removed"]) > 15:
                    preview += f"\n  … 另有 {len(guard['removed']) - 15} 个"
                pro = ""
                if guard["protected"]:
                    pro = "\n\n命中保护名单（这些绝对不能删）：\n" + "\n".join("  ⚠ " + p for p in guard["protected"][:15])

                log.add("warn", f"{full} 已拦下：{'，'.join(bits)}", project,
                        preview + pro, actor=actor)
                return {
                    "ok": False, "changed": False, "blocked": True,
                    "message": f"已拦下本次同步：{'，'.join(bits)}",
                    "hint": (f"上游这次会删掉文件，同步后你 fork 里这些文件也会消失。\n"
                             f"{preview}{pro}\n\n"
                             f"确认要继续就再点一次「确认同步」，或把该项目的删除保护改成 off。"),
                    "deletions": guard["removed"],
                    "project": store.project(project["id"]),
                }
            if guard["removed"]:
                preview = "、".join(guard["removed"][:5])
                log.add("warn", f"{full} 注意：本次同步会删除 {len(guard['removed'])} 个文件（{preview}…）",
                        project, "\n".join(guard["removed"][:50]), actor=actor)

        if project.get("local_path"):
            if strategy == "ff" and (project.get("ahead") or 0) > 0:
                return {
                    "ok": False, "changed": False,
                    "message": f"分支已分叉（你多了 {project['ahead']} 个提交），ff 策略不合并",
                    "hint": "把策略改成 merge，或改成 force 丢弃自己的提交",
                    "project": project,
                }
            try:
                res = _sync_local(store, log, project, actor)
            except AppError as exc:
                project["error_count"] = int(project.get("error_count") or 0) + 1
                store.upsert(project)
                log.add("error", f"{full} 本地同步失败：{exc.message}", project, exc.hint, actor=actor)
                return {"ok": False, "changed": False, "message": exc.message, "hint": exc.hint, "project": store.project(project["id"])}
        else:
            if strategy == "force" or allow_force:
                if not allow_force and (project.get("ahead") or 0) > 0:
                    return {
                        "ok": False, "changed": False,
                        "message": f"force 策略会丢弃你分支上的 {project['ahead']} 个提交",
                        "hint": "确认后使用「强制对齐」按钮（需二次确认）",
                        "project": project,
                    }
                try:
                    res = _force_align(store, log, project)
                except GitHubError as exc:
                    project["error_count"] = int(project.get("error_count") or 0) + 1
                    store.upsert(project)
                    log.add("error", f"{full} 强制对齐失败：{exc.message}", project, exc.hint, actor=actor)
                    return {"ok": False, "changed": False, "message": exc.message, "hint": exc.hint, "project": store.project(project["id"])}
            else:
                if strategy == "ff" and (project.get("ahead") or 0) > 0:
                    return {
                        "ok": False, "changed": False,
                        "message": f"分支已分叉（你多了 {project['ahead']} 个提交），ff 策略不合并",
                        "hint": "把策略改成 merge，或改成 force 丢弃自己的提交",
                        "project": project,
                    }
                try:
                    res = _merge_upstream(store, log, project, actor)
                except GitHubError as exc:
                    project["error_count"] = int(project.get("error_count") or 0) + 1
                    store.upsert(project)
                    log.add("error", f"{full} 同步失败：{exc.message}", project, exc.hint, actor=actor)
                    return {"ok": False, "changed": False, "message": exc.message, "hint": exc.hint, "project": store.project(project["id"])}
                except AppError as exc:
                    project["error_count"] = int(project.get("error_count") or 0) + 1
                    store.upsert(project)
                    log.add("error", f"{full} 同步失败：{exc.message}", project, exc.hint, actor=actor)
                    return {"ok": False, "changed": False, "message": exc.message, "hint": exc.hint, "project": store.project(project["id"])}

        project["last_sync"] = now_iso()
        project["sync_count"] = int(project.get("sync_count") or 0) + 1
        store.upsert(project)
        log.add("success", f"{full} 同步完成：{res.get('message', '')}", project, res.get("detail", ""), actor=actor)

        try:
            project = check_project(store, log, project)
        except AppError:
            pass
        return {"ok": True, "changed": True, "message": res.get("message", "同步完成"), "project": project}


# --------------------------------------------------------------------------- #
# 定时调度
# --------------------------------------------------------------------------- #

class Scheduler:
    def __init__(self, store: Store, log: Logger):
        self.store = store
        self.log = log
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.next_run = 0.0
        self.last_pass = ""
        self.last_pass_result = ""

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="forksync-scheduler", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def status(self) -> dict:
        cfg = self.store.public_state()
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "auto_sync": cfg.get("auto_sync", False),
            "interval_minutes": cfg.get("interval_minutes", 30),
            "next_run_in": max(0, int(self.next_run - time.time())) if self.next_run else None,
            "last_pass": self.last_pass,
            "last_pass_result": self.last_pass_result,
        }

    def kick(self) -> None:
        """配置变更后立即重排。"""
        self.next_run = 0.0

    def _loop(self) -> None:
        while not self.stop_event.wait(2.0):
            cfg = self.store.cfg
            if not cfg.get("auto_sync"):
                self.next_run = 0.0
                continue
            interval = max(1, int(cfg.get("interval_minutes") or 30)) * 60
            now = time.time()
            if self.next_run == 0.0:
                self.next_run = now + interval
                continue
            if now < self.next_run:
                continue
            self.next_run = now + interval
            try:
                self.run_pass()
            except Exception:
                self.log.add("error", "定时任务异常", None, traceback.format_exc()[-1500:], actor="auto")

    def run_pass(self) -> str:
        projects = [p for p in self.store.projects() if p.get("enabled", True)]
        if not projects:
            return "没有启用的项目"
        ok = fail = skipped = 0
        for project in projects:
            try:
                res = sync_project(self.store, self.log, project, actor="auto")
                if res.get("ok") and res.get("changed"):
                    ok += 1
                elif res.get("ok"):
                    skipped += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
                self.log.add("error", f"{project.get('full_name')} 同步异常", project,
                             traceback.format_exc()[-1500:], actor="auto")
        self.last_pass = now_iso()
        self.last_pass_result = f"更新 {ok} 个，跳过 {skipped} 个，失败 {fail} 个"
        self.log.add("info", f"定时任务结束：{self.last_pass_result}", actor="auto")
        return self.last_pass_result


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #

INDEX_HTML = WEB_DIR / "index.html"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = f"ForkSync/{VERSION}"
    protocol_version = "HTTP/1.1"
    store: Store = None          # type: ignore
    log: Logger = None           # type: ignore
    sched: Scheduler = None      # type: ignore
    access_key: str = ""

    # -- 基础 -------------------------------------------------------------- #
    def log_message(self, fmt, *args):  # 静音默认访问日志
        return

    def _json(self, obj, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            raise AppError("请求体不是合法 JSON")

    def _check_auth(self) -> bool:
        if not self.access_key:
            return True
        given = self.headers.get("X-Access-Key") or urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.path).query).get("key", [""])[0]
        if given == self.access_key:
            return True
        self._json({"error": "需要访问密钥", "need_key": True}, 401)
        return False

    def _static(self):
        parts = urllib.parse.urlsplit(self.path)
        rel = "index.html" if parts.path in ("/", "") else parts.path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._json({"error": "forbidden"}, 403)
        if not target.exists() or target.is_dir():
            return self._json({"error": "not found", "path": rel}, 404)
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # -- GET --------------------------------------------------------------- #
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/"):
            return self._static()
        if not self._check_auth():
            return
        try:
            return self._route_get(path)
        except AppError as exc:
            return self._json({"error": exc.message, "hint": exc.hint}, exc.status)
        except Exception:
            return self._json({"error": "服务端异常", "hint": traceback.format_exc()[-1200:]}, 500)

    def _route_get(self, path: str):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        if path == "/api/state":
            projects = self.store.projects()
            cfg = self.store.public_state()
            stats = {
                "total": len(projects),
                "behind": sum(1 for p in projects if p.get("status") == "behind"),
                "diverged": sum(1 for p in projects if p.get("status") == "diverged"),
                "error": sum(1 for p in projects if p.get("status") == "error"),
                "up-to-date": sum(1 for p in projects if p.get("status") == "up-to-date"),
            }
            return self._json({
                "version": VERSION,
                "config": cfg,
                "projects": projects,
                "stats": stats,
                "scheduler": self.sched.status(),
                "now": now_iso(),
            })

        if path == "/api/logs":
            limit = min(1000, max(1, int(query.get("limit", ["200"])[0])))
            return self._json({"logs": self.log.tail(
                limit=limit,
                project_id=query.get("project", [""])[0],
                level=query.get("level", [""])[0],
            )})

        if path == "/api/token/test":
            token = self.store.token()
            if not token:
                return self._json({"ok": False, "error": "尚未配置 token"}, 400)
            info = token_scopes(token)
            _, rl, _ = gh("GET", "/rate_limit", token=token)
            core = (rl.get("resources") or {}).get("core") or {}
            warn = ""
            if info["workflow_scope"] == "no":
                warn = ("该 classic token 没有 workflow 权限：只要上游改动包含 .github/workflows/ 下的文件，"
                        "对应仓库就会同步失败（报 refusing to allow ... without workflow scope）。"
                        "建议到 github.com/settings/tokens 勾上 workflow 后重新生成。")
            elif info["workflow_scope"] == "unknown":
                warn = ("fine-grained token 无法从响应头判断权限。请确认已授予「Workflows: Read and write」，"
                        "否则上游改动含 .github/workflows/ 时会同步失败。")
            return self._json({
                "ok": True,
                "login": info["login"],
                "name": info["name"],
                "classic": info["classic"],
                "scopes": info["scopes"],
                "workflow_scope": info["workflow_scope"],
                "warning": warn,
                "rate": {"limit": core.get("limit"), "remaining": core.get("remaining"), "reset": core.get("reset")},
            })

        raise AppError("接口不存在", 404)

    # -- POST / PATCH / DELETE -------------------------------------------- #
    def do_POST(self):
        return self._mutate("POST")

    def do_PATCH(self):
        return self._mutate("PATCH")

    def do_DELETE(self):
        return self._mutate("DELETE")

    def _mutate(self, method: str):
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if not self._check_auth():
            return
        try:
            body = self._body() if method != "DELETE" else {}
            return self._route_mutate(method, path, body)
        except AppError as exc:
            return self._json({"error": exc.message, "hint": exc.hint}, exc.status)
        except GitHubError as exc:
            return self._json({"error": exc.message, "hint": exc.hint}, 502)
        except Exception:
            return self._json({"error": "服务端异常", "hint": traceback.format_exc()[-1200:]}, 500)

    def _route_mutate(self, method: str, path: str, body: dict):
        # ---- 设置 ---- #
        if path == "/api/settings" and method == "POST":
            changed = []
            with self.store.lock:
                if "token" in body:
                    self.store.cfg["token"] = str(body.get("token") or "").strip()
                    changed.append("token")
                if "auto_sync" in body:
                    self.store.cfg["auto_sync"] = bool(body.get("auto_sync"))
                    changed.append("auto_sync")
                if "interval_minutes" in body:
                    try:
                        iv = int(body.get("interval_minutes"))
                    except Exception:
                        raise AppError("间隔必须是整数分钟")
                    if iv < 1 or iv > 10080:
                        raise AppError("间隔范围 1 ~ 10080 分钟")
                    self.store.cfg["interval_minutes"] = iv
                    changed.append("interval_minutes")
                if "default_strategy" in body:
                    st = str(body.get("default_strategy") or "ff").lower()
                    if st not in ("ff", "merge", "force"):
                        raise AppError("策略只能是 ff / merge / force")
                    self.store.cfg["default_strategy"] = st
                    changed.append("default_strategy")
                if "deletion_guard" in body:
                    dg = str(body.get("deletion_guard") or "warn").lower()
                    if dg not in ("off", "warn", "block"):
                        raise AppError("删除保护只能是 off / warn / block")
                    self.store.cfg["deletion_guard"] = dg
                    changed.append("deletion_guard")
                if "protected_paths" in body:
                    self.store.cfg["protected_paths"] = str(body.get("protected_paths") or "").strip()
                    changed.append("protected_paths")
                self.store.save()
            self.sched.kick()
            self.log.add("info", f"设置已更新：{', '.join(changed) or '无变化'}", actor="settings")
            return self._json({"ok": True, "config": self.store.public_state()})

        # ---- 新增项目 ---- #
        if path == "/api/projects" and method == "POST":
            full = parse_repo_input(str(body.get("input") or body.get("url") or ""))
            strategy = str(body.get("strategy") or self.store.cfg.get("default_strategy") or "ff").lower()
            if strategy not in ("ff", "merge", "force"):
                raise AppError("策略只能是 ff / merge / force")
            branch = str(body.get("branch") or "").strip()
            local_path = str(body.get("local_path") or "").strip()

            token = self.store.token()
            info = repo_info(full, token)
            if not info.get("fork"):
                raise AppError(
                    f"{full} 不是 fork",
                    hint="只能添加 fork 出来的仓库。若想把上游当作源头长期跟随，请先在 GitHub 上 fork 再添加这里",
                )
            parent = info.get("parent") or {}
            upstream = parent.get("full_name")
            if not upstream:
                raise AppError(f"{full} 缺少上游信息，可能上游已删除或转为私有")

            project = new_project(full, upstream, branch or info.get("default_branch") or "main", strategy)
            project["local_path"] = local_path
            existing = [p.get("full_name", "").lower() for p in self.store.projects()]
            if full.lower() in existing:
                raise AppError(f"{full} 已在列表中", hint="直接点「同步」即可")
            self.store.upsert(project)
            self.log.add("info", f"已添加 {full}（上游 {upstream}）", project, actor="add")
            try:
                project = check_project(self.store, self.log, project)
            except AppError:
                project = self.store.project(project["id"])
            return self._json({"ok": True, "project": project})

        m = re.fullmatch(r"/api/projects/([0-9a-f]{6,32})(?:/(sync|check|force))?", path)
        if m:
            pid, action = m.group(1), m.group(2)
            project = self.store.project(pid)

            if method == "PATCH" and not action:
                allowed = {}
                if "enabled" in body:
                    allowed["enabled"] = bool(body.get("enabled"))
                if "strategy" in body:
                    st = str(body.get("strategy") or "ff").lower()
                    if st not in ("ff", "merge", "force"):
                        raise AppError("策略只能是 ff / merge / force")
                    allowed["strategy"] = st
                if "branch" in body:
                    allowed["branch"] = str(body.get("branch") or "").strip() or project.get("branch")
                if "local_path" in body:
                    allowed["local_path"] = str(body.get("local_path") or "").strip()
                if "sync_tags" in body:
                    allowed["sync_tags"] = bool(body.get("sync_tags"))
                if "deletion_guard" in body:
                    dg = str(body.get("deletion_guard") or "").lower()
                    if dg not in ("", "off", "warn", "block"):
                        raise AppError("删除保护只能是 off / warn / block（留空表示跟随全局）")
                    allowed["deletion_guard"] = dg
                if not allowed:
                    raise AppError("没有可更新的字段")
                project = self.store.patch(pid, allowed)
                self.log.add("info", f"{project['full_name']} 配置已更新：{', '.join(allowed)}", project, actor="edit")
                if "branch" in allowed:
                    try:
                        project = check_project(self.store, self.log, project)
                    except AppError:
                        pass
                return self._json({"ok": True, "project": project})

            if method == "DELETE" and not action:
                self.store.remove(pid)
                self.log.add("warn", f"已移除 {project['full_name']}（不影响 GitHub 上的仓库）", project, actor="delete")
                return self._json({"ok": True})

            if method == "POST" and action == "check":
                project = check_project(self.store, self.log, project)
                return self._json({"ok": True, "project": project})

            if method == "POST" and action == "sync":
                res = sync_project(self.store, self.log, project, actor="manual",
                                   strategy_override=str(body.get("strategy") or ""),
                                   allow_force=bool(body.get("force")),
                                   allow_deletions=bool(body.get("allow_deletions")))
                if res.get("ok") and res.get("changed"):
                    self.store.patch(pid, {"last_deletion_ack": now_iso()})
                return self._json(res, 200 if res.get("ok") else 409)

            if method == "POST" and action == "force":
                if not body.get("confirm"):
                    raise AppError("强制对齐需要二次确认", hint="会丢弃你 fork 分支上独有的提交")
                res = sync_project(self.store, self.log, project, actor="manual",
                                   strategy_override="force", allow_force=True,
                                   allow_deletions=bool(body.get("allow_deletions")))
                return self._json(res, 200 if res.get("ok") else 409)

        # ---- 批量 ---- #
        if path == "/api/sync_all" and method == "POST":
            user_projects = [p for p in self.store.projects() if p.get("enabled", True)]
            if not user_projects:
                raise AppError("还没有启用任何项目")
            results = []
            for project in user_projects:
                try:
                    res = sync_project(self.store, self.log, project, actor="manual-batch")
                except AppError as exc:
                    res = {"ok": False, "changed": False, "message": exc.message, "hint": exc.hint,
                           "project": self.store.project(project["id"])}
                except Exception:
                    res = {"ok": False, "changed": False, "message": "同步异常",
                           "hint": traceback.format_exc()[-800:], "project": self.store.project(project["id"])}
                results.append(res)
            summary = {
                "total": len(results),
                "updated": sum(1 for r in results if r.get("ok") and r.get("changed")),
                "skipped": sum(1 for r in results if r.get("ok") and not r.get("changed")),
                "failed": sum(1 for r in results if not r.get("ok")),
            }
            self.log.add("info", f"批量同步结束：更新 {summary['updated']}，跳过 {summary['skipped']}，失败 {summary['failed']}", actor="manual-batch")
            return self._json({"ok": True, "summary": summary, "results": results})

        if path == "/api/check_all" and method == "POST":
            projects = self.store.projects()
            for project in projects:
                try:
                    check_project(self.store, self.log, project)
                except AppError:
                    continue
            return self._json({"ok": True, "projects": self.store.projects()})

        raise AppError("接口不存在", 404)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="ForkSync — GitHub fork 自动同步 WebUI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（只允许本机）")
    parser.add_argument("--port", type=int, default=8770, help="监听端口，默认 8770")
    parser.add_argument("--access-key", default="", help="可选访问密钥；设置后 API 需要带 X-Access-Key")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--interval", type=int, default=0, help="覆盖自动同步间隔（分钟），仅本次运行")
    args = parser.parse_args()

    store = Store(CONFIG_PATH)
    log = Logger(LOG_PATH)
    sched = Scheduler(store, log)

    if args.interval:
        with store.lock:
            store.cfg["interval_minutes"] = max(1, args.interval)
            store.cfg["auto_sync"] = True
            store.save()

    Handler.store = store
    Handler.log = log
    Handler.sched = sched
    Handler.access_key = args.access_key

    sched.start()

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"[x] 端口 {args.port} 无法监听：{exc}\n    换个端口：python app.py --port 8771")
        return 1
    httpd.daemon_threads = True

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print("=" * 62)
    print(f"  ForkSync {VERSION} 已启动")
    print(f"  WebUI : {url}")
    print(f"  配置  : {CONFIG_PATH}")
    print(f"  日志  : {LOG_PATH}")
    print(f"  自动同步: {'开' if store.cfg.get('auto_sync') else '关'}"
          f"（间隔 {store.cfg.get('interval_minutes')} 分钟）")
    print("  Ctrl+C 停止")
    print("=" * 62)

    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 正在退出…")
    finally:
        sched.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
