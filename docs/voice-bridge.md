# voice_bridge —— 免摄像头语音直连方案

> 不再借用摄像头开关当信号位。直接轮询小米云的小爱对话记录，
> 识别到口令就调用 Home Assistant 脚本。链路更短、无需占用设备开关、不依赖训练计划。

```
你说「清理猫砂盆」
      │
      ▼
小爱音箱正常应答（训练计划/播报不受影响）
      │
      ▼
小米云对话记录接口        ← 每 1.5 秒轮询一次，只拉最近 3 条
      │
      ▼
voice_bridge.py          ← 关键词匹配 + 冷却去重 + 指数退避
      │
      ▼
Home Assistant 脚本       ← script.cat_litter_clean 等
      │
      ▼
PetKit 集成 → 小佩设备执行
```

## 为什么之前接口拉不到数据（重要踩坑记录）

对话接口 `userprofile.mina.mi.com/device_profile/v2/conversation` 有一个非常隐蔽的要求：

**cookie 里的 `deviceId` 必须填音箱自己的 deviceID（设备列表返回的那串 uuid）。**

如果填登录时随机生成的设备 ID（miservice 默认行为），接口返回
`code 0` 但 `records` 永远是空的——不报错、就是没数据。本项目排查了半天，
最终对照 hass-xiaomi-miot 的源码才发现这个差异。

## 部署步骤

1. 安装依赖（独立 venv，不污染 HA 环境）：

   ```
   python -m venv venv-voice
   venv-voice\Scripts\pip install miservice pyjwt
   ```

2. 免密登录（推荐）：

   ```
   venv-voice\Scripts\python.exe voice_bridge.py --login -v
   ```

   按提示用浏览器登录 account.xiaomi.com，F12 → Application → Cookies
   复制 `passToken` 和 `userId` 粘贴进来。成功后令牌落盘 `.mi.token`，
   之后启动全程免密。

   备选：`--password` 走账号密码登录（会触发短信验证码，
   频繁尝试会遇到 `10012 非法请求` 风控，等 1~2 小时再试）。

3. 验证音箱和口令：

   ```
   venv-voice\Scripts\python.exe voice_bridge.py --devices   # 列出音箱
   venv-voice\Scripts\python.exe voice_bridge.py --probe 60  # 听 60 秒，只打印不执行
   ```

4. 正式运行：

   ```
   venv-voice\Scripts\python.exe voice_bridge.py --run
   ```

## 添加口令：改 JSON 或用管理面板

### 方式一：Web 管理面板（推荐，MVP）

启动后自动开启，浏览器访问 `http://127.0.0.1:8124`：

- **口令列表**：查看/删除已配置的口令
- **添加口令**：下拉选择 HA 设备功能（小佩的按钮、喂食选项、开关等，
  自动从 HA 读取真实实体和友好名），填入触发关键词，保存即生效
- **测试**：输入一句话模拟识别，立刻显示命中哪条口令，可手动执行验证
- **日志**：实时滚动显示识别与执行情况

面板直接把口令映射到设备服务（如 `button/press`），**不需要先在 HA 写脚本**；
传统 `script` 映射依然支持。

### 集成中心：面板内装驱动 + 登录品牌

面板第 6 个标签页，不用打开 HA 网页就能接入一个新品牌：

1. **搜索**：输入品牌/设备名（如 `petkit`、`govee`、`tuya`），后端搜 GitHub 上的
   Home Assistant 集成，按「仓库名命中 > 描述命中 > star」排序；也支持直接粘贴
   `owner/repo` 或 GitHub 链接安装
2. **安装**：自动下载 zip（GitHub 直连失败时切镜像）→ 解压时扫描
   `custom_components/<domain>/manifest.json` 并读取真实 domain 落盘 →
   调 HA API 重启 → 每 5 秒探测直到恢复
3. **配置 / 登录**：代理 HA 的 config flow，动态渲染表单（文本/密码/数字/开关/下拉），
   逐步骤提交；账密型集成全程不离开面板，OAuth 型会提示跳转授权一次
4. **卸载**：删除该集成下的 config entries → 删除 `custom_components/<domain>` →
   重启 HA。HA 内置集成、非法域名会被拒绝

面板还会探测 HA 是否在运行：没运行会在集成中心顶部显示提示卡，
点「启动 Home Assistant」即可后台拉起 `hass.exe`（独立进程，面板退出不影响）。

> **Windows 原生跑 HA 的两个坑**
>
> 1. 探活只连 TCP 端口，不再请求 `/api/`。无令牌请求会被 HA 记成一次「登录失败」，
>    日志刷满 `invalid authentication`，次数够多会把 `127.0.0.1` 封禁（403），
>    之后面板会一直显示「HA 未运行」，只能重启 HA 才恢复。
> 2. 没有服务管理器托管时，HA 收到「重启」指令会以退出码 100 直接结束进程、
>    **不会自己回来**。所以面板在安装/卸载集成后会一直盯着端口，一旦关闭就立刻重新拉起。
> 3. HA 以 `CREATE_BREAKAWAY_FROM_JOB` 脱离面板的进程树启动，关掉面板窗口不会连带杀掉 HA；
>    看门狗不区分 HA 由谁启动，检测到掉线就自动拉回（30 分钟内最多 6 次，防启动即崩溃的死循环），
>    面板刚启动时若发现 HA 没运行也会在 10 秒内自动拉起。

> **装完集成点「配置」没反应？通常是它的 Python 依赖装不上**
>
> 面板会把 HA 的原始报错显示出来，并顺带提示缺哪个依赖。常见两种：
>
> - **依赖要求的 Python 版本比 HA 高**。例：`deye_dehumidifier` 3.x 依赖 `libdeye==3.0.4`，
>   而后者要求 Python ≥ 3.14.2；HA 若跑在 3.12/3.13，这个集成根本加载不了。
>   改装有对应旧版本的集成（如 `v2.1.8` → `libdeye==2.1.6`）即可。
> - **`aiodns` + Windows 冲突**。Windows 上 HA 用的是 Proactor 事件循环，而 aiodns 要求
>   SelectorEventLoop，任何用到 HA 共享 aiohttp 会话的集成都会报
>   `aiodns needs a SelectorEventLoop on Windows`。
>   处理：`python -m pip uninstall aiodns`（aiohttp 会自动回落到线程 DNS 解析）。
>   注意升级 HA 时 pip 可能把 aiodns 又装回来，报错复发就再卸一次。

> 安装来源限制在 GitHub 公开仓库的 HA 集成，**不执行任意脚本**；
> 但仍建议优先选 star 多、更新活跃的仓库，第三方集成代码质量需自行判断。

### 方式二：直接改 JSON

`voice_bridge.json`（与脚本同目录，首次运行自动生成）：

```json
{
  "commands": [
    {"keywords": ["清理猫砂盆"], "script": "cat_litter_clean"},
    {"keywords": ["倒掉猫砂"], "service": "button/press",
     "data": {"entity_id": "button.xxx_dump_litter"}}
  ]
}
```

两种映射写法：

- `script`：调用 HA 脚本（`script.xxx`）
- `service` + `data`：直接调用任意 HA 服务（button/press、select/select_option、
  switch/turn_on……），无需建脚本

- 命中规则：识别文本**包含**任一关键词即触发
- 保存文件后约 2 秒内自动加载，无需重启（日志会提示「口令配置已热更新」）
- 写错格式不会崩：沿用上一次的合法配置并在日志里提醒

## 设计细节

| 机制 | 说明 |
|---|---|
| 去重 | 持久化 `requestId` 队列（最近 50 条）+ 时间戳水位，重启不重复触发 |
| 防连喊 | 同一脚本 6 秒冷却 |
| 容错 | 轮询失败指数退避 1s → 60s；HA 调用失败时保留时间戳，不丢指令 |
| HA 鉴权 | 从 `.storage/auth` 自签 access token，20 分钟自动续期 |
| 轮询频率 | 1.5 秒（xiaomusic 社区默认 1 秒，此频率长期使用无风控案例） |

## 与摄像头信号位方案（miot_signal）的关系

两条链路最终都汇聚到同一批 HA 脚本，**不能同时开**，否则一句口令触发两次。
建议：voice_bridge 验证通过后，关掉小爱训练计划里拨摄像头开关的动作，
miot_signal 组件可保留在 HA 里但不再配置。

## 文件清单

| 文件 | 用途 | 能否上传 git |
|---|---|---|
| `voice_bridge.py` | 主程序 | ✅ |
| `voice_bridge.json` | 个人口令配置 | ❌（模板见 examples/） |
| `.mi.token` | 小米登录令牌（含敏感凭据） | ❌ 绝不能 |
| `voice_bridge.state` | 去重状态 | ❌ |
| `voice_bridge.log` | 日志 | ❌ |
