/* task_logger.c - 示例插件：把每次成功的下载追加到 CSV
 *
 * 记录写在插件 DLL 同目录的 download_history.csv：
 *     时间,大小(字节),SHA256,文件路径
 *
 * 编译：gcc -shared -O2 -I.. -o task_logger.dll task_logger.c
 */
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <windows.h>
#include "plugin_api.h"

int fd_plugin_api_version(void) { return FD_PLUGIN_API_VERSION; }
const char* fd_plugin_name(void) { return "Download History CSV"; }
const char* fd_plugin_version(void) { return "1.0.0"; }
const char* fd_plugin_description(void) { return "append completed downloads to download_history.csv"; }
const char* fd_plugin_author(void) { return "FastDownloader example"; }

static int loaded = 0;
static char log_path[1024];

static void build_log_path(void) {
    /* 日志放在 DLL 自己旁边：用模块句柄取自身路径 */
    HMODULE self = NULL;
    /* 用"函数地址"反查自身模块路径，这样不需要依赖任何外部传参 */
    if (GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                           GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           (LPCSTR)&build_log_path, &self) && self) {
        DWORD n = GetModuleFileNameA(self, log_path, (DWORD)sizeof(log_path));
        if (n > 0) {
            char* slash = strrchr(log_path, '\\');
            if (slash) {
                *(slash + 1) = 0;
                strncat(log_path, "download_history.csv",
                        sizeof(log_path) - strlen(log_path) - 1);
                return;
            }
        }
    }
    strcpy(log_path, "download_history.csv");
}

int fd_plugin_on_load(void) {
    FILE* f;
    build_log_path();
    loaded = 1;
    f = fopen(log_path, "a");
    if (!f) return 0;                 /* 写不了也不阻止加载，只是记不下来 */
    fclose(f);
    return 0;
}

void fd_plugin_on_task_done(const char* save_path, long long size, const char* sha256) {
    FILE* f;
    time_t now;
    struct tm* tm_info;
    char stamp[32];

    if (!loaded) return;
    f = fopen(log_path, "a");
    if (!f) return;
    now = time(NULL);
    tm_info = localtime(&now);
    strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", tm_info);
    fprintf(f, "%s,%lld,%s,%s\n", stamp, size,
            (sha256 && sha256[0]) ? sha256 : "-",
            save_path ? save_path : "-");
    fclose(f);
}
