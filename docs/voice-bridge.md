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

## 添加口令：改 JSON 即可，自动热更新

`voice_bridge.json`（与脚本同目录，首次运行自动生成）：

```json
{
  "commands": [
    {"keywords": ["打扫猫砂盆", "清理猫砂盆", "铲屎"], "script": "cat_litter_clean"},
    {"keywords": ["喂猫", "加餐"], "script": "cat_feed"}
  ]
}
```

- 命中规则：识别文本**包含**任一关键词即触发
- 保存文件后约 2 秒内自动加载，无需重启（日志会提示「口令配置已热更新」）
- 写错格式不会崩：沿用上一次的合法配置并在日志里提醒
- `script` 对应 HA 里已有的脚本（`scripts.yaml` 或 UI 里建的）

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
