# qoder-checkin

> 仓库名 `qoder-checkin`；里面的可执行文件仍叫 `qoder_claim.py`，命令行/数据目录叫 `qoder-claim`（`%LOCALAPPDATA%\qoder-claim\`）。同一件事，别被名字绕晕。

Qoder 桌面端「每天领 100 Credits」活动的自动领取脚本。**单文件、零第三方依赖**（只用标准库），Windows / macOS / Linux 都能跑。

```
python qoder_claim.py --status     # 只看当前活动，不领取
python qoder_claim.py              # 领取（自动挑区域端点）
```

> ⚠️ 免责声明见文末 [风险与合规](#风险与合规)，使用前请先读一遍。

---

## 它解决什么问题

Qoder 客户端里那个「专属活动权益 / 立即查收」弹框，需要**每天手动点一次**。实测发现：

- 弹框页面（`growth-page/activity-iframe`）只会预取 `VIEW_DETAILS` 类活动，**不会自动领取**发币类活动；
- 活动窗口是 **每天 10:00 ~ 次日 09:59（UTC+8）**，`campaignId` 每天更换；
- 所以想不断签，就得有个东西每天定点去打接口 —— 这就是本工具。

## 工作原理（逆向 Qoder 桌面端 0.4.2 得到）

整个领取流程只有两个 HTTP 调用：

```
GET  https://openapi.qoder.sh/sash/api/v1/me/campaigns
POST https://openapi.qoder.sh/sash/api/v1/me/campaigns/{campaignId}/claim
```

三条关键结论：

1. **必须带请求头 `Cosy-ClientType: 10`**。只带 `Authorization: Bearer <token>` 时服务端返回
   `{"showCampaign": false, "campaigns": []}`，看起来像"没活动"，其实是被端类型过滤掉了。
   这是最容易踩的坑。
2. **claim 接口幂等**。重复调用返回 `{"status": "CLAIMED", "replayed": true}`，不会重复发币，
   所以脚本可以放心重试。
3. **不能写死 campaignId**。每次都从列表里现找 `actionType == "CLAIM_BENEFIT"` 且
   `claimStatus == "CLAIMABLE"` 的那条。

请求里**不需要** `Cosy-Machine*` 等设备风控头，普通 `Bearer` 就够了。

国际版 `openapi.qoder.sh` 和国内版 `openapi.qoder.com.cn` 两个端点都活着，脚本会按顺序试、
把第一个能用的记进 `state.json`，所以国内/国际账号都不用配置。

## 安装

只需要 Python ≥ 3.8（用到 `ctypes.wintypes`、`pathlib`、`urllib`，全在标准库里）。

```bash
git clone https://github.com/sunp-1/qoder-checkin.git
cd qoder-checkin
python qoder_claim.py --status
```

## 提供 token（三选一）

| 方式 | 命令 | 适用 |
| --- | --- | --- |
| 显式传参 | `--token <t>` 或环境变量 `QODER_CLAIM_TOKEN` | 跨平台，最透明，推荐 |
| 从文件读 | `--from-file session.json`（`{"token": "...", "refreshToken": "..."}`） | 自己管凭据 |
| 自动读本机登录态 | 不加任何参数（**仅 Windows**） | 最省事 |

### Windows 自动读取是怎么做的

Qoder 把 `{token, refreshToken, expiresAt}` 存在

```
%APPDATA%\com.qoder.app.<channel>\auth.v1.dat
```

这是 Chromium 的 `v10` 格式：`b"v10"` + 12 字节 IV + AES-256-GCM 密文 + 16 字节 tag。
密钥在同目录 `Local State` 的 `os_crypt.encrypted_key` 里（base64，去掉 `DPAPI` 前缀后用
当前用户的 DPAPI 解开）。

脚本对这份数据的处理原则：

- **只要 `--token` / `QODER_CLAIM_TOKEN` 有值，这段代码根本不会被调用** —— 不会打开 `%APPDATA%` 下任何文件；
- **只读不写**，绝不回写 `auth.v1.dat` / `Local State`；
- 解密优先用已安装的 `cryptography`，没装就**降级到纯 `ctypes` 调 Windows CNG**（`bcrypt.dll`），
  所以不需要 `pip install` 任何东西；
- token 过期时用 `refreshToken` 调 `POST /api/v1/deviceToken/refresh` 换新 token，
  新 token **只在当前进程内存里用**，同样不落盘。

固有约束：DPAPI 绑定 Windows 用户，所以**必须有该用户已登录的桌面会话**，
纯 SSH / 服务账号下解不开（脚本会明确报错，不会静默失败）。

macOS / Linux 没有这条路径（客户端凭据结构不同，未逆向），请用 `--token`。
在 Windows 上想拿 token 也简单：跑一次 `python -c "import qoder_claim;print(qoder_claim.read_local_session()['token'])"`，
或者直接抓客户端请求头。

## 命令行

```
--status            只列活动，不领取
--once              只试一次，不重试（调试 / 给自动化任务用，推荐）
--dry-run           只看会领到什么，不真领
--json              输出一行 JSON，方便被自动化任务或上层脚本解析
--base-url URL      手动指定端点，默认自动挑
--force             忽略本地 state，重新向服务端确认（幂等，不会重复发币）
--token TOKEN       直接给 access token（也可用环境变量 QODER_CLAIM_TOKEN）
--from-file PATH    从 JSON 文件读 {token, refreshToken}
```

退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 领取成功，或本窗口已经领过 |
| 1 | 本轮没领到（活动未下发 / 网络错误），可以等下次触发 |
| 2 | 拿不到 token（`--token` / `--from-file` 给了但内容为空） |
| 3 | 环境不支持：非 Windows 却想自动读本机登录态、或本机凭据解不开 |

`--json` 输出形如：

```json
{"verdict": "claimed", "detail": "+100 Credits (act-20260923-214)", "date": "2026-09-26", "endpoint": "https://openapi.qoder.sh"}
```

## 定时执行：用 Qoder 自带的「自动化」

不用碰任务计划程序 / crontab。Qoder 客户端左侧栏有个「自动化」面板，能建定时任务、看每次执行记录、手动补跑、随时暂停 —— 对这种每天一次的小事刚好。

**唯一的限制**：本地自动化只在 Qoder 客户端进程存在时才会触发（不用前台，托盘里就行）。所以要么电脑常开，要么把 Qoder 设成开机自启。错过 10:01 不致命 —— 活动窗口有 24 小时，在面板里点一次「立即运行」就补上了。

### 做法一：直接让 Agent 建（推荐，30 秒）

在 Qoder 对话框里发这句，路径换成你自己的：

> 建一个每天 10:01（Asia/Shanghai）的自动化任务：运行 `python D:/path/to/qoder_claim.py --once`，按它的输出用一句中文汇报结论。不要修改我的任何文件，不要自己写循环重试。

Agent 会用内置的 `qoder_cron` 工具建好，建完在「自动化」面板就能看到。

### 做法二：面板里手动填

| 字段 | 填什么 | 为什么 |
| --- | --- | --- |
| 名称 | `Qoder 每日 Credits 签到` | 随便 |
| 计划类型 | Cron | 要按"每天几点"跑就选 cron |
| 表达式 | `1 10 * * *` | 10:01 触发；想加兜底填 `1 10,20 * * *`，10:01 和 20:01 各试一次 |
| 时区 | `Asia/Shanghai` | 活动按 UTC+8 刷新，时区选错就天天错过 |
| 工作目录 | 脚本所在目录 | |
| 模型 | 最便宜的那个就够 | 每次触发都会开一个 Agent 会话、消耗少量 Credits；这活儿只是跑一条命令 |
| 权限 | 自动批准（或 Full Access） | 选需要确认的模式，定时任务会卡在"等人点同意"上，等于没跑 |
| 输出 | 独立会话（independent） | 每天一次，不需要上下文延续 |

指令（prompt）直接粘这段，把路径换掉：

```
运行 `python D:/path/to/qoder_claim.py --once`，这条命令自己会完成查询和领取。
按它的输出用一句中文汇报结论：
- verdict=claimed → 今日签到成功，把到账的 Credits 数报出来
- verdict=done    → 今天已经领过了，结束
- verdict=pending / 退出码 1 → 本轮没领到，说下一个触发点会自动再试
- 退出码 2 或 3   → 拿不到登录态：再跑一次 --status，把输出原样贴出来，并提示我需要在 Qoder 里重新登录
不要修改任何文件，不要自己写循环或 sleep 重试，整个任务一分钟内结束。
```

### 建完先验一次

面板里对该任务点「立即运行」，然后看它的执行记录：脚本输出一行 `{"verdict": ...}` 就是通了，任务状态显示 `succeeded` 说明自动化这一环没问题。别等第二天才发现配错了。

> 本工具对调度方式没有任何要求，接到你自己习惯的定时器里也行；本文只写 Qoder 自动化这一种。

## 文件位置

日志和状态写到（可用 `QODER_CLAIM_HOME` 覆盖）：

- Windows：`%LOCALAPPDATA%\qoder-claim\`
- Linux/macOS：`$XDG_DATA_HOME/qoder-claim`，默认 `~/.local/share/qoder-claim/`

内容是 `qoder-claim.log`（普通文本）和 `state.json`（`{claimed_on, claimed_at, base}`）。
**这里不存 token**，也不会有任何遥测/上报 —— 除了你自己配的端点，脚本不联网到第二个地方。

## 已知边界

- 依赖 Qoder 的**未公开内部接口**，客户端升级可能哪天就改路径、改字段、改鉴权方式。
  出问题时的自查顺序：`--status` 看 HTTP 码 → 401 是 token 问题、空列表八成是 `Cosy-ClientType`
  变了、404 是接口挪位置了。
- 这波活动本身有结束时间（当前账号看到 `endAt` 在 2026-09-30 前后），活动下线后
  `campaigns` 会返回空列表，脚本会安静地报 `done`。
- 只对"发币型"活动（`CLAIM_BENEFIT`）有效；其它 `actionType` 会被忽略。
- 不带 `--once` 时，脚本自己会按 60s 间隔最多重试 20 次（活动偶尔在 10:00 之后几十秒才下发）。
  挂到自动化面板上时建议加 `--once`：让调度器负责重试，任务本身一分钟内结束，别占着会话。
- 自动化面板里配 `1 10,20 * * *` 这种双触发点，比让脚本干等 20 分钟省 Credits。

## 风险与合规

- 这是**第三方非官方工具**，与 Qoder 没有任何关系，也没有得到它的背书。
- 自动化领取属于对活动接口的手动重放，**可能不符合服务条款**。是否使用请自行判断并承担后果；
  请只用于你自己的账号，不要用来批量薅他人/批量注册账号。
- 工具会读取本机 Qoder 的登录凭据（只读、不落盘、不外传）。**代码就在单文件里，欢迎审计**；
  不放心的话请只用 `--token` 模式，并自行确认 Windows 自动读取那段不被触发。
- 不要把 `--token` 写进任何会提交的文件，用环境变量或 `--from-file` 指到 `.gitignore` 里的路径。

## License

MIT —— 见 [LICENSE](LICENSE)。

---

<details>
<summary>English summary</summary>

A single-file, dependency-free script that auto-claims Qoder's daily "100 Credits" campaign.
It calls two undocumented endpoints on the Qoder openapi host (`GET /sash/api/v1/me/campaigns`
then `POST .../{campaignId}/claim`). Two facts that matter: the request must carry the header
`Cosy-ClientType: 10` or the server returns an empty campaign list, and the claim endpoint is
idempotent (`replayed: true`) so retrying is safe. The daily window is 10:00 → 09:59 UTC+8 and
the `campaignId` rotates every day, so nothing is hardcoded. On Windows it can decrypt the
desktop client's Chromium-style `v10` credential store in-place (DPAPI + AES-256-GCM via
`ctypes`/CNG, no `pip install`) — read-only, never written back, never sent anywhere else.
Elsewhere, pass `--token`. For scheduling, use Qoder's built-in **Automations** panel rather
than a system scheduler — see the "定时执行" section for the exact cron expression, timezone
(`Asia/Shanghai`, since the campaign refreshes at 10:00 UTC+8) and prompt to paste; local
automations only fire while the Qoder client is running, and the campaign window is 24 hours
so a missed 10:01 run can just be triggered manually. This is an unofficial third-party tool:
it can break on any client update and automating a promo endpoint may not be covered by the
service terms.

</details>
