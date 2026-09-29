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

触发架构（重要）
----------------
主触发 = 外部免费定时器（cron-job.org）每天 10:10 CST 调 workflow_dispatch。
兜底   = GitHub 自带 schedule（11:10 CST）。
原因   = GitHub 的 schedule 是 best-effort，2026 年 8 月下旬起大面积出现
         「延迟数小时 → 彻底不再产生运行」，详见 README。
去重   = 兜底跑起来时用 already_succeeded_today() 判断今天是否已由主触发完成，
         是则安静退出，避免同一天推两张卡片。
"""

from __future__ import annotations

import datetime
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
            dur = _pick_depart_duration(dd, target)
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


def _pick_depart_duration(dd: dict, target: dict) -> str:
    """取本次旅行的时长（小时）。

    坑：depart 响应顶层的 duration_hours 实测恒为 0（假值），
    真正的时长在响应里的 location.duration_hours；而 config 里的目的地对象
    又没有 duration_hours 字段。所以按优先级找第一个「有效且非 0」的值。
    """
    for cand in ((dd.get("location") or {}).get("duration_hours"),
                 dd.get("duration_hours"),
                 target.get("duration_hours")):
        if isinstance(cand, (int, float)) and cand:
            return str(int(cand))
    return "?"


def _describe_eta(st: dict, result: dict) -> None:
    """用服务端时间戳换算剩余时间，避免依赖 runner 本地时钟。"""
    arrive_at, server_now = st.get("arrive_at"), st.get("server_now")
    if isinstance(arrive_at, (int, float)) and isinstance(server_now, (int, float)):
        left = max(0, int(arrive_at - server_now))
        result["lines"].append("距离回家还有 %d 小时 %d 分" % (left // 3600, left % 3600 // 60))


# --------------------------------------------------------------------------
# 4. 推送（飞书自定义机器人 webhook）
# --------------------------------------------------------------------------
def _feishu_sign(secret: str, timestamp: int) -> str:
    """飞书签名：以 "timestamp\\nsecret" 为密钥，对空串做 HMAC-SHA256 再 base64。"""
    import base64
    import hashlib
    import hmac
    string_to_sign = "%s\n%s" % (timestamp, secret)
    digest = hmac.new(string_to_sign.encode("utf-8"),
                      digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _feishu_post(webhook: str, payload: dict) -> tuple[int, object]:
    """POST 到 webhook 地址；URL 本身是敏感值，错误信息里也不能带出来。"""
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": USER_AGENT},
        method="POST",
    )
    last: tuple[int, object] = (-1, {})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT,
                                        context=ssl.create_default_context()) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
                # 飞书无论成败都返回 HTTP 200，成败看 body.code
                return r.status, body
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                last = (e.code, json.loads(raw))
            except Exception:
                last = (e.code, {"raw": raw[:300]})
            if e.code < 500:
                return last
        except Exception as e:
            # 网络异常里可能带上完整 URL（含 token），这里统一脱敏
            last = (-1, {"error": redact("%s: %s" % (type(e).__name__, e))})
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return last


def build_card(title: str, sections: list[str], ok: bool) -> dict:
    """飞书卡片（schema 2.0）。用 markdown 元素，比纯文本可读得多。"""
    elements: list[dict] = []
    for i, sec in enumerate(sections):
        if i:
            elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": sec,
                         "text_align": "left", "text_size": "normal_v2"})
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "green" if ok else "red",
            },
            "body": {"direction": "vertical", "padding": "12px 12px 12px 12px",
                     "elements": elements},
        },
    }


def feishu_send(title: str, sections: list[str], plain: str, ok: bool) -> None:
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        log("\n[推送] 未配置 FEISHU_WEBHOOK，跳过推送")
        return
    register_secret(webhook)                     # 带 token 的 URL 本身就是敏感值

    # 机器人若开了「自定义关键词」，消息里必须出现该词，否则整条被拒
    keyword = os.environ.get("FEISHU_KEYWORD", "").strip()
    if keyword and keyword not in title:
        title = "%s %s" % (keyword, title)

    payload = build_card(title, sections, ok)

    secret = os.environ.get("FEISHU_SECRET", "").strip()
    if secret:
        register_secret(secret)
        ts = int(time.time())                    # 必须与标准时间偏差 1 小时以内
        payload["timestamp"] = str(ts)
        payload["sign"] = _feishu_sign(secret, ts)

    code, body = _feishu_post(webhook, payload)
    log("\n[推送] 飞书卡片 → HTTP %s %s"
        % (code, redact(json.dumps(body, ensure_ascii=False)[:200])))
    if isinstance(body, dict) and body.get("code") == 0:
        return

    # 卡片失败就退回纯文本再试一次：宁可格式朴素，也不能丢通知
    log("[推送] 卡片发送未成功，回退纯文本重试")
    fallback = {"msg_type": "text", "content": {"text": title + "\n\n" + plain}}
    if secret:
        ts = int(time.time())
        fallback["timestamp"] = str(ts)
        fallback["sign"] = _feishu_sign(secret, ts)
    code2, body2 = _feishu_post(webhook, fallback)
    log("[推送] 飞书纯文本 → HTTP %s %s"
        % (code2, redact(json.dumps(body2, ensure_ascii=False)[:200])))


# --------------------------------------------------------------------------
# 4.5 触发来源与时间（便于事后分辨「自动跑」还是「手动跑」）
# --------------------------------------------------------------------------
def _now_cst() -> str:
    """当前中国标准时间。

    注意：GitHub runner 的时区是 UTC，直接用 time.localtime() 会在
    北京时间 00:00~08:00 之间得出**前一天**的日期，所以统一显式加 8 小时。
    """
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def trigger_source() -> tuple[str, str]:
    """本次运行的触发来源，返回 (短标签, 详细说明)。

    workflow_dispatch 有两个来源在事件层面长得完全一样，无法自动区分，
    所以约定：外部定时器（cron-job.org）dispatch 时带 inputs.source=cron，
    Actions 页面手点 Run workflow 则落到默认值 manual。
    """
    event = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    hint = os.environ.get("WB_TRIGGER_SOURCE", "").strip().lower()

    if event == "schedule":
        return "GitHub 自带 cron（兜底）", "event=schedule"
    if event == "workflow_dispatch":
        if hint == "cron":
            return "外部定时器", "event=workflow_dispatch · inputs.source=cron"
        if hint == "manual":
            return "手动触发", "event=workflow_dispatch · 页面/API 手动"
        return "手动触发（来源未标注）", "event=workflow_dispatch · inputs.source 缺失"
    if event:
        return event, "event=%s" % event
    return "本机运行", "非 GitHub Actions 环境"


# --------------------------------------------------------------------------
# 5. 组装报告
# --------------------------------------------------------------------------
def build_report(signin: dict, cat: dict, token_src: str,
                 trigger_label: str = "") -> tuple[str, list[str], str]:
    """返回 (标题, 卡片的分段 markdown, 纯文本兜底)。

    签到与猫猫各自独立成段，飞书卡片里用分隔线隔开，一眼能分清。
    """
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

    now = _now_cst()                            # 中国标准时间，不要用 runner 的 localtime（UTC）
    stamp = now[5:10]                           # MM-DD
    title = "WorkBuddy 日报 %s · %s" % (stamp, "签到✅" if signin["ok"] else "签到❌")

    sec_signin = "\n".join(["**📅 签到**　" + s_head, ""]
                           + ["- " + x for x in signin["lines"]])
    sec_cat = "\n".join(["**🐱 猫猫旅行**　" + c_head, ""]
                        + ["- " + x for x in cat["lines"]])
    sec_foot = "_%s CST · 触发：%s · 凭证：%s_" % (
        now[11:16], trigger_label or "未知", token_src)

    sections = [sec_signin, sec_cat, sec_foot]
    plain = "\n".join([title, "", sec_signin.replace("**", ""), "",
                       sec_cat.replace("**", ""), "", sec_foot.strip("_")])
    return title, sections, plain


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


# --------------------------------------------------------------------------
# 兜底触发去重（只对 schedule 事件生效）
# --------------------------------------------------------------------------
def _cst_day_start_utc() -> str:
    """「中国标准时间今天 00:00」对应的 UTC 时刻（ISO8601，带 Z）。"""
    now = datetime.datetime.now(datetime.timezone.utc)
    cst = now + datetime.timedelta(hours=8)
    midnight = cst.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - datetime.timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")


def already_succeeded_today() -> bool:
    """今天（CST）是否已经有过一次成功的运行。

    为什么需要它
    ------------
    GitHub 的 schedule 事件是 best-effort：长期存在延迟、甚至被静默丢弃的情况，
    所以另外配了一个外部定时器走 workflow_dispatch 作**主触发**。
    GitHub 自带的 schedule 保留作兜底；兜底真跑起来时，用本函数判断
    「今天的活是不是已经被主触发干完了」，避免同一天推两张卡片。

    只在 schedule 事件下调用。手动补跑（workflow_dispatch）不去重，
    这样随时点 Run workflow 都能拿到一份完整报告。

    本机运行时没有 GITHUB_TOKEN / GITHUB_REPOSITORY，直接返回 False。
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    me = os.environ.get("GITHUB_RUN_ID", "").strip()
    if not token or not repo:
        return False
    register_secret(token)

    url = ("https://api.github.com/repos/%s/actions/runs?created=%s..&per_page=50"
           % (repo, _cst_day_start_utc()))
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        })
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log("[去重] 查询今日运行记录失败（按「未重复」处理，照常执行）：%s: %s"
            % (type(e).__name__, e))
        return False

    for run in data.get("workflow_runs", []):
        if str(run.get("id")) == me:
            continue
        if run.get("status") == "completed" and run.get("conclusion") == "success":
            log("[去重] 今日已有成功运行 #%s（event=%s，%s），本次兜底触发跳过，不重复推送。"
                % (run.get("id"), run.get("event"), run.get("created_at")))
            return True
    return False


def main() -> int:
    src_label, src_detail = trigger_source()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    actor = os.environ.get("GITHUB_ACTOR", "").strip()

    log("=" * 72)
    log("WorkBuddy 每日任务开始（签到 + 猫猫旅行）")
    log("=" * 72)
    log("触发来源：%s（%s）" % (src_label, src_detail))
    if run_id:
        log("运行编号：#%s%s" % (run_id, "　触发者：%s" % actor if actor else ""))
    log("开始时间：%s（中国标准时间）" % _now_cst())

    # 兜底去重：schedule（GitHub 自带 cron）如果发现今天已经有成功运行，安静退出。
    # 主触发是外部定时器 → workflow_dispatch，所以正常情况下走不到这里。
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and already_succeeded_today():
        log("今天的任务已由主触发完成，本次兜底运行不再重复执行与推送。")
        log("=" * 72)
        return 0

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

    title, sections, plain = build_report(signin, cat, src, src_label)
    emit_step_summary("\n".join(sections).replace("**", "")
                      + "\n\n```\n" + raw.strip() + "\n```")

    if cat["departed"]:
        flag = "🐱已派"
    elif cat["ok"]:
        flag = "🐱正常"
    else:
        flag = "🐱异常"
    feishu_send(title, sections, plain, signin["ok"])

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
