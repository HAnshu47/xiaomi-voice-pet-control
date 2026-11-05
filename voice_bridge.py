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
import threading
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

# Web 管理面板（口令管理/设备功能/测试/日志）。绑定本机回环，局域网不可见
PANEL_HOST = "127.0.0.1"
PANEL_PORT = 8124

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

    每条口令：keywords 必填；动作二选一：
      - script: HA 脚本 ID（script.xxx）
      - service: 直接调 HA 服务，如 "button/press"，data 里带 entity_id 等参数
    """
    if not COMMANDS_JSON.is_file():
        _save_commands(DEFAULT_COMMANDS)
        return [dict(c) for c in DEFAULT_COMMANDS]
    try:
        data = json.loads(COMMANDS_JSON.read_text(encoding="utf-8"))
        cmds = []
        for c in data.get("commands", []):
            kws = [str(k).strip() for k in c.get("keywords", []) if str(k).strip()]
            if not kws:
                continue
            entry: dict = {"keywords": kws}
            if c.get("script"):
                entry["script"] = str(c["script"])
            elif c.get("service"):
                entry["service"] = str(c["service"])
                if c.get("data"):
                    entry["data"] = c["data"]
            else:
                continue
            cmds.append(entry)
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
    _ring.setFormatter(fmt)
    log.addHandler(_ring)


class _RingHandler(logging.Handler):
    """保留最近 N 条日志给 Web 面板展示。"""

    def __init__(self, cap: int = 300):
        super().__init__()
        self.buf: deque = deque(maxlen=cap)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buf.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


_ring = _RingHandler()


# ---------------------------------------------------------------------------
# HA 调用（自签 access token，每 20 分钟自动续）
# ---------------------------------------------------------------------------
_ha_token = {"value": "", "exp": 0.0}
_ha_warn_at = 0.0        # 上次「HA 拉取失败」告警时间，用于降噪


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


_HA_OPENER = None


def _ha_open(req, timeout: int = 15):
    """所有本机 HA 请求都绕过系统代理，否则会被 http_proxy 环境变量劫持。"""
    global _HA_OPENER
    from urllib import request as urlrequest

    if _HA_OPENER is None:
        _HA_OPENER = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    return _HA_OPENER.open(req, timeout=timeout)

def _ha_token_get() -> str:
    if not _ha_token["value"] or time.time() > _ha_token["exp"]:
        _ha_token["value"] = _ha_make_token()
        _ha_token["exp"] = time.time() + 1200
    return _ha_token["value"]


def call_ha(action: str, data: dict) -> tuple[int, str]:
    """调用 HA 服务（如 script/turn_on、button/press），返回 (http状态码, 响应)。不抛异常。"""
    from urllib import error as urlerror, request as urlrequest

    url = f"{HA_BASE}/api/services/{action}"
    req = urlrequest.Request(
        url, method="POST",
        data=json.dumps(data).encode(),
        headers={"Authorization": f"Bearer {_ha_token_get()}",
                 "Content-Type": "application/json"},
    )
    try:
        with _ha_open(req, 15) as r:
            return r.status, r.read().decode("utf-8", "replace")[:200]
    except urlerror.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def exec_command(cmd: dict) -> tuple[int, str]:
    """执行一条口令映射：优先 script，其次直接调设备服务。"""
    if cmd.get("script"):
        return call_ha("script/turn_on", {"entity_id": f"script.{cmd['script']}"})
    if cmd.get("service"):
        return call_ha(cmd["service"], dict(cmd.get("data") or {}))
    return 0, "口令映射缺少 script/service 字段"


# 集成 -> 品牌显示名。按实体「所属 HA 集成」分品牌，避免厂商名把同一
# 集成的设备拆散（如小米米家集成下厂商有 小米/米家/小白 等多种）
INTEGRATION_CANDIDATES = ["xiaomi_home", "xiaomi_miot", "miio", "petkit", "tuya",
                          "mqtt", "esphome", "shelly", "switch_as_x", "template"]
BRAND_LABELS = {"xiaomi_home": "小米米家", "xiaomi_miot": "小米米家",
                "miio": "小米米家", "xiaomi": "小米米家", "mijia": "小米米家",
                "petkit": "小佩 PetKit", "tuya": "涂鸦"}
_ha_emap = {"value": {}, "exp": 0.0}   # 实体 -> 集成 映射缓存


def _ha_integration_map() -> dict:
    """返回 {entity_id: 集成域名}。用一次模板调用批量反查，缓存 10 分钟。"""
    if _ha_emap["value"] and time.time() < _ha_emap["exp"]:
        return _ha_emap["value"]
    from urllib import request as urlrequest

    doms = ", ".join(f"'{d}'" for d in INTEGRATION_CANDIDATES)
    tpl = (
        "{% set ns = namespace(out={}) %}"
        f"{{% for dom in [{doms}] %}}"
        "{% for eid in integration_entities(dom) %}"
        "{% set ns.out = dict(ns.out, **{eid: dom}) %}"
        "{% endfor %}{% endfor %}{{ ns.out | to_json }}"
    )
    try:
        req = urlrequest.Request(
            f"{HA_BASE}/api/template", method="POST",
            data=json.dumps({"template": tpl}).encode(),
            headers={"Authorization": f"Bearer {_ha_token_get()}",
                     "Content-Type": "application/json"},
        )
        with _ha_open(req, 30) as r:
            _ha_emap["value"] = json.loads(r.read().decode("utf-8", "replace"))
        _ha_emap["exp"] = time.time() + 600
    except Exception as e:  # noqa: BLE001
        log.warning("拉取集成映射失败(品牌将按厂商显示): %s", e)
        _ha_emap["exp"] = time.time() + 60
    return _ha_emap["value"]


def ha_get_states() -> list:
    """拉取 HA 实体列表（面板用），只保留可直接操作的域。"""
    global _ha_warn_at
    from urllib import error as urlerror, request as urlrequest

    req = urlrequest.Request(
        f"{HA_BASE}/api/states",
        headers={"Authorization": f"Bearer {_ha_token_get()}"},
    )
    try:
        with _ha_open(req, 15) as r:
            states = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        # HA 没启动时会被反复调用，60 秒内只提醒一次，避免刷屏
        if time.time() - _ha_warn_at > 60:
            _ha_warn_at = time.time()
            log.warning("拉取 HA 实体失败(HA 未运行？): %s", e)
        return []
    keep = {"button", "select", "switch", "vacuum", "script", "fan", "light"}
    out = []
    for s in states:
        domain = s["entity_id"].split(".", 1)[0]
        if domain not in keep:
            continue
        item = {"entity_id": s["entity_id"], "domain": domain,
                "name": s.get("attributes", {}).get("friendly_name", s["entity_id"])}
        if domain == "select":
            item["options"] = s.get("attributes", {}).get("options", [])
        if domain == "switch":
            item["state"] = s.get("state")
        out.append(item)

    # 设备名归属：REST states 不带设备信息，用 /api/template 一次拿全
    # 注意：Jinja 循环内 set 不出作用域，必须用 namespace 带出循环
    tpl = (
        "{% set ns = namespace(out={}) %}"
        "{% for s in states %}{% set did = device_id(s.entity_id) %}"
        "{% if did %}{% set dev = device_attr(did, 'name_by_user') or "
        "device_attr(did, 'name') or did %}"
        "{% set ns.out = dict(ns.out, **{s.entity_id: {'d': dev, 'b': "
        "device_attr(did, 'manufacturer') or ''}}) %}"
        "{% endif %}{% endfor %}{{ ns.out | to_json }}"
    )
    dmap = {}
    try:
        req2 = urlrequest.Request(
            f"{HA_BASE}/api/template", method="POST",
            data=json.dumps({"template": tpl}).encode(),
            headers={"Authorization": f"Bearer {_ha_token_get()}",
                     "Content-Type": "application/json"},
        )
        with _ha_open(req2, 20) as r:
            dmap = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        log.warning("拉取设备分组失败(改用扁平模式): %s", e)

    emap = _ha_integration_map()
    for item in out:
        info = dmap.get(item["entity_id"]) or {}
        if item["domain"] == "script":
            item["brand"] = "脚本"
            item["device"] = item["name"]
        else:
            dom = emap.get(item["entity_id"])
            if dom:
                item["brand"] = BRAND_LABELS.get(dom, dom)
            else:
                mfr = str(info.get("b", "")).strip()
                item["brand"] = mfr or "其他设备"
            item["device"] = str(info.get("d", "")).strip() or item["name"]

    out.sort(key=lambda x: (x["brand"], x["device"], x["name"]))
    return out


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


class _NoLogin(Exception):
    """无可用令牌且不允许交互登录（--run 交给面板登录接管）。"""


async def build_session(use_password: bool = False, interactive: bool = True) -> tuple:
    """返回 (account, cookie_dict, hardware, device_id, speaker_name)。

    登录优先级：
    1. 已保存的令牌（.mi.token，含 passToken，可免密续登录）
    2. 浏览器 passToken 辅助（不需要密码；interactive=False 时不问，
       抛 _NoLogin，由面板登录接管）
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
        if not interactive:
            raise _NoLogin()
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
    _speaker_info.update({"name": chosen["name"], "hardware": chosen["hardware"],
                          "id": chosen["id"]})
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


def match_command(text: str) -> dict | None:
    """把识别文本匹配到口令映射。返回命中的命令或 None（含冷却控制）。"""
    for cmd in COMMANDS:
        if any(k in text for k in cmd["keywords"]):
            label = cmd.get("script") or cmd.get("service") or "?"
            now = time.time()
            if now - _last_fired.get(label, 0) < COOLDOWN:
                log.info("口令「%s」命中 %s，但在冷却期内，跳过", text, label)
                return None
            _last_fired[label] = now
            return cmd
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
                cmd = match_command(text)
                if cmd is None:
                    continue
                label = cmd.get("script") or cmd.get("service")
                code, body = exec_command(cmd)
                log.info("HA %s -> HTTP %s %s", label, code, body[:80])
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


# ---------------------------------------------------------------------------
# Web 管理面板（127.0.0.1:8124）
# ---------------------------------------------------------------------------
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_speaker_info = {"name": "", "hardware": "", "id": ""}
_session = {"account": None, "cookie": {}, "hardware": "", "id": "", "name": "",
            "poll_task": None}
_LOOP: "asyncio.AbstractEventLoop | None" = None   # 主事件循环（面板线程回调用）


def _start_poll_task(probe_seconds: float = 0) -> "asyncio.Task":
    """启动（或复用）轮询任务。未登录时不启动（登录成功后会自动调）。"""
    s = _session
    if not s["cookie"]:
        return None
    if s["poll_task"] and not s["poll_task"].done():
        return s["poll_task"]
    s["poll_task"] = asyncio.create_task(
        poll_loop(s["hardware"], s["cookie"], probe_seconds))
    return s["poll_task"]


async def runtime_login(pass_token: str, user_id: str) -> dict:
    """面板登录：浏览器 passToken -> 建立运行时 session。

    失败抛 ValueError（原因已写进日志）。
    """
    from miservice import MiAccount, MiNAService
    from miservice.miaccount import get_random

    pass_token, user_id = (pass_token or "").strip(), str(user_id or "").strip()
    if not pass_token or not user_id:
        raise ValueError("passToken 和 userId 都要填")
    account = MiAccount(None, "", "", token_store=TOKEN_PATH)
    account.token = {"deviceId": get_random(16).upper(),
                     "userId": user_id, "passToken": pass_token}
    if not await _login_via_passtoken(account):
        raise ValueError("passToken 未被小米接受（原因见日志），"
                         "请确认是 account.xiaomi.com 域下的最新值")
    lst = (await MiNAService(account).device_list()) or []
    if not lst:
        raise ValueError("登录成功，但账号下没有小爱音箱")
    chosen = next((d for d in lst if SPEAKER and SPEAKER in
                   (d.get("name"), d.get("deviceID"))), lst[0])
    cookie = {"userId": account.token["userId"],
              "serviceToken": account.token["micoapi"][1],
              "deviceId": chosen.get("deviceID")}
    s = _session
    s["account"] = account
    s["cookie"] = cookie
    s["hardware"] = chosen.get("hardware", "")
    s["id"] = chosen.get("deviceID", "")
    s["name"] = chosen.get("name", "")
    _speaker_info.update({"name": s["name"], "hardware": s["hardware"], "id": s["id"]})
    log.info("面板登录成功，使用音箱：%s (%s)", s["name"], s["hardware"])
    _start_poll_task()
    return {"name": s["name"], "hardware": s["hardware"]}


# 面板密码登录的状态机：idle -> sending -> otp_required -> ok / error
_login_state = {"state": "idle", "msg": "", "otp_future": None}


async def _panel_otp_callback(otp_method: str) -> str:
    """miservice 登录验证码回调：等面板提交验证码（5 分钟超时）。"""
    fut = asyncio.get_running_loop().create_future()
    _login_state["otp_future"] = fut
    _login_state["state"] = "otp_required"
    where = "邮箱" if otp_method == "Email" else "手机短信"
    _login_state["msg"] = f"验证码已发送到{where}，请在面板输入"
    log.info("登录需要验证码（%s），请在面板输入", otp_method)
    try:
        return str(await asyncio.wait_for(fut, timeout=300)).strip()
    except asyncio.TimeoutError:
        raise Exception("验证码等待超时（5 分钟），请重新登录")
    finally:
        _login_state["otp_future"] = None


async def runtime_login_password(user: str, pwd: str) -> None:
    """面板密码登录（后台任务）。过程与结果写入 _login_state。"""
    from miservice import MiAccount, MiNAService

    _login_state.update({"state": "sending", "msg": "正在登录…", "otp_future": None})
    try:
        account = MiAccount(None, user.strip(), pwd, token_store=TOKEN_PATH,
                            otp_callback=_panel_otp_callback)
        if not await account.login("micoapi"):
            raise ValueError(f"密码登录失败：{account._login_error}"
                             "（若提示「非法请求」10012，是小米风控，等 1~2 小时再试）")
        lst = (await MiNAService(account).device_list()) or []
        if not lst:
            raise ValueError("登录成功，但账号下没有小爱音箱")
        chosen = next((d for d in lst if SPEAKER and SPEAKER in
                       (d.get("name"), d.get("deviceID"))), lst[0])
        s = _session
        s["account"] = account
        s["cookie"] = {"userId": account.token["userId"],
                       "serviceToken": account.token["micoapi"][1],
                       "deviceId": chosen.get("deviceID")}
        s["hardware"] = chosen.get("hardware", "")
        s["id"] = chosen.get("deviceID", "")
        s["name"] = chosen.get("name", "")
        _speaker_info.update({"name": s["name"], "hardware": s["hardware"], "id": s["id"]})
        _login_state["state"] = "ok"
        _login_state["msg"] = s["name"]
        log.info("面板密码登录成功，使用音箱：%s (%s)", s["name"], s["hardware"])
        _start_poll_task()
    except Exception as e:  # noqa: BLE001
        _login_state["state"] = "error"
        _login_state["msg"] = str(e)[:300]
        log.warning("面板密码登录失败: %s", e)


async def _panel_submit_otp(code: str) -> dict:
    """面板提交验证码：喂给等待中的登录任务并等它收尾。"""
    fut = _login_state.get("otp_future")
    if not fut or fut.done():
        raise ValueError("当前没有等待验证码的登录流程")
    if not code.strip():
        raise ValueError("验证码不能为空")
    fut.set_result(code)
    while _login_state["state"] in ("sending", "otp_required"):
        await asyncio.sleep(0.5)
    if _login_state["state"] == "ok":
        return {"name": _session["name"], "hardware": _session["hardware"]}
    raise ValueError(_login_state["msg"] or "登录失败")


# ---------------------------------------------------------------------------
# 集成中心：面板内安装品牌集成（驱动）+ 代理 HA 配置/登录流程
# ---------------------------------------------------------------------------
HA_CONFIG_DIR = BASE_DIR / "homeassistant"

IC_CATALOG = [
    {"domain": "petkit", "name": "小佩 PetKit",
     "repo": "RobertD502/home-assistant-petkit", "login": "password",
     "desc": "猫砂盆/喂食器/饮水机等小佩设备，账号密码登录"},
    {"domain": "xiaomi_miot", "name": "小米米家 xiaomi_miot",
     "repo": "al-one/hass-xiaomi-miot", "login": "password",
     "desc": "小米账号密码登录，米家设备覆盖广（社区维护版）"},
    {"domain": "xiaomi_home", "name": "小米官方 ha_xiaomi_home",
     "repo": "XiaoMi/ha_xiaomi_home", "login": "oauth",
     "desc": "小米官方维护；登录需跳转小米授权页一次（OAuth）"},
    {"domain": "tuya", "name": "涂鸦 Tuya", "core": True,
     "login": "oauth",
     "desc": "大量贴牌智能设备；HA 内置集成，无需安装，直接配置"},
]

_IC_STATE = {"state": "idle", "msg": "", "domain": "", "repo": "", "ts": time.time()}
_IC_TERMINAL = ("idle", "done", "error")


def _ic_set(state: str, msg: str = "", **kw) -> None:
    """更新安装任务状态并打时间戳（前端据此判断是否卡死）。"""
    _IC_STATE.update(kw)
    _IC_STATE.update(state=state, msg=msg, ts=time.time())


def _ic_busy() -> bool:
    """是否已有任务在跑；卡住超过 10 分钟视为失败，允许重试。"""
    st = _IC_STATE["state"]
    if st in _IC_TERMINAL:
        return False
    if time.time() - _IC_STATE.get("ts", 0) > 600:
        _ic_set("error", "上次任务超时未结束（已自动解除占用），可以重试")
        return False
    return True


def _ha_api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """调 HA REST API，返回 (http状态码, 解析后的JSON)。不抛异常。"""
    from urllib import error as urlerror, request as urlrequest

    req = urlrequest.Request(
        f"{HA_BASE}{path}", method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {_ha_token_get()}",
                 "Content-Type": "application/json"},
    )
    try:
        with _ha_open(req, 30) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urlerror.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return e.code, {"message": raw[:200]}
    except Exception as e:  # noqa: BLE001
        return 0, {"message": f"{type(e).__name__}: {e}"}


# ---- HA 探活 / 启动（面板上可直接拉起 HA，省得去双击 bat） ----

_HA_EXE_CANDIDATES = (
    r"C:\Python312\Scripts\hass.exe",
    r"C:\Python311\Scripts\hass.exe",
    r"C:\Python310\Scripts\hass.exe",
)


def ha_alive() -> bool:
    """HA 的 /api/ 是否可用（不需要令牌也能判断进程在不在）。"""
    from urllib import error as urlerror, request as urlrequest

    try:
        with _ha_open(f"{HA_BASE}/api/", 3) as r:
            return r.status in (200, 401)
    except urlerror.HTTPError as e:
        return e.code == 401
    except Exception:  # noqa: BLE001
        return False


def ha_start() -> dict:
    """后台拉起 HA（hass.exe），成功返回 launched。"""
    import subprocess

    if ha_alive():
        return {"ok": True, "msg": "HA 已在运行"}

    exe = None
    for p in _HA_EXE_CANDIDATES:
        if Path(p).is_file():
            exe = p
            break
    if exe is None:
        import shutil
        exe = shutil.which("hass")
    if exe is None:
        return {"ok": False, "error": "找不到 hass.exe，请确认 Python 环境或手动双击 start_ha.bat"}

    # 脱离控制台启动，voice_bridge 退出后 HA 仍存活
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [exe, "--config", str(HA_CONFIG_DIR), "--ignore-os-check"],
            cwd=str(HA_CONFIG_DIR), creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"启动失败: {e}"}
    log.info("已尝试启动 Home Assistant（约 1~2 分钟就绪）")
    return {"ok": True, "msg": "已启动，约 1~2 分钟就绪"}


async def _ic_restart_ha(timeout_s: int = 150) -> str:
    """重启 HA 并等它回来。

    返回 "skipped"（HA 本来没运行，不用重启）/ "ok" / "restarted"（自动拉起成功）。
    """
    if not ha_alive():
        log.warning("HA 未运行，跳过重启（启动 HA 后集成即生效）")
        return "skipped"

    call_ha("homeassistant/restart", {})
    # 先等它真的停下去（否则会把「正在关机」的旧进程误判为已恢复）
    down = False
    for _ in range(20):
        await asyncio.sleep(3)
        if not ha_alive():
            down = True
            break
    if not down:
        log.warning("HA 没有进入重启状态（可能重启指令未生效），继续按存活处理")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if ha_alive():
            return "ok"
        await asyncio.sleep(3)

    # 重启后没回来：可能是进程直接退出了，尝试自己拉起来
    log.warning("HA 重启后未恢复，尝试手动启动")
    ha_start()
    deadline = time.time() + 180
    while time.time() < deadline:
        await asyncio.sleep(3)
        if ha_alive():
            return "restarted"
    raise ValueError("HA 重启后未能恢复（已尝试自动拉起）。"
                     "请双击 start_ha.bat 或点「启动 Home Assistant」后重试")


def _ic_download(url: str) -> bytes:
    """下载集成压缩包；GitHub 直连失败时自动尝试镜像加速。"""
    last = None
    mirrors = (url, "https://ghfast.top/" + url, "https://ghproxy.net/" + url)
    from urllib import request as urlrequest
    for u in mirrors:
        try:
            req = urlrequest.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urlrequest.urlopen(req, timeout=180) as r:
                data = r.read()
            if len(data) > 10_000:      # 镜像失败时可能返回错误页，按大小区分
                return data
            last = RuntimeError(f"响应过小({len(data)}B)")
        except Exception as e:  # noqa: BLE001
            last = e
    raise ValueError(f"下载失败: {last}")


def _ic_unzip(data: bytes) -> tuple[str, dict]:
    """从压缩包里定位 custom_components/<domain>/，返回 (domain, {相对路径: 内容})。"""
    import io
    import zipfile

    zin = zipfile.ZipFile(io.BytesIO(data))
    names = [n.replace("\\", "/") for n in zin.namelist() if not n.endswith("/")]

    # 找最浅的一句 custom_components/<x>/manifest.json
    best = None      # (深度, 目录名, manifest 路径)
    for n in names:
        p = n.split("/")
        if p[-1] != "manifest.json" or "custom_components" not in p:
            continue
        j = len(p) - 1 - p[::-1].index("custom_components")
        if len(p) <= j + 2:
            continue
        if best is None or j < best[0]:
            best = (j, p[j + 1], n)
    if best is None:
        raise ValueError("这不是 Home Assistant 自定义集成："
                         "压缩包里找不到 custom_components/<集成名>/manifest.json")

    _, dirname, mpath = best
    try:
        manifest = json.loads(zin.read(mpath).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        manifest = {}
    domain = str(manifest.get("domain") or dirname).strip()

    files = {}
    for n in names:
        p = n.split("/")
        if "custom_components" not in p:
            continue
        j = len(p) - 1 - p[::-1].index("custom_components")
        if len(p) > j + 2 and p[j + 1] == dirname:
            files["/".join(p[j + 2:])] = zin.read(n)
    if not files:
        raise ValueError("集成目录是空的，解压失败")
    return domain, files


def _gh_json(url: str) -> dict | list:
    """请求 GitHub API（未鉴权，够用）。"""
    from urllib import request as urlrequest

    req = urlrequest.Request(url, headers={
        "User-Agent": "voice-bridge", "Accept": "application/vnd.github+json"})
    # 外网请求保留系统代理（国内直连 GitHub 常不通）
    with urlrequest.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ic_search(q: str, limit: int = 15) -> list:
    """搜索 GitHub 上的 Home Assistant 自定义集成。"""
    from urllib.parse import quote

    q = (q or "").strip()
    if not q:
        return []
    query = f"{q} home-assistant in:name,description,topics"
    url = ("https://api.github.com/search/repositories?q=" + quote(query) +
           f"&sort=stars&order=desc&per_page={max(limit, 20)}")
    try:
        data = _gh_json(url)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"GitHub 搜索失败（可能被墙/限流）: {e}")

    out = []
    tokens = [t for t in q.lower().replace("-", " ").split() if len(t) >= 2]
    for it in (data.get("items") or []):
        name = it.get("full_name") or ""
        desc = (it.get("description") or "").strip()
        topics = [t.lower() for t in (it.get("topics") or [])]
        blob = f"{name} {desc} {' '.join(topics)}".lower()
        # 必须同时满足：① 像 HA 集成 ② 命中用户关键词（否则搜 socket 会出来一堆无关项目）
        ha_like = ("home assistant" in blob or "home-assistant" in blob or
                   "homeassistant" in blob or "hass" in blob or
                   "home-assistant" in topics or "homeassistant" in topics)
        hit_kw = not tokens or any(t in blob for t in tokens)
        if not (ha_like and hit_kw):
            continue
        # 相关性排序：仓库名命中 > 描述命中 > star 数
        name_l = name.lower()
        score = (3 if any(t in name_l for t in tokens) else 0) + \
                (1 if any(t in desc.lower() for t in tokens) else 0) + \
                min((it.get("stargazers_count") or 0) / 1000.0, 1.0)
        out.append({"repo": name, "desc": desc[:160], "_score": score,
                    "stars": it.get("stargazers_count", 0),
                    "branch": it.get("default_branch", "main"),
                    "updated": (it.get("pushed_at") or "")[:10],
                    "url": it.get("html_url", "")})
    out.sort(key=lambda x: (-x["_score"], -x["stars"]))
    for r in out:
        r.pop("_score", None)
    return out[:limit]


def _ic_repo_zip(repo: str) -> str:
    """把 owner/name 或 GitHub 链接规范成 zip 下载地址。"""
    repo = (repo or "").strip().rstrip("/")
    for pre in ("https://github.com/", "http://github.com/", "github.com/"):
        if repo.startswith(pre):
            repo = repo[len(pre):]
    if repo.endswith(".zip"):
        return "https://github.com/" + repo
    if repo.endswith(".git"):
        repo = repo[:-4]
    if repo.count("/") != 1:
        raise ValueError("仓库格式应为 owner/name，例如 RobertD501/ha-petkit")
    try:
        info = _gh_json(f"https://api.github.com/repos/{repo}")
        branch = info.get("default_branch") or "main"
    except Exception:  # noqa: BLE001
        branch = "main"
    return f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"


async def ic_install(domain: str = "", repo: str = "") -> None:
    """安装任务：下载 -> 解压到 custom_components -> 重启 HA -> 等恢复。

    domain 走内置推荐清单；repo 走用户自己搜索/粘贴的仓库。
    """
    entry = next((e for e in IC_CATALOG if e["domain"] == domain), None) if domain else None
    if entry and entry.get("core"):
        _ic_set("done", "核心集成无需安装，直接点「配置 / 登录」", domain=domain)
        return
    if entry:
        zip_url = await asyncio.to_thread(_ic_repo_zip, entry["repo"])
    elif repo:
        zip_url = await asyncio.to_thread(_ic_repo_zip, repo)
    else:
        raise ValueError("缺少要安装的集成（domain 或 repo）")

    _ic_set("downloading", f"正在下载 {repo or domain} …", domain=domain, repo=repo)
    data = await asyncio.to_thread(_ic_download, zip_url)

    _ic_set("extracting", "正在解压安装…")
    real_domain, files = _ic_unzip(data)
    if real_domain in ("", ".", "..") or "/" in real_domain:
        raise ValueError(f"集成域名异常: {real_domain!r}")
    dest = HA_CONFIG_DIR / "custom_components" / real_domain
    dest.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        f = dest / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(content)
    log.info("集成已解压: %s -> %s (%d 个文件)", repo or domain, dest, len(files))

    _ic_set("restarting", "安装完成，正在重启 HA（约 1 分钟）…", domain=real_domain)
    result = await _ic_restart_ha()

    if result == "skipped":
        _ic_set("done", f"✅ {real_domain} 已安装。HA 当前没运行，"
                        "点上面「启动 Home Assistant」后即可配置",
                domain=real_domain, repo=repo)
    elif result == "restarted":
        _ic_set("done", f"✅ {real_domain} 已安装（HA 已自动重新拉起），"
                        "点「配置 / 登录」接入账号", domain=real_domain, repo=repo)
    else:
        _ic_set("done", f"✅ {real_domain} 安装完成，点「配置 / 登录」接入账号",
                domain=real_domain, repo=repo)


async def ic_uninstall(domain: str) -> None:
    """卸载自定义集成：删配置条目 -> 删目录 -> 重启 HA。"""
    import shutil

    domain = (domain or "").strip()
    if not domain or "/" in domain or domain in (".", ".."):
        raise ValueError("集成域名无效")

    dest = HA_CONFIG_DIR / "custom_components" / domain
    if not dest.is_dir():
        raise ValueError(f"没有找到可卸载的自定义集成 {domain}"
                         "（HA 内置集成不能这样卸载）")

    # 先删掉该域下的配置条目，否则 HA 重启会报「集成未找到」
    removed = 0
    if ha_alive():
        _ic_set("removing", "正在移除配置条目…", domain=domain)
        _s, entries = _ha_api("GET", "/api/config/config_entries/entry")
        if isinstance(entries, list):
            for e in entries:
                if e.get("domain") == domain:
                    _ha_api("DELETE",
                            f"/api/config/config_entries/entry/{e.get('entry_id')}")
                    removed += 1

    _ic_set("removing", f"正在删除集成文件（已移除 {removed} 个配置条目）…", domain=domain)
    await asyncio.to_thread(shutil.rmtree, dest, True)
    log.info("已卸载集成: %s (配置条目 %d 个)", domain, removed)

    result = await _ic_restart_ha()
    if result == "skipped":
        _ic_set("done", f"✅ {domain} 已删除（HA 未运行，下次启动即生效）", domain="")
    elif result == "restarted":
        _ic_set("done", f"✅ {domain} 已卸载（HA 已自动重新拉起）", domain="")
    else:
        _ic_set("done", f"✅ {domain} 已卸载并重启 HA", domain="")


async def ic_flow_start(handler: str) -> dict:
    """发起 HA 配置流程（登录），返回第一步表单定义。"""
    status, data = _ha_api("POST", "/api/config/config_entries/flow",
                           {"handler": handler})
    return {"status": status, **data}


async def ic_flow_submit(flow_id: str, fields: dict) -> dict:
    """提交配置流程当前步骤，返回下一步/成功/中止。"""
    status, data = _ha_api("POST", f"/api/config/config_entries/flow/{flow_id}",
                           fields)
    return {"status": status, **data}


async def runtime_logout() -> None:
    """面板退出登录：停监听、删令牌、清状态。"""
    s = _session
    if s["poll_task"] and not s["poll_task"].done():
        s["poll_task"].cancel()
        try:
            await s["poll_task"]
        except asyncio.CancelledError:
            pass
    s.update({"account": None, "cookie": {}, "hardware": "", "id": "",
              "name": "", "poll_task": None})
    _speaker_info.update({"name": "", "hardware": "", "id": ""})
    try:
        os.remove(TOKEN_PATH)
        log.info("已退出登录：令牌已删除，监听停止")
    except FileNotFoundError:
        log.info("已退出登录：本来就没有令牌")
    except Exception as e:  # noqa: BLE001
        log.warning("删除令牌失败: %s", e)

PANEL_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>voice_bridge 管理面板</title>
<style>
  :root { --bd:#e2e8f0; --bg:#f7f8fa; --main:#2b6cb0; --ok:#276749; --err:#c53030; }
  * { box-sizing:border-box; }
  body { font-family:"Microsoft YaHei",system-ui,sans-serif; margin:0; background:var(--bg); color:#1a202c; }
  header { background:#fff; border-bottom:1px solid var(--bd); padding:14px 20px; display:flex; align-items:center; gap:14px; }
  header h1 { font-size:18px; margin:0; }
  header .status { font-size:13px; color:#4a5568; }
  nav { display:flex; gap:4px; padding:10px 20px 0; background:#fff; border-bottom:1px solid var(--bd); }
  nav button { border:none; background:none; padding:10px 16px; font-size:14px; cursor:pointer;
               border-bottom:2px solid transparent; color:#4a5568; }
  nav button.on { color:var(--main); border-bottom-color:var(--main); font-weight:600; }
  main { max-width:860px; margin:20px auto; padding:0 16px; }
  .card { background:#fff; border:1px solid var(--bd); border-radius:10px; padding:16px 18px; margin-bottom:16px; }
  .card h3 { margin:0 0 12px; font-size:15px; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--bd); vertical-align:top; }
  th { color:#718096; font-weight:600; font-size:13px; }
  .kw { display:inline-block; background:#ebf4ff; color:var(--main); border-radius:4px;
        padding:2px 8px; margin:2px 4px 2px 0; font-size:13px; }
  button.act { border:1px solid var(--bd); background:#fff; border-radius:6px; padding:5px 12px;
               cursor:pointer; font-size:13px; }
  button.act:hover { border-color:var(--main); color:var(--main); }
  button.pri { background:var(--main); color:#fff; border:none; border-radius:6px; padding:8px 18px;
               cursor:pointer; font-size:14px; }
  button.danger { color:var(--err); }
  input[type=text], select { border:1px solid var(--bd); border-radius:6px; padding:8px 10px;
               font-size:14px; width:100%; }
  label { display:block; font-size:13px; color:#4a5568; margin:12px 0 4px; }
  .hint { font-size:12.5px; color:#718096; line-height:1.7; }
  .msg { padding:8px 12px; border-radius:6px; font-size:13.5px; margin:10px 0; display:none; }
  .msg.ok { background:#f0fff4; color:var(--ok); display:block; }
  .msg.err { background:#fff5f5; color:var(--err); display:block; }
  pre.log { background:#1a202c; color:#a0aec0; font-size:12px; padding:14px; border-radius:8px;
            max-height:420px; overflow-y:auto; margin:0; white-space:pre-wrap; word-break:break-all; }
  pre.guide { background:#f6f8fa; border:1px dashed var(--bd); border-radius:8px; padding:12px;
              font-size:13px; white-space:pre-wrap; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .row > * { flex:1; }
  .row > button { flex:0 0 auto; }
</style>
</head>
<body>
<header>
  <h1>🐾 voice_bridge</h1>
  <span class="status" id="status">加载中…</span>
</header>
<nav>
  <button data-tab="list" class="on">口令列表</button>
  <button data-tab="add">添加口令</button>
  <button data-tab="test">测试</button>
  <button data-tab="log">日志</button>
  <button data-tab="acct">账号</button>
  <button data-tab="ic">集成中心</button>
</nav>
<main>

<div id="tab-list" class="tab">
  <div class="card">
    <h3>已配置的口令</h3>
    <div id="cmdlist">加载中…</div>
  </div>
  <div class="card hint">
    <b>说明</b>：口令识别基于「小爱对话记录」，音箱回复内容由训练计划决定，二者互不影响。
    删除口令不会影响音箱说话，只影响设备动作。
  </div>
</div>

<div id="tab-add" class="tab" style="display:none">
  <div class="card">
    <h3>① 选择设备指令（品牌 → 设备 → 指令）</h3>
    <label>品牌 / 集成</label>
    <select id="sel-brand" onchange="onBrand()"><option value="">加载中…</option></select>
    <label>设备</label>
    <select id="sel-device" disabled onchange="onDevice()"><option value="">先选品牌</option></select>
    <label>指令</label>
    <select id="sel-entity" disabled><option value="">先选设备</option></select>
    <div id="opt-wrap" style="display:none">
      <label>选择选项（选项类指令需要）</label>
      <select id="sel-option"></select>
    </div>
    <div style="margin-top:10px; padding-top:8px; border-top:1px dashed var(--bd, #ddd)">
      <label>或直接选择 HA 脚本（可选，与上面二选一）</label>
      <select id="sel-script"><option value="">— 不使用脚本 —</option></select>
    </div>
    <div class="hint" style="margin-top:8px" id="entity-preview"></div>
  </div>
  <div class="card">
    <h3>② 填写小爱口令</h3>
    <label>触发关键词（用逗号分隔，识别文本「包含」关键词即触发）</label>
    <input type="text" id="in-kw" placeholder="例如：倒掉猫砂, 换猫砂">
    <div class="hint">小爱同学不认识这些词也没关系，bridge 只看对话记录里的原文。</div>
  </div>
  <div class="card">
    <h3>③ 保存</h3>
    <div class="row">
      <button class="pri" onclick="saveCmd()">保存口令</button>
      <button class="act" onclick="copyGuide()">复制训练计划指引</button>
    </div>
    <div class="msg" id="msg-add"></div>
    <pre class="guide" id="guide" style="display:none"></pre>
  </div>
</div>

<div id="tab-test" class="tab" style="display:none">
  <div class="card">
    <h3>模拟识别测试</h3>
    <label>输入一句你会对小爱说的话</label>
    <input type="text" id="in-test" placeholder="例如：小爱同学，倒掉猫砂">
    <div class="row" style="margin-top:12px">
      <button class="pri" onclick="testMatch()">检测匹配</button>
      <button class="act" onclick="execMatch()">执行命中的动作</button>
    </div>
    <div class="msg" id="msg-test"></div>
    <pre class="guide" id="test-out" style="display:none"></pre>
  </div>
</div>

<div id="tab-log" class="tab" style="display:none">
  <div class="card">
    <h3>实时日志（每 2 秒刷新）</h3>
    <pre class="log" id="logbox">…</pre>
  </div>
</div>

<div id="tab-acct" class="tab" style="display:none">
  <div class="card">
    <h3>账号状态</h3>
    <div id="acct-status">加载中…</div>
    <div class="row" style="margin-top:10px">
      <button class="act danger" id="btn-logout" style="display:none" onclick="doLogout()">退出登录</button>
    </div>
  </div>
  <div class="card" id="login-card">
    <h3>登录小米账号</h3>
    <label>小米账号（手机号/邮箱）</label>
    <input type="text" id="in-user" placeholder="手机号或邮箱">
    <label>密码</label>
    <input type="password" id="in-pwd" placeholder="小米账号密码">
    <div class="row" style="margin-top:10px">
      <button class="pri" onclick="doPwdLogin()">登录</button>
    </div>
    <div id="otp-row" style="display:none">
      <label>短信验证码（已发送到手机）</label>
      <div class="row">
        <input type="text" id="in-otp" style="max-width:160px" placeholder="6 位数字">
        <button class="pri" onclick="doOtp()">提交验证码</button>
      </div>
    </div>
    <div class="msg" id="msg-acct"></div>
    <details style="margin-top:12px">
      <summary class="hint" style="cursor:pointer">passToken 方式登录（备用，不需要密码）</summary>
      <div class="hint" style="margin-top:6px">
        1. 浏览器打开 <b>account.xiaomi.com</b> 并登录 → 2. F12 → Application → Cookies
        → https://account.xiaomi.com → 3. 复制 <b>passToken</b> 和 <b>userId</b>
      </div>
      <label>passToken</label>
      <textarea id="in-pt" rows="3" style="width:100%; box-sizing:border-box; font-size:12px"
                placeholder="V1:xxxxxxxx…"></textarea>
      <label>userId（纯数字）</label>
      <input type="text" id="in-uid" placeholder="例如 1234567890">
      <div class="row" style="margin-top:8px">
        <button class="act" onclick="doLogin()">用 passToken 登录</button>
      </div>
    </details>
  </div>
</div>
<div id="tab-ic" class="tab" style="display:none">
  <div class="card" id="ha-down-card" style="display:none; border-color:#f6ad55; background:#fffaf0">
    <h3>⚠️ Home Assistant 没有运行</h3>
    <div class="hint" style="margin-bottom:8px">
      面板的所有设备动作都要靠 HA 执行，HA 关着就会一直报「连接被拒绝」。
    </div>
    <button class="pri" onclick="haStart()">启动 Home Assistant</button>
    <div class="msg" id="msg-ha"></div>
  </div>
  <div class="card">
    <h3>我的集成（可卸载）</h3>
    <div id="ic-installed">加载中…</div>
  </div>
  <div class="card">
    <h3>搜索集成并安装</h3>
    <div class="hint" style="margin-bottom:8px">
      搜 GitHub 上的 Home Assistant 集成（关键词：品牌名 / 设备名 / 功能）。安装 = 下载驱动 →
      自动重启 HA，装好后回到这里点「配置 / 登录」。
    </div>
    <div class="row">
      <input type="text" id="ic-q" placeholder="例如 petkit、tuya、govee、智能插座…"
             style="flex:1" onkeydown="if(event.key==='Enter')icSearch()">
      <button class="pri" onclick="icSearch()">搜索</button>
    </div>
    <div id="ic-results"></div>
    <div class="hint" style="margin-top:10px">
      也可以直接粘贴仓库地址/名称安装：
      <input type="text" id="ic-repo" placeholder="owner/repo 或 https://github.com/owner/repo"
             style="width:60%">
      <button class="act" onclick="icInstallRepo()">安装</button>
    </div>
  </div>
  <div class="card">
    <h3>推荐（常用集成，一键安装）</h3>
    <div id="ic-catalog">加载中…</div>
    <div class="msg" id="msg-ic"></div>
    <div id="ic-stuck" style="display:none; margin-top:6px">
      <button class="act" onclick="icReset()">任务好像卡住了，强制解除</button>
    </div>
  </div>
  <div class="card" id="ic-login-card" style="display:none">
    <h3 id="ic-login-title">配置 / 登录</h3>
    <div id="ic-form"></div>
    <div class="row" style="margin-top:10px">
      <button class="pri" onclick="icSubmit()">提交</button>
    </div>
    <div class="msg" id="msg-ic-flow"></div>
  </div>
</div>

</main>
<script>
let CMDS = [];
let TEST_HIT = null;
const $ = id => document.getElementById(id);

document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  document.querySelectorAll('.tab').forEach(t => t.style.display = 'none');
  $('tab-' + b.dataset.tab).style.display = '';
  if (b.dataset.tab === 'log') refreshLog();
  if (b.dataset.tab === 'list') refreshCmds();
  if (b.dataset.tab === 'acct') refreshAcct();
  if (b.dataset.tab === 'ic') refreshIC();
});

async function jget(url) { const r = await fetch(url); return r.json(); }
async function jpost(url, data) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                              body: JSON.stringify(data)});
  return r.json();
}

function targetLabel(c) {
  if (c.script) return '脚本 script.' + c.script;
  if (c.service) return '服务 ' + c.service + ' ← ' + ((c.data||{}).entity_id || '');
  return '?';
}

async function refreshCmds() {
  const d = await jget('/api/commands');
  CMDS = d.commands || [];
  const el = $('cmdlist');
  if (!CMDS.length) { el.innerHTML = '<p class="hint">还没有口令，去「添加口令」建一条。</p>'; return; }
  el.innerHTML = '<table><tr><th>关键词</th><th>动作</th><th></th></tr>' +
    CMDS.map((c, i) =>
      `<tr><td>${c.keywords.map(k => `<span class="kw">${k}</span>`).join('')}</td>` +
      `<td style="font-size:13px">${targetLabel(c)}</td>` +
      `<td><button class="act danger" onclick="delCmd(${i})">删除</button></td></tr>`).join('') +
    '</table>';
}

async function delCmd(i) {
  if (!confirm('删除这条口令？')) return;
  CMDS.splice(i, 1);
  const d = await jpost('/api/commands', {commands: CMDS});
  showMsg('msg-add', d.ok ? '已删除' : (d.error || '失败'), d.ok);
  refreshCmds();
}

const DOMAIN_LABEL = {button:'按钮', select:'选项', switch:'开关', vacuum:'清洁',
                      script:'脚本', fan:'风扇', light:'灯'};
const dlabel = d => DOMAIN_LABEL[d] || d;

async function loadEntities() {
  const selB = $('sel-brand'), selS = $('sel-script');
  if (!PANEL_ENTITIES.length) {
    selB.innerHTML = '<option value="">HA 实体加载失败（HA 未运行？）</option>';
    return;
  }
  const nonScript = PANEL_ENTITIES.filter(e => e.brand !== '脚本');
  const brands = [...new Set(nonScript.map(e => e.brand))];
  brands.sort((a, b) => a.localeCompare(b, 'zh'));
  selB.innerHTML = '<option value="">— 请选择 —</option>' +
    brands.map(b => `<option value="${b}">${b}（${nonScript.filter(e => e.brand === b).length} 条指令）</option>`).join('');
  selB.disabled = false;
  const scripts = PANEL_ENTITIES.filter(e => e.brand === '脚本');
  selS.innerHTML = '<option value="">— 不使用脚本 —</option>' +
    scripts.map(e => `<option value="${e.entity_id}">${e.name}</option>`).join('');
}

$('sel-script').onchange = () => {
  const sid = $('sel-script').value;
  if (sid) {
    $('sel-entity').value = '';
    $('opt-wrap').style.display = 'none';
    $('entity-preview').textContent = '动作：运行脚本 ' + sid;
  } else {
    $('entity-preview').textContent = '';
  }
};

function onBrand() {
  const b = $('sel-brand').value;
  const selD = $('sel-device'), selE = $('sel-entity');
  selE.disabled = true; selE.innerHTML = '<option value="">先选设备</option>';
  $('opt-wrap').style.display = 'none';
  $('entity-preview').textContent = '';
  if (!b) {
    selD.disabled = true; selD.innerHTML = '<option value="">先选品牌</option>';
    return;
  }
  const devs = [...new Set(PANEL_ENTITIES.filter(e => e.brand === b).map(e => e.device))];
  devs.sort((a, c) => a.localeCompare(c, 'zh'));
  selD.innerHTML = '<option value="">— 请选择 —</option>' +
    devs.map(d => `<option value="${d}">${d}</option>`).join('');
  selD.disabled = false;
}

function onDevice() {
  const b = $('sel-brand').value, dv = $('sel-device').value;
  const selE = $('sel-entity');
  $('opt-wrap').style.display = 'none';
  $('entity-preview').textContent = '';
  if (!dv) {
    selE.disabled = true; selE.innerHTML = '<option value="">先选设备</option>';
    return;
  }
  const ents = PANEL_ENTITIES.filter(e => e.brand === b && e.device === dv);
  ents.sort((a, c) => (a.domain + a.name).localeCompare(c.domain + c.name, 'zh'));
  selE.innerHTML = '<option value="">— 请选择 —</option>' +
    ents.map(e => `<option value="${e.entity_id}">${dlabel(e.domain)}｜${e.name}</option>`).join('');
  selE.disabled = false;
}

$('sel-entity').onchange = () => {
  const eid = $('sel-entity').value;
  if (eid) {
    $('sel-script').value = '';
    $('entity-preview').textContent = '';
  }
  const e = (PANEL_ENTITIES||[]).find(x => x.entity_id === eid);
  const domain = eid.split('.')[0];
  $('opt-wrap').style.display = domain === 'select' ? '' : 'none';
  if (domain === 'select' && e) {
    $('sel-option').innerHTML = (e.options||[]).map(o => `<option value="${o}">${o}</option>`).join('');
  }
  $('entity-preview').textContent = eid ? '动作：' + ({
    button:'按下按钮', select:'切换到指定选项', switch:'开启',
    vacuum:'执行服务', script:'运行脚本', fan:'开启', light:'开启'}[domain] || domain) : '';
};
let PANEL_ENTITIES = [];

async function saveCmd() {
  const sid = $('sel-script').value;
  const eid = sid ? '' : $('sel-entity').value;
  const kws = $('in-kw').value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
  const show = (ok, t) => showMsg('msg-add', t, ok);
  if (!sid && !eid) return show(false, '请先选择设备指令或 HA 脚本');
  if (!kws.length) return show(false, '请填写至少一个关键词');
  const domain = eid.split('.')[0];
  const cmd = {keywords: kws};
  if (sid) { cmd.script = sid.split('.')[1]; }
  else if (domain === 'button') { cmd.service = 'button/press'; cmd.data = {entity_id: eid}; }
  else if (domain === 'select') { cmd.service = 'select/select_option';
    cmd.data = {entity_id: eid, option: $('sel-option').value}; }
  else if (domain === 'script') { cmd.script = eid.split('.')[1] || eid; }
  else { cmd.service = 'switch/turn_on'; cmd.data = {entity_id: eid}; }
  const d = await jpost('/api/commands', {commands: CMDS.concat([cmd])});
  if (d.ok) {
    show(true, '已保存并生效');
    $('in-kw').value = '';
    CMDS.push(cmd);
    $('guide').style.display = '';
    $('guide').textContent = guideText(cmd);
  } else show(false, d.error || '保存失败');
}

function guideText(cmd) {
  const kws = cmd.keywords.join('「 或 」');
  return `在小爱 App 创建同名词语（仅影响音箱回复，不做任何动作）：
1. 打开「小爱音箱」App → 训练计划 → 创建训练计划
2. 预设语句填：${kws}
3. 小爱回复填：好的，马上执行（随意）
4. 执行动作：什么都不要添加！动作由 voice_bridge 完成
5. 保存并启用`;
}
function copyGuide() {
  navigator.clipboard.writeText($('guide').textContent || '先保存一条口令');
  showMsg('msg-add', '指引已复制到剪贴板', true);
}

async function testMatch() {
  const text = $('in-test').value.trim();
  if (!text) return;
  const d = await jpost('/api/test', {text});
  TEST_HIT = d.match;
  const out = $('test-out'); out.style.display = '';
  if (d.match) {
    out.textContent = '✅ 命中口令：\n' + JSON.stringify(d.match, null, 2);
    showMsg('msg-test', '命中「' + d.match.keywords[0] + '」', true);
  } else {
    out.textContent = '❌ 没有命中任何口令';
    showMsg('msg-test', '未命中', false);
  }
}
async function execMatch() {
  if (TEST_HIT == null) return showMsg('msg-test', '先检测匹配', false);
  const idx = CMDS.findIndex(c => JSON.stringify(c) === JSON.stringify(TEST_HIT));
  if (idx < 0) return showMsg('msg-test', '口令已变更，请重新检测', false);
  const d = await jpost('/api/exec', {index: idx});
  showMsg('msg-test', `执行结果：HTTP ${d.code} ${d.body || ''}`, d.code === 200);
}

async function refreshLog() {
  const d = await jget('/api/logs');
  const box = $('logbox');
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.textContent = (d.lines || []).join('\n') || '（暂无日志）';
  if (stick) box.scrollTop = box.scrollHeight;
}

async function refreshAcct() {
  const s = await jget('/api/status');
  const el = $('acct-status');
  if (s.logged_in) {
    el.innerHTML = '<span style="color:#2a9d5c">● 已登录</span>｜音箱：' + s.speaker +
                   '（' + s.hardware + '）｜口令 ' + s.commands + ' 条｜每 ' + s.poll + 's 轮询';
    $('btn-logout').style.display = '';
    $('login-card').style.display = 'none';
  } else {
    el.innerHTML = '<span style="color:#c0392b">● 未登录</span>——监听未运行，请在下方登录';
    $('btn-logout').style.display = 'none';
    $('login-card').style.display = '';
  }
  $('status').textContent = s.logged_in
    ? `音箱：${s.speaker}（${s.hardware}）｜口令 ${s.commands} 条｜每 ${s.poll}s 轮询`
    : '未登录——登录后开始监听';
}

let LOGIN_POLL = null;

function startLoginPoll() {
  if (LOGIN_POLL) clearInterval(LOGIN_POLL);
  LOGIN_POLL = setInterval(async () => {
    const d = await jget('/api/login/state');
    if (d.state === 'otp_required') {
      $('otp-row').style.display = '';
      showMsg('msg-acct', d.msg || '验证码已发送，请输入', true);
    } else if (d.state === 'sending') {
      showMsg('msg-acct', d.msg || '正在登录…', true);
    } else if (d.state === 'ok') {
      clearInterval(LOGIN_POLL); LOGIN_POLL = null;
      showMsg('msg-acct', '登录成功：' + d.msg, true);
      refreshAcct();
    } else if (d.state === 'error') {
      clearInterval(LOGIN_POLL); LOGIN_POLL = null;
      $('otp-row').style.display = 'none';
      showMsg('msg-acct', d.msg || '登录失败', false);
    }
  }, 2000);
}

async function doPwdLogin() {
  const user = $('in-user').value.trim(), pwd = $('in-pwd').value;
  if (!user || !pwd) return showMsg('msg-acct', '请填写账号和密码', false);
  const d = await jpost('/api/login', {user, pwd});
  if (d.ok) startLoginPoll();
  else showMsg('msg-acct', d.error || '登录请求失败', false);
}

async function doOtp() {
  const code = $('in-otp').value.trim();
  if (!code) return showMsg('msg-acct', '请输入验证码', false);
  const d = await jpost('/api/login/otp', {code});
  if (d.ok) {
    showMsg('msg-acct', '验证通过，登录成功：' + (d.name || ''), true);
    $('otp-row').style.display = 'none';
    $('in-user').value = ''; $('in-pwd').value = ''; $('in-otp').value = '';
    if (LOGIN_POLL) { clearInterval(LOGIN_POLL); LOGIN_POLL = null; }
    refreshAcct();
  } else {
    showMsg('msg-acct', d.error || '验证失败', false);
  }
}

async function doLogin() {
  const d = await jpost('/api/login', {passToken: $('in-pt').value, userId: $('in-uid').value});
  if (d.ok) {
    showMsg('msg-acct', '登录成功，音箱：' + d.name, true);
    $('in-pt').value = ''; $('in-uid').value = '';
    refreshAcct();
  } else {
    showMsg('msg-acct', d.error || '登录失败，详见日志页', false);
  }
}

async function doLogout() {
  if (!confirm('退出登录后监听停止、已保存的令牌删除，确定？')) return;
  const d = await jpost('/api/logout', {});
  showMsg('msg-acct', d.ok ? '已退出登录' : (d.error || '失败'), d.ok);
  refreshAcct();
}

/* ---------- 集成中心 ---------- */
let IC_FLOW = null;
let IC_POLL_T = null;

async function refreshIC() {
  const d = await jget('/api/ic/list');
  $('ha-down-card').style.display = d.ha_alive ? 'none' : '';
  $('ic-installed').innerHTML = d.installed.length
    ? '<table><tr><th>集成</th><th>版本</th><th>操作</th></tr>' +
      d.installed.map(i => `<tr><td>${i.name}（${i.domain}）</td><td>${i.version || '-'}</td>` +
        `<td><button class="act" onclick="icFlow('${i.domain}')">配置</button> ` +
        `<button class="act" style="color:#c53030" onclick="icUninstall('${i.domain}','${i.name}')">卸载</button></td></tr>`).join('') +
      '</table>'
    : '<p class="hint">还没有自定义集成</p>';
  $('ic-catalog').innerHTML = d.catalog.map(e => {
    const badge = e.login === 'password'
      ? '<span class="kw">账密登录</span>' : '<span class="kw">OAuth 授权</span>';
    const btn = e.installed
      ? `<button class="act" onclick="icFlow('${e.domain}')">配置 / 登录</button>`
      : `<button class="pri" onclick="icInstall('${e.domain}')">安装</button>`;
    return `<div style="border:1px solid #e5e5e5;border-radius:8px;padding:10px;margin:8px 0">` +
      `<b>${e.name}</b> ${badge}<br>` +
      `<span class="hint">${e.desc || ''}</span>` +
      `<div class="row" style="margin-top:6px">${btn}` +
      (e.repo ? ` <a class="hint" href="https://github.com/${e.repo}" target="_blank">源码</a>` : '') +
      `</div></div>`;
  }).join('');
  if (['downloading', 'extracting', 'restarting', 'removing'].includes(d.state)) {
    showMsg('msg-ic', d.msg, true, true);
    icPollState();
  }
}

async function haStart() {
  showMsg('msg-ha', '正在启动 HA（约 1~2 分钟）…', true);
  const d = await jpost('/api/ha/start', {});
  if (d.ok) {
    showMsg('msg-ha', d.msg || '已启动', true);
    let n = 0;
    const t = setInterval(async () => {
      const r = await jget('/api/ic/list');
      if (r.ha_alive || ++n > 30) {
        clearInterval(t);
        if (r.ha_alive) { showMsg('msg-ha', 'HA 已就绪', true); refreshIC(); }
      }
    }, 5000);
  } else showMsg('msg-ha', d.error || '启动失败', false);
}

async function icSearch() {
  const q = $('ic-q').value.trim();
  if (!q) return;
  $('ic-results').innerHTML = '<p class="hint">搜索中…</p>';
  const d = await jget('/api/ic/search?q=' + encodeURIComponent(q));
  if (!d.ok) { $('ic-results').innerHTML = `<p style="color:#c53030">${d.error || '搜索失败'}</p>`; return; }
  if (!d.results.length) {
    $('ic-results').innerHTML = '<p class="hint">没有搜到合适的集成，换个关键词试试（英文品牌名更容易命中）</p>';
    return;
  }
  $('ic-results').innerHTML = d.results.map(r =>
    `<div style="border:1px solid #e5e5e5;border-radius:8px;padding:10px;margin:8px 0;cursor:default">` +
    `<b>${r.repo}</b> <span class="hint">★ ${r.stars} · 更新 ${r.updated}</span><br>` +
    `<span class="hint">${r.desc || '（无描述）'}</span>` +
    `<div class="row" style="margin-top:6px">` +
    `<button class="pri" onclick="icInstall('', '${r.repo}')">安装</button> ` +
    `<a class="hint" href="${r.url}" target="_blank">GitHub</a></div></div>`).join('');
}

function icInstallRepo() {
  const v = $('ic-repo').value.trim();
  if (!v) return;
  icInstall('', v);
}

async function icUninstall(domain, name) {
  if (!confirm(`卸载「${name || domain}」？\n\n会删除 custom_components/${domain} 目录、`
             + `移除该集成下的全部配置条目，并重启 HA。设备在 HA 里的实体也会消失。`)) return;
  const d = await jpost('/api/ic/uninstall', {domain});
  if (d.ok) {
    showMsg('msg-ic', `正在卸载 ${domain}（删配置 → 删文件 → 重启 HA）…`, true);
    icPollState();
  } else showMsg('msg-ic', d.error || '卸载失败', false);
}

async function icInstall(domain, repo) {
  const d = await jpost('/api/ic/install', {domain: domain || '', repo: repo || ''});
  if (d.ok) {
    showMsg('msg-ic', '安装任务已开始：下载 → 解压 → 自动重启 HA（约 1 分钟）', true);
    icPollState();
  } else showMsg('msg-ic', d.error || '失败', false);
}

function icPollState() {
  if (IC_POLL_T) return;              // 只保留一个轮询，重复调用直接忽略
  IC_POLL_T = setInterval(async () => {
    let d;
    try { d = await jget('/api/ic/state'); } catch (e) { return; }
    const busy = !['done', 'error', 'idle'].includes(d.state);
    showMsg('msg-ic', d.msg || d.state, d.state !== 'error', busy);
    $('ic-stuck').style.display = (busy && d.age > 240) ? '' : 'none';
    if (!busy) {
      clearInterval(IC_POLL_T); IC_POLL_T = null;
      $('ic-stuck').style.display = 'none';
      const el = $('msg-ic');         // 结束后 6 秒淡出
      clearTimeout(el._hide);
      el._hide = setTimeout(() => {
        el.className = 'msg'; el.dataset.text = ''; el.dataset.ok = '';
      }, 6000);
      refreshIC();
    }
  }, 2000);
}

async function icReset() {
  const d = await jpost('/api/ic/reset', {});
  showMsg('msg-ic', d.ok ? '已解除占用，可以重新操作' : (d.error || '无法重置'), d.ok);
  $('ic-stuck').style.display = 'none';
  refreshIC();
}

async function icFlow(domain) {
  const d = await jpost('/api/ic/flow', {handler: domain});
  IC_FLOW = d;
  if (d.type === 'form') icRenderForm(d);
  else if (d.type === 'external') {
    $('ic-login-card').style.display = '';
    $('ic-form').innerHTML =
      `<p>该品牌需要 OAuth 授权，请在 HA 网页完成一次：</p>` +
      `<p><a href="http://127.0.0.1:8123/config/integrations" target="_blank">点此打开 HA 集成页</a></p>` +
      `<p class="hint">${(d.description_placeholders || {}).url || ''}</p>`;
    $('msg-ic-flow').className = 'msg';
  } else if (d.type === 'abort') {
    showMsg('msg-ic-flow', '配置中止：' + (d.reason || '未知原因'), false);
  } else showMsg('msg-ic-flow', '异常响应：' + JSON.stringify(d).slice(0, 150), false);
}

function icRenderForm(step) {
  $('ic-login-card').style.display = '';
  $('ic-login-title').textContent = '配置 / 登录：' + (step.title || IC_FLOW.handler || '');
  const ph = step.description_placeholders || {};
  const schema = step.data_schema || [];
  $('ic-form').innerHTML = schema.map(([key, sch]) => {
    const label = ph[key] || key;
    const val = sch.default != null ? sch.default : '';
    if (sch.type === 'boolean') {
      return `<label>${label}</label><select id="f_${key}">` +
        `<option value="true" ${val ? 'selected' : ''}>开</option>` +
        `<option value="false" ${val === false ? 'selected' : ''}>关</option></select>`;
    }
    if (sch.type === 'select') {
      return `<label>${label}</label><select id="f_${key}">` +
        (sch.options || []).map(o => {
          const [ov, ol] = Array.isArray(o) ? o : [o, o];
          return `<option value="${ov}" ${ov === val ? 'selected' : ''}>${ol}</option>`;
        }).join('') + '</select>';
    }
    if (sch.type === 'integer' || sch.type === 'float') {
      return `<label>${label}</label><input type="number" id="f_${key}" value="${val}">`;
    }
    const isSecret = /pass|pwd|token|secret|key/i.test(key);
    return `<label>${label}</label><input type="${isSecret ? 'password' : 'text'}" id="f_${key}" value="${val}">`;
  }).join('') || '<p class="hint">本步骤无需填写，直接提交</p>';
  $('msg-ic-flow').className = 'msg';
}

async function icSubmit() {
  if (!IC_FLOW || !IC_FLOW.flow_id) return showMsg('msg-ic-flow', '没有进行中的配置', false);
  const fields = {};
  $('ic-form').querySelectorAll('[id^="f_"]').forEach(el => {
    const k = el.id.slice(2);
    let v = el.value;
    if (el.tagName === 'SELECT' && (v === 'true' || v === 'false')) v = v === 'true';
    else if (el.type === 'number') v = Number(v);
    fields[k] = v;
  });
  const d = await jpost('/api/ic/flow_submit', {flow_id: IC_FLOW.flow_id, fields});
  IC_FLOW = d;
  if (d.type === 'form') {
    icRenderForm(d);
    showMsg('msg-ic-flow', '本步已通过，继续填写', true);
  } else if (d.type === 'create_entry') {
    showMsg('msg-ic-flow', '✅ 配置成功！设备已接入，去「添加口令」页选它', true);
    $('ic-login-card').style.display = 'none';
    PANEL_ENTITIES = (await jget('/api/ha/entities')).entities || [];
    loadEntities();
  } else if (d.type === 'external') {
    $('ic-form').innerHTML =
      '<p>需要 OAuth 授权，请打开 <a href="http://127.0.0.1:8123/config/integrations" target="_blank">HA 集成页</a> 完成授权</p>';
  } else if (d.type === 'abort') {
    showMsg('msg-ic-flow', '配置中止：' + (d.reason || '未知原因') + '（可能是账号已配置过）', false);
  } else {
    showMsg('msg-ic-flow', '错误：' + JSON.stringify(d.errors || d).slice(0, 150), false);
  }
}

function showMsg(id, text, ok, sticky) {
  const el = $(id);
  if (!el) return;
  // 内容没变就不重绘，否则 2 秒一次轮询会让提示条反复闪烁
  if (el.dataset.text === text && el.dataset.ok === String(!!ok)) return;
  el.dataset.text = text;
  el.dataset.ok = String(!!ok);
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  el.textContent = text;
  clearTimeout(el._hide);
  if (!sticky) {
    el._hide = setTimeout(() => {
      el.className = 'msg';
      el.dataset.text = '';
      el.dataset.ok = '';
    }, 4500);
  }
}

(async function init() {
  const s = await jget('/api/status');
  $('status').textContent = `音箱：${s.speaker}（${s.hardware}）｜口令 ${s.commands} 条｜每 ${s.poll}s 轮询` +
    (s.ha_alive ? '｜HA ✅' : '｜HA ❌ 未运行（去「集成中心」一键启动）');
  PANEL_ENTITIES = (await jget('/api/ha/entities')).entities || [];
  loadEntities();
  refreshCmds();
  refreshAcct();
  setInterval(refreshLog, 2000);
})();
</script>
</body>
</html>
"""


class _PanelHandler(BaseHTTPRequestHandler):
    """管理面板的 HTTP 处理器。仅响应本机回环地址。"""

    def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8") -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._send(200, PANEL_HTML.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/commands":
            self._send(200, {"commands": COMMANDS})
        elif self.path == "/api/logs":
            self._send(200, {"lines": list(_ring.buf)})
        elif self.path == "/api/ha/entities":
            self._send(200, {"entities": ha_get_states()})
        elif self.path == "/api/ic/list":
            installed = []
            cc = HA_CONFIG_DIR / "custom_components"
            if cc.is_dir():
                for d in sorted(cc.iterdir()):
                    mf = d / "manifest.json"
                    if d.is_dir() and mf.is_file():
                        try:
                            m = json.loads(mf.read_text(encoding="utf-8"))
                        except Exception:  # noqa: BLE001
                            m = {}
                        installed.append({"domain": d.name,
                                          "name": m.get("name", d.name),
                                          "version": m.get("version", ""),
                                          "repo": (m.get("documentation") or
                                                   m.get("issue_tracker") or "")})
            domains = {i["domain"] for i in installed}
            catalog = [dict(e, installed=e["domain"] in domains or bool(e.get("core")))
                       for e in IC_CATALOG]
            self._send(200, {"installed": installed, "catalog": catalog,
                             "ha_alive": ha_alive(),
                             "state": _IC_STATE["state"], "msg": _IC_STATE["msg"]})
        elif self.path.startswith("/api/ic/search"):
            from urllib.parse import parse_qs, urlparse
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]
            try:
                results = ic_search(q)
            except Exception as e:  # noqa: BLE001
                return self._send(200, {"ok": False, "error": str(e)[:200],
                                        "results": []})
            self._send(200, {"ok": True, "results": results})
        elif self.path == "/api/ic/state":
            self._send(200, {**_IC_STATE,
                             "age": int(time.time() - _IC_STATE.get("ts", 0))})
        elif self.path == "/api/login/state":
            self._send(200, {"state": _login_state["state"],
                             "msg": _login_state["msg"]})
        elif self.path == "/api/status":
            self._send(200, {"speaker": _speaker_info["name"] or "未知",
                             "hardware": _speaker_info["hardware"] or "?",
                             "logged_in": bool(_session["cookie"]),
                             "ha_alive": ha_alive(),
                             "commands": len(COMMANDS), "poll": POLL_INTERVAL})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        global COMMANDS
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            return self._send(400, {"ok": False, "error": "bad json"})

        if self.path == "/api/commands":
            incoming = body.get("commands")
            if not isinstance(incoming, list):
                return self._send(400, {"ok": False, "error": "commands 必须是列表"})
            # 复用 load_commands 的校验逻辑：先落盘再重读
            _save_commands(incoming)
            COMMANDS = load_commands()
            log.info("面板更新口令配置，现在 %d 条", len(COMMANDS))
            self._send(200, {"ok": True, "commands": len(COMMANDS)})
        elif self.path == "/api/test":
            text = str(body.get("text", ""))
            cmd = None
            for c in COMMANDS:
                if any(k in text for k in c["keywords"]):
                    cmd = c
                    break
            self._send(200, {"match": cmd})
        elif self.path == "/api/exec":
            try:
                cmd = COMMANDS[int(body.get("index", -1))]
            except (IndexError, ValueError, TypeError):
                return self._send(400, {"ok": False, "error": "index 无效"})
            code, resp = exec_command(cmd)
            log.info("面板手动执行 %s -> HTTP %s", cmd.get("script") or cmd.get("service"), code)
            self._send(200, {"code": code, "body": resp[:120]})
        elif self.path == "/api/ic/install":
            if _ic_busy():
                return self._send(400, {"ok": False, "error": "已有任务在进行"})
            domain = str(body.get("domain", "") or "")
            repo = str(body.get("repo", "") or "")
            if not domain and not repo:
                return self._send(400, {"ok": False, "error": "缺少 domain 或 repo"})
            _ic_set("downloading", "准备下载…", domain=domain, repo=repo)
            fut = asyncio.run_coroutine_threadsafe(
                ic_install(domain, repo), _LOOP)
            # 只报启动成功与否，过程走 /api/ic/state 轮询
            def _on_done(f):
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001
                    _ic_set("error", str(e)[:300])
                    log.warning("集成安装失败: %s", e)
            fut.add_done_callback(_on_done)
            self._send(200, {"ok": True, "state": "started"})
        elif self.path == "/api/ic/uninstall":
            if _ic_busy():
                return self._send(400, {"ok": False, "error": "已有任务在进行"})
            domain = str(body.get("domain", "") or "").strip()
            if not domain:
                return self._send(400, {"ok": False, "error": "缺少 domain"})
            _ic_set("removing", "准备卸载…", domain=domain)
            fut = asyncio.run_coroutine_threadsafe(ic_uninstall(domain), _LOOP)

            def _on_done2(f):
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001
                    _ic_set("error", str(e)[:300])
                    log.warning("卸载失败: %s", e)
            fut.add_done_callback(_on_done2)
            self._send(200, {"ok": True, "state": "started"})
        elif self.path == "/api/ic/reset":
            age = time.time() - _IC_STATE.get("ts", 0)
            if _IC_STATE["state"] in _IC_TERMINAL or age > 240:
                _ic_set("error", "已手动解除占用（原任务可能仍在后台跑完）",
                        domain="", repo="")
                self._send(200, {"ok": True})
            else:
                self._send(200, {"ok": False,
                                 "error": f"任务才开始 {int(age)} 秒，再等一会儿"})
        elif self.path == "/api/ha/start":
            self._send(200, ha_start())
        elif self.path == "/api/ic/flow":
            status, data = _ha_api("POST", "/api/config/config_entries/flow",
                                   {"handler": str(body.get("handler", ""))})
            self._send(200, {"status": status, **data})
        elif self.path == "/api/ic/flow_submit":
            status, data = _ha_api(
                "POST", f"/api/config/config_entries/flow/{body.get('flow_id', '')}",
                dict(body.get("fields") or {}))
            self._send(200, {"status": status, **data})
        elif self.path in ("/api/login", "/api/login/otp", "/api/logout") and _LOOP is not None:
            # 登录/登出需要在主事件循环里跑（miservice 是异步的）
            if self.path == "/api/logout":
                coro = runtime_logout()
            elif self.path == "/api/login/otp":
                coro = _panel_submit_otp(str(body.get("code", "")))
            elif body.get("passToken"):
                coro = runtime_login(body.get("passToken", ""), body.get("userId", ""))
            elif body.get("user") and body.get("pwd"):
                if _login_state["state"] not in ("idle", "ok", "error"):
                    return self._send(200, {"ok": True, "state": _login_state["state"]})
                coro = runtime_login_password(body["user"], body["pwd"])
                asyncio.run_coroutine_threadsafe(coro, _LOOP)
                return self._send(200, {"ok": True, "state": "started"})
            else:
                return self._send(400, {"ok": False,
                                        "error": "缺少 passToken 或 user/pwd"})
            fut = asyncio.run_coroutine_threadsafe(coro, _LOOP)
            try:
                result = fut.result(timeout=90)
            except Exception as e:  # noqa: BLE001
                self._send(200, {"ok": False, "error": str(e)[:300]})
            else:
                self._send(200, {"ok": True, **(result or {})})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args) -> None:  # 静默访问日志，只留业务日志
        pass


def start_panel() -> None:
    """在后台线程启动管理面板 HTTP 服务。"""
    try:
        srv = ThreadingHTTPServer((PANEL_HOST, PANEL_PORT), _PanelHandler)
    except OSError as e:
        log.warning("面板启动失败(%s)，可能已被占用；若已有一个面板在跑可忽略", e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="panel").start()
    log.info("管理面板已启动: http://%s:%d", PANEL_HOST, PANEL_PORT)


async def main_async() -> None:
    global _LOOP
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

    try:
        account, cookie, hardware, device_id, name = await build_session(
            args.password, interactive=not args.run)
        _session.update({"account": account, "cookie": cookie, "hardware": hardware,
                         "id": device_id, "name": name})
    except _NoLogin:
        log.warning("没有已保存的登录令牌——监听暂不启动，"
                    "请打开管理面板「账号」页登录")
    _LOOP = asyncio.get_running_loop()
    start_panel()

    if args.devices:
        if not _session["account"]:
            raise SystemExit("未登录，无法列出设备；先执行 --login")
        from miservice import MiNAService
        for d in (await MiNAService(_session["account"]).device_list()) or []:
            log.info("音箱: name=%s hardware=%s deviceID=%s", d.get("name"),
                     d.get("hardware"), d.get("deviceID"))
        return

    if args.probe:
        if not _session["cookie"]:
            raise SystemExit("未登录，probe 不可用；先执行 --login 或面板登录")
        await poll_loop(_session["hardware"], _session["cookie"],
                        probe_seconds=args.probe)
        return

    _start_poll_task()      # 未登录时 _start_poll_task 内部 no-op 由登录触发
    await asyncio.Event().wait()   # 常驻；面板可随时退出/重登录并重启监听


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
