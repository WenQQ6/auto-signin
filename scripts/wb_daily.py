#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy「Buddy 加油站」每日签到 + 猫猫旅行 —— 云端 runner 版。

一次运行把两件事都做掉：
  1) 签到   POST /v2/billing/meter/checkin-activity-status  → 未签则 POST /v2/billing/meter/daily-checkin
  2) 猫猫   GET  /v2/activity/growth/buddy/travel/status
            → state=arrived 先 POST .../buddy/travel/claim 把到家的积分领掉
            → 领完后若 state=idle 且 daily_limit_reached=false 才 POST .../buddy/travel/depart
              （今天已经派过 ⇒ daily_limit_reached=true ⇒ 直接跳过，不白撞墙）

硬约束（与需求一一对应）：
  * 凭证只从环境变量读（GitHub Actions Secrets），绝不读本机登录态、绝不写入磁盘。
  * 所有日志/输出统一脱敏，明文 token 不会出现在 Actions 日志里。
  * 猫猫模块的任何异常都被就地捕获，不改变签到结论；签到成功 ⇒ 进程退出码 0。
  * 解释器路径不写死，由 workflow 用 `command -v` 动态解析后再调用本文件。

退出码：
  0  签到成功（含"今日已签到"）—— 无论猫猫是否成功
  1  签到失败（认证/网络/服务端错误）
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# 配置（全部可由环境变量覆盖；接口路径均为实测确认）
# --------------------------------------------------------------------------
API_BASE = os.environ.get("WB_API_BASE", "https://copilot.tencent.com").rstrip("/")
DOMAIN = os.environ.get("WB_DOMAIN", "www.codebuddy.cn")

P_SIGNIN_STATUS = "/v2/billing/meter/checkin-activity-status"
P_SIGNIN_CLAIM = "/v2/billing/meter/daily-checkin"

G = "/v2/activity/growth"
P_TRAVEL_STATUS = G + "/buddy/travel/status"
P_TRAVEL_CLAIM = G + "/buddy/travel/claim"
P_TRAVEL_CONFIG = G + "/buddy/travel/config"
P_TRAVEL_DEPART = G + "/buddy/travel/depart"

P_TOKEN_REFRESH = "/v2/plugin/auth/token/refresh"

HTTP_TIMEOUT = 25
USER_AGENT = "WorkBuddy"

# 脱敏用：把实际明文 token 换成占位符
_SECRETS: list[str] = []
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def register_secret(value: str) -> None:
    """登记一个明文敏感值，后续所有输出都会被它替换掉。"""
    if value and len(value) >= 8:
        _SECRETS.append(value)


def redact(text: str) -> str:
    """脱敏：真实 token → 占位符；任何漏网的 JWT/UUID 也一并打码。"""
    out = str(text)
    for s in _SECRETS:
        out = out.replace(s, "<REDACTED>")
    out = _JWT_RE.sub("<REDACTED_JWT>", out)
    out = _UUID_RE.sub("<REDACTED_UUID>", out)
    return out


def log(msg: str) -> None:
    print(redact(msg), flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _request(method: str, path: str, token: str | None = None, payload=None,
             extra_headers: dict | None = None, retries: int = 0) -> tuple[int, object]:
    """发一次请求，返回 (状态码, 解析后的 body)。

    写操作（签到/领取/派发）默认不重试：若服务端已处理完才超时，重试会重复提交。
    只对 GET 或显式 retries>0 的调用做退避重试（5xx / 网络类）。
    """
    url = API_BASE + path
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": USER_AGENT, "X-Domain": DOMAIN}
    if token:
        headers["Authorization"] = "Bearer " + token
    uid = os.environ.get("WB_USER_ID", "")
    if uid:
        headers["X-User-Id"] = uid
    if extra_headers:
        headers.update(extra_headers)

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    ctx = ssl.create_default_context()

    last: tuple[int, object] = (-1, {"error": "not attempted"})
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
                raw = r.read().decode("utf-8", "replace")
                try:
                    return r.status, json.loads(raw)
                except Exception:
                    return r.status, {"raw": raw[:800]}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                parsed: object = json.loads(raw)
            except Exception:
                parsed = {"raw": raw[:800]}
            last = (e.code, parsed)
            # 4xx 是业务规则/参数问题，重试一百次也是同一个答案
            if e.code < 500:
                return last
        except Exception as e:  # 网络不可达 / 超时
            last = (-1, {"error": "%s: %s" % (type(e).__name__, e)})
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return last


def unwrap(body: object) -> dict:
    """统一取出业务数据：{code,msg,requestId,data:{...}} → data。"""
    if isinstance(body, dict):
        if isinstance(body.get("data"), dict):
            return body["data"]
        return body
    return {}


# --------------------------------------------------------------------------
# 1. 取 token：优先 refreshToken 换新，失败回落到 accessToken
# --------------------------------------------------------------------------
def resolve_token() -> tuple[str | None, str]:
    """返回 (token, 来源说明)。不打印任何 token 明文。"""
    refresh_token = os.environ.get("WB_REFRESH_TOKEN", "").strip()
    access_token = os.environ.get("WB_ACCESS_TOKEN", "").strip()
    register_secret(refresh_token)
    register_secret(access_token)

    if refresh_token:
        code, body = _request(
            "POST", P_TOKEN_REFRESH, payload={},
            extra_headers={"X-Refresh-Token": refresh_token,
                           "X-Auth-Refresh-Source": "plugin"},
            retries=1,
        )
        data = unwrap(body)
        new_at = data.get("accessToken")
        if code == 200 and isinstance(new_at, str) and new_at:
            register_secret(new_at)
            rt = data.get("refreshToken")
            if isinstance(rt, str):
                register_secret(rt)
            return new_at, "refreshToken 续期成功（HTTP 200）"
        log("  ! refreshToken 续期失败：HTTP %s %s" %
            (code, redact(str(body)[:200])))
        if access_token:
            return access_token, "续期失败，回落到仓库里的 accessToken"
        return None, "续期失败且没有可用的 accessToken"

    if access_token:
        return access_token, "直接使用仓库里的 accessToken（未配置 refreshToken）"
    return None, "环境变量里没有任何凭证"


# --------------------------------------------------------------------------
# 2. 签到
# --------------------------------------------------------------------------
def do_signin(token: str) -> dict:
    """签到。返回 {'ok':bool, 'state':str, 'lines':[...], 'raw':{...}}"""
    result = {"ok": False, "state": "ERROR", "lines": [], "raw": {}}

    code, body = _request("POST", P_SIGNIN_STATUS, token, {})
    result["raw"]["checkin-activity-status"] = body
    if code != 200:
        result["lines"].append("查询签到状态失败：HTTP %s" % code)
        return result

    st = unwrap(body)
    result["raw"]["_status"] = st
    activity = st.get("activity_name") or st.get("theme_name") or "-"

    if not st.get("active", True):
        result["ok"] = False
        result["state"] = "INACTIVE"
        result["lines"].append("签到活动当前未开启（%s）" % activity)
        return result

    if st.get("today_checked_in"):
        result["ok"] = True
        result["state"] = "ALREADY"
        result["lines"].append("今日已签到（幂等跳过，无需重复领取）")
    else:
        ccode, cbody = _request("POST", P_SIGNIN_CLAIM, token, {})
        result["raw"]["daily-checkin"] = cbody
        if ccode != 200:
            result["lines"].append("领取签到积分失败：HTTP %s" % ccode)
            return result
        got = unwrap(cbody)
        result["ok"] = True
        result["state"] = "SUCCESS"
        gained = got.get("credit") or got.get("today_credit") or got.get("daily_credit")
        result["lines"].append("签到成功" + ("，+%s 积分" % gained if gained else ""))
        # 领取后复核一次，拿到最新的连签/累计值
        rcode, rbody = _request("POST", P_SIGNIN_STATUS, token, {}, retries=1)
        if rcode == 200:
            st = unwrap(rbody)
            result["raw"]["checkin-activity-status-after"] = rbody

    result["lines"].append("连签 %s 天 · 今日 +%s · 累计 %s" % (
        st.get("streak_days", "-"), st.get("today_credit", "-"), st.get("total_credits", "-")))
    result["lines"].append("活动：%s / %s（%s ~ %s）" % (
        st.get("theme_name", "-"), activity,
        st.get("start_time", "-"), st.get("end_time", "-")))
    return result


# --------------------------------------------------------------------------
# 3. 猫猫旅行
# --------------------------------------------------------------------------
def do_cat(token: str) -> dict:
    """猫猫旅行：先领到家的积分，再判断能否派新的一趟。整体包在 try 里。"""
    result = {"ok": False, "lines": [], "raw": {}, "claimed": 0, "departed": False}
    try:
        code, body = _request("GET", P_TRAVEL_STATUS, token, retries=2)
        result["raw"]["buddy/travel/status"] = body
        if code != 200:
            result["lines"].append("查询旅行状态失败：HTTP %s" % code)
            return result

        st = unwrap(body)
        state = st.get("state")
        daily_limit = bool(st.get("daily_limit_reached"))
        loc = (st.get("location") or {}).get("name", "?")

        if state == "traveling":
            result["ok"] = True
            result["lines"].append("猫猫正在「%s」旅行中，等它回家" % loc)
            _describe_eta(st, result)
            return result

        # --- 先领掉已经到家的旅行积分 ---
        if state == "arrived":
            ccode, cbody = _request("POST", P_TRAVEL_CLAIM, token,
                                    {"record_id": st.get("record_id")})
            result["raw"]["buddy/travel/claim"] = cbody
            got = unwrap(cbody)
            if ccode == 200 and got.get("reward_credit") is not None:
                credit = got.get("reward_credit")
                result["claimed"] = credit
                result["lines"].append("从「%s」回家，领取旅行积分 +%s" % (loc, credit))
                state = "idle"          # 只有领取成功才允许派新的一趟
            else:
                msg = got.get("msg") or ("HTTP %s" % ccode)
                result["lines"].append("领取旅行积分失败：%s（本轮不再派发，避免覆盖未领奖励）" % msg)
                return result
        elif state == "idle":
            result["lines"].append("猫猫在家，等待派发")

        # --- 再判断能不能派新的一趟 ---
        if state != "idle":
            result["lines"].append("当前状态 %s，本轮不派发" % state)
            return result

        if daily_limit:
            result["ok"] = True
            result["lines"].append("今日旅行名额已用完，不重复派发")
            return result

        ccode, cbody = _request("GET", P_TRAVEL_CONFIG, token, retries=2)
        result["raw"]["buddy/travel/config"] = cbody
        locs = unwrap(cbody).get("locations") or [] if ccode == 200 else []
        if not locs:
            result["lines"].append("没有可选的旅行目的地（HTTP %s）" % ccode)
            return result

        target = locs[0]
        dcode, dbody = _request("POST", P_TRAVEL_DEPART, token,
                                {"location_id": target.get("id")})
        result["raw"]["buddy/travel/depart"] = dbody
        if dcode == 200:
            dd = unwrap(dbody)
            name = (dd.get("location") or {}).get("name") or target.get("name", "?")
            dur = dd.get("duration_hours") or target.get("duration_hours", "?")
            result["ok"] = True
            result["departed"] = True
            result["lines"].append("已派猫猫前往「%s」（%s 小时后回家）" % (name, dur))
        else:
            msg = unwrap(dbody).get("msg") or ("HTTP %s" % dcode)
            result["lines"].append("派发失败：%s" % msg)
        return result

    except Exception as e:                     # 猫猫模块兜底，绝不影响签到
        result["ok"] = False
        result["lines"].append("猫猫模块异常：%s: %s" % (type(e).__name__, e))
        return result


def _describe_eta(st: dict, result: dict) -> None:
    """用服务端时间戳换算剩余时间，避免依赖 runner 本地时钟。"""
    arrive_at, server_now = st.get("arrive_at"), st.get("server_now")
    if isinstance(arrive_at, (int, float)) and isinstance(server_now, (int, float)):
        left = max(0, int(arrive_at - server_now))
        result["lines"].append("距离回家还有 %d 小时 %d 分" % (left // 3600, left % 3600 // 60))


# --------------------------------------------------------------------------
# 4. 推送（PushPlus → 微信服务号）
# --------------------------------------------------------------------------
def pushplus_send(title: str, content: str) -> None:
    """PushPlus 走独立域名，单独实现。"""
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        log("\n[推送] 未配置 PUSHPLUS_TOKEN，跳过推送")
        return
    register_secret(token)
    payload = {"token": token, "title": title, "content": content,
               "template": "markdown", "channel": "wechat"}
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    last: tuple[int, object] = (-1, {})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT,
                                        context=ssl.create_default_context()) as r:
                last = (r.status, json.loads(r.read().decode("utf-8", "replace")))
                break
        except urllib.error.HTTPError as e:
            last = (e.code, {"raw": e.read().decode("utf-8", "replace")[:300]})
            if e.code < 500:
                break
        except Exception as e:
            last = (-1, {"error": "%s: %s" % (type(e).__name__, e)})
        time.sleep(1.5 * (attempt + 1))
    log("\n[推送] PushPlus → HTTP %s %s" % (last[0], redact(json.dumps(last[1], ensure_ascii=False)[:200])))


# --------------------------------------------------------------------------
# 5. 组装报告
# --------------------------------------------------------------------------
def build_markdown(signin: dict, cat: dict, token_src: str) -> str:
    if signin["state"] == "SUCCESS":
        s_head = "✅ 签到成功"
    elif signin["state"] == "ALREADY":
        s_head = "✅ 今日已签到（无需重复领取）"
    elif signin["state"] == "INACTIVE":
        s_head = "⚠️ 签到活动未开启"
    else:
        s_head = "❌ 签到失败"

    if cat["departed"]:
        c_head = "🐱 已派出新的一趟"
    elif cat["claimed"]:
        c_head = "🐱 已领回旅行积分"
    elif cat["ok"]:
        c_head = "🐱 旅行中"
    else:
        c_head = "⚠️ 猫猫部分异常"

    lines = [
        "## 📅 签到", s_head, "",
        *["- " + x for x in signin["lines"]], "",
        "## 🐱 猫猫旅行", c_head, "",
        *["- " + x for x in cat["lines"]], "",
        "---", "凭证来源：%s" % token_src,
    ]
    return "\n".join(lines)


def dump_raw(signin: dict, cat: dict) -> str:
    """把原始返回（脱敏后）汇总，便于排查 / 验收。"""
    out = ["\n" + "=" * 72, "原始返回（已脱敏）", "=" * 72]
    for name, body in list(signin["raw"].items()) + list(cat["raw"].items()):
        if name.startswith("_"):
            continue
        out.append("\n--- %s ---" % name)
        out.append(json.dumps(body, ensure_ascii=False, indent=2)[:4000])
    return "\n".join(out)


def emit_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception as e:
            log("写 step summary 失败：%s" % e)


def main() -> int:
    log("=" * 72)
    log("WorkBuddy 每日任务开始（签到 + 猫猫旅行）")
    log("=" * 72)

    token, src = resolve_token()
    log("[凭证] %s" % src)
    if not token:
        log("没有可用凭证，任务终止。请在本机重新运行 scripts/inject_secrets.py 注入。")
        emit_step_summary("## ❌ WorkBuddy 每日任务失败\n\n没有可用凭证。请在本机重新注入 Secret。")
        return 1

    log("\n--- 1/2 签到 ---")
    signin = do_signin(token)
    for line in signin["lines"]:
        log("  " + line)

    log("\n--- 2/2 猫猫旅行 ---")
    try:
        cat = do_cat(token)
    except Exception as e:                      # 双保险：猫猫绝不影响签到结论
        cat = {"ok": False, "lines": ["猫猫模块未捕获异常：%s" % e],
               "raw": {}, "claimed": 0, "departed": False}
    for line in cat["lines"]:
        log("  " + line)

    raw = dump_raw(signin, cat)
    log(raw)

    md = build_markdown(signin, cat, src)
    emit_step_summary(md + "\n\n```\n" + raw.strip() + "\n```")

    stamp = time.strftime("%m-%d", time.localtime())
    if cat["departed"]:
        flag = "🐱已派"
    elif cat["ok"]:
        flag = "🐱正常"
    else:
        flag = "🐱异常"
    pushplus_send("WorkBuddy 日报 %s · %s" % (stamp, "✅签到" if signin["ok"] else "❌签到"),
                  md)

    log("\n" + "=" * 72)
    if signin["ok"]:
        log("结论：签到成功（%s）。猫猫部分：%s" % (signin["state"], flag))
        log("=" * 72)
        return 0
    log("结论：签到失败，需要关注。")
    log("=" * 72)
    return 1


if __name__ == "__main__":
    sys.exit(main())
