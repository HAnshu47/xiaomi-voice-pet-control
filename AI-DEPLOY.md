# AI 部署手册

> 这一份文档是写给 **AI 助手**看的。人把它交给 AI，AI 就能自己一步步部署完；
> 不懂技术的使用者只需要在 4 个必须人工的环节点几下。
>
> 如果你就是那位使用者，直接跳到最后的 [第 7 节：给我怎么用](#7-使用者操作卡不懂技术就看这里)。

---

## 0. 怎么用这份文档

**方式 A（推荐）**：新建一个对话，把下面这段完整复制给 AI，然后把本文件也发过去。

```
你是一名 Windows 部署助手。请严格按照我提供的 AI-DEPLOY.md 执行部署，
要求：
1. 按 S0 → S8 顺序推进，每个阶段结束时用文档里写的「成功判据」自检，
   不通过就不要进入下一阶段，先按「失败处理」排查。
2. 遇到文档标注「人工」的步骤，停下来，用一句中文告诉我具体点什么、
   填什么，等我回复「好了」再继续。不要一次抛给我五个问题。
3. 全程不要让我重启电脑。需要重启 Home Assistant 时，只重启 HA 服务。
4. 动手前先跑 S0 预检，把结果告诉我，再决定走哪条分支。
5. 每完成一个阶段，用 3 行以内汇报：做了什么 / 验证结果 / 下一步。
6. 不要修改与目标无关的系统设置，不要装 Docker、VirtualBox、WSL。
```

**方式 B**：直接把本文件拖给 AI，说「按这个部署」。AI 一般能自己读懂结构。

**方式 C**：你自己照着第 3 节敲命令。命令都是可复制的，人也能跑。

---

## 1. 目标与环境事实

### 要达成的效果

对着小爱音箱说一句话，小佩（PetKit）猫砂盆 / 喂食器执行动作。

```
你说「清理猫砂盆」
   ↓  小爱音箱听到 → 米家场景把「摄像头·休眠」改成关
   ↓  小米云属性变化
   ↓  Home Assistant 轮询发现 false → 触发脚本
   ↓  调 PetKit 云 → 猫砂盆开始清扫
   ↓  2 秒后自动把摄像头唤醒（保证下次还能再触发）
```

### 为什么必须这样绕

| 直接做法 | 为什么不行 |
|---|---|
| 小爱直接控制小佩 | 小佩不在米家生态里，小爱根本看不到它 |
| 自定义技能 / 开放平台 | 需要企业资质、备案域名、审核，个人办不下来 |
| 抓音箱的语音文本 | 小米云只推属性（音量、静音、播放状态），不推"你说了什么" |
| webhook / 自动化触发器 | 部分 Windows 环境下 HA 自动化触发器整体失效（本项目实测踩到，见 S8 备注） |

**唯一稳定且免费的入口：某个米家设备的属性值发生变化。** 所以整套方案的核心是「借一个开关当信号位」。

### 本机实测环境

| 项 | 值 |
|---|---|
| 系统 | Windows 11（家庭版可） |
| Python | 3.12.10（装在 `C:\Python312`） |
| Home Assistant | Core 2025.1.4，**原生跑在 Windows**，无 Docker / 无虚拟机 |
| aiohttp | 3.11.11（需打 DNS 补丁） |
| 集成 | HACS 2.0.5、PetKit 0.1.14、xiaomi_home 0.5.0 |
| 配置目录 | `D:\homeassistant` |
| 端口 | 8123 |

### 硬约束（违反必踩坑）

1. **不要装 Docker / VirtualBox / WSL** 来跑 HA。企业管控机器上这三个基本都装不上，本项目前期全部失败，最后靠原生 Python 才通。
2. **不要用 `default_config`**。它会强制拉起 `bluetooth` / `dhcp` / `ssdp` / `usb`，Windows 上缺 DLL，启动即失败。用 `examples/configuration.yaml` 那种显式列表。
3. **必须加 `--ignore-os-check`**，HA 官方不支持 Windows。
4. **必须打两个补丁**（S2），否则 HACS 装不上、小米云属性读不到。
5. **信号位不能用「麦克风静音」**。实测能写但读回不生效（同一台音箱，昨天能今天不能）。
6. **接电灯的智能插座不能当信号位**。开灯 = 触发动作，会误动作。
7. **`.storage/` 目录里有账号 refresh_token、设备 token，绝对不能提交或外发。**
8. **不要用 API 触发 `homeassistant.restart`**。本机实测重启后进程不会自恢复，必须人工启动。改用停止进程 + 启动脚本。

---

## 2. 决策树（先探测，别急着装）

```
S0 预检
 ├─ http://127.0.0.1:8123 返回 200？
 │    └─ 是 → HA 已在跑，跳到 S4
 ├─ Python 3.11+ 存在？
 │    ├─ 否 → 装 Python（S1 第一步），继续
 │    └─ 是 → S1
 └─ 已有 HA 配置目录但起不来？
      └─ 看 <配置目录>/home-assistant.log 尾部，按第 6 节故障表处理
```

---

## 3. 分阶段执行

> 约定：`<PY>` 指 Python 可执行文件（如 `C:\Python312\python.exe`），
> `<CFG>` 指 HA 配置目录（如 `D:\homeassistant`）。

### S0 · 预检

```bash
# 1) HA 是否已在跑
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8123/

# 2) Python 版本
<PY> --version

# 3) 是否已装 homeassistant
<PY> -c "import homeassistant.const as c; print(c.__version__)"
```

**成功判据**：三条命令都有输出；能明确回答"HA 在不在跑、Python 什么版本、HA 装没装"。

**失败处理**：

- `curl` 返回 `000` → HA 没跑，继续 S1
- `python: command not found` → 装 Python 3.12（官网安装包，勾选 Add to PATH）
- `ModuleNotFoundError: homeassistant` → 继续 S1

---

### S1 · 安装 Home Assistant Core

```bash
<PY> -m pip install --upgrade pip
<PY> -m pip install "homeassistant==2025.1.4"
```

> 用固定版本，别用 `latest`。HA 小版本升级经常引入 Windows 不兼容项，出问题很难定位。
> 装完 `pip install "pyjwt"`，后面 CLI 工具要用。

创建配置目录，放入 `examples/configuration.yaml` 为 `<CFG>/configuration.yaml`，再建三个空的被 include 文件：

```bash
mkdir -p <CFG>
cp examples/configuration.yaml <CFG>/configuration.yaml
cp examples/scripts.yaml       <CFG>/scripts.yaml
touch <CFG>/automations.yaml <CFG>/scenes.yaml
mkdir -p <CFG>/themes
```

**成功判据**：`<PY> -c "import homeassistant.const as c; print(c.__version__)"` 打印 `2025.1.4`。

**失败处理**：

- pip 下载慢 → 加国内源 `-i https://pypi.tuna.tsinghua.edu.cn/simple`
- 编译类报错 → 确认装的是 3.12 而不是 3.13（3.13 部分依赖还没有轮子）

---

### S2 · 打两个 Windows 补丁

这两个补丁是本项目在 Windows 上能跑起来的**前提**，不打则在 S4、S5 必挂。

#### 补丁 ①：aiohttp 强制用线程池 DNS

症状：HACS 装集成时只报 `Unknown error`，日志里有
`ImportError: DLL load failed while importing _cares`。

```bash
<PY> - <<'EOF'
import io, os, aiohttp
p = os.path.join(os.path.dirname(aiohttp.__file__), "resolver.py")
s = io.open(p, encoding="utf-8").read()
marker = "# [miot-signal] force threaded resolver"
if marker in s:
    print("already patched:", p)
else:
    # 追加在文件末尾，模块级最后一次赋值生效，不受原文件 if/else 结构影响
    s += f"\n\n{marker}\nDefaultResolver = ThreadedResolver\n"
    io.open(p, "w", encoding="utf-8").write(s)
    print("patched:", p)
EOF
```

> 为什么用「追加」而不是「替换某一行」：aiohttp 各版本的 `DefaultResolver`
> 赋值位置不同（有的在 if/else 里），按行替换可能匹配不到却静默成功。
> 追加到最后一行，Python 模块加载时后赋值覆盖先赋值，任何版本都成立。

#### 补丁 ②：给 Proactor 事件循环补 add_reader

症状：小米集成能写不能读，日志反复出现 `mips sub when not connected`。
原因：Windows 默认事件循环是 `ProactorEventLoop`，它不实现 `add_reader`，
而小米的 MQTT(MIPS) 依赖它。

```bash
cp examples/patches/sitecustomize.py "<PY 所在目录>/Lib/site-packages/sitecustomize.py"
```

**成功判据**：

```bash
<PY> -c "import aiohttp.resolver as r; print(r.DefaultResolver)"
<PY> -c "import asyncio; print(hasattr(asyncio.ProactorEventLoop,'add_reader'))"
```

第一条必须打印 `<class 'aiohttp.resolver.ThreadedResolver'>`，
第二条必须打印 `True`。**任何一条不对都不要往下走。**

---

### S3 · 首次启动 + 建 owner 账号（含人工）

```bash
cd <CFG>
<PY> -m homeassistant --config "<CFG>" --ignore-os-check
```

**人工环节 ①（必须）**：等 40～90 秒，让使用者打开 `http://127.0.0.1:8123`，
注册一个 owner 账号（用户名 / 密码自己定）。注册完告诉 AI「好了」。

**成功判据**：`curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8123/` 返回 `200`，
且 `<CFG>/.storage/auth` 文件存在。

**失败处理**：

- 端口没起来 → `tail -30 <CFG>/home-assistant.log`
- 报 `default_config` 相关错误 → 说明用了 default_config，换成 examples 里的配置
- 报 `No module named 'xxx'` → 缺依赖，`<PY> -m pip install xxx`

**启动脚本**（长期用这个，别每次敲命令）：把 `examples/start_ha.bat` 里的
Python 路径和配置路径改成自己的，以后双击即可。

> ⚠️ AI 注意：从这里开始，**不要用 `homeassistant.restart` 服务重启**。
> 正确做法是 `taskkill /IM hass.exe /F`（或结束对应 python 进程），再执行启动脚本。
> 而且这一步要**交给使用者双击**，AI 自己拉起的进程在会话结束后可能被回收。

---

### S4 · 装三个集成（含人工）

**① HACS**（装第三方集成的市场）

```bash
# 下载 HACS 到 custom_components，重启 HA
mkdir -p <CFG>/custom_components
# 从 github.com/hacs/integration 的 release 包解压到 <CFG>/custom_components/hacs
```

重启后在 HA 界面「设置 → 设备与服务 → 添加集成 → HACS」，
**人工环节 ②**：它会显示一个 8 位设备码，让使用者打开 `https://github.com/login/device` 输入并授权。

**② PetKit**（小佩设备）

HACS → 集成 → 搜 `PetKit` → 下载 → 重启 HA → 添加集成，填：
- 账号：**建议用小佩 App 的副账号**（主账号登录可能踢掉手机端）
- 密码
- 区域：`China`

**人工环节 ③**：验证码 / 二次确认由使用者完成。

**③ xiaomi_home**（小米官方，提供设备原始属性）

HACS → 搜 `Xiaomi Home` → 下载 → 重启 → 添加集成 → 小米账号 OAuth。

**已知坑**：OAuth 回调地址是 `homeassistant.local`，很多机器解析不到，报
`DNS_PROBE_FINISHED_NXDOMAIN`。解决办法：让使用者在浏览器**地址栏里把域名改成
`127.0.0.1`**，回车继续，就能完成授权，不用改 hosts。

**成功判据**：

```bash
<PY> tools/ha_cli.py states | grep -c "quan_zi\|pura\|manual_feed"
<PY> tools/ha_cli.py states | grep -c "xiaomi_cn_"
```

两条都 > 0，说明 PetKit 设备和小米设备都进 HA 了。

> `tools/ha_cli.py` 需要 `pip install pyjwt`，并设置 `HA_AUTH_FILE` 指向 `<CFG>/.storage/auth`。

---

### S5 · 探测信号位（关键步骤，决定能挂几条指令）

先装本项目组件：

```bash
cp -r custom_components/miot_signal <CFG>/custom_components/
# 在 configuration.yaml 里加一行：miot_signal:
```

重启 HA，然后用 `miot_signal.read` 逐个探测：

```bash
<PY> tools/ha_cli.py call miot_signal read '{"did":"<摄像头DID>","siid":2,"piid":1}'
```

批量扫一遍（推荐，一次看清哪些编号有值）：

```bash
<PY> - <<'EOF'
import ha_cli
def rd(did, siid, piid):
    try:
        r = ha_cli.api('/api/services/miot_signal/read?return_response','POST',
                       {'did':did,'siid':siid,'piid':piid})
        return r.get('service_response',{}).get('value')
    except Exception:
        return None
DID = "<改成你的设备DID>"
for siid in range(2, 9):
    row = [f"{siid}:{piid}={v}" for piid in range(1, 10)
           if (v := rd(DID, siid, piid)) is not None]
    print(" | ".join(row) if row else f"{siid}: -")
EOF
```

**怎么拿到 DID**：读 `<CFG>/.storage/xiaomi_home/miot_devices/<家庭ID>_cn.dict`，
或从实体 ID 里抠（`switch.chuangmi_cn_1111111111_026c02_...` 中间那串数字）。
**不要把这个文件外发**，里面有设备 token。

**合格信号位的三个条件**（缺一不可）：

1. 可读 —— 上面的命令返回的不是 `null`
2. 可写 —— 米家场景里能选到它
3. 无副作用 —— 改了不影响设备正常功能

**已验证可用的（因设备而异，仅供参考）**：

| 设备 | siid:piid | 含义 | 副作用 |
|---|---|---|---|
| 小米摄像机 | 2:1 | 开关 / 休眠（`false` = 休眠） | 休眠期间不录像 |
| 小米摄像机 | 5:1 | 移动侦测 | 关闭期间不推送报警 |
| 小米摄像机 | 2:8 | 移动追踪 | 几乎无感 |
| 小米摄像机 | 2:9 | 时间水印 | 影响画面角标（**米家场景里选不到**） |
| 米家插座 | 2:1 | 开关 | 接什么就影响什么，**建议空插** |
| 小爱音箱 | 4:1 | 麦克风静音 | ⚠️ 实测写不进去，别用 |

**成功判据**：至少找到 1 个「可读 + 米家场景可选 + 无副作用」的属性，并记录下它的
`did / siid / piid / 触发值`。

**失败处理**：

- 全是 `null` → 该 did 下这个 siid 范围没有属性，扩大扫描范围到 2..12
- 读得到但米家场景选不到 → 换下一个，别硬凑（时间水印就是这种情况）
- 能写但读回不变 → 该属性是"只写"的，换下一个（麦克风静音就是这种情况）

---

### S6 · 写配置（信号绑定 + 动作脚本）

把 `examples/miot_signal.yaml` 复制到 `<CFG>/miot_signal.yaml`，
把每条 `did`、`restore_entity` 换成自己的：

```yaml
interval: 2
signals:
  - name: clean
    did: "1111111111"          # 你的摄像头 DID
    siid: 2
    piid: 1                    # 2:1 休眠，false = 休眠
    trigger: false             # 变成 false 时触发
    script: cat_litter_clean   # 执行哪个脚本
    delay: 2                   # 2 秒后恢复
    restore_entity: switch.your_camera_on_p_2_1
    restore_value: "on"
```

把 `examples/scripts.yaml` 里的实体 ID 换成自己的，然后热加载：

```bash
<PY> tools/ha_cli.py call miot_signal reload
<PY> tools/ha_cli.py call script reload
```

**成功判据**：日志出现 `轮询启动`，且列出你配的所有信号名：

```bash
grep "轮询启动" <CFG>/home-assistant.log | tail -1
```

**失败处理**：

- `轮询启动` 里有名字缺失 → yaml 缩进错，`python -c "import yaml;yaml.safe_load(open(...))"` 验一遍
- 实体 ID 写错 → `<PY> tools/ha_cli.py states | grep 关键字` 查真实实体名
- 服务不存在 → 组件没加载，看日志里 `custom_components.miot_signal` 的报错

---

### S7 · 米家侧建场景（含人工）

**人工环节 ④**。AI 无法操作米家 App，只能给出明确的点法：

| 场景名 | 场景里的动作 | 对应 HA 动作 |
|---|---|---|
| 清理猫砂盆 | 选摄像头 → **休眠** | 清扫 → 2 秒后唤醒 |
| 添加猫粮 | 选摄像头 → **关闭移动侦测** | 出粮 10g → 2 秒后开回 |
| 洗猫砂盆（可选） | 选**空插的**插座 → 开或关 | 倒空猫砂 → 3 秒后复位 |

路径：米家 App → 智能 → 右上角「+」→ 新建场景 → 命名 → 添加执行动作 → 保存。

> 为什么用「场景」而不是「训练计划」：
> 训练计划的设备功能列表很短（摄像头只有休眠 / 移动侦测两项），插座根本不出现；
> 场景的设备列表更全，且场景能同步到小爱，说「打开 清理猫砂盆」即可。

**关键细节**：

- 动作必须让属性**真的发生变化**。平时移动侦测是开着的，场景里就要选「**关闭**」，
  如果选「开启」则状态没变，HA 收不到信号。
- 恢复由 HA 自动做，**不要**在场景里加延时或恢复动作。
- 「清理猫砂盆」用摄像头的「休眠」，会让摄像头停一会儿，但它 2 秒后自动唤醒。

**成功判据**：人工点一次场景（不用说话），然后：

```bash
grep "命中" <CFG>/home-assistant.log | tail -3
```

能看到 `miot_signal 命中【...】信号`。

---

### S8 · 验收

逐条走一遍，每步看两个东西：米家侧属性变了没、HA 侧日志有没有命中。

```bash
# 1) 组件与服务在不在
<PY> tools/ha_cli.py state script.cat_litter_clean
<PY> tools/ha_cli.py state sensor.xiaomi_cn_2037519512_l05c   # 任一小米实体即可

# 2) 信号是否在轮询
grep "轮询启动" <CFG>/home-assistant.log | tail -1

# 3) 手动触发一次，绕过语音，先确认执行链路通
<PY> tools/ha_cli.py call script turn_on '{"entity_id":"script.cat_litter_clean"}'

# 4) 再测语音（让使用者喊一句）
grep "命中" <CFG>/home-assistant.log | tail -3
```

**通过标准**：

- [ ] 手动执行脚本 → 设备真的动作（证明"手"是通的）
- [ ] 喊话 → 日志出现「命中」（证明"耳朵"是通的）
- [ ] 动作完成后，被借用的开关**自动恢复到原状**（证明能重复触发）
- [ ] 隔 30 秒再喊一次，仍然生效（证明不是一次性）

**已知例外**：本机实测 HA 的自动化触发器（`state` / `event` / `webhook` / `time_pattern`）
全部不触发，但 `automation.trigger` 手动执行 action 正常。
所以本项目**把执行逻辑直接写在组件里**，不依赖 automation。
如果验收时发现「日志有命中但设备没动」，先怀疑自动化链路，改成组件内直接调用。

---

## 4. 必须人工完成的 4 件事（AI 做不了，别浪费时间尝试）

| # | 环节 | 使用者要做什么 | 大约耗时 |
|---|---|---|---|
| ① | S3 | 浏览器打开 `127.0.0.1:8123`，注册 HA 账号 | 1 分钟 |
| ② | S4 | HACS 授权：打开 `github.com/login/device` 输入 8 位码 | 1 分钟 |
| ③ | S4 | PetKit 登录（副账号 + 密码）、小米 OAuth（域名改成 `127.0.0.1`） | 3 分钟 |
| ④ | S7 | 米家 App 里建 2～3 个场景 | 3 分钟 |

**另外两件**：

- **启动 HA**：双击 `start_ha.bat`。AI 拉起的进程在会话结束后可能被系统回收，交给使用者双击最稳。
- **喊话测试**：只有人真的开口，才能验证完整链路。

---

## 5. AI 行为边界（重要）

**允许**：读写配置文件、装 pip 包、调 HA REST API、读日志、改自己的组件代码。

**禁止**：

1. 让使用者重启电脑。整条链路最多只需要重启 HA 服务。
2. 用 `homeassistant.restart` 服务重启（本机不自恢复）。
3. 装 Docker / VirtualBox / WSL / 任何虚拟机方案。
4. 触碰 `/`、`C:\`、`AppData`、桌面等个人目录做批量操作。
5. 提交或外发 `.storage/`、`secrets.yaml`、`<家庭ID>_cn.dict`、日志里的 token。
6. 把接电灯的插座当信号位。
7. 用「麦克风静音」「时间水印」当信号位（前者写不进，后者米家选不到）。
8. 一次抛给使用者超过一个问题。

---

## 6. 故障对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| 8123 打不开，日志停在加载 | 用 `default_config`，缺 Windows DLL | 换 `examples/configuration.yaml` 的显式列表 |
| HACS 装集成报 `Unknown error` | aiodns / _cares DLL 加载失败 | 打补丁 ①（aiohttp → ThreadedResolver） |
| 日志反复 `mips sub when not connected` | Proactor 无 `add_reader` | 打补丁 ②（sitecustomize.py） |
| 小米授权报 `NXDOMAIN` | 回调域名 `homeassistant.local` 解析不到 | 浏览器地址栏改成 `127.0.0.1` |
| 喊了没反应，日志无命中 | 属性**没有真的变**（场景选了同值） | 场景里改成相反方向（如"关闭移动侦测"） |
| 喊了没反应，日志有命中 | 执行链路问题（自动化触发器） | 改成组件内直接调用脚本 |
| 只能触发一次，第二次没反应 | 没有恢复原状 | 检查 `restore_entity` / `delay` |
| 某个设备经常离线导致失效 | 设备掉线，云侧不下发 | 换常在线设备当信号位，或加空插插座 |
| 属性读得到但写不进去 | 该属性只读/只写 | 换下一个属性 |
| 重启后 HA 没起来 | API 重启不自恢复 | 人工双击启动脚本 |

---

## 7. 使用者操作卡（不懂技术就看这里）

### 装好之后，你每天只需要做一件事：说话

| 你说 | 会发生什么 |
|---|---|
| **清理猫砂盆** | 猫砂盆转一圈铲屎，约 1 分钟后停 |
| **添加猫粮** | 喂食器出 10g，约一小把 |

说之前先喊一声「小爱同学」唤醒，再说这句话，中间别停。

### 如果说了一次没反应

按顺序试，通常在第一步就解决：

1. **再说一遍**，慢一点，把整句说完
2. 打开米家 App 看看那个设备是不是**离线**（左上角显示"设备离线"就是这个原因）
3. 打开电脑桌面上的 `start_ha.bat` 看窗口在不在，不在就双击它，等 1 分钟再试
4. 还是不行，把 HA 配置目录下的 `home-assistant.log` 最后 30 行发给帮你部署的人

### 两个别碰的地方

- **别在米家 App 里手动点摄像头的「休眠」**。这等同于喊了"清理猫砂盆"，会让猫砂盆开始工作。
- **别删电脑上的 HA 配置目录**（就是 `configuration.yaml` 所在的那个文件夹）。
  里面是整套配置和账号登录状态，删了要全部重来。

### 洗猫砂盆（不常用）

需要先倒空猫砂。如果你没有配第三条语音指令，直接打开小佩 App 点两下最快，
不用为一年几次的操作折腾。
