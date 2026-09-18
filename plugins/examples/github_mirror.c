/* github_mirror.c - 示例插件：把 GitHub 下载链接改写成镜像
 *
 * 默认镜像前缀 https://ghfast.top/ ，可以用环境变量 FD_GITHUB_MIRROR 覆盖：
 *     set FD_GITHUB_MIRROR=https://ghproxy.net/
 * 设为空字符串则关闭改写。
 *
 * 改写规则（只处理下载直链，不动 API 请求）：
 *   https://github.com/<owner>/<repo>/releases/download/...  ->  <mirror>https://github.com/...
 *   https://raw.githubusercontent.com/...                    ->  <mirror>https://raw.githubusercontent.com/...
 *
 * 编译：gcc -shared -O2 -I.. -o github_mirror.dll github_mirror.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "plugin_api.h"

int fd_plugin_api_version(void) { return FD_PLUGIN_API_VERSION; }
const char* fd_plugin_name(void) { return "GitHub Mirror Rewrite"; }
const char* fd_plugin_version(void) { return "1.0.0"; }
const char* fd_plugin_description(void) { return "rewrite github download links through a mirror (FD_GITHUB_MIRROR)"; }
const char* fd_plugin_author(void) { return "FastDownloader example"; }

static int looks_like_download(const char* url) {
    return strstr(url, "github.com/") != NULL &&
           strstr(url, "/releases/download/") != NULL;
}

static int looks_like_raw(const char* url) {
    return strncmp(url, "https://raw.githubusercontent.com/", 34) == 0;
}

int fd_plugin_on_url(char* url, int size) {
    const char* mirror = getenv("FD_GITHUB_MIRROR");
    char out[4096];
    int mlen;

    if (mirror == NULL) mirror = "https://ghfast.top/";   /* 默认镜像，可关可换 */
    mlen = (int)strlen(mirror);
    if (mlen == 0) return 0;                              /* 显式关闭 */
    if (strncmp(url, mirror, mlen) == 0) return 0;        /* 已经是镜像地址，别套娃 */
    if (!looks_like_download(url) && !looks_like_raw(url)) return 0;
    if (mlen + (int)strlen(url) + 1 > (int)sizeof(out)) return 0;
    if (mlen + (int)strlen(url) + 1 > size) return 0;

    memcpy(out, mirror, mlen);
    strcpy(out + mlen, url);
    strcpy(url, out);
    return 1;
}
