"""miot_signal —— 用小米设备的属性变化当「语音信号」，触发 Home Assistant 动作。

背景
----
小爱训练计划只能做一件事：改变某个米家设备的某个属性（开/关）。
而小佩的猫砂盆、喂食器不在米家里，小爱无法直接控制。
所以链路是：  你说话 → 小爱训练改某个开关 → 本组件读到开关变化 → 执行脚本。

小米云几乎不推送小爱音箱的属性，本组件用 xiaomi_home 内部的云客户端
定时批量 pull（prop/get），值发生变化时在事件总线触发 miot_signal 事件，
并直接执行配置里绑定的脚本（不依赖 automation，本机自动化触发器不可用）。

配置
----
全部绑定写在 config 目录下的 miot_signal.yaml，改完调用服务 miot_signal.reload
即可生效，不需要重启 Home Assistant。

services:
  miot_signal.reload   重新读取 miot_signal.yaml
  miot_signal.read     读取单个属性（调试用）
  miot_signal.watch    手工启动轮询（一般不需要）
  miot_signal.stop     停止轮询
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.util.yaml import load_yaml

_LOGGER = logging.getLogger(__name__)

DOMAIN = "miot_signal"

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({}, extra=vol.ALLOW_EXTRA)},
    extra=vol.ALLOW_EXTRA,
)

READ_SCHEMA = vol.Schema(
    {
        vol.Required("did"): cv.string,
        vol.Required("siid"): vol.Coerce(int),
        vol.Required("piid"): vol.Coerce(int),
        vol.Optional("immediately", default=True): cv.boolean,
    }
)

WATCH_SCHEMA = vol.Schema(
    {
        vol.Optional("interval", default=2): vol.Coerce(float),
        vol.Optional("items"): vol.Any(None, list),
    }
)

STOP_SCHEMA = vol.Schema({})
RELOAD_SCHEMA = vol.Schema({})

CONFIG_FILE = "miot_signal.yaml"

# 配置文件缺失时的兜底：不观察任何属性（避免踩到别人的设备 DID）
DEFAULT_CONFIG: dict[str, Any] = {"interval": 2.0, "signals": []}


def _spawn(hass: HomeAssistant, coro) -> Any:
    """后台任务，避免阻塞 HA 的启动阶段。"""
    make = getattr(hass, "async_create_background_task", None)
    if make is not None:
        return make(coro, "miot_signal")
    return hass.async_create_task(coro)


def _match(value: Any, trigger: Any) -> bool:
    """比较云端返回值与配置里的触发值（兼容 bool / 0-1 / 字符串）。

    trigger 写成 any 表示「只要变了就触发」，用于开关两个方向都要生效的场景。
    """
    if isinstance(trigger, str) and trigger.lower() in ("any", "change"):
        return True
    if isinstance(trigger, bool):
        if isinstance(value, bool):
            return value is trigger
        try:
            return bool(int(value)) is trigger
        except (TypeError, ValueError):
            return False
    if isinstance(trigger, (int, float)):
        try:
            return float(value) == float(trigger)
        except (TypeError, ValueError):
            return False
    return str(value) == str(trigger)


async def _restore(hass: HomeAssistant, entity_id: str, value: str) -> None:
    """把信号开关恢复原状，保证下次还能再触发。"""
    if not entity_id:
        return
    domain = entity_id.split(".", 1)[0]
    on = str(value).lower() in ("on", "true", "1")
    try:
        if domain in ("switch", "light", "input_boolean", "fan"):
            await hass.services.async_call(
                domain, "turn_on" if on else "turn_off",
                {"entity_id": entity_id}, blocking=False)
        elif domain == "select":
            await hass.services.async_call(
                "select", "select_option",
                {"entity_id": entity_id, "option": value}, blocking=False)
        _LOGGER.info("miot_signal 已恢复 %s = %s", entity_id, value)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("miot_signal 恢复 %s 失败: %s", entity_id, err)


async def _run_action(
    hass: HomeAssistant, signal: dict[str, Any], old: Any = None
) -> None:
    """信号命中：执行脚本，等一会儿，再把信号开关恢复。"""
    name = signal.get("name")
    script = signal.get("script")
    _LOGGER.info("miot_signal 命中【%s】信号 → %s", name, script)
    if script:
        target = str(script)
        if not target.startswith("script."):
            target = f"script.{target}"
        try:
            await hass.services.async_call(
                "script", "turn_on", {"entity_id": target}, blocking=False)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("miot_signal 调用 %s 失败: %s", target, err)
            return
    delay = float(signal.get("delay") or 0)
    if delay:
        await asyncio.sleep(delay)
    restore_value = signal.get("restore_value", "on")
    if str(restore_value).lower() in ("old", "prev", "上次"):
        restore_value = old
    await _restore(
        hass,
        str(signal.get("restore_entity") or ""),
        str(restore_value),
    )


def _get_client(hass: HomeAssistant) -> Any:
    data = hass.data.get("xiaomi_home", {})
    clients = data.get("miot_clients", {})
    if not clients:
        return None
    return next(iter(clients.values()))


def _get_cloud(client: Any) -> Any:
    for attr in ("miot_cloud", "_http", "http"):
        obj = getattr(client, attr, None)
        if obj is not None and hasattr(obj, "get_prop_async"):
            return obj
    return None


async def _read_prop(
    client: Any, did: str, siid: int, piid: int,
    timeout: float = 15, immediately: bool = True,
) -> Any:
    cloud = _get_cloud(client)
    if cloud is None:
        raise RuntimeError("找不到可用的 MIoT 云客户端")
    return await asyncio.wait_for(
        cloud.get_prop_async(did, siid, piid, immediately=immediately),
        timeout=timeout,
    )


async def _read_batch(hass: HomeAssistant, items: list) -> dict[str, Any]:
    """一次 HTTP 请求批量读取多个属性。"""
    client = _get_client(hass)
    if client is None:
        raise RuntimeError("xiaomi_home 尚未就绪")
    cloud = _get_cloud(client)
    if cloud is None:
        raise RuntimeError("找不到可用的 MIoT 云客户端")
    params = [
        {"did": did, "siid": siid, "piid": piid} for did, siid, piid, _ in items
    ]
    results = await asyncio.wait_for(cloud.get_props_async(params), timeout=20)
    out: dict[str, Any] = {}
    for item in results or []:
        did = str(item.get("did"))
        siid = item.get("siid")
        piid = item.get("piid")
        if "value" not in item:
            continue
        for d, s, p, name in items:
            if str(d) == did and int(s) == int(siid) and int(p) == int(piid):
                out[name] = item["value"]
    return out


def _normalize(doc: dict[str, Any]) -> tuple[list, list[dict[str, Any]], float]:
    """把 yaml 配置整理成 (轮询项, 绑定列表, 间隔)。"""
    interval = float(doc.get("interval", 2))
    signals: list[dict[str, Any]] = []
    for raw in doc.get("signals") or []:
        if not raw or "did" not in raw:
            continue
        signals.append(
            {
                "name": str(raw.get("name") or f"sig{len(signals)}"),
                "did": str(raw["did"]),
                "siid": int(raw.get("siid", 2)),
                "piid": int(raw.get("piid", 1)),
                "trigger": raw.get("trigger", False),
                "script": raw.get("script"),
                "delay": float(raw.get("delay") or 2),
                "restore_entity": raw.get("restore_entity"),
                "restore_value": raw.get("restore_value", "on"),
            }
        )
    items = [(s["did"], s["siid"], s["piid"], s["name"]) for s in signals]
    return items, signals, interval


async def async_setup(hass: HomeAssistant, config) -> bool:
    state: dict[str, Any] = {
        "cache": {},
        "task": None,
        "signals": [],
        "interval": 2.0,
        "items": [],
    }
    hass.data.setdefault(DOMAIN, {})["state"] = state

    async def handle_read(call: ServiceCall) -> dict:
        client = _get_client(hass)
        if client is None:
            return {"error": "client_not_ready"}
        try:
            value = await _read_prop(
                client, call.data["did"], call.data["siid"], call.data["piid"],
                immediately=call.data.get("immediately", True))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "miot_signal read %s.%s.%s 失败: %s: %s",
                call.data["did"], call.data["siid"], call.data["piid"],
                type(err).__name__, err)
            return {"error": f"{type(err).__name__}: {err}"}
        return {"value": value}

    async def poll_loop(
        items: list, signals: list[dict[str, Any]], interval: float
    ) -> None:
        _LOGGER.info(
            "miot_signal 轮询启动: %s，间隔 %ss",
            [f"{n}({d}/{s}/{p})" for d, s, p, n in items], interval)
        while True:
            try:
                values = await _read_batch(hass, items)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "miot_signal 批量读取失败: %s: %s", type(err).__name__, err)
                await asyncio.sleep(interval)
                continue
            for name, value in values.items():
                old = state["cache"].get(name, "__init__")
                if value == old:
                    continue
                state["cache"][name] = value
                if old != "__init__":
                    _LOGGER.info("miot_signal %s: %r -> %r", name, old, value)
                hass.bus.async_fire(
                    "miot_signal",
                    {"name": name, "value": value, "old": old},
                )
                if old == "__init__":
                    continue
                for sig in signals:
                    if sig["name"] == name and _match(value, sig["trigger"]):
                        _spawn(hass, _run_action(hass, sig, old))
            await asyncio.sleep(interval)

    def start(items: list, signals: list[dict[str, Any]], interval: float) -> None:
        if state["task"] and not state["task"].done():
            state["task"].cancel()
        state["cache"].clear()
        state["items"] = items
        state["signals"] = signals
        state["interval"] = interval
        if not items:
            _LOGGER.warning("miot_signal 没有需要观察的属性，轮询未启动")
            state["task"] = None
            return
        state["task"] = _spawn(hass, poll_loop(items, signals, interval))

    def load_config() -> dict[str, Any]:
        path = hass.config.path(CONFIG_FILE)
        if not os.path.isfile(path):
            _LOGGER.info("miot_signal 未找到 %s，使用内置默认配置", path)
            return DEFAULT_CONFIG
        try:
            doc = load_yaml(path) or {}
            _LOGGER.info("miot_signal 已读取 %s", path)
            return doc
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("miot_signal 解析 %s 失败: %s，回退默认配置", path, err)
            return DEFAULT_CONFIG

    def _apply() -> None:
        doc = load_config()
        items, signals, interval = _normalize(doc)
        _LOGGER.info(
            "miot_signal 配置: %s",
            [
                f"{s['name']}={s['did']}/{s['siid']}/{s['piid']}"
                f" triggers {s['trigger']} -> {s['script']}"
                for s in signals
            ],
        )
        start(items, signals, interval)

    async def handle_watch(call: ServiceCall) -> None:
        raw = call.data.get("items")
        interval = float(call.data.get("interval") or 2)
        if raw:
            items = [(str(i[0]), int(i[1]), int(i[2]), str(i[3])) for i in raw]
            start(items, state["signals"], interval)
        else:
            _apply()

    async def handle_reload(call: ServiceCall) -> None:
        _apply()

    async def handle_stop(call: ServiceCall) -> None:
        if state["task"] and not state["task"].done():
            state["task"].cancel()
        state["task"] = None
        _LOGGER.info("miot_signal 轮询已停止")

    hass.services.async_register(
        DOMAIN, "read", handle_read, schema=READ_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "watch", handle_watch, schema=WATCH_SCHEMA)
    hass.services.async_register(DOMAIN, "stop", handle_stop, schema=STOP_SCHEMA)
    hass.services.async_register(DOMAIN, "reload", handle_reload, schema=RELOAD_SCHEMA)

    async def _autostart() -> None:
        # 等 xiaomi_home 登录小米云后再开始轮询
        for _ in range(30):
            if _get_client(hass) is not None:
                break
            await asyncio.sleep(3)
        _apply()

    _spawn(hass, _autostart())
    return True
