# 怎么找一个可用的信号位

要加一条新的语音指令，你需要找到一个**同时满足三个条件**的属性：

1. **可读** —— HA 能通过小米云 `prop/get` 拉到值
2. **可写** —— 米家场景 / 小爱训练能改它
3. **只影响自己** —— 改了不会影响设备的正常功能

---

## 第一步：拿到设备 DID

DID 是小米云的设备唯一 ID，不是 HA 的实体 ID，也不是设备序列号。

**方法 A：从 HA 存储里读**（推荐）

```
<HA配置目录>/.storage/xiaomi_home/miot_devices/<家庭ID>_cn.dict
```

这是一个 Python dict 字面量，里面每个设备都有 `did` / `name` / `model` / `urn`。

> 注意：这个文件里还包含设备的 `token`，**不要直接分享或提交**，只抄 `did` 和 `model`。

**方法 B：从实体 ID 反推**

xiaomi_home 生成的实体 ID 里带着 DID，例如：

```
switch.chuangmi_cn_1111111111_026c02_motion_detection_p_5_1
                     ^^^^^^^^^ 这一段就是 DID
```

注意是 `did` 中间那串数字，前后是厂商前缀和设备型号。

---

## 第二步：探测 siid / piid

小米的 MIoT 协议用 `siid`（服务） + `piid`（属性）定位一个值。

用组件的调试服务一个个试：

```bash
python tools/ha_cli.py call miot_signal read '{"did":"YOUR_DID","siid":2,"piid":1}'
```

返回值不是 `null` 的就是存在的属性。**`null` 说明这个编号没有值，或者读不到。**

常见的几组（因设备而异，仅供参考）：

| 设备 | siid:piid | 含义 |
|---|---|---|
| 小米摄像机 | 2:1 | 开关 / 休眠（`false` = 休眠） |
| 小米摄像机 | 5:1 | 移动侦测 |
| 小米摄像机 | 2:8 | 移动追踪 |
| 小米摄像机 | 2:9 | 时间水印 |
| 小爱音箱 | 4:1 | 麦克风静音（**能写但不生效，别用**） |
| 米家插座 | 2:1 | 开关 |

批量扫一遍的写法：

```bash
for siid in 2 3 4 5; do
  for piid in 1 2 3 4 5 6 7 8 9; do
    echo -n "$siid:$piid = "
    python tools/ha_cli.py call miot_signal read \
      "{\"did\":\"YOUR_DID\",\"siid\":$siid,\"piid\":$piid}"
  done
done
```

---

## 第三步：确认可写

HA 里 xiaomi_home 会为每个可写布尔属性生成一个 `switch.` 实体。找到它，手动切一下，
再读回属性值 —— **读回的值必须真的变了**，才算可写。

```bash
# 切一下
python tools/ha_cli.py call switch turn_on '{"entity_id":"switch.xxx_p_2_1"}'
# 等 4 秒再读回
python tools/ha_cli.py call miot_signal read '{"did":"YOUR_DID","siid":2,"piid":1}'
```

很多属性会出现「开关实体状态变了、但云端属性没变」的假象（音箱静音就是这样）。
**只信读回值。**

---

## 第四步：确认米家那边能控制它

这是最容易翻车的一步：属性在 HA 里可读可写，不代表**米家场景**能选到它。

去米家 App → 智能 → 新建场景 → 执行动作 → 选该设备，看功能列表里有没有这一项。

- 有 → 可以用了
- 没有 → 换一个属性，或者换一个设备

---

## 第五步：写进配置

```yaml
- name: my_signal
  did: "YOUR_DID"
  siid: 2
  piid: 8
  trigger: false            # 或者 true，或者 any
  script: my_script
  delay: 2
  restore_entity: switch.xxx_p_2_8
  restore_value: "on"
```

然后调用服务 `miot_signal.reload`，看日志有没有打印新的绑定：

```
miot_signal 配置: ['my_signal=YOUR_DID/2/8 triggers False -> my_script']
```

---

## 选信号位的一条经验

**优先选「摆设类」属性**：时间水印、移动追踪、宽动态、指示灯。

这类开关改了不影响核心功能，就算忘了恢复也无所谓。
反例：供电、录制模式、报警开关 —— 改错了设备就直接不工作了。
