# 示例插件

三个可直接使用/参考的插件源码，都只依赖 `gcc`：

| 插件 | 作用 |
|------|------|
| `strip_tracking` | 下载前去掉 URL 里的 `utm_*` / `fbclid` / `gclid` 等统计参数 |
| `github_mirror` | 把 GitHub 下载直链改写成镜像（默认 `https://ghfast.top/`，可用环境变量 `FD_GITHUB_MIRROR` 更换或清空关闭） |
| `task_logger` | 每次下载成功追加一行到 DLL 同目录的 `download_history.csv` |

## 编译

```bat
build_examples.bat
```

或者手动：

```bat
gcc -shared -O2 -I.. -o dist\strip_tracking.dll strip_tracking.c
```

编译产物在 `dist\`，**直接把 .dll 拖进主窗口**即可安装。

## 自己写一个

接口定义见 `../plugin_api.h`，只有 `fd_plugin_api_version()` 是必须的，
其它钩子按需实现即可。