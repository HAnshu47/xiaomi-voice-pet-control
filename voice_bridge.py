"""小爱语音 -> Home Assistant 桥接（单文件）。

原理：用小米账号登录小米云（全程 443 端口），轮询小爱音箱的对话记录接口，
识别到口令后调用本地 HA 的脚本，再由小爱按训练计划/场景正常播报。

用法（在 D:\\xiaomi 下）：
    venv-voice\\Scripts\\python.exe voice_bridge.py --login     # 首次：按提示用浏览器 passToken 免密登录
    venv-voice\\Scripts\\python.exe voice_bridge.py --login --password   # 备选：账号密码登录（易触发风控）
    venv-voice\\Scripts\\python.exe voice_bridge.py --devices   # 列出小爱音箱，确认名字和型号
    venv-voice\\Scripts\\python.exe voice_bridge.py --probe 60  # 监听 60 秒，验证口令是否出现在记录里
    venv-voice\\Scripts\\python.exe voice_bridge.py --run       # 正式常驻运行

口令配置：voice_bridge.json（与脚本同目录）。修改后保存即自动生效，无需重启。
格式：{"commands": [{"keywords": ["关键词1", "关键词2"], "script": "ha脚本名"}]}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置区（按需修改）
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TOKEN_PATH = str(BASE_DIR / ".mi.token")          # 小米登录令牌（含密码派生物，勿外传）
STATE_PATH = BASE_DIR / "voice_bridge.state"      # 记录已处理到哪条，重启不会重复触发
LOG_PATH = BASE_DIR / "voice_bridge.log"

MI_USER = ""            # 小米账号（手机号/邮箱）；留空则 --login 时交互输入
MI_PASS = ""            # 小米密码；建议留空，--login 时交互输入更安全

SPEAKER = ""            # 小爱音箱名称或 deviceID；留空 = 自动选第一台

# 口令配置放在 voice_bridge.json 里，改完保存自动生效（无需重启）
COMMANDS_JSON = BASE_DIR / "voice_bridge.json"

DEFAULT_COMMANDS = [
    {"keywords": ["打扫猫砂盆", "清理猫砂盆", "铲屎"], "script": "cat_litter_clean"},
    {"keywords": ["喂猫", "加餐", "添加猫粮", "猫粮"], "script": "cat_feed"},
    {"keywords": ["洗猫砂盆", "倒猫砂"], "script": "cat_wash"},
]

HA_BASE = "http://127.0.0.1:8123"
HA_AUTH_FILE = r"D:\xiaomi\homeassistant\.storage\auth"   # 自签 token 的凭据来源

POLL_INTERVAL = 1.5     # 轮询间隔（秒）。低于 1 秒有风控风险
COOLDOWN = 6.0          # 同一脚本两次触发的最小间隔（秒），防连喊重复执行
MAX_BACKOFF = 60        # 出错后的最大退避（秒）
# ---------------------------------------------------------------------------

# 直连小米云（本机已实测可达）；若走公司代理，把下面设为 True
USE_ENV_PROXY = False

LATEST_ASK_API = ("https://userprofile.mina.mi.com/device_profile/v2/conversation"
                  "?hardware={hardware}&timestamp={timestamp}&limit=3")
UA_MINA = ("MiHome/6.0.103 (com.xiaomi.mihome; build:6.0.103.1; iOS 14.4.0) "
           "Alamofire/6.0.103 MICO/iOSApp/appStore/6.0.103")

log = logging.getLogger("voice_bridge")


def _save_commands(cmds: list) -> None:
    COMMANDS_JSON.write_text(
        json.dumps({"commands": cmds}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def load_commands() -> list:
    """从 voice_bridge.json 读口令配置；文件不存在则创建默认配置。

    命中规则：识别文本「包含」任一关键词即触发。
    """
    if not COMMANDS_JSON.is_file():
        _save_commands(DEFAULT_COMMANDS)
        return list(DEFAULT_COMMANDS)
    try:
        data = json.loads(COMMANDS_JSON.read_text(encoding="utf-8"))
        cmds = [{"keywords": [str(k) for k in c.get("keywords", [])],
                 "script": str(c["script"])}
                for c in data.get("commands", []) if c.get("script")]
        if cmds:
            return cmds
        log.warning("voice_bridge.json 里没有有效指令，沿用内置默认")
    except Exception as e:  # noqa: BLE001
        log.warning("voice_bridge.json 解析失败(%s)，沿用内置默认", e)
    return [dict(c) for c in DEFAULT_COMMANDS]


COMMANDS = load_commands()


def setup_logging(verbose: bool) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)


# ---------------------------------------------------------------------------
# HA 调用（自签 access token，每 20 分钟自动续）
# ---------------------------------------------------------------------------
_ha_token = {"value": "", "exp": 0.0}


def _ha_make_token() -> str:
    import jwt  # pyjwt，装在 venv-voice 里

    data = json.loads(Path(HA_AUTH_FILE).read_text(encoding="utf-8"))["data"]
    tokens = data["refresh_tokens"]
    if isinstance(tokens, dict):
        tokens = list(tokens.values())
    owner_ids = {u["id"] for u in data["users"] if u.get("is_owner")}
    rt = next(t for t in tokens if t["user_id"] in owner_ids)
    now = int(time.time())
    payload = {"iss": rt["id"], "iat": now, "exp": now + 1800,
               "sub": rt["user_id"], "type": "access_token"}
    return jwt.encode(payload, rt["jwt_key"], algorithm="HS256")


def _ha_token_get() -> str:
    if not _ha_token["value"] or time.time() > _ha_token["exp"]:
        _ha_token["value"] = _ha_make_token()
        _ha_token["exp"] = time.time() + 1200
    return _ha_token["value"]


def call_ha_script(script: str) -> tuple[int, str]:
    """调用 HA 脚本，返回 (http状态码, 响应文本)。不抛异常。"""
    from urllib import error as urlerror, request as urlrequest

    url = f"{HA_BASE}/api/services/script/turn_on"
    req = urlrequest.Request(
        url, method="POST",
        data=json.dumps({"entity_id": f"script.{script}"}).encode(),
        headers={"Authorization": f"Bearer {_ha_token_get()}",
                 "Content-Type": "application/json"},
    )
    try:
        with urlrequest.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")[:200]
    except urlerror.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 小米登录与对话记录
# ---------------------------------------------------------------------------
async def _login_via_passtoken(account) -> bool:
    """用浏览器 passToken 直接完成 micoapi 登录。

    不走 account.login()（那条路在 passToken 被拒时会退回密码+OTP，
    把真实失败原因掩盖掉）。这里直接调 serviceLogin，把小米的原始
    响应打出来；code==0 就手动换取 serviceToken 并落盘。
    """
    resp = await account._serviceLogin('serviceLogin?sid=micoapi&_json=true')
    log.debug("serviceLogin(passToken) 原始响应: %s", str(resp)[:600])
    if resp.get('code') != 0:
        print(f"\n[passToken 未被小米接受] 原始响应：{str(resp)[:400]}")
        print("常见原因：1) 复制的不是 account.xiaomi.com 域下的 passToken；"
              "2) 浏览器里是旧登录态，需重新登录后再复制；3) 复制时被截断")
        return False
    try:
        for key in ('userId', 'passToken', 'location', 'nonce', 'ssecurity'):
            if key not in resp:
                raise Exception(f"响应缺少字段 {key}：{str(resp)[:300]}")
        account.token['userId'] = resp['userId']
        account.token['passToken'] = resp['passToken']
        service_token = await account._securityTokenService(
            resp['location'], resp['nonce'], resp['ssecurity'])
        account.token['micoapi'] = (resp['ssecurity'], service_token)
        await account.token_store.save_token(account.token)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"\n[passToken 被接受，但换取 serviceToken 失败] {e}")
        return False


async def build_session(use_password: bool = False) -> tuple:
    """返回 (account, cookie_dict, hardware, device_id, speaker_name)。

    登录优先级：
    1. 已保存的令牌（.mi.token，含 passToken，可免密续登录）
    2. 浏览器 passToken 辅助（不需要密码）
    3. 账号+密码登录（仅 --password 时才问，且易触发小米风控）
    """
    from miservice import MiAccount, MiNAService

    user, pwd = MI_USER, MI_PASS
    if use_password and (not user or not pwd):
        user = user or input("小米账号（手机号/邮箱）: ").strip()
        pwd = pwd or input("小米密码: ").strip()

    async def _new_account() -> "MiAccount":
        return MiAccount(None, user, pwd, token_store=TOKEN_PATH)

    def _logged_in(acct: "MiAccount") -> bool:
        return bool(acct.token and "micoapi" in acct.token)

    account = await _new_account()

    # 1) 尝试已保存的令牌：login() 会带 passToken 调 serviceLogin，
    #    有效则直接换新 serviceToken，全程不需要密码
    stored = await account.token_store.load_token()
    if stored:
        account.token = stored
        if await account.login("micoapi"):
            log.info("使用已保存的令牌登录成功")
        elif not use_password:
            # login() 失败时会清空 token，重建一个干净的账号对象
            account = await _new_account()

    # 2) 没有可用令牌且未指定 --password → 浏览器 passToken 辅助
    if not _logged_in(account) and not use_password:
        print("\n" + "=" * 60)
        print("需要浏览器辅助登录一次（不需要输入密码）：")
        print("1. 用浏览器打开 https://account.xiaomi.com 并登录同一个账号")
        print("2. 按 F12 -> Application(应用) -> Cookies -> https://account.xiaomi.com")
        print("3. 找到并复制两个值：passToken（长字符串）和 userId（纯数字）")
        print("=" * 60)
        pass_token = input("粘贴 passToken: ").strip()
        user_id = input("粘贴 userId: ").strip()
        if not pass_token or not user_id:
            raise SystemExit("未提供 passToken/userId，登录中止")
        from miservice.miaccount import get_random
        account = await _new_account()
        account.token = {"deviceId": get_random(16).upper(),
                         "userId": user_id, "passToken": pass_token}
        if not await _login_via_passtoken(account):
            raise SystemExit("passToken 登录失败，见上方原始响应；"
                             "也可以稍后用 --password 走密码登录"
                             "（若提示「非法请求」10012，等 1~2 小时风控冷却）")

    # 3) --password 指定时走传统密码登录
    if not _logged_in(account):
        if not await account.login("micoapi"):
            raise SystemExit(f"密码登录失败：{account._login_error}\n"
                             "若提示「非法请求」(10012)，等 1~2 小时风控冷却后重试")

    token = account.token

    mina = MiNAService(account)
    lst = (await mina.device_list()) or []   # 此版本 device_list 直接返回列表
    speakers = [{"name": d.get("name", ""), "id": d.get("deviceID"),
                 "hardware": d.get("hardware"), "did": d.get("miotDID", {})}
                for d in lst]
    if not speakers:
        raise SystemExit("账号下没有找到小爱音箱")

    chosen = None
    if SPEAKER:
        for s in speakers:
            if SPEAKER in (s["name"], s["id"]):
                chosen = s
                break
        if chosen is None:
            raise SystemExit(f"找不到叫「{SPEAKER}」的音箱。现有：{speakers}")
    else:
        chosen = speakers[0]
    log.info("使用音箱：%s (%s, %s)", chosen["name"], chosen["hardware"], chosen["id"][:12])

    # 关键：deviceId cookie 必须是音箱自己的 deviceID（uuid），
    # 用登录生成的随机 id 会查不到任何对话记录
    cookie = {"userId": token["userId"],
              "serviceToken": token["micoapi"][1],
              "deviceId": chosen["id"]}
    return account, cookie, chosen["hardware"], chosen["id"], chosen["name"]


def _parse_json(raw: bytes) -> dict:
    txt = raw.decode("utf-8", "replace").strip()
    if txt.startswith("&&&START&&&"):
        txt = txt[11:]
    return json.loads(txt or "{}")


async def fetch_records(cookie: dict, hardware: str) -> list[dict]:
    from miservice.biohttp import ClientSession

    url = LATEST_ASK_API.format(hardware=hardware, timestamp=int(time.time() * 1000))
    async with ClientSession() as s:
        async with s.get(url, cookies=cookie, headers={"User-Agent": UA_MINA}) as r:
            if r.status != 200:
                raise RuntimeError(f"对话接口 HTTP {r.status}")
            data = _parse_json(await r.read())
    if data.get("code") != 0:
        raise RuntimeError(f"对话接口返回异常: {str(data)[:200]}")
    inner = json.loads(data.get("data") or "{}")
    return inner.get("records") or []


# ---------------------------------------------------------------------------
# 状态持久化与分发
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"last_time": 0, "request_ids": [], "cooldowns": {}}


def save_state(st: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    tmp.replace(STATE_PATH)


_last_fired: dict[str, float] = {}


def dispatch(text: str) -> str | None:
    """把识别文本匹配到脚本。返回脚本名或 None。"""
    for cmd in COMMANDS:
        if any(k in text for k in cmd["keywords"]):
            script = cmd["script"]
            now = time.time()
            if now - _last_fired.get(script, 0) < COOLDOWN:
                log.info("口令「%s」命中 %s，但在冷却期内，跳过", text, script)
                return None
            _last_fired[script] = now
            return script
    return None


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
async def poll_loop(hardware: str, cookie: dict, probe_seconds: float = 0) -> None:
    global COMMANDS
    st = load_state()
    seen = deque(st.get("request_ids", []), maxlen=50)
    last_time = st.get("last_time", 0)
    seen_set = set(seen)
    backoff = 1.0
    probe_end = time.time() + probe_seconds if probe_seconds else None
    json_mtime = COMMANDS_JSON.stat().st_mtime if COMMANDS_JSON.is_file() else 0.0
    log.info("开始监听%s，每 %.1f 秒查一次（口令 %d 条，改 voice_bridge.json 自动生效）",
             f"（probe 模式 {probe_seconds:.0f} 秒）" if probe_seconds else "",
             POLL_INTERVAL, len(COMMANDS))

    while True:
        try:
            # 口令配置热更新：文件被修改后自动重新加载
            if COMMANDS_JSON.is_file():
                mtime = COMMANDS_JSON.stat().st_mtime
                if mtime != json_mtime:
                    json_mtime = mtime
                    new_cmds = load_commands()
                    if new_cmds != COMMANDS:
                        log.info("口令配置已热更新，现在 %d 条", len(new_cmds))
                    COMMANDS = new_cmds
            records = await fetch_records(cookie, hardware)
            backoff = 1.0
            for rec in records:
                rid = rec.get("requestId") or f"{rec.get('time')}"
                ts = rec.get("time", 0)
                text = (rec.get("query") or "").strip()
                if not text or rid in seen_set or ts <= last_time:
                    continue
                seen_set.add(rid)
                seen.append(rid)
                last_time = max(last_time, ts)
                # probe 模式只打印不执行
                if probe_end:
                    log.info("[记录] %s", text)
                    continue
                log.info("识别到：%s", text)
                script = dispatch(text)
                if script is None:
                    continue
                code, body = call_ha_script(script)
                log.info("HA %s -> HTTP %s %s", script, code, body[:80])
                if code != 200:
                    log.warning("HA 调用失败，保留时间戳以避免重复；下次重试请手动喊一次")
            if seen_set:
                st.update({"last_time": last_time, "request_ids": list(seen)})
                save_state(st)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("轮询异常: %s: %s；%.0f 秒后重试",
                        type(e).__name__, str(e)[:150], backoff)
            if "401" in str(e) or "auth" in str(e).lower():
                log.error("令牌可能已过期，请重新执行: voice_bridge.py --login")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue

        if probe_end and time.time() >= probe_end:
            log.info("probe 结束。如果上面没有出现你喊的话，说明该口令类型不进对话记录")
            return
        await asyncio.sleep(POLL_INTERVAL)


async def main_async() -> None:
    ap = argparse.ArgumentParser(description="小爱语音 -> HA 桥接")
    ap.add_argument("--login", action="store_true", help="登录并保存令牌（默认免密：令牌/浏览器 passToken）")
    ap.add_argument("--password", action="store_true",
                    help="改用账号+密码登录（会触发短信验证，且易被小米风控拦截）")
    ap.add_argument("--devices", action="store_true", help="列出小爱音箱")
    ap.add_argument("--probe", type=float, default=0, metavar="秒",
                    help="只打印识别文本，不执行（验证用）")
    ap.add_argument("--run", action="store_true", help="常驻运行")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging(args.verbose)

    if not (args.login or args.devices or args.probe or args.run):
        ap.print_help()
        return

    account, cookie, hardware, device_id, name = await build_session(args.password)

    if args.devices:
        from miservice import MiNAService
        for d in (await MiNAService(account).device_list()) or []:
            log.info("音箱: name=%s hardware=%s deviceID=%s", d.get("name"),
                     d.get("hardware"), d.get("deviceID"))
        return

    await poll_loop(hardware, cookie, probe_seconds=args.probe or 0)


def main() -> None:
    if not USE_ENV_PROXY:
        # 直连：清掉代理环境变量（本机已实测小米云直连可达）
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                  "all_proxy", "ALL_PROXY"):
            os.environ.pop(k, None)
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("已退出")


if __name__ == "__main__":
    main()
