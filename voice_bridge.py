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
        with urlrequest.urlopen(req, timeout=15) as r:
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


def ha_get_states() -> list:
    """拉取 HA 实体列表（面板用），只保留可直接操作的域。"""
    from urllib import error as urlerror, request as urlrequest

    req = urlrequest.Request(
        f"{HA_BASE}/api/states",
        headers={"Authorization": f"Bearer {_ha_token_get()}"},
    )
    try:
        with urlrequest.urlopen(req, timeout=15) as r:
            states = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        log.warning("拉取 HA 实体失败: %s", e)
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

    # 设备/品牌归属：REST states 不带设备信息，用 /api/template 一次拿全
    # （device_id() -> 设备名、platform -> 所属集成，即"品牌"）
    tpl = (
        "{% set out = {} %}"
        "{% for s in states %}{% set did = device_id(s.entity_id) %}"
        "{% if did %}{% set dev = device_attr(did, 'name_by_user') or "
        "device_attr(did, 'name') or did %}"
        "{% set out = dict(out, **{s.entity_id: {'d': dev, 'b': "
        "device_attr(did, 'platform') or device_attr(did, 'manufacturer') or ''}}) %}"
        "{% endif %}{% endfor %}{{ out | to_json }}"
    )
    dmap = {}
    try:
        req2 = urlrequest.Request(
            f"{HA_BASE}/api/template", method="POST",
            data=json.dumps({"template": tpl}).encode(),
            headers={"Authorization": f"Bearer {_ha_token_get()}",
                     "Content-Type": "application/json"},
        )
        with urlrequest.urlopen(req2, timeout=20) as r:
            dmap = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        log.warning("拉取设备分组失败(改用扁平模式): %s", e)

    for item in out:
        info = dmap.get(item["entity_id"]) or {}
        if item["domain"] == "script":
            item["brand"] = "脚本"
            item["device"] = item["name"]
        else:
            item["brand"] = (str(info.get("b", "")).strip()
                             or "其他设备").title() if info else "其他设备"
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
  const selB = $('sel-brand');
  if (!PANEL_ENTITIES.length) {
    selB.innerHTML = '<option value="">HA 实体加载失败（HA 未运行？）</option>';
    return;
  }
  const brands = [...new Set(PANEL_ENTITIES.map(e => e.brand))];
  brands.sort((a, b) => a.localeCompare(b, 'zh'));
  selB.innerHTML = '<option value="">— 请选择 —</option>' +
    brands.map(b => `<option value="${b}">${b}（${PANEL_ENTITIES.filter(e => e.brand === b).length} 条指令）</option>`).join('');
  selB.disabled = false;
}

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
  const eid = $('sel-entity').value;
  const kws = $('in-kw').value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
  const show = (ok, t) => showMsg('msg-add', t, ok);
  if (!eid) return show(false, '请先选择设备功能');
  if (!kws.length) return show(false, '请填写至少一个关键词');
  const domain = eid.split('.')[0];
  const cmd = {keywords: kws};
  if (domain === 'button') { cmd.service = 'button/press'; cmd.data = {entity_id: eid}; }
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

function showMsg(id, text, ok) {
  const el = $(id);
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  el.textContent = text;
  setTimeout(() => { el.className = 'msg'; }, 4000);
}

(async function init() {
  const s = await jget('/api/status');
  $('status').textContent = `音箱：${s.speaker}（${s.hardware}）｜口令 ${s.commands} 条｜每 ${s.poll}s 轮询`;
  PANEL_ENTITIES = (await jget('/api/ha/entities')).entities || [];
  loadEntities();
  refreshCmds();
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
        elif self.path == "/api/status":
            self._send(200, {"speaker": _speaker_info["name"] or "未知",
                             "hardware": _speaker_info["hardware"] or "?",
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
    start_panel()

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
