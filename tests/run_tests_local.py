#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地 git 模式的编排逻辑自测（在进程内替换 git 调用，不碰真实仓库、不需要装 git）。

    python tests/run_tests_local.py

验证：不是 git 工作区 / 自动补 upstream remote / 本地无分支时自动创建 /
      ff·merge·force 三种策略的命令 / token 不泄漏到展示文本 /
      无 token 提前拦住 / tags 推送失败被忽略 / 未装 git 时报错清晰
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = "ghp_SECRET_TOKEN_do_not_leak_1234"
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"   [{detail}]" if detail and not ok else ""))


class FakeProc:
    def __init__(self, code, out=""):
        self.returncode = code
        self.stdout = out
        self.stderr = "" if code == 0 else out


class FakeGit:
    """记录每次 git 调用，并按 mode 决定哪些命令失败。"""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls: list[list[str]] = []

    def run(self, args, **kw):
        args = list(args)
        # 去掉 [git, "-C", path] 前缀，只留真实子命令
        sub = args[3:] if len(args) > 2 and args[1] == "-C" else args[1:]
        self.calls.append(sub)
        cmd = sub[0] if sub else ""
        if cmd == "rev-parse" and self.mode == "notrepo":
            return FakeProc(128, "fatal: not a git repository (or any of the parent directories): .git")
        if cmd == "checkout" and self.mode == "nobranch":
            if len(sub) < 2 or sub[1] != "-B":
                return FakeProc(1, f"error: pathspec '{sub[-1]}' did not match any file(s) known to git")
        if cmd == "merge" and self.mode == "mergebad":
            return FakeProc(1, "fatal: Not possible to fast-forward, aborting.")
        if cmd == "push" and "--tags" in sub and self.mode == "tagsfail":
            return FakeProc(1, "error: failed to push some refs to 'origin'")
        return FakeProc(0, f"ok: {cmd}")

    def flat(self):
        return " | ".join(" ".join(c) for c in self.calls)

    def has(self, *prefix):
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)

    def find(self, cmd):
        return [c for c in self.calls if c and c[0] == cmd]


def load_app(home: Path, token: str | None = TOKEN):
    os.environ["FORKSYNC_HOME"] = str(home)
    if token:
        os.environ["FORKSYNC_TOKEN"] = token
    else:
        os.environ.pop("FORKSYNC_TOKEN", None)
    for mod in list(sys.modules):
        if mod == "app":
            del sys.modules[mod]
    import app
    return app


def make_project(app, work: Path, strategy="ff", sync_tags=False):
    return {
        "id": "x", "full_name": "me/proj", "upstream": "up/proj", "branch": "master",
        "strategy": strategy, "local_path": str(work), "sync_tags": sync_tags, "enabled": True,
    }


def drive(app, fake: FakeGit, work: Path, strategy="ff", sync_tags=False):
    """把 app 里的 which/subprocess 换成替身，跑一次 _sync_local。"""
    app.shutil.which = lambda name: "C:/fake/git.exe" if name == "git" else None
    app.subprocess.run = fake.run
    store = app.Store(app.CONFIG_PATH)
    log = app.Logger(app.LOG_PATH)
    return app._sync_local(store, log, make_project(app, work, strategy, sync_tags), "test")


def run_case(app, mode, work, strategy="ff", sync_tags=False):
    fake = FakeGit(mode)
    try:
        res = drive(app, fake, work, strategy, sync_tags)
        return ("OK", res, fake)
    except app.AppError as exc:
        return ("ERR", {"message": exc.message, "hint": exc.hint}, fake)
    except Exception as exc:  # noqa: BLE001
        return ("EXC", {"message": f"{type(exc).__name__}: {exc}"}, fake)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    tmp = Path(tempfile.mkdtemp(prefix="forksync-local-"))
    work = tmp / "repo"
    work.mkdir()
    try:
        app = load_app(tmp)

        print("\n[1] 不是 git 工作区")
        kind, r, fake = run_case(app, "notrepo", work)
        check("报错而不是继续", kind == "ERR", str(r))
        check("提示『不是 git 工作区』", "不是 git 工作区" in r.get("message", ""), str(r))
        check("提醒要指向含 .git 的仓库根", ".git" in (r.get("hint") or ""), str(r))

        print("\n[2] ff 快进：命令顺序")
        kind, r, fake = run_case(app, "ok", work)
        check("成功返回", kind == "OK", str(r))
        check("先 rev-parse 校验", fake.calls and fake.calls[0][:1] == ["rev-parse"], fake.flat())
        check("读 remote 列表", fake.has("remote"), fake.flat())
        check("补 upstream remote", fake.has("remote", "add"), fake.flat())
        check("fetch upstream --tags --prune",
              any("upstream" in c and "--tags" in c for c in fake.find("fetch")), fake.flat())
        check("fetch origin", any("origin" in c for c in fake.find("fetch")), fake.flat())
        check("checkout master", fake.has("checkout", "master"), fake.flat())
        check("ff 用 merge --ff-only upstream/master",
              fake.has("merge", "--ff-only", "upstream/master"), fake.flat())
        check("最后 push 到 origin", fake.has("push") and "refs/heads/master" in fake.flat(), fake.flat())
        check("update-ref 没有被误用", not fake.has("update-ref"), fake.flat())

        print("\n[3] upstream 已存在时用 set-url 而非重复 add")
        fake = FakeGit("ok")
        fake.existing = ["origin", "upstream"]

        class FakeGitWithUpstream(FakeGit):
            def run(self, args, **kw):
                args = list(args)
                sub = args[3:] if len(args) > 2 and args[1] == "-C" else args[1:]
                if sub and sub[0] == "remote" and len(sub) == 1:
                    self.calls.append(sub)
                    return FakeProc(0, "origin\nupstream")
                return super().run(args, **kw)

        fake = FakeGitWithUpstream("ok")
        app.shutil.which = lambda name: "C:/fake/git.exe" if name == "git" else None
        app.subprocess.run = fake.run
        app._sync_local(app.Store(app.CONFIG_PATH), app.Logger(app.LOG_PATH),
                        make_project(app, work), "test")
        check("调用了 remote set-url", fake.has("remote", "set-url"), fake.flat())
        check("没有再 remote add", not fake.has("remote", "add"), fake.flat())

        print("\n[4] 本地没有目标分支 → 自动从 upstream 创建")
        kind, r, fake = run_case(app, "nobranch", work)
        check("整体成功", kind == "OK", str(r))
        check("回退到 checkout -B master upstream/master",
              fake.has("checkout", "-B", "master", "upstream/master"), fake.flat())
        check("细节里说明了创建动作", "创建" in (r.get("detail") or ""), str(r)[:200])

        print("\n[5] merge / force 策略")
        _, _, fake = run_case(app, "ok", work, strategy="merge")
        check("merge 用 merge --no-edit", fake.has("merge", "--no-edit"), fake.flat())
        _, _, fake = run_case(app, "ok", work, strategy="force")
        check("force 用 reset --hard upstream/master",
              fake.has("reset", "--hard", "upstream/master"), fake.flat())

        print("\n[6] 合并失败应如实报错")
        kind, r, fake = run_case(app, "mergebad", work, strategy="ff")
        check("抛错并带 git 原文", kind == "ERR" and "fast-forward" in (r.get("hint") or ""), str(r))

        print("\n[7] token 不能出现在展示文本里")
        kind, r, fake = run_case(app, "ok", work)
        blob = str(r)
        check("结果文本里没有明文 token", TOKEN not in blob, blob[:200])
        check("有打码占位 ***", "***" in blob, blob[:200])
        push_urls = [c[1] for c in fake.find("push") if len(c) > 1]
        check("push 真的带上了 token（说明脱敏只作用于展示）",
              any(TOKEN in u for u in push_urls), str(push_urls)[:200])

        print("\n[8] tags 推送失败被忽略，不影响整体")
        kind, r, fake = run_case(app, "tagsfail", work, sync_tags=True)
        check("整体仍成功", kind == "OK", str(r))
        check("注明了 tags 被忽略", "忽略" in (r.get("detail") or ""), str(r)[:300])

        print("\n[9] 无 token → 提前拦住")
        app2 = load_app(tmp, token=None)
        kind, r, fake = run_case(app2, "ok", work)
        check("报错", kind == "ERR", str(r))
        check("说明本地模式推送需要 token", "本地模式推送需要 token" in r.get("message", ""), str(r))
        check("拦截发生在改动本地仓库之前（没有 remote add）",
              not fake.has("remote", "add"), fake.flat())

        print("\n[10] 未安装 git → 报错清晰")
        app3 = load_app(tmp, token=TOKEN)
        app3.shutil.which = lambda name: None
        try:
            app3._sync_local(app3.Store(app3.CONFIG_PATH), app3.Logger(app3.LOG_PATH),
                             make_project(app3, work), "test")
            check("应当抛错", False, "没有抛错")
        except app3.AppError as exc:
            check("提示未检测到 git", "未检测到 git" in exc.message, exc.message)
            check("给出替代方案", "API 模式" in (exc.hint or ""), exc.hint)

        print("\n[11] 本地路径不存在")
        app4 = load_app(tmp, token=TOKEN)
        app4.shutil.which = lambda name: "C:/fake/git.exe"
        try:
            app4._sync_local(app4.Store(app4.CONFIG_PATH), app4.Logger(app4.LOG_PATH),
                             make_project(app4, tmp / "missing"), "test")
            check("应当抛错", False, "没有抛错")
        except app4.AppError as exc:
            check("提示路径不存在", "不存在" in exc.message, exc.message)

    finally:
        os.environ.pop("FORKSYNC_HOME", None)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"   ✗ {f}")
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
