# qoder-checkin

Qoder 桌面端「每天领 100 Credits」活动的自动签到脚本。**单文件、零第三方依赖**（只用 Python 标准库），Windows / macOS / Linux 都能跑；**不写任何文件**，没有日志也没有状态文件，输出只在 stdout。

> 仓库叫 `qoder-checkin`，里面的可执行文件叫 `qoder_claim.py`，是同一件事。

## 快速开始

```bash
git clone https://github.com/sunp-1/qoder-checkin.git
cd qoder-checkin
python qoder_claim.py --status      # 先看一眼当前活动，确认能连上
python qoder_claim.py               # 领一次
```

需要 Python ≥ 3.8，不用 `pip install` 任何东西。

## 四种用法

| 你想干什么 | 命令 | 详细说明 |
| --- | --- | --- |
| 手动领一次 | `python qoder_claim.py` | 就是上面这两行 |
| 只看有没有活动、不领 | `python qoder_claim.py --status` | 只读，绝对不动接口 |
| 每天自动领 | 挂到 Qoder「自动化」面板 | 见 [定时执行](#定时执行用-qoder-自带的自动化) |
| 不想让它碰本机文件 | `python qoder_claim.py --token <t>` | 见 [提供 token](#提供-token三选一) |

调试时先加 `--dry-run`：只查询会领到什么，不真领。

## 提供 token（三选一）

| 方式 | 怎么写 | 适用 |
| --- | --- | --- |
| 显式传参 | `--token <t>`，或环境变量 `QODER_CLAIM_TOKEN` | 跨平台，最透明，推荐 |
| 从文件读 | `--from-file session.json`，内容 `{"token": "...", "refreshToken": "..."}` | 想自己管凭据 |
| 自动读本机登录态 | 什么都不加（**仅 Windows**） | 最省事 |

自动读取这件事，三条保证：

- **只要 `--token` 或 `QODER_CLAIM_TOKEN` 有值，读本机文件的代码路径根本不会被调用**；
- 需要读的时候**只读不写**，不会改动、复制、外传 Qoder 客户端目录下的任何文件；
- 解密只用 Windows 自带能力（没装 `cryptography` 就走系统 CNG），零外部依赖，代码就在单文件里，欢迎审计。

固有约束：DPAPI 跟 Windows 用户绑定，所以必须有该用户**已登录的桌面会话**；纯 SSH、服务账号下解不开，脚本会明确报错而不是静默失败。macOS / Linux 客户端的凭据结构不同（未做适配），请用 `--token`。

Windows 上想拿 token 自己管：

```bash
python -c "import qoder_claim;print(qoder_claim.read_local_session()['token'])"
```

## 命令行

```
--status            只列活动，不领取
--once              只试一次，不重试（挂定时任务用，推荐）
--dry-run           只看会领到什么，不真领
--json              只输出一行 JSON，过程信息全部静音（便于被自动化任务解析）
--base-url URL      手动指定 API 端点，默认自动在国际版/国内版之间挑
--token TOKEN       直接给 access token（也可用环境变量 QODER_CLAIM_TOKEN）
--from-file PATH    从 JSON 文件读 {token, refreshToken}
```

不带 `--once` 时，如果活动还没下发，脚本会按 60 秒间隔最多重试 20 次；挂到定时任务上时请用 `--once`，让调度器负责重试，任务本身一分钟内结束。

退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 领取成功，或本窗口已经领过 |
| 1 | 本轮没领到（活动未下发 / 网络错误），等下次触发 |
| 2 | 拿不到 token（给了 `--token` / `--from-file` 但内容为空） |
| 3 | 环境不支持：非 Windows 却想自动读本机登录态，或本机凭据解不开 |

`--json` 输出形如：

```json
{"verdict": "claimed", "detail": "+100 Credits (campaignKey)", "date": "2026-09-26", "endpoint": "https://openapi.qoder.sh"}
```

`verdict` 四种：`claimed` 领到了 / `done` 这个窗口已经领过了 / `pending` 活动还没下发 / `error` 鉴权或接口出错。连"拿不到 token"这种启动阶段就失败的情况，`--json` 也会照样吐一行 JSON（`verdict: error`），不会被一句裸错误信息打断解析。

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
运行 `python D:/path/to/qoder_claim.py --once --json`，这条命令自己会完成查询和领取。
按它输出里的 verdict 用一句中文汇报结论：
- claimed → 今日签到成功，把到账的 Credits 数报出来
- done    → 今天已经领过了，结束
- pending → 本轮没领到，说下一个触发点会自动再试
- error   → 鉴权或接口出错，再跑一次 --status 把输出贴出来，并提示我可能需要在 Qoder 里重新登录
不要修改任何文件，不要自己写循环或 sleep 重试，整个任务一分钟内结束。
```

### 建完先验一次

面板里对该任务点「立即运行」，看它的执行记录：输出一行 `{"verdict": ...}` 就是脚本通了，任务状态 `succeeded` 说明自动化这一环也没问题。别等第二天才发现配错了。

> 本工具对调度方式没有任何要求，接到你自己习惯的定时器里也行；本文只写 Qoder 自动化这一种。

## 它解决什么问题

Qoder 客户端里那个「专属活动权益 / 立即查收」的弹框，需要**每天手动点一次**，弹框本身不会替你领。活动每天定点刷新、当天的活动编号还会变，所以想不断签，就得有个东西每天定时去跑一次 —— 这就是本工具干的事。至于它内部怎么跟服务端说话，代码里一目了然，本文不展开。

## 已知边界

- 依赖服务端的**未公开接口**，客户端升级可能哪天就改路径、改字段、改鉴权方式。
  出问题先 `--status` 看 HTTP 码：401 是 token 问题、活动列表变空八成是端类型标识改了、404 是接口挪位置了。
- 活动本身有结束时间，下线后接口返回空列表，脚本会安静地报 `done`，不会疯狂重试。
- 只对"发币型"活动有效，其它类型的活动会被忽略。
- 不落盘意味着**没有跨天的去重记录**：是否已领完全看服务端返回的状态。好在领取动作本身是幂等的，重复跑不会重复发币。

## 不写文件、不联网到第三方

- 不创建日志、状态、缓存、配置 —— 整个程序运行期间**不写任何一个文件**；
- 不把 token 存到别处，也不打印 token（过程信息里只出现"有效期至"这类元数据）；
- 除了你自己指定的那个 API 端点，不向任何其他地址发请求；
- `--token` 模式下一行本机文件代码都不会执行。

## 风险与合规

- 这是**第三方非官方工具**，与 Qoder 没有任何关系，也没有得到它的背书。
- 自动化领取属于对活动接口的手动重放，**可能不符合服务条款**。是否使用请自行判断并承担后果；请只用于你自己的账号，不要用来批量薅他人 / 批量注册账号。
- 代码就在单个文件里，**欢迎审计**；不放心的话只用 `--token` 模式。
- 不要把 token 写进任何会提交的文件，用环境变量，或让 `--from-file` 指到仓库外的路径。

## License

MIT —— 见 [LICENSE](LICENSE)。

---

<details>
<summary>English summary</summary>

A single-file, dependency-free script that checks in to Qoder's daily "100 Credits" campaign
so you don't have to click the promo dialog by hand every day. It writes **no files at all** —
no logs, no state, no cached credentials — and prints to stdout only. Get a token in one of
three ways (`--token` / `QODER_CLAIM_TOKEN`, `--from-file`, or on Windows let it read the
desktop client's own login state, read-only); for scheduling, use Qoder's built-in **Automations**
panel — see the "定时执行" section for the cron expression, timezone (`Asia/Shanghai`) and the
prompt to paste. Local automations only fire while the Qoder client is running, and the campaign
window lasts 24 hours, so a missed run can just be triggered manually. Unofficial third-party
tool: it can break on any client update, and automating a promo endpoint may not be covered by
the service terms.

</details>
