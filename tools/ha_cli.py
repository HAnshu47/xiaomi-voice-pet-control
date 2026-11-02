"""极简 HA REST API 命令行工具（免手工创建长期令牌）。

原理：直接读 HA 的 .storage/auth，用 owner 的 refresh_token 现场签一个
30 分钟的 access_token，然后调 REST API。

用法：
    python ha_cli.py states                 # 列出所有实体
    python ha_cli.py states light           # 按前缀过滤
    python ha_cli.py state <entity_id>
    python ha_cli.py call <domain> <service> '<json>'
    python ha_cli.py token                  # 只打印 token

依赖：pip install pyjwt
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

import jwt

# 如果没设置环境变量，就按默认位置找
AUTH = os.environ.get("HA_AUTH_FILE") or os.path.join(
    os.path.expanduser("~"), ".homeassistant", ".storage", "auth"
)
BASE = os.environ.get("HA_BASE_URL", "http://127.0.0.1:8123")


def make_token() -> str:
    data = json.load(open(AUTH, encoding="utf-8"))["data"]
    tokens = data["refresh_tokens"]
    if isinstance(tokens, dict):
        tokens = list(tokens.values())
    owner_ids = {u["id"] for u in data["users"] if u.get("is_owner")}
    rt = next(t for t in tokens if t["user_id"] in owner_ids)
    now = int(time.time())
    payload = {
        "iss": rt["id"],
        "iat": now,
        "exp": now + 1800,
        "sub": rt["user_id"],
        "type": "access_token",
    }
    return jwt.encode(payload, rt["jwt_key"], algorithm="HS256")


def api(path: str, method: str = "GET", body: dict | None = None):
    req = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {make_token()}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "token":
        print(make_token())
    elif cmd == "states":
        prefix = sys.argv[2] if len(sys.argv) > 2 else ""
        for s in api("/api/states"):
            if s["entity_id"].startswith(prefix):
                print(s["entity_id"], "=", s["state"])
    elif cmd == "state":
        print(json.dumps(api(f"/api/states/{sys.argv[2]}"),
                         ensure_ascii=False, indent=2))
    elif cmd == "call":
        domain, service = sys.argv[2], sys.argv[3]
        body = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
        print(api(f"/api/services/{domain}/{service}", "POST", body))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
