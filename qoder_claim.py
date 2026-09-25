#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qoder-claim —— Qoder 每日活动 Credits 自动领取（单文件，零第三方依赖）

  GET  {base}/sash/api/v1/me/campaigns
  POST {base}/sash/api/v1/me/campaigns/{campaignId}/claim      （幂等）

Token 从哪来（三选一，按优先级）：
  1. --token / 环境变量 QODER_CLAIM_TOKEN      —— 跨平台，最透明
  2. --from-file PATH                          —— 自己从别处导出的 JSON
  3. Windows 自动读取本机 Qoder 桌面端登录态    —— 见 read_local_session() 的说明

不写任何文件：没有日志、没有 state、不把 token 落到别处，输出只有 stdout。

用法：
  python qoder_claim.py --status              # 只读，看当前活动
  python qoder_claim.py                       # 领取（带重试）
  python qoder_claim.py --once                # 只试一次，不重试（挂定时任务用）
  python qoder_claim.py --once --dry-run      # 只试一次、且不真领
  python qoder_claim.py --json                # 输出一行 JSON，便于被自动化任务解析

退出码：0 成功/已领   1 本轮没领到   2 拿不到 token   3 参数或环境不支持
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.1.0"

# ---------------------------------------------------------------- 常量

TZ_LOCAL = timezone(timedelta(hours=8))          # 活动按 UTC+8 刷新
DEFAULT_BASES = [                                 # 国际版 / 国内版，自动挑能用的那个
    "https://openapi.qoder.sh",
    "https://openapi.qoder.com.cn",
]
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Qoder/claim",
    "Cosy-ClientType": "10",                      # 缺这个头 → 服务端返回 campaigns: []
}
RETRY_INTERVAL_S = 60
MAX_ATTEMPTS = 20

IS_WINDOWS = sys.platform == "win32"
QUIET = False                                    # --json 时置真：只往 stdout 打一行 JSON


def _init_stdio() -> None:
    """控制台是 GBK/CP437 时也别因为一个中文字崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def log(msg: str) -> None:
    """唯一的输出通道：stdout。本工具不写任何文件。"""
    if QUIET or sys.stdout is None:
        return
    line = f"[{datetime.now(TZ_LOCAL).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:                   # 极端情况下 reconfigure 没生效的兜底
        print(line.encode("ascii", "replace").decode("ascii"))


def today() -> str:
    return datetime.now(TZ_LOCAL).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 本地登录态（Windows）
#
# Qoder 桌面端把登录凭据存在 %APPDATA%\com.qoder.app.<channel>\auth.v1.dat，
# 是 Chromium 那种「DPAPI 包住的密钥 + AES-GCM」格式。本工具做的事：
#   * 只读这个文件，绝不写、绝不复制走；
#   * 解密优先用已安装的 cryptography，没装就降级用 Windows 自带的 CNG（bcrypt.dll），
#     所以零 pip 依赖；
#   * 只要 --token 有值，这一整段代码都不会被调用。
# 约束：DPAPI 绑定 Windows 用户，必须有该用户已登录的桌面会话。
# macOS / Linux 请改用 --token。

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class _AeadInfo(ctypes.Structure):        # BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO
    _fields_ = [
        ("cbSize", wt.ULONG), ("dwInfoVersion", wt.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)), ("cbNonce", wt.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)), ("cbAuthData", wt.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)), ("cbTag", wt.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)), ("cbMacContext", wt.ULONG),
        ("cbAAD", wt.ULONG), ("cbData", ctypes.c_ulonglong), ("dwFlags", wt.ULONG),
    ]


def _dpapi_unprotect(data: bytes) -> bytes:
    src = _DataBlob(len(data), ctypes.create_string_buffer(data, len(data)))
    dst = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst)
    ):
        raise OSError(f"DPAPI 解不开（{ctypes.WinError()}）——需要同一用户且已登录桌面")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _decrypt_gcm_cng(key: bytes, nonce: bytes, blob: bytes) -> bytes:
    """纯 ctypes 走 Windows CNG，避免任何 pip 依赖。blob = 密文 + 16 字节 tag。"""
    bcrypt = ctypes.WinDLL("bcrypt.dll")

    def chk(res: int, what: str) -> None:
        if res != 0:
            raise OSError(f"CNG {what} 失败 NTSTATUS=0x{res & 0xFFFFFFFF:08x}")

    ct, tag = blob[:-16], blob[-16:]
    h_alg = ctypes.c_void_p()
    chk(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0), "OpenAlg")
    mode = "ChainingModeGCM"
    chk(bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, (len(mode) + 1) * 2, 0), "SetProperty")
    h_key = ctypes.c_void_p()
    kbuf = ctypes.create_string_buffer(key, len(key))
    chk(bcrypt.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0, kbuf, len(key), 0), "GenKey")

    nbuf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
    tbuf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
    info = _AeadInfo()
    info.cbSize = ctypes.sizeof(_AeadInfo)
    info.dwInfoVersion = 1
    info.pbNonce = ctypes.cast(nbuf, ctypes.POINTER(ctypes.c_ubyte))
    info.cbNonce = len(nonce)
    info.pbTag = ctypes.cast(tbuf, ctypes.POINTER(ctypes.c_ubyte))
    info.cbTag = len(tag)

    ibuf = ctypes.create_string_buffer(ct, len(ct))
    obuf = ctypes.create_string_buffer(len(ct))
    done = ctypes.c_ulong()
    chk(bcrypt.BCryptDecrypt(h_key, ibuf, len(ct), ctypes.byref(info), None, 0,
                             obuf, len(ct), ctypes.byref(done), 0), "Decrypt")
    return obuf.raw[: done.value]


def _decrypt_gcm(key: bytes, nonce: bytes, blob: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return _decrypt_gcm_cng(key, nonce, blob)      # 没装就降级 CNG
    return AESGCM(key).decrypt(nonce, blob, None)


def _appdata_dirs() -> list[Path]:
    override = os.environ.get("QODER_AUTH_DIR")
    if override:
        return [Path(override).expanduser()]
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    try:
        roots = sorted(glob.glob(str(appdata / "com.qoder.app.*")), key=os.path.getmtime, reverse=True)
    except OSError:
        roots = []
    return [Path(r) for r in roots]


def _read_store(root: Path) -> dict:
    raw = (root / "auth.v1.dat").read_bytes()
    if raw[:3] != b"v10":
        raise ValueError(f"未知凭据格式 {raw[:3]!r}")
    if len(raw) < 60:
        raise ValueError("凭据文件过短，可能正被客户端改写")
    state = json.loads((root / "Local State").read_text(encoding="utf-8"))
    key_blob = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if key_blob[:5] != b"DPAPI":
        raise ValueError("Local State 的 os_crypt 结构变了")
    sess = json.loads(_decrypt_gcm(_dpapi_unprotect(key_blob[5:]), raw[3:15], raw[15:]).decode("utf-8"))
    if not sess.get("token"):
        raise ValueError("解密成功但没有 token 字段")
    return sess


def read_local_session() -> dict:
    """Windows：扫所有 Qoder 渠道目录，取第一个能解出 token 的。"""
    if not IS_WINDOWS:
        raise RuntimeError("自动读取本机登录态目前只支持 Windows；macOS/Linux 请用 --token")
    dirs = _appdata_dirs()
    if not dirs:
        raise RuntimeError("没找到 %APPDATA%\\com.qoder.app.*，Qoder 桌面端装过吗？")
    errs = []
    for root in dirs:
        if not (root / "auth.v1.dat").exists():
            errs.append(f"{root.name}: 无 auth.v1.dat")
            continue
        try:
            sess = _read_store(root)
            log(f"已读取本机登录态：{root.name}（token 有效期至 {sess.get('expiresAt')}）")
            return sess
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            errs.append(f"{root.name}: {exc}")
    raise RuntimeError("所有 Qoder 目录都解不出登录态 -> " + "; ".join(errs))


# ---------------------------------------------------------------- 服务端

class NoToken(RuntimeError):
    """拿不到 token —— 单独一个类型，好映射到退出码 2。"""


class Api:
    def __init__(self, token: str, bases: list[str], refresh: str | None = None):
        self.token = token
        self.refresh = refresh
        self.bases = list(bases)
        self.base: str | None = None       # 只在本次进程里记住能用的端点，不落盘

    def _raw(self, base: str, path: str, method: str = "GET") -> tuple[int, dict | str]:
        req = urllib.request.Request(
            base + path, method=method,
            headers={**HEADERS, "Authorization": f"Bearer {self.token}"},
            data=b"" if method == "POST" else None,
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read().decode("utf-8", "replace")
            except OSError:
                return exc.code, str(exc.reason)
        except (urllib.error.URLError, OSError) as exc:
            return 0, str(exc)
        try:
            return 200, json.loads(body)
        except ValueError:
            return 200, body

    def call(self, path: str, method: str = "GET") -> tuple[int, dict | str]:
        """按顺序试候选端点，第一个不是 404/网络错误就认定为本机对应的区域。"""
        order = ([self.base] if self.base else []) + [b for b in self.bases if b != self.base]
        last: tuple[int, dict | str] = (0, "no base")
        for base in order:
            code, body = self._raw(base, path, method)
            if code == 401 and self._try_refresh():
                code, body = self._raw(base, path, method)
            if code in (0, 404):
                last = (code, body)
                continue
            self.base = base
            return code, body
        return last

    def _try_refresh(self) -> bool:
        """access token 被拒时用 refreshToken 换一个新的（只在本进程用，不回写任何文件）。"""
        if not self.refresh or self.base is None:
            return False
        req = urllib.request.Request(
            self.base + "/api/v1/deviceToken/refresh", method="POST",
            headers={**HEADERS, "Content-Type": "application/json"},
            data=json.dumps({"refresh_token": self.refresh}).encode(),
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"refresh 失败：{exc}")
            return False
        tok = data.get("token") or data.get("accessToken") or data.get("access_token")
        if not tok:
            return False
        log("token 已用 refreshToken 续期")
        self.token = tok
        return True

    def campaigns(self) -> tuple[int, dict | str]:
        return self.call("/sash/api/v1/me/campaigns")

    def claim(self, campaign_id: str) -> tuple[int, dict | str]:
        return self.call(f"/sash/api/v1/me/campaigns/{campaign_id}/claim", "POST")


def benefit_campaigns(payload: dict) -> list[dict]:
    return [c for c in payload.get("campaigns", []) if c.get("actionType") == "CLAIM_BENEFIT"]


def claimable_of(payload: dict) -> list[dict]:
    return [c for c in benefit_campaigns(payload) if c.get("claimStatus") == "CLAIMABLE"]


def claimed_now(payload: dict, now: float) -> list[dict]:
    return [c for c in benefit_campaigns(payload)
            if c.get("claimStatus") == "CLAIMED" and c.get("startAt", 0) <= now < c.get("endAt", 0)]


def describe(c: dict) -> str:
    amount = (c.get("benefit") or {}).get("amount")
    window = ""
    if c.get("startAt") and c.get("endAt"):
        f = "%m-%d %H:%M"
        window = " [{}~{}]".format(datetime.fromtimestamp(c["startAt"], TZ_LOCAL).strftime(f),
                                   datetime.fromtimestamp(c["endAt"], TZ_LOCAL).strftime(f))
    return f"{c.get('campaignKey')} amount={amount} status={c.get('claimStatus')}{window}"


def attempt(api: Api, dry_run: bool) -> tuple[str, str]:
    """一次完整尝试，返回 (结论, 说明)，结论 in {claimed, done, pending, error}。"""
    code, payload = api.campaigns()
    if code == 401:
        return "error", "HTTP 401：token 无效或已过期"
    if code != 200 or not isinstance(payload, dict):
        return "error", f"查询失败 HTTP {code}: {str(payload)[:160]}"

    todo = claimable_of(payload)
    if todo:
        if dry_run:
            return "done", "[dry-run] 可领取：" + "; ".join(describe(c) for c in todo)
        results = []
        for c in todo:
            scode, resp = api.claim(c["campaignId"])
            data = resp.get("data") if isinstance(resp, dict) and "data" in resp else resp
            ok = scode == 200 and isinstance(data, dict) and data.get("status") == "CLAIMED"
            amount = ((data.get("benefit") or {}).get("amount") if isinstance(data, dict) else None) \
                or (c.get("benefit") or {}).get("amount")
            replayed = bool(isinstance(data, dict) and data.get("replayed"))
            results.append((ok, f"+{amount} Credits ({c.get('campaignKey')}){' [幂等重复]' if replayed else ''}"))
        for ok, msg in results:
            log(("领取成功 " if ok else "领取失败 ") + msg)
        if any(not ok for ok, _ in results):
            return "error", f"{sum(1 for ok, _ in results if not ok)}/{len(results)} 个活动领取失败"
        return "claimed", "; ".join(m for _, m in results)

    now = time.time()
    if claimed_now(payload, now):
        return "done", "本窗口已领取：" + "; ".join(describe(c) for c in claimed_now(payload, now))
    if not payload.get("campaigns"):
        return "done", "活动列表为空（这波活动已结束，或服务端不再对账号开放）"
    listed = "; ".join(describe(c) for c in benefit_campaigns(payload))
    return "pending", ("今日活动未下发：" + listed[:200]) if listed else "无可领取的发币活动"


# ---------------------------------------------------------------- CLI

def build_api(args) -> Api:
    token, refresh, sess = args.token, None, None
    if not token and args.from_file:
        sess = json.loads(Path(args.from_file).expanduser().read_text(encoding="utf-8"))
    if not token and sess is None:
        sess = read_local_session()
    if sess:
        token = sess.get("token")
        refresh = sess.get("refreshToken")
    if not token:
        raise NoToken("拿不到 token：用 --token / QODER_CLAIM_TOKEN 提供，或在 Windows 上装好并登录 Qoder 桌面端")
    bases = [args.base_url] if args.base_url else DEFAULT_BASES
    return Api(token, bases, refresh)


def out(text: str) -> None:
    if sys.stdout is not None:
        print(text)


def fail(args, code: int, msg: str) -> int:
    """启动阶段就失败时也要有输出：--json 模式下不能一声不吭只留个退出码。"""
    if args.as_json:
        out(json.dumps({"verdict": "error", "detail": msg, "date": today(),
                        "endpoint": None}, ensure_ascii=False))
    else:
        log(msg)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qoder-claim", description="Qoder 每日活动 Credits 自动领取", epilog=f"v{VERSION}")
    parser.add_argument("--token", default=os.environ.get("QODER_CLAIM_TOKEN"),
                        help="access token；不给就自动读本机 Qoder 桌面端登录态（仅 Windows）")
    parser.add_argument("--from-file", help="从 JSON 文件读 {token, refreshToken}")
    parser.add_argument("--base-url", help="指定 API 端点，默认自动在 .sh / .com.cn 之间挑")
    parser.add_argument("--status", action="store_true", help="只列活动，不领取")
    parser.add_argument("--once", action="store_true", help="只试一次，不重试（挂定时任务用）")
    parser.add_argument("--dry-run", action="store_true", help="只查询会领到什么，不真领")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="只输出一行 JSON（过程信息全部静音），便于被自动化任务解析")
    args = parser.parse_args(argv)

    global QUIET
    _init_stdio()
    QUIET = args.as_json

    try:
        api = build_api(args)
    except NoToken as exc:
        return fail(args, 2, f"没有 token：{exc}")
    except Exception as exc:
        return fail(args, 3, f"初始化失败：{exc}")

    if args.status:
        code, payload = api.campaigns()
        rows = [describe(c) for c in payload.get("campaigns", [])] if isinstance(payload, dict) else []
        if args.as_json:
            out(json.dumps({"http": code, "endpoint": api.base, "campaigns": rows}, ensure_ascii=False))
        else:
            log(f"GET /me/campaigns -> HTTP {code} @ {api.base}")
            for r in rows:
                log("  · " + r)
        return 0 if code == 200 else 1

    attempts = 1 if args.once else MAX_ATTEMPTS
    verdict, detail = "pending", "未执行"
    for i in range(attempts):
        verdict, detail = attempt(api, args.dry_run)
        if verdict in ("claimed", "done"):
            break
        if verdict == "error" and (args.once or i == attempts - 1):
            break                                   # 错误也重试到上限，但不多等一轮
        if i + 1 < attempts:                        # 只在真要重试时 sleep，避免退出前白等 60s
            log(f"[{i + 1}/{attempts}] {detail}，{RETRY_INTERVAL_S}s 后重试")
            time.sleep(RETRY_INTERVAL_S)

    result = {"verdict": verdict, "detail": detail, "date": today(), "endpoint": api.base}
    if args.as_json:
        out(json.dumps(result, ensure_ascii=False))
    else:
        log(f"{verdict}: {detail}")
    return {"claimed": 0, "done": 0, "pending": 1, "error": 1}[verdict]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
