# 让小爱同学控制小佩猫砂盆 / 喂食器（以及德业除湿机、任何 HA 设备）

> 对着小爱说一句「清理猫砂盆」，猫砂盆自己开始铲屎；说「添加猫粮」，喂食器出 10g；
> 说「打开除湿机」，德业除湿机开始工作。小佩、德业不在米家里，小爱本来碰不到它们
> —— 这个项目就是那座桥。

面向 **Windows + Home Assistant Core** 的完整实践记录，包含代码、配置、以及踩过的所有坑。

> **不想自己动手？** 把 [`AI-DEPLOY.md`](AI-DEPLOY.md) 直接发给任意 AI 助手，
> 它会按阶段自己部署、自己验证，你只在 4 个必须人工的环节点几下。

---

## 两代方案

| | **v2 语音桥 + Web 面板**（当前，推荐） | v1 摄像头信号位 |
|---|---|---|
| 原理 | 直接读取小米云的**小爱对话记录**，识别你说了什么 | 借摄像头/插座的开关属性当信号位 |
| 额外硬件 | **不需要**（不需要摄像头、不需要插座） | 每条口令占用一个信号位 |
| 添加口令 | Web 面板里**点三下**：选品牌 → 选设备 → 选指令 | 每条口令要在米家建一个场景 + 改 YAML |
| 配置方式 | 内置 Web 管理面板（登录、口令、集成安装全在面板里） | 手工编辑 YAML |
| 依赖训练计划/场景 | **不依赖** | 依赖（每条口令一个场景） |
| 代码 | [`voice_bridge.py`](docs/voice-bridge.md) | `custom_components/miot_signal/` |

v1 方案的原理仍然值得一看（文档在本文后半部分），但新部署请直接用 v2。

---

## v2 快速开始（Web 面板版）

详细文档：[`docs/voice-bridge.md`](docs/voice-bridge.md)

### 1. 装 Home Assistant（Windows）

```bat
pip install homeassistant==2025.1.4
```

需要两个 Windows 补丁（DNS 解析器 + Proactor 事件循环），见
[`examples/patches/`](examples/patches/)，一步步有说明。

### 2. 安装设备品牌集成

在 HA 的 HACS 里装你需要的品牌集成（PetKit / Xiaomi Home / 德业等）。
也可以之后在面板的「集成中心」里直接搜索安装，不用碰 HACS。

### 3. 启动语音桥

```bat
venv-voice\Scripts\python.exe voice_bridge.py --run
```

浏览器打开 `http://127.0.0.1:8124`，一切操作都在面板里完成：

- **账号页**：面板内直接登录小米账号（账密 + 短信验证码），不用再跳浏览器复制令牌
- **集成中心**：搜索 GitHub 上的 HA 集成、一键安装/卸载；HA 没运行时一键拉起（掉线自动救回）
- **添加口令**：三级联动选品牌 → 设备 → 指令，填上你对小爱说的话即可
- **口令列表 / 测试 / 日志**：管理、验证、排错都在这里

### 4. 对小爱说话

没有这一步了——**不需要**在米家建场景或训练计划。面板里配好口令后直接说：

```
「小爱同学，清理猫砂盆」
```

1.5 秒内识别、执行。改口令不用重启，面板里改完立即生效。

### 口令配置（可选手工编辑）

口令存在 `voice_bridge.json`（面板修改后自动写入），格式：

```json
{
  "commands": [
    {"keywords": ["清理猫砂盆", "铲屎"], "script": "cat_litter_clean"},
    {"keywords": ["打开除湿机"], "service": "humidifier/turn_on",
     "data": {"entity_id": "humidifier.xxx"}}
  ]
}
```

`script`（HA 脚本）和 `service`（直接调设备服务）二选一，同时填时脚本优先。

---

## 能做什么（实测）

| 你说 | 发生什么 |
|---|---|
| 清理猫砂盆 | 猫砂盆（小佩 Pura MAX）执行一次清扫 |
| 添加猫粮 | 喂食器（小佩 Solo）出粮，5g 一档 |
| 打开/关闭除湿机 | 德业除湿机开关、童锁、负离子、连续除湿、风速 |

被测设备：

- 猫砂盆：小佩 Pura MAX（PetKit 集成）
- 喂食器：小佩 Solo（PetKit 集成）
- 除湿机：德业（deye_dehumidifier 集成）
- 语音入口：小爱音箱 Play 增强版

原理上任何接入了 HA 的设备都能这样被小爱控制——品牌只是集成，口令只是服务调用。

---

## v2 的开销与限制

1. **资源占用**：轮询进程平均 ~2% 单核 CPU、45 MB 内存、每天约 200 MB 流量，可忽略。
2. **对话记录识别**：读取的是小米云上的对话记录（与你小爱 App 里看到的相同），
   口令需要说完后 1.5~3 秒内被识别执行。
3. **HA 掉线窗口**：HA 进程不在时喊的口令会丢弃（不重试）；
   看门狗会在 10 秒内自动拉起 HA，1~2 分钟后恢复正常。
4. **小米风控**：只用账号令牌读取对话记录，频率与官方 App 同级；账密登录偶发短信验证，
   面板内输入验证码即可。
5. **Windows 原生 HA 的坑**：探活不能发无令牌请求（会被 HA 封禁 IP）、
   HA 收到重启指令不会自己回来（面板已处理），详见
   [`docs/voice-bridge.md`](docs/voice-bridge.md)。

---

## v1 方案：摄像头信号位（历史，原理仍值得一看）

### 原理：小爱碰不到小佩，那怎么办

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

### 信号位是有配额的

一个设备能提供几个信号位，取决于它的属性里有多少个「可读 + 可写 + 米家能控制」的开关。

- 小米摄像机（本项目用的型号）：**2 个** —— 休眠、移动侦测
- 小爱音箱：理论上有静音、播放状态，但实测**属性写入不生效**，不可靠
- 米家插座：**1 个**，最稳，推荐专门买一个空插着不接负载

想加第 N 条语音指令，就得有第 N 个信号位。这是硬约束，绕不过去。

### v1 部署步骤（如需复现）

<details>
<summary>展开（点这里）</summary>

1. **装 HA Core**：`pip install homeassistant==2025.1.4`，启动加 `--ignore-os-check`。
2. **打两个 Windows 补丁**（DNS 解析器、Proactor 事件循环），见 [`examples/patches/`](examples/patches/)。
3. **装集成**：HACS 装 PetKit（建议用副账号登录，主账号会掉线）与 Xiaomi Home。
4. **放自定义组件**：复制 `custom_components/miot_signal/` 到 HA，`configuration.yaml` 加 `miot_signal:`。
5. **配信号绑定**：参照 [`examples/miot_signal.yaml`](examples/miot_signal.yaml)，
   `did/siid/piid` 的找法见 [docs/find-signal-slot.md](docs/find-signal-slot.md)。
6. **配动作脚本**：合并 [`examples/scripts.yaml`](examples/scripts.yaml) 到你的 `scripts.yaml`。
7. **米家建场景**：每条口令一个场景（「清理猫砂盆」→ 摄像头休眠），
   训练计划里「当我说 XX → 执行场景」。
8. **验证**：喊一句，HA 日志应出现 `miot_signal 命中【clean】信号 → cat_litter_clean`。

服务与事件：

| 服务 | 作用 |
|---|---|
| `miot_signal.reload` | 重新读取 `miot_signal.yaml`，改绑定不用重启 |
| `miot_signal.read` | 读单个属性，调试探测用 |

绑定字段：`did`（小米云设备 ID）、`siid`/`piid`（MIoT 服务号/属性号）、
`trigger`（触发值，`any` 表示只要变了）、`script`（要执行的脚本）、
`delay`（执行后等几秒恢复）、`restore_entity`/`restore_value`（恢复成什么）。

</details>

### v1 已知限制

1. 信号位有限（摄像机只给 2 个），加口令必须加设备
2. 摄像头掉线则全部失效
3. 不能拿接灯的插座当信号（开关灯会误触发）
4. HA 的自动化触发器在这台机器上不响应，动作逻辑全部写在自定义组件里（见 [docs/pitfalls.md](docs/pitfalls.md)）

---

## 环境

| 项 | 版本 / 说明 |
|---|---|
| 系统 | Windows 10 / 11 |
| Python | 3.12.x |
| Home Assistant | Core 2025.1.4（原生运行，非 Docker / 非 HAOS） |
| HACS | 2.x |
| PetKit 集成 | `robertd502/home-assistant-petkit` |
| 小米集成 | `xiaomi_home`（官方） |
| 德业集成 | `stackia/ha-deye-dehumidifier`（v2.1.8，兼容 Python 3.12） |
| 巴法云 / 云端中转 | **不需要**，本项目完全不用第三方服务器 |

> HA 官方不支持 Windows。本项目能跑，但依赖两个补丁。
> 如果你有树莓派 / NAS / Docker，**强烈建议直接上 Docker**，能省掉 80% 的麻烦。

---

## 目录

```
voice_bridge.py                  语音桥 + Web 管理面板（v2 核心，单文件）
custom_components/miot_signal/   v1 自定义组件（摄像头信号位）
examples/
  configuration.yaml             精简版 configuration.yaml
  scripts.yaml                   动作脚本模板
  miot_signal.yaml               v1 信号绑定模板
  voice_bridge.json              v2 口令配置模板
  start_ha.bat / start_voice.bat Windows 启动脚本
  patches/                       Windows 必需的补丁
tools/
  ha_cli.py                      免手工建令牌的 HA REST 命令行工具
docs/
  voice-bridge.md                v2 完整文档（推荐先读）
  architecture.md                v1 链路细节与设计取舍
  find-signal-slot.md            怎么找 did / siid / piid
  pitfalls.md                    踩坑记录
```

---

## 声明

- 本项目**不修改、不刷写、不破解**任何设备固件。
- 只使用各平台官方提供的集成与接口：Home Assistant、PetKit、小米 IoT。
- 所有账号、设备都需自备；仓库内不含任何真实设备 ID、账号或令牌
  （登录令牌保存在本机运行时文件中，已被 `.gitignore` 排除）。
- 涉及宠物喂食、猫砂盆机械动作，请自行确认安全性后再接入语音控制。

## License

MIT
