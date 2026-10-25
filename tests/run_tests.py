#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ForkSync 离线自测：用假 GitHub API 把全部分支跑一遍（不联网、不动真实仓库）。

    python tests/run_tests.py

覆盖：添加项目 / 状态检查 / ff 快进 / 已最新跳过 / 冲突 409 / 分叉拒绝 /
      merge 策略 / 强制对齐 / 无 token 守卫 / 批量接口 / 参数校验 / 删除
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
PY = sys.executable

MOCK_PORT, APP_PORT, APP2_PORT = 8799, 8771, 8772
MOCK = f"http://127.0.0.1:{MOCK_PORT}"
APP = f"http://127.0.0.1:{APP_PORT}"
APP2 = f"http://127.0.0.1:{APP2_PORT}"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"   [{detail}]" if detail and not ok else ""))


def req(url: str, method: str = "GET", body=None, timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, (json.loads(raw) if raw.strip() else {})
        except Exception:
            return exc.code, {"raw": raw}
    except Exception as exc:  # 连接失败
        return 0, {"error": str(exc)}


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_up(url: str, seconds: int = 20) -> bool:
    for _ in range(seconds * 4):
        code, _ = req(url, timeout=3)
        if code == 200:
            return True
        time.sleep(0.25)
    return False


def start(args: list[str], home: Path, extra_env: dict | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env["FORKSYNC_API_BASE"] = MOCK
    env["FORKSYNC_HOME"] = str(home)
    env.pop("FORKSYNC_TOKEN", None)
    env.update(extra_env or {})
    return subprocess.Popen([PY, *args], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ctl(**kw) -> dict:
    _, data = req(f"{MOCK}/_ctl", "POST", kw)
    return data


def main() -> int:
    for p in (MOCK_PORT, APP_PORT, APP2_PORT):
        if port_busy(p):
            print(f"[x] 端口 {p} 已被占用，请先关闭旧的 ForkSync / mock 进程")
            return 1

    tmp = HERE / "tmp"
    tmp2 = HERE / "tmp2"
    for d in (tmp, tmp2):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)

    procs: list[subprocess.Popen] = []
    try:
        print("\n[1] 启动 mock GitHub API 与两个 ForkSync 实例")
        procs.append(start([str(HERE / "mock_github.py"), str(MOCK_PORT)], tmp))
        procs.append(start(["app.py", "--port", str(APP_PORT)], tmp,
                           {"FORKSYNC_TOKEN": "ghp_dummy_token_1234567890"}))
        procs.append(start(["app.py", "--port", str(APP2_PORT)], tmp2))  # 无 token
        ok = wait_up(f"{APP}/api/state") and wait_up(f"{APP2}/api/state") and wait_up(f"{MOCK}/_state")
        check("三个服务都起来了", ok)
        if not ok:
            return 1

        print("\n[2] 添加 fork")
        ctl(behind=3, ahead=0, conflict=False)
        code, r = req(f"{APP}/api/projects", "POST", {"input": "https://github.com/me/proj", "strategy": "ff"})
        check("add 返回 200", code == 200, str(r))
        proj = r.get("project", {})
        pid = proj.get("id", "")
        check("识别出上游 up/proj", proj.get("upstream") == "up/proj", str(proj))
        check("默认分支 = master", proj.get("branch") == "master", str(proj))
        check("状态 = 落后 3", proj.get("status") == "behind" and proj.get("behind") == 3, str(proj))
        check("token 来源标记为 env", req(f"{APP}/api/state")[1].get("config", {}).get("token_source") == "env")

        print("\n[3] ff 快进同步")
        code, r = req(f"{APP}/api/projects/{pid}/sync", "POST", {})
        check("sync 返回 200", code == 200, str(r))
        check("changed = true", r.get("changed") is True, str(r))
        check("消息包含 fast-forwarded", "fast-forwarded" in (r.get("message") or ""), str(r))
        check("同步后状态 = up-to-date", r.get("project", {}).get("status") == "up-to-date", str(r))
        check("sync_count 累加为 1", r.get("project", {}).get("sync_count") == 1, str(r))
        check("last_sync 已写入", bool(r.get("project", {}).get("last_sync")))
        check("mock 记录 1 次合并", req(f"{MOCK}/_state")[1].get("merged") == 1)

        print("\n[4] 已最新时重复同步")
        code, r = req(f"{APP}/api/projects/{pid}/sync", "POST", {})
        check("跳过而非报错", code == 200 and r.get("changed") is False, str(r))
        check("提示已是最新", "最新" in (r.get("message") or ""), str(r))

        print("\n[5] 冲突场景（上游改动与自身提交冲突）")
        ctl(behind=2, ahead=0, conflict=True)
        code, r = req(f"{APP}/api/projects/{pid}/sync", "POST", {})
        check("返回 409", code == 409, str(r))
        check("提示同步冲突", "冲突" in (r.get("message") or ""), str(r))
        check("带解决建议", bool(r.get("hint")), str(r))
        check("error_count 记录为 1", r.get("project", {}).get("error_count") == 1, str(r))

        print("\n[6] 分叉 + ff 策略应拒绝")
        ctl(behind=2, ahead=3, conflict=False)
        code, r = req(f"{APP}/api/projects/{pid}/sync", "POST", {})
        check("返回 409", code == 409, str(r))
        check("提示已分叉", "分叉" in (r.get("message") or ""), str(r))
        check("建议改用 merge/force", "merge" in (r.get("hint") or ""), str(r))

        print("\n[7] merge 策略合并分叉分支")
        req(f"{APP}/api/projects/{pid}", "PATCH", {"strategy": "merge"})
        code, r = req(f"{APP}/api/projects/{pid}/sync", "POST", {})
        check("sync 成功", code == 200 and r.get("changed") is True, str(r))
        check("merge_type = merge", "merge" in (r.get("message") or ""), str(r))
        check("合并后 behind 归零", r.get("project", {}).get("behind") == 0, str(r))

        print("\n[8] token 缺少 workflow 权限时的提示")
        ctl(behind=2, ahead=0, conflict=False, workflow_block=True,
            scopes="repo")
        req(f"{APP}/api/projects/{pid}", "PATCH", {"strategy": "ff"})
        code, r = req(f"{APP}/api/projects/{pid}/sync", "POST", {})
        check("返回失败", code == 409 and r.get("ok") is False, str(r))
        check("点明是 workflow 权限问题", "workflow" in (r.get("message") or ""), str(r))
        check("报出被拦的文件名", ".github/workflows/build.yml" in (r.get("message") or ""), str(r))
        check("给出补权限的办法", "workflow" in (r.get("hint") or "") and "settings/tokens" in (r.get("hint") or ""), str(r))
        check("不再原样抛 GitHub 英文", "refusing to allow" not in (r.get("message") or ""), str(r))

        print("\n[9] token 权限探测能提前预警")
        code, r = req(f"{APP}/api/token/test")
        check("识别为 classic token", r.get("classic") is True, str(r))
        check("判定缺少 workflow 权限", r.get("workflow_scope") == "no", str(r))
        check("给出预警文案", "workflow" in (r.get("warning") or ""), str(r))
        ctl(scopes="repo, workflow")
        code, r = req(f"{APP}/api/token/test")
        check("补上权限后不再预警", r.get("workflow_scope") == "yes" and not r.get("warning"), str(r))
        ctl(scopes=None)
        code, r = req(f"{APP}/api/token/test")
        check("fine-grained 无法判定时提示 unknown",
              r.get("classic") is False and r.get("workflow_scope") == "unknown", str(r))

        print("\n[10] 删除保护：上游删文件时必须预警")
        ctl(behind=2, ahead=0, conflict=False, workflow_block=False,
            fork_sha="b" * 40, up_sha="a" * 40,
            files=[{"filename": "Ua/prompts/Dev-debugging.md", "status": "removed"},
                   {"filename": "COMPRESSION-ANALYSIS.md", "status": "removed"},
                   {"filename": "Ua/inject.cpp", "status": "modified"},
                   {"filename": "prompts/Ua.md", "status": "added"}])
        req(f"{APP}/api/settings", "POST", {"deletion_guard": "warn"})
        code, r = req(f"{APP}/api/projects/{pid}/check", "POST", {})
        check("检查时列出将被删除的文件",
              r.get("project", {}).get("pending_deletion_count") == 2, str(r.get("project", {}))[:300])
        check("消息里提示了删除数量",
              "删除 2 个文件" in (r.get("project", {}).get("message") or ""), str(r.get("project", {}))[:200])
        check("记录了被删路径",
              "Ua/prompts/Dev-debugging.md" in (r.get("project", {}).get("pending_deletions") or []),
              str(r.get("project", {}).get("pending_deletions")))

        print("\n[10b] warn 模式：放行但记录下来")
        code, r = req(f"{APP}/api/projects/{pid}/sync", "POST", {})
        check("warn 模式不拦", r.get("blocked") is not True, str(r)[:200])
        code, r = req(f"{APP}/api/logs?limit=40&level=warn")
        check("日志里有删除预警", any("删除" in (l.get("message") or "") for l in r.get("logs", [])), str(r)[:300])

        print("\n[10c] block 模式：必须手动确认")
        ctl(behind=2, ahead=0, conflict=False, fork_sha="b" * 40, up_sha="a" * 40,
            files=[{"filename": "important/config.yml", "status": "removed"}])
        req(f"{APP}/api/settings", "POST", {"deletion_guard": "block"})
        merged_before = req(f"{MOCK}/_state")[1].get("merged")
        code, r = req(f"{APP}/api/projects/{pid}/sync", "POST", {})
        check("被拦下", code == 409 and r.get("blocked") is True, str(r)[:250])
        check("返回被删文件列表", r.get("deletions") == ["important/config.yml"], str(r.get("deletions")))
        check("提示里有被删文件", "important/config.yml" in (r.get("hint") or ""), str(r)[:300])
        check("确认前没有真的合并", req(f"{MOCK}/_state")[1].get("merged") == merged_before,
              f"{merged_before} -> {req(f'{MOCK}/_state')[1].get('merged')}")

        print("\n[10d] 确认后放行")
        code, r = req(f"{APP}/api/projects/{pid}/sync", "POST", {"allow_deletions": True})
        check("带确认后成功", r.get("ok") is True, str(r)[:200])
        check("确实合并了", req(f"{MOCK}/_state")[1].get("merged") == merged_before + 1)

        print("\n[10e] 保护名单：warn 模式也拦")
        ctl(behind=2, ahead=0, conflict=False, fork_sha="b" * 40, up_sha="a" * 40,
            files=[{"filename": "docs/manual.md", "status": "removed"},
                   {"filename": "tmp/a.log", "status": "removed"}])
        req(f"{APP}/api/settings", "POST", {"deletion_guard": "warn", "protected_paths": r"\.md$"})
        code, r = req(f"{APP}/api/projects/{pid}/sync", "POST", {})
        check("命中保护名单被拦下", r.get("blocked") is True, str(r)[:250])
        hint = r.get("hint") or ""
        pro_part = hint.split("命中保护名单")[-1] if "命中保护名单" in hint else ""
        check("保护名单段落里只列了 md", "docs/manual.md" in pro_part and "tmp/a.log" not in pro_part,
              pro_part[:200])
        check("两个删除都被列出（预览与保护名单分开）",
              "tmp/a.log" in hint and "docs/manual.md" in hint, hint[:300])
        req(f"{APP}/api/settings", "POST", {"protected_paths": ""})

        print("\n[10f] 项目级覆盖全局")
        req(f"{APP}/api/settings", "POST", {"deletion_guard": "block"})
        code, r = req(f"{APP}/api/projects/{pid}", "PATCH", {"deletion_guard": "off"})
        check("项目级 off 生效", r.get("project", {}).get("deletion_guard") == "off", str(r)[:200])
        code, r = req(f"{APP}/api/projects/{pid}/sync", "POST", {})
        check("off 时不拦（直接同步）", r.get("blocked") is not True, str(r)[:200])
        req(f"{APP}/api/projects/{pid}", "PATCH", {"deletion_guard": ""})
        req(f"{APP}/api/settings", "POST", {"deletion_guard": "warn"})

        print("\n[10g] 非法保护配置被拒")
        code, r = req(f"{APP}/api/settings", "POST", {"deletion_guard": "xxx"})
        check("非法 guard 被拒", code == 400, str(r))

        print("\n[11] 强制对齐")
        ctl(behind=2, ahead=1, conflict=False, workflow_block=False,
            files=[], fork_sha="b" * 40, up_sha="a" * 40)
        req(f"{APP}/api/projects/{pid}", "PATCH", {"strategy": "ff"})
        code, r = req(f"{APP}/api/projects/{pid}/force", "POST", {})
        check("缺少确认时拒绝", code == 400 and "确认" in (r.get("error") or ""), str(r))
        code, r = req(f"{APP}/api/projects/{pid}/force", "POST", {"confirm": True})
        check("确认后成功", code == 200 and r.get("ok"), str(r))
        check("提示已强制对齐", "强制对齐" in (r.get("message") or ""), str(r))
        check("mock 记录 1 次强制", req(f"{MOCK}/_state")[1].get("forced") == 1)

        print("\n[11] 无 token 实例应被守卫拦住")
        ctl(behind=5, ahead=0, conflict=False, files=[], fork_sha="b" * 40, up_sha="a" * 40)
        merged_before = req(f"{MOCK}/_state")[1].get("merged", 0)
        code, r = req(f"{APP2}/api/projects", "POST", {"input": "me/proj"})
        check("仍可添加并检查（只读）", code == 200 and r.get("project", {}).get("behind") == 5, str(r))
        pid2 = r.get("project", {}).get("id", "")
        code, r = req(f"{APP2}/api/projects/{pid2}/sync", "POST", {})
        check("写入被拒绝", code == 409 and "尚未配置 GitHub token" in (r.get("message") or ""), str(r))
        check("给出了配置指引", "Personal Access Token" in (r.get("hint") or ""), str(r))
        check("mock 未被误触发合并", req(f"{MOCK}/_state")[1].get("merged", 0) == merged_before,
              f"{merged_before} -> {req(f'{MOCK}/_state')[1].get('merged')}")

        print("\n[12] 参数校验与重复添加")
        code, r = req(f"{APP2}/api/projects", "POST", {"input": "me/proj"})
        check("重复添加被拒", code == 400 and "已在列表" in (r.get("error") or ""), str(r))
        code, r = req(f"{APP2}/api/projects", "POST", {"input": "git@github.com:me/proj.git"})
        check("git@ 形式也能识别为重复", code == 400, str(r))
        code, r = req(f"{APP2}/api/settings", "POST", {"interval_minutes": 0})
        check("非法间隔被拒", code == 400, str(r))
        code, r = req(f"{APP2}/api/settings", "POST", {"default_strategy": "xxx"})
        check("非法策略被拒", code == 400, str(r))
        code, r = req(f"{APP2}/api/nope")
        check("未知接口 404", code == 404, str(r))

        print("\n[13] 批量接口与日志")
        ctl(behind=1, ahead=0, conflict=False)
        code, r = req(f"{APP}/api/sync_all", "POST", {})
        check("sync_all 成功", code == 200 and r.get("ok"), str(r))
        check("summary 字段齐全",
              all(k in r.get("summary", {}) for k in ("total", "updated", "skipped", "failed")), str(r))
        code, r = req(f"{APP}/api/check_all", "POST", {})
        check("check_all 成功", code == 200 and r.get("ok"), str(r))
        code, r = req(f"{APP}/api/logs?limit=50")
        check("日志可读且有内容", code == 200 and len(r.get("logs", [])) > 3, str(r))
        check("日志含 success 级别", any(l.get("level") == "success" for l in r.get("logs", [])))
        code, r = req(f"{APP}/api/logs?limit=50&level=error")
        check("日志可按级别过滤", all(l.get("level") == "error" for l in r.get("logs", [])))

        print("\n[14] 项目设置与删除")
        code, r = req(f"{APP}/api/projects/{pid}", "PATCH",
                      {"enabled": False, "branch": "dev", "local_path": "", "sync_tags": True})
        p = r.get("project", {})
        check("PATCH 生效", code == 200 and p.get("enabled") is False and p.get("sync_tags") is True, str(r))
        code, r = req(f"{APP}/api/sync_all", "POST", {})
        check("停用项目不参与批量", code == 400 and "没有启用" in (r.get("error") or ""), str(r))
        code, r = req(f"{APP}/api/projects/{pid}", "DELETE")
        check("删除成功", code == 200, str(r))
        check("列表已清空", len(req(f"{APP}/api/state")[1].get("projects", [])) == 0)
        code, r = req(f"{APP}/api/projects/{pid}", "DELETE")
        check("重复删除返回 404", code == 404, str(r))

    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(0.4)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(tmp2, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"   ✗ {f}")
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:  # Windows 控制台默认 GBK，强制 UTF-8 才能输出 ✓ / ✗
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
