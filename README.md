# 让小爱同学控制小佩猫砂盆 / 喂食器

> 对着小爱说一句「清理猫砂盆」，猫砂盆自己开始铲屎；说「添加猫粮」，喂食器出 10g。
> 小佩不在米家里，小爱本来碰不到它 —— 这个项目就是那座桥。

面向 **Windows + Home Assistant Core** 的完整实践记录，包含代码、配置、以及踩过的所有坑。

---

## 能做什么

| 你说 | 发生什么 |
|---|---|
| 清理猫砂盆 | 猫砂盆执行一次清扫 |
| 添加猫粮 | 喂食器出粮（份量可调，5g 一档） |
| 洗猫砂盆 | 猫砂全部倒进垃圾箱（可选，需要占用一个额外开关位） |

被测设备：

- 猫砂盆：小佩 Pura MAX（PetKit 集成）
- 喂食器：小佩 Solo（PetKit 集成）
- 语音入口：小爱音箱 Play 增强版
- 信号源：小米智能摄像机云台版 SE+

---

## 原理：小爱碰不到小佩，那怎么办

小爱能控制的只有**米家设备**。小佩的设备在 PetKit 自己的云上，米家里根本没有它，
所以「小爱 → 小佩」这条直连不存在，任何配置都变不出来。

能走通的只有一条间接路径：**借一个米家设备的开关，当信号位。**

```
你说「清理猫砂盆」
      │
      ▼
米家场景 / 小爱训练            ← 唯一能被语音触发的东西
      │  把摄像头「休眠」按下（true → false）
      ▼
小米云（设备属性）              ← 只改状态，不改设备
      │  每 2 秒被拉取一次
      ▼
Home Assistant + miot_signal   ← 发现属性变了
      │  调用 script.cat_litter_clean
      ▼
PetKit 集成 → 小佩云 → 猫砂盆开始铲屎
      │  2 秒后把摄像头唤醒，恢复原状
      ▼
下次还能再触发
```

关键点：**没有任何设备被修改或破解。** 摄像头的固件、功能、米家里的表现都没变，
只是它那两个本来就有的开关，被借用来传一个比特的信息。

### 为什么必须这么绕

- 小爱开放平台的自定义技能要企业资质 + 公网 HTTPS + 审核，个人拿不到
- 小米云只推送设备属性，**不推送你说了什么**，语音文本不离开音箱
- 逆向米家 App 接口只能替你「点按钮」，还是得有个按钮可点

所以「你说了哪句话」这件事，唯一能被程序读到的形式就是：**某个设备的某个属性变了。**

### 信号位是有配额的

一个设备能提供几个信号位，取决于它的属性里有多少个「可读 + 可写 + 米家能控制」的开关。

- 小米摄像机（本项目用的型号）：**2 个** —— 休眠、移动侦测
- 小爱音箱：理论上有静音、播放状态，但实测**属性写入不生效**，不可靠
- 米家插座：**1 个**，最稳，推荐专门买一个空插着不接负载

想加第 N 条语音指令，就得有第 N 个信号位。这是硬约束，绕不过去。

---

## 环境

| 项 | 版本 / 说明 |
|---|---|
| 系统 | Windows 10 / 11 |
| Python | 3.12.x |
| Home Assistant | Core 2025.1.4（原生运行，非 Docker / 非 HAOS） |
| HACS | 2.x |
| PetKit 集成 | `robertd502/home-assistant-petkit` |
| 小米集成 | `xiaomi_home`（官方，v0.5.0） |
| 巴法云 / 云端中转 | **不需要**，本项目完全不用第三方服务器 |

> HA 官方不支持 Windows。本项目能跑，但依赖两个补丁，见「部署步骤」第 2 步。
> 如果你有树莓派 / NAS / Docker，**强烈建议直接上 Docker**，能省掉 80% 的麻烦。

---

## 部署步骤

### 1. 装 Home Assistant Core

```bat
pip install homeassistant==2025.1.4
```

启动（`--ignore-os-check` 必须加，否则直接拒绝启动）：

```bat
C:\Python312\Scripts\hass.exe --config "D:\homeassistant" --ignore-os-check
```

配置目录建议放非系统盘，路径不要有空格和中文。

### 2. 打两个 Windows 补丁（关键，跳过会卡住）

**补丁 A：DNS 解析器**

aiohttp 在 Windows 上默认用 aiodns，某些环境 DLL 加载失败，导致 HACS 装不了东西。
编辑 `site-packages/aiohttp/resolver.py`，末尾改成：

```python
DefaultResolver: _DefaultType = ThreadedResolver
```

详细说明见 [`examples/patches/aiohttp-resolver.md`](examples/patches/aiohttp-resolver.md)。

**补丁 B：Proactor 事件循环**

Windows 默认事件循环不实现 `add_reader/add_writer`，而 xiaomi_home 的 MQTT 通道要用，
不打补丁的话云端属性推送收不到（能写不能读）。

把 [`examples/patches/sitecustomize.py`](examples/patches/sitecustomize.py) 复制到
`site-packages/sitecustomize.py`，Python 启动时会自动加载。

### 3. 装集成

- HACS → 自定义仓库 `https://github.com/robertd502/home-assistant-petkit` → 安装 PetKit
- HACS → 搜索 `Xiaomi Home`（官方集成）→ 安装

**PetKit 登录注意**：用小佩 App 注册一个**副账号**，把设备共享给它，用副账号登录 HA。
主账号登录会掉线，副账号稳定得多。地区选 China。

### 4. 放自定义组件

把本仓库的 `custom_components/miot_signal/` 整个目录复制到：

```
<HA配置目录>/custom_components/miot_signal/
```

然后在 `configuration.yaml` 里加一行：

```yaml
miot_signal:
```

> 不要用 `default_config:`，它在 Windows 上会强制加载 bluetooth / ssdp / usb 等集成，
> 启动容易失败。用 [`examples/configuration.yaml`](examples/configuration.yaml) 里那种显式列表。

### 5. 配信号绑定

把 [`examples/miot_signal.yaml`](examples/miot_signal.yaml) 复制到 HA 配置目录，
把里面的 `did` 和实体 ID 换成你自己的。

怎么拿到 `did / siid / piid`，见 [docs/find-signal-slot.md](docs/find-signal-slot.md)。

### 6. 配动作脚本

把 [`examples/scripts.yaml`](examples/scripts.yaml) 里的内容合并进你的 `scripts.yaml`。

脚本里的 `entity_id` 取决于你的设备在 HA 里叫什么名字，不一定和示例一样，
用「开发者工具 → 状态」搜索确认后再替换。

### 7. 小爱侧：建场景

**不要用「训练计划」直接控制设备**，它的功能列表只暴露一部分属性（比如摄像头的
时间水印、移动追踪就选不到）。用「**场景**」更可靠，因为场景里能选到设备的全部可控功能。

米家 App → 智能 → 右上「+」新建场景：

| 场景名 | 执行动作 |
|---|---|
| 清理猫砂盆 | 摄像头 → 休眠 |
| 添加猫粮 | 摄像头 → 关闭移动侦测 |

保存后直接对小爱说「打开 清理猫砂盆」，或说「执行 清理猫砂盆」。
如果小爱不认，就在训练计划里加一条「当我说 XX」→ 操作选「执行场景」→ 选该场景。

### 8. 验证

喊一句，然后看 HA 日志（配置里把 `custom_components.miot_signal` 设成 debug）：

```
miot_signal cam_power: True -> False
miot_signal 命中【clean】信号 → cat_litter_clean
miot_signal 已恢复 switch.xxx = on
```

三行都出现，链路就通了。

改完绑定不用重启，调用服务 `miot_signal.reload` 即可。

---

## 组件说明

### 服务

| 服务 | 作用 |
|---|---|
| `miot_signal.reload` | 重新读取 `miot_signal.yaml`，改绑定不用重启 |
| `miot_signal.read` | 读单个属性，调试探测用 |
| `miot_signal.watch` | 手工启动轮询（一般不用） |
| `miot_signal.stop` | 停止轮询 |

### 事件

属性变化时在事件总线触发 `miot_signal` 事件：

```yaml
{name: cam_power, value: false, old: true}
```

（本项目不用事件驱动，因为这台机器上 HA 的自动化触发器全部不响应，见
[docs/pitfalls.md](docs/pitfalls.md)。动作直接写在组件里。）

### 绑定字段

| 字段 | 说明 |
|---|---|
| `did` | 设备 DID（小米云设备 ID，不是实体 ID） |
| `siid` / `piid` | MIoT 服务号 / 属性号 |
| `trigger` | 属性变成这个值时执行；`any` 表示只要变了就执行 |
| `script` | 要执行的脚本名 |
| `delay` | 执行后等几秒恢复 |
| `restore_entity` / `restore_value` | 恢复哪个实体、恢复成什么；`old` 表示恢复变化前的值 |

---

## 已知限制（先看这里再动手）

1. **信号位有限**。摄像机只给 2 个，想加第三条必须再加一个设备（插座最稳）。
2. **摄像头掉线则全部失效**。所有信号都压在摄像头上，它离线时语音指令就哑了。
3. **不能拿接灯的插座当信号**。你开灯关灯都会触发，会误倒猫砂。
4. **小爱音箱的静音属性不可靠**。实测能写但读回不生效，别拿它当信号位。
5. **Windows 原生运行非官方支持**。升级 HA、升级 Python 都可能需要重新打补丁。
6. **HA 的自动化触发器在这台机器上不响应**。原因未查明，所以本项目的动作逻辑
   全部写在自定义组件里，不依赖 `automation`。换台机器可能就没这个问题。

---

## 目录

```
custom_components/miot_signal/   自定义组件（核心）
examples/
  configuration.yaml             精简版 configuration.yaml
  scripts.yaml                   被语音触发的动作脚本
  miot_signal.yaml               信号绑定模板
  start_ha.bat                   Windows 启动脚本
  patches/                       Windows 必需的补丁
tools/
  ha_cli.py                      免手工建令牌的 HA REST 命令行工具
docs/
  architecture.md                链路细节与设计取舍
  find-signal-slot.md            怎么找 did / siid / piid
  pitfalls.md                    踩坑记录（建议先读）
```

---

## 声明

- 本项目**不修改、不刷写、不破解**任何设备固件。
- 只使用各平台官方提供的集成与接口：Home Assistant、PetKit、小米 IoT。
- 所有账号、设备都需自备；仓库内不含任何真实设备 ID、账号或令牌。
- 涉及宠物喂食、猫砂盆机械动作，请自行确认安全性后再接入语音控制。

## License

MIT
