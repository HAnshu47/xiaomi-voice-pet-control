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

编辑 `site-packages/aiohttp/resolver.py` 末尾：

```python
# 原始内容
DefaultResolver: _DefaultType = DefaultResolver  # 或自动探测那一段

# 改成
DefaultResolver: _DefaultType = ThreadedResolver
```

具体位置在文件末尾，找到给 `DefaultResolver` 赋值的那几行，直接替换成：

```python
DefaultResolver: _DefaultType = ThreadedResolver
```

## 注意

- 这是改第三方包，升级 aiohttp 后会被覆盖，需要重新改一次。
- 只影响 DNS 解析走线程池，功能上没有差别，性能损耗可以忽略（轮询场景每秒几次请求）。
- 改完必须重启 HA 才生效。
