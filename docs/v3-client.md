# v3 客户端（桌面版）

把整套语音桥做成「下载 → 安装 → 托盘常驻」的桌面客户端：
用户**不需要装 Python、不需要单独启动 Home Assistant、不碰命令行**。

## 架构

```
安装目录/
├─ runtime/              内嵌 Python 3.12.8 + 全部依赖（HA 2025.1.4 / miservice / pystray…）
├─ app/                  voice_bridge.py（桥 + 面板） + tray.pyw（托盘） + icon.ico
├─ config/homeassistant/ HA 配置（首启自动补全；用户的令牌、口令、集成都在这附近）
└─ 客户端.vbs / 使用说明.txt
```

- **托盘（tray.pyw）**：橙色猫爪图标。菜单：打开面板 / 启动 HA / 查看日志 /
  开机自启开关 / 退出。只做「壳」，通过面板 HTTP API 与 voice_bridge 交互，
  并带 10 秒周期的桥接进程看门狗（与 HA 看门狗同一思路）。
- **单实例**：托盘用命名互斥量，面板用端口独占（`allow_reuse_address=False`），
  双保险防止双实例。
- **HA 生命周期**：由 voice_bridge 的看门狗托管（掉线 10 秒拉回），
  启动命令自动识别客户端布局（`runtime/python.exe -m homeassistant`），
  开发机布局（hass.exe）仍然兼容。
- **退出托盘**：只停桥接，HA 独立进程继续服务口令。

## 构建

```bash
# 1. 组装便携目录（可重复执行；runtime 已装好则跳过）
python client/build_runtime.py            # → build/XiaoAiPetControl/
python client/build_runtime.py --zip      # 额外产出单文件 zip（免安装版）

# 2. 打成安装包（需要 Inno Setup 6 的 ISCC.exe）
ISCC.exe client/installer.iss             # → build/Output/小爱宠物语音控制-Setup-3.0.0.exe
```

### 依赖钉定说明

- `client/ha_pins.txt`：从 HA 2025.1.4 的 METADATA 提取的官方钉死依赖清单
  （按 win/cp312 环境标记过滤）。HA 依赖树太大，pip 直接解析会
  `resolution-too-deep`，所以构建脚本分四步走：
  1. `fnvhash==0.1.0`（只有 sdist，单独放行）
  2. `pip install -r ha_pins.txt --no-deps`（零回溯装齐钉死包）
  3. HA / miservice / pystray / six（`--no-deps`）
  4. `client/repair_deps.py` 修复遍：元数据级扫描缺失的传递依赖
     （如 python-slugify → text-unidecode），逐个精确补装到闭环
- 升级 HA 版本时需要重新生成 `ha_pins.txt`（改脚本里两处版本号即可）。

## 面板端口

默认 8124。端口被占时可用环境变量改：`VB_PANEL_PORT=9124`。

## 用户数据位置

| 数据 | 位置 | 卸载是否保留 |
|---|---|---|
| 小米登录令牌 | `app/.mi.token` | 保留（含敏感信息，手动删除） |
| 口令配置 | `app/voice_bridge.json` | 保留 |
| HA 配置与集成 | `config/homeassistant/` | 保留 |
| 日志 | `app/voice_bridge.log` | 保留 |

安装器卸载**不删用户数据**；完全清除请手动删除安装目录。

## 已验证

- 内嵌 runtime：HA 2025.1.4 / miservice / pystray / PIL / six 全部导入成功
- 便携目录启动 `voice_bridge.py --run`：面板正常监听，未登录时正确提示去面板登录
- 托盘模块加载、图标构建、路径解析（桥接脚本 / 运行时定位）通过
- 托盘 GUI 与安装器的实机验证：双击 `客户端.vbs` 实测（需在真机桌面环境）
