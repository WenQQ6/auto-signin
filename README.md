# auto-signin

WorkBuddy「Buddy 加油站」**每日签到 + 猫猫旅行** 自动化。

跑在 GitHub Actions 的免费云端 runner 上，每天定时执行一次，把两件事一起做掉，
结果通过飞书自定义机器人推送（卡片消息）。**不需要任何常开设备。**

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

飞书卡片消息，**签到和猫猫分成两个独立小节**，中间用分隔线隔开：

```markdown
WorkBuddy 日报 09-28 · 签到✅          ← 卡片标题栏（成功绿色 / 失败红色）

📅 签到　✅ 签到成功
- 签到成功，+100 积分
- 连签 7 天 · 今日 +100 · 累计 700
- 活动：Buddy加油站 / 高校新生攻略（... ~ ...）
──────────────────────────────
🐱 猫猫旅行　🐱 已派出新的一趟
- 从「咖啡馆」回家，领取旅行积分 +6
- 已派猫猫前往「商场店铺」（4 小时后回家）
──────────────────────────────
凭证来源：refreshToken 续期成功（HTTP 200）
```

卡片发送失败时会自动回退成纯文本再试一次——宁可格式朴素，也不能丢通知。

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

# 想顺手把飞书 webhook 也写进去
python scripts/inject_secrets.py --repo WenQQ6/auto-signin \
    --feishu-webhook https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
# 机器人开了「签名校验」就再加 --feishu-secret <密钥>
```

写入的 Secret：

| Secret | 说明 |
|---|---|
| `WB_ACCESS_TOKEN` | 兜底用的访问令牌 |
| `WB_REFRESH_TOKEN` | 主用：每次运行前用它换新 token |
| `WB_USER_ID` | 请求头 `X-User-Id` |
| `FEISHU_WEBHOOK` | 飞书自定义机器人 webhook 地址（含 token，本身即敏感值） |
| `FEISHU_SECRET` | 飞书签名校验密钥；未开签名校验则不需要 |

`workbuddy-desktop.info` 和任何明文 token **都不进仓库**（见 `.gitignore`）。
飞书 webhook 地址同样按敏感值处理：它被登记进脱敏表，日志里只会看到 `<REDACTED>`。

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

**每天 10:10（中国标准时间）自动跑一次，本机不需要开机。**

### 触发架构（三条并存，角色互不重叠）

```
cron-job.org 外部定时器（每天 10:10 CST）
      │  POST /repos/WenQQ6/auto-signin/actions/workflows/daily-cron.yml/dispatches
      │  body: {"ref":"main"}
      ▼
daily-cron.yml                     ← 定时器专用入口（只有它会被定时器打到）
      │  workflow_call  with: entry=cron
      ▼
daily.yml (entry=cron)             ← 正式任务：签到 + 猫猫旅行 → 推飞书
      ▲
      ├─ daily.yml  event=workflow_dispatch ← 手动补跑（Actions 页点 Run workflow）
      └─ daily.yml  event=schedule          ← 兜底（GitHub 自带 cron，11:10 CST）
```

| 触发 | 判定依据 | 时间 | 角色 |
|---|---|---|---|
| `daily-cron.yml` | — | 10:10 CST | 定时器专用入口，只给 cron-job.org 用 |
| `daily.yml` | `entry=cron` | 10:10 CST | **正式任务**（由上面转调） |
| `daily.yml` | `event=workflow_dispatch` | 任意 | 手动补跑，随时可用，不去重 |
| `daily.yml` | `event=schedule` | 11:10 CST | 兜底；跑起来时用「今日已成功运行」去重 |

### 为什么要把定时器单独开一个入口文件

`workflow_dispatch` 有个坑：**外部定时器调 dispatches 接口产生的事件，和人在页面
点 Run workflow 产生的事件，事件名完全一样**，在 GitHub 的运行记录里无法分辨。

试过两个都不行的方案：

| 方案 | 为什么不行 |
|---|---|
| 定时器在请求体里自报 `inputs.source=cron` | 标记是**自我声明**，谁都能写。实际就出过一次误标：验证功能时手动 dispatch 顺手带了 `source=cron`，飞书卡片上就显示成「外部定时器」，看起来像自动跑的 |
| 靠 `event=workflow_call` 区分 | 实测**不成立**：reusable workflow 里的 `github.event_name` **继承 caller 的事件名**，仍然是 `workflow_dispatch`，不是 `workflow_call` |

最终方案：`daily-cron.yml` 在 **workflow 层**写死 `with: {entry: cron}` 传给被调用的
`daily.yml`。这个标记写在**仓库文件**里，不是请求体里——想伪造必须拥有仓库写权限，
而定时器用的令牌只需要 Actions 权限，改不了文件。**所以它是可信的。**

> ⚠️ 补跑请点 `daily.yml` 的 **Run workflow**，不要点 `daily-cron.yml` 的
> （后者是定时器专用入口，手点它会被标记成「外部定时器」）。

### 触发来源标注

日志头部与飞书卡片底部都会写明，例如：

```
触发来源：外部定时器（经 daily-cron.yml 转调（workflow 入参 entry=cron））
运行编号：#36545361129　触发者：WenQQ6
开始时间：2026-09-29 16:51:24（中国标准时间）
```

| 卡片/日志显示 | 依据 | 含义 |
|---|---|---|
| `触发：外部定时器` | `entry=cron` | cron-job.org 自动触发（经 daily-cron.yml 转调） |
| `触发：手动触发` | `event=workflow_dispatch` | 有人在 Actions 页面点了 Run workflow |
| `触发：GitHub 自带 cron（兜底）` | `event=schedule` | GitHub 自己的定时器触发了（故障已恢复） |
| `触发：本机运行` | 无 event 环境变量 | 在电脑上直接跑脚本 |

> 依据是**写死在仓库 workflow 文件里的入口标记**，不是请求体里的自报字段，
> 伪造需要仓库写权限，因此可信。该标注纯粹用于事后追溯，不参与任何业务判断。

### 为什么不用 GitHub 自带 cron 做主触发

GitHub 的 `schedule` 事件是 **best-effort**。从 **2026 年 8 月下旬**开始出现一次持续性的
调度器故障：定时运行先变成长时间迟到（数小时），**然后彻底不再产生任何运行记录**，
而仓库侧没有任何报错、没有失败 job，只有手动 `workflow_dispatch` 一切正常。

本仓库实测：从未产生过任何一条 `event=schedule` 的运行；另建公开/私有两个探针仓库
（cron `*/5 * * * *`）同样 0 次，`disable→enable` + 改 cron 重新注册也无效。

> 注意：网上流传的「免费账号私有仓库不能用 schedule」是**假说**——官方文档没有此限制，
> 而且我们的**公开**探针同样不触发，已排除。

### 兜底去重

`daily.yml` 里的 schedule 若哪天恢复，脚本会先调 GitHub API 查
「今天（CST）是否已有成功运行」。有则打印一行日志后退出，**不会重复推送飞书卡片**。
手动补跑（workflow_dispatch）不做去重，随时点 Run workflow 都能拿到完整报告。

### 几点须知

| 事项 | 说明 |
|---|---|
| cron 时区 | GitHub 的 cron **固定走 UTC**。改时间记得减 8 小时 |
| 触发分支 | schedule 只在**默认分支**（`main`）生效 |
| 额度 | 私有仓库每月 2000 分钟免费额度，单次约 15～20 秒，一天一跑绰绰有余 |
| 手动补跑 | 仓库 Actions 页 → 选 workflow → **Run workflow** |

### 关于「60 天无活动自动停用」

GitHub 官方文档写的是**公开仓库**才会因 60 天无活动被自动停用定时任务。
本仓库是 private，按文档不受限制；但社区多次报告该限制在私有仓库上也触发过，
而且失败是**静默的**（不报错、不通知，只会发现签到没了）。

本任务尤其容易中招：脚本只读仓库、从不回写任何文件，仓库会长期零活动。
因此加了 `keepalive.yml`，**每月 1 日**自动产生一次空提交，保证仓库始终有活动。

> `keepalive.yml` 不读取任何 Secret，权限只有 `contents: write`；
> 持有凭证的 `daily.yml` 是 `contents: read` + `actions: read`。
> `daily-cron.yml` 本身不碰任何 Secret（secret 通过 `secrets: inherit` 直传给
> 被调用的 `daily.yml`），权限同样只有 `contents: read` + `actions: read`。
> 三者彻底隔离。

---

## 文件

```
.github/workflows/daily.yml        正式任务：签到 + 猫猫旅行 + 推送
                                   （workflow_call 定时器 / workflow_dispatch 手动 / schedule 兜底）
.github/workflows/daily-cron.yml   外部定时器专用入口，转调 daily.yml
.github/workflows/keepalive.yml    每月保活提交，防止定时任务被静默停用
scripts/wb_daily.py                云端主脚本：签到 + 猫猫 + 推送
scripts/inject_secrets.py          本机运行：读登录态 → 注入仓库 Secret
```

---

## 免责声明

非官方脚本，接口逆向自 WorkBuddy 桌面端。仅供个人账号自动化使用，
请自行评估风险；接口或活动规则变更时脚本可能需要相应调整。
