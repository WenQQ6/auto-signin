#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机凭证导出 / 注入脚本（在你自己电脑上运行，不在云端跑）。

作用：
    读取本机 WorkBuddy 桌面端的登录态文件 workbuddy-desktop.info，
    把 accessToken / refreshToken / userId 加密写入 GitHub 仓库的 Actions Secrets。
    Actions 端只从 Secret 读取，仓库里不会出现任何明文 token。

为什么云端不需要你的电脑常开：
    实测（2026-09-28）POST /v2/plugin/auth/token/refresh 用同一枚 refreshToken
    可反复换取新 token，服务端不会立即作废旧值。因此 Actions 每次运行都能自己续期。
    但 token 有约 60 天有效期，建议每隔一两个月在本机重跑一次本脚本兜底。

用法：
    # 1) 推荐：先安装 GitHub CLI 并 gh auth login
    python inject_secrets.py --repo <你的用户名>/auto-signin

    # 2) 无 gh CLI：用 Personal Access Token（需 repo + Secrets 读写权限）
    set GH_TOKEN=ghp_xxx           # Windows
    python inject_secrets.py --repo <用户名>/auto-signin

    # 想顺手把 PushPlus token 也写进去
    python inject_secrets.py --repo <用户名>/auto-signin --pushplus-token <你的token>

依赖：
    gh 方式无需额外依赖；REST 方式需要 pynacl（pip install pynacl）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

AUTH_BASENAME = os.path.join("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")


# --------------------------------------------------------------------------
# 1. 找到本机登录态文件
# --------------------------------------------------------------------------
def find_auth_file() -> str:
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        return override

    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    candidates = [
        os.path.join(local, AUTH_BASENAME),                                    # Windows 桌面端
        os.path.join(home, "Library", "Application Support", AUTH_BASENAME),    # macOS 桌面端
        os.path.join(xdg, "CodeBuddy", "auth", "workbuddy-desktop.info"),       # Linux CLI
        os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info"),     # 兜底
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise SystemExit(
        "未找到 WorkBuddy 登录态文件，检查过：\n  " + "\n  ".join(candidates) +
        "\n请先登录 WorkBuddy 桌面端，或用 WORKBUDDY_AUTH_FILE 指定路径。"
    )


def read_credentials(path: str) -> dict:
    """读取并抽取需要的字段。返回的 dict 含明文 token，只在内存里流转。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except PermissionError:
        raise SystemExit("登录态文件正被 WorkBuddy 占用（它在刷新 token），等几秒再试。")

    auth = data.get("auth") or {}
    account = data.get("account") or {}

    at = auth.get("accessToken")
    rt = auth.get("refreshToken")
    uid = account.get("uid")

    # 新版客户端可能把凭据包成加密信封，本脚本不处理，明确报错而不是静默失败
    for name, val in (("accessToken", at), ("refreshToken", rt)):
        if isinstance(val, str) and val.startswith("$wbEncrypted"):
            raise SystemExit(
                "%s 是加密信封（$wbEncrypted），本脚本无法直接使用。\n"
                "请改用仓库自带的 signin.py 思路，或先把桌面端升级/重新登录后重试。" % name
            )

    if not at:
        raise SystemExit("登录态文件里没有 accessToken，请重新登录 WorkBuddy 桌面端。")

    return {
        "WB_ACCESS_TOKEN": at,
        "WB_REFRESH_TOKEN": rt or "",
        "WB_USER_ID": uid or "",
        "_nickname": account.get("nickname") or "-",
        "_expiresAt": auth.get("expiresAt"),
        "_refreshExpiresAt": auth.get("refreshExpiresAt"),
    }


def fmt_ts(ms) -> str:
    if not isinstance(ms, (int, float)):
        return "-"
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# 2a. 用 gh CLI 写入
# --------------------------------------------------------------------------
def inject_via_gh(repo: str, secrets: dict) -> None:
    for name, value in secrets.items():
        subprocess.run(["gh", "secret", "set", name, "--repo", repo, "--body", value],
                       check=True, stdout=subprocess.DEVNULL)


# --------------------------------------------------------------------------
# 2b. 用 REST API + libsodium sealed box 写入
# --------------------------------------------------------------------------
def inject_via_api(repo: str, secrets: dict) -> None:
    try:
        from nacl import encoding, public
    except ImportError:
        raise SystemExit(
            "REST 方式需要 pynacl：\n  pip install pynacl\n"
            "或者改用 GitHub CLI（gh auth login 后无需任何 Python 依赖）。"
        )

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("未设置 GH_TOKEN 环境变量（需要 repo + Secrets 读写权限）。")

    def api(method: str, path: str, payload=None):
        req = urllib.request.Request(
            "https://api.github.com" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "workbuddy-inject"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raise SystemExit("GitHub API %s %s 失败：HTTP %s %s" %
                             (method, path, e.code, e.read().decode()[:300]))

    _, key_info = api("GET", "/repos/%s/actions/secrets/public-key" % repo)
    pk = public.PublicKey(key_info["key"].encode(), encoding.Base64Encoder())

    for name, value in secrets.items():
        sealed = base64.b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()
        api("PUT", "/repos/%s/actions/secrets/%s" % (repo, name),
            {"encrypted_value": sealed, "key_id": key_info["key_id"]})


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="把本机 WorkBuddy 登录态注入 GitHub 仓库 Secrets")
    ap.add_argument("--repo", required=True, help="owner/repo，例如 wenqiqin6/auto-signin")
    ap.add_argument("--pushplus-token", default="", help="可选：顺便写入 PUSHPLUS_TOKEN")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要写入的字段名，不实际写")
    args = ap.parse_args()

    path = find_auth_file()
    print("登录态文件：%s" % path)
    creds = read_credentials(path)
    print("账号：%s" % creds["_nickname"])
    print("accessToken 有效期至：%s" % fmt_ts(creds["_expiresAt"]))
    print("refreshToken 有效期至：%s" % fmt_ts(creds["_refreshExpiresAt"]))

    secrets = {
        "WB_ACCESS_TOKEN": creds["WB_ACCESS_TOKEN"],
        "WB_REFRESH_TOKEN": creds["WB_REFRESH_TOKEN"],
        "WB_USER_ID": creds["WB_USER_ID"],
    }
    if args.pushplus_token:
        secrets["PUSHPLUS_TOKEN"] = args.pushplus_token
    secrets = {k: v for k, v in secrets.items() if v}

    print("\n准备写入 %s 的 Secrets：%s" % (args.repo, ", ".join(sorted(secrets))))
    if args.dry_run:
        print("（dry-run，未实际写入）")
        return 0

    if shutil.which("gh"):
        print("方式：GitHub CLI")
        inject_via_gh(args.repo, secrets)
    else:
        print("方式：GitHub REST API（pynacl 加密）")
        inject_via_api(args.repo, secrets)

    print("完成。Actions 下次运行即可使用新凭证（无需重跑 workflow 定义）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
