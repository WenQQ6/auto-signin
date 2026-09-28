# auto-signin

WorkBuddy「Buddy 加油站」**每日签到 + 猫猫旅行** 自动化。

跑在 GitHub Actions 的免费云端 runner 上，每天定时执行一次，把两件事一起做掉，
结果通过 PushPlus 推到微信。**不需要任何常开设备。**

---

## 每天发生什么

一次运行按顺序做两件事：

### 1. 签到
```
POST /v2/billing/meter/checkin-activity-status   查今天签了没
POST /v2/billing/meter/daily-checkin             没签才领（接口幂等，重复调用不会刷分）
```

### 2. 猫猫旅行
```
GET  /v2/activity/growth/buddy/travel/status     查猫猫状态
POST /v2/activity/growth/buddy/travel/claim      ① 已经到家 → 先把旅行积分领掉
GET  /v2/activity/growth/buddy/travel/config     取可选目的地
POST /v2/activity/growth/buddy/travel/depart     ② 再派新的一趟
```

判断顺序严格是「**先领后派**」：

| 当前状态 | 行为 |
|---|---|
| `arrived`（到家了） | 先 `claim` 把积分领掉，**领成功后**才允许派新的一趟 |
| `idle` + `daily_limit_reached=false` | 派新的一趟 |
| `idle` + `daily_limit_reached=true` | 今天已派过 → **跳过**，不白撞墙 |
| `traveling`（在路上） | 只汇报「还有几小时回家」 |
| `claim` 失败 | 本轮**不再派发**，避免覆盖掉未领取的奖励 |

> 接口名全部为实测确认，未采用网上流传的旧名字（例如旧版把 `/tasks/accept` 当领奖用，
> 新服务端一律 400）。

---

## 结果怎么告诉你

微信推送（PushPlus，markdown 模板），**签到和猫猫分成两个独立小节**：

```markdown
## 📅 签到
✅ 签到成功
- 签到成功，+100 积分
- 连签 7 天 · 今日 +100 · 累计 700
- 活动：Buddy加油站 / 高校新生攻略（... ~ ...）

## 🐱 猫猫旅行
🐱 已派出新的一趟
- 从「咖啡馆」回家，领取旅行积分 +6
- 已派猫猫前往「商场店铺」（4 小时后回家）
```

### 失败隔离

**猫猫部分的任何异常都不会影响签到的结论。**

- 猫猫模块的请求全部包在独立 try 里，异常就地捕获并如实报告
- 退出码只看签到：签到成功（含「今日已签到」）⇒ 退出码 `0`
- 只有签到本身失败（认证/网络/服务端错误）才返回非零

---

## 部署（已经做好了，这里是存档说明）

### 1. 注入凭证

云端没有本机登录态，凭证由本机脚本导出后写入仓库 Secret：

```bash
# 需要 GitHub CLI（gh auth login），或设置 GH_TOKEN 环境变量
python scripts/inject_secrets.py --repo WenQQ6/auto-signin

# 想顺手把 PushPlus token 也写进去
python scripts/inject_secrets.py --repo WenQQ6/auto-signin --pushplus-token <token>
```

写入的 Secret：

| Secret | 说明 |
|---|---|
| `WB_ACCESS_TOKEN` | 兜底用的访问令牌 |
| `WB_REFRESH_TOKEN` | 主用：每次运行前用它换新 token |
| `WB_USER_ID` | 请求头 `X-User-Id` |
| `PUSHPLUS_TOKEN` | 微信推送令牌 |

`workbuddy-desktop.info` 和任何明文 token **都不进仓库**（见 `.gitignore`）。

### 2. 令牌续期（本方案的重点）

实测（2026-09-28）`POST /v2/plugin/auth/token/refresh` **用同一枚 refreshToken 可反复换取新 token，
服务端不会立即作废旧值**。因此：

```
每次运行 → 先用 refreshToken 换新 accessToken
        → 换失败（4xx/网络）才回落到仓库里的 accessToken 兜底
```

好处是 **不需要本机常开**，而且不影响你桌面端已登录的状态。

但 token 本身仍有约 60 天硬有效期，所以建议**每一两个月在本机重跑一次
`inject_secrets.py`** 兜底。

---

## 手动补跑

仓库 → Actions → `WorkBuddy 每日签到 + 猫猫旅行` → **Run workflow**

每次运行的日志里都会打印**脱敏后的原始返回**，同时写入 Job Summary，
格式如下：

```
--- checkin-activity-status ---
{"code":0,"msg":"OK","requestId":"<REDACTED_UUID>","data":{"today_checked_in":true,...}}
```

所有输出统一脱敏：已知 token 直接替换成 `<REDACTED>`，
漏网的 JWT / UUID 也会被打码。**明文 token 永远不会出现在日志里。**

---

## 定时

```yaml
on:
  schedule:
    - cron: "10 0 * * *"   # 00:10 UTC = 08:10 中国标准时间
```

GitHub 的定时任务在高负载时可能延迟数分钟到数十分钟，属正常现象。
需要精确时间或多跑几次时，改这一行即可（多个 cron 表达式用列表形式并列）。

---

## 文件

```
.github/workflows/daily.yml   定时任务定义（cron + 手动触发）
scripts/wb_daily.py           云端主脚本：签到 + 猫猫 + 推送
scripts/inject_secrets.py     本机运行：读登录态 → 注入仓库 Secret
```

---

## 免责声明

非官方脚本，接口逆向自 WorkBuddy 桌面端。仅供个人账号自动化使用，
请自行评估风险；接口或活动规则变更时脚本可能需要相应调整。
