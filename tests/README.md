# 测试

标准库 `unittest`，无需额外依赖（GUI 测试需要 PyQt5）。

```bash
# 全部测试
python -m unittest discover -s tests -v

# 只跑引擎测试（多线程/断点续传/重试/限速/代理/Range 完整性）
python tests/test_engine.py -v

# 只跑队列与持久化测试
python tests/test_ui_queue.py -v
```

## 测试内容

| 文件 | 覆盖 |
|------|------|
| `fault_server.py` | 进程内**故障注入** HTTP 服务器：支持 Range 206、限速、忽略 Range、篡改 Content-Range、随机断连、无 Content-Length、401 认证、404、空响应 |
| `proxy_server.py` | 本地 HTTP 代理，用于验证代理配置确实生效 |
| `test_engine.py` | 分片下载正确性（SHA256 比对）、服务器不支持 Range 时降级单线程、未知大小下载、暂停/继续、停止/重启、**崩溃恢复**（子进程被硬杀后续传）、**重试退避**（503/断连/重试预算/4xx 不重试）、**Range 错位防护**、**令牌桶限速**（含运行中改速率）、代理、自定义请求头 |
| `test_ui_queue.py` | 队列并发上限、优先级插队、任务持久化与恢复、配置校验与损坏文件容错 |
| `test_ui_smoke.py` | 主窗口/设置对话框构建、自定义请求头解析、状态映射 |

## 说明

- 测试全部在 `127.0.0.1` 上跑，不依赖外网。
- 引擎对回环地址会跳过浏览器 Cookie 扫描，因此测试启动很快。
- 限速/超时类测试使用宽松阈值（通常 ±50%），避免在繁忙机器上误报。
