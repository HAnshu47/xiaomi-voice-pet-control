# aiohttp 的 DNS 解析器补丁（Windows 必需）

## 现象

HACS 装集成时报 `Unknown error`，日志里是 aiodns / winloop 的 DLL 加载失败：

```
ImportError: DLL load failed while importing _cares: 找不到指定的模块。
```

## 原因

aiohttp 在 Windows 上默认优先用 `aiodns`（依赖 `pycares` 的 C 扩展）。
某些 Windows 环境里这个 DLL 加载会失败，导致所有 HTTPS 请求直接起不来。

## 解决

强制 aiohttp 只用线程池解析器 `ThreadedResolver`，不走 aiodns。

**推荐做法：在文件末尾追加一行赋值**，不要去找原来那一行改：

```python
import io, os, aiohttp
p = os.path.join(os.path.dirname(aiohttp.__file__), "resolver.py")
s = io.open(p, encoding="utf-8").read()
marker = "# [miot-signal] force threaded resolver"
if marker not in s:
    s += f"\n\n{marker}\nDefaultResolver = ThreadedResolver\n"
    io.open(p, "w", encoding="utf-8").write(s)
```

原理：Python 模块加载时，后赋值的覆盖先赋值的。原文件里的
`DefaultResolver` 可能写在 `if/else` 分支里，按行替换容易匹配不到却静默成功；
追加到最后一行，任何 aiohttp 版本都成立。

验证：

```bash
python -c "import aiohttp.resolver as r; print(r.DefaultResolver)"
# 应输出 <class 'aiohttp.resolver.ThreadedResolver'>
```

## 注意

- 这是改第三方包，升级 aiohttp 后会被覆盖，需要重新改一次。
- 只影响 DNS 解析走线程池，功能上没有差别，性能损耗可以忽略（轮询场景每秒几次请求）。
- 改完必须重启 HA 才生效。
