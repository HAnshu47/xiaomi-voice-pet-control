# 踩坑记录

按「会浪费你多少时间」排序。这些都是实际踩过、确认过的，不是猜测。

---

## 1. 这台机器上 HA 的自动化触发器全部不响应

**现象**：`automation` 里写 `state` / `event` / `webhook` / `time_pattern` 任何触发器都不触发；
但用 `automation.trigger` 手动执行 action 又是正常的。

**尝试过**：四种触发器全试过，重启、重载、改 YAML 模式、删掉重建，结果一样。

**结论**：原因未查明。可能是 Windows 原生运行 + 事件循环补丁的副作用。

**规避**：把动作逻辑直接写进自定义组件的回调里，完全不用 `automation`。
本项目的 `miot_signal` 就是这么做的 —— 属性一变就 `await hass.services.async_call(...)`。

> 换台机器（Docker / 树莓派）大概率没有这个问题，可以先测一下再决定要不要用组件这套。

---

## 2. 小爱音箱的「静音」属性：能写，但不生效

**现象**：`switch.xiaomi_..._mute_p_4_1` 调用 `turn_on` 不报错，但等 4 秒再读属性，值还是 `False`。
所以「开启静音」这个训练动作根本不会改变属性，信号永远读不到。

更早的时候这个属性是好用的，跑了几天之后突然失效。**同一台设备，同一种云端写入方式，结果不稳定。**

**结论**：音箱不要当信号源。哪怕现在测通了，也不代表明天还能用。

---

## 3. 小爱「训练计划」的功能列表只暴露一部分属性

**现象**：摄像头的设置页里有「时间水印」「移动追踪」「宽动态」，但在训练计划的
「设备控制 → 选择功能」里根本看不到，只有：

- 休眠 / 取消休眠
- 开启 / 关闭移动侦测
- 查看摄像头画面
- 停止摄像头视频画面

**结论**：训练计划只认「行为类」功能，不认「设置类」功能。
要控制设置类属性，改用**米家场景** —— 场景里能选到更多。

---

## 4. 拿接灯的插座当信号 = 灾难

插座当信号位的原理是「状态变了就触发」。如果这个插座接的是灯：

- 你每天开灯关灯，都会被判定成语音指令 → 猫砂被倒掉
- 更糟的是，如果场景设的是「关闭插座」，而灯本来就关着，状态没变，喊了反而没反应

**结论**：当信号位的插座必须**空插着，不接任何负载**。几十块钱，别省。

---

## 5. 通过 API 重启 HA，进程不会自己回来

**现象**：调用 `homeassistant.restart` 后，HA 完成 shutdown，然后就没有然后了 ——
进程消失，8123 端口不再监听，日志停在 `loader` 阶段。

**结论**：这台机器上必须手动双击启动脚本。

```
start_ha.bat
```

如果是从命令行 `start /min` 拉起来的，父进程一退出子进程也会被带走，
所以启动脚本要用 `start "" /min` 真正脱离控制台。

---

## 6. 自定义组件用 `hass.async_create_task` 会卡住启动

**现象**：启动日志里出现

```
Setup timed out for bootstrap waiting on {<Task pending name='...' coro=<poll_loop()>>
```

启动硬生生多等 4 分钟。

**原因**：`async_setup` 期间创建的 task 会被 bootstrap 追踪并等待，而轮询循环是死循环，永远不结束。

**修复**：改用 `hass.async_create_background_task(coro, name)`，bootstrap 就不再等它。

```python
make = getattr(hass, "async_create_background_task", None)
task = make(coro, "miot_signal") if make else hass.async_create_task(coro)
```

---

## 7. 小佩主账号会掉线，用副账号

PetKit 集成用主账号登录，过一段时间 token 失效、设备全变 `unavailable`。
改用另一个小佩账号（把设备共享过去）登录 HA，稳定得多。

地区必须选 **China**，否则连不上。

---

## 8. 小米 OAuth 回调的域名解析不了

**现象**：添加 Xiaomi Home 集成时，回调地址 `homeassistant.local` 报
`DNS_PROBE_FINISHED_NXDOMAIN`，授权页面打不开。

**规避**：浏览器地址栏里把 `homeassistant.local` 手动改成 `127.0.0.1`，回车即可完成授权。

（改 hosts 文件也行，但需要管理员权限，企业电脑常常改不了。）

---

## 9. 不要用 `default_config:`

它会强制加载 `bluetooth` / `dhcp` / `ssdp` / `usb` / `assist_pipeline`，
这些在 Windows 上需要 C 编译器或系统 DLL，会让启动直接失败或卡住。

用显式列表，见 `examples/configuration.yaml`。

---

## 10. 设备「休眠」时属性依然可读

值得记一下：摄像头被休眠（`2:1 = False`）之后，其他属性（移动侦测、时间水印）**仍然能读**。
所以休眠可以安全地当信号位，读完之后再把摄像头唤醒即可。

但**离线**的设备读不到 —— 掉线就是真的没数据，`prop/get` 会失败或返回旧值。
