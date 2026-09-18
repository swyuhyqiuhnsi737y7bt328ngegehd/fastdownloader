/* plugin_api.h - Fast Downloader Pro 插件接口（C ABI）
 *
 * 为什么用纯 C 接口而不是“注入”或 Python C 扩展：
 *   - 纯 C ABI 不依赖 Python 版本/ABI，MSVC 和 MinGW 编出来的 DLL 都能直接加载；
 *   - 主程序用 ctypes 显式调用固定名字的导出函数，插件崩了也只是返回错误，
 *     不会把宿主进程带下水；
 *   - 注入意味着在主进程里跑任意代码，既没必要也难排查。
 *
 * 一个插件就是一个 .dll，导出下面这些函数。只有 fd_plugin_api_version()
 * 是必须的，其余按需实现；没实现的钩子宿主会跳过。
 *
 * 编译示例（MinGW）：
 *     gcc -shared -O2 -o myplugin.dll myplugin.c
 * 编译示例（MSVC）：
 *     cl /LD /O2 myplugin.c
 */
#ifndef FD_PLUGIN_API_H
#define FD_PLUGIN_API_H

#define FD_PLUGIN_API_VERSION 1

#ifdef _WIN32
#  define FD_EXPORT __declspec(dllexport)
#else
#  define FD_EXPORT __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* ---------- 必须实现 ---------- */

/* 返回 FD_PLUGIN_API_VERSION。版本不匹配的插件会被拒绝加载。 */
FD_EXPORT int fd_plugin_api_version(void);

/* ---------- 元信息（可选，返回 UTF-8 字符串，生命周期需长于调用） ---------- */

FD_EXPORT const char* fd_plugin_name(void);         /* 插件名，缺省用文件名 */
FD_EXPORT const char* fd_plugin_version(void);      /* 版本号，如 "1.0.0" */
FD_EXPORT const char* fd_plugin_description(void);  /* 一句话说明 */
FD_EXPORT const char* fd_plugin_author(void);       /* 作者 */

/* ---------- 生命周期（可选） ---------- */

/* 加载成功时调用。返回非 0 表示插件自认为不可用，宿主会记为“加载失败”。 */
FD_EXPORT int fd_plugin_on_load(void);

/* 程序退出 / 插件停用时调用，用于释放资源。 */
FD_EXPORT void fd_plugin_on_unload(void);

/* ---------- 钩子（可选） ---------- */

/* 任务创建前改写下载链接。
 * url 是可写的缓冲区，容量为 size 字节（含结尾 0）。
 * 想改写就写回同一个缓冲区；返回 0 表示“未修改”，1 表示“已修改”，
 * 负数表示“拒绝这个链接”（任务不会创建）。
 * 例：把 github.com 换成镜像站、去掉 utm_* 跟踪参数。 */
FD_EXPORT int fd_plugin_on_url(char* url, int size);

/* 为某个链接追加 HTTP 请求头。
 * out 是缓冲区，容量 size 字节，按 "Name: Value\n" 每行一条追加。
 * 返回追加的字节数（0 表示不加）。
 * 例：给特定站点补 Referer、UA、Authorization。 */
FD_EXPORT int fd_plugin_on_headers(const char* url, char* out, int size);

/* 任务开始下载（拿到最终链接与保存路径）。 */
FD_EXPORT void fd_plugin_on_task_start(const char* url, const char* save_path);

/* 任务成功完成。sha256 可能是空字符串（未做校验时）。 */
FD_EXPORT void fd_plugin_on_task_done(const char* save_path, long long size,
                                      const char* sha256);

/* 任务失败。 */
FD_EXPORT void fd_plugin_on_error(const char* url, const char* message);

/* 任务进度（可能被高频调用，请自己节流）。 */
FD_EXPORT void fd_plugin_on_progress(const char* url, double percent);

#ifdef __cplusplus
}
#endif

#endif /* FD_PLUGIN_API_H */
