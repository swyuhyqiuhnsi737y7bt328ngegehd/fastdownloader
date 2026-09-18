/* strip_tracking.c - 示例插件：下载前去掉 URL 里的统计参数
 *
 * 效果：https://x.com/f.zip?utm_source=tg&id=7&fbclid=abc
 *    -> https://x.com/f.zip?id=7
 *
 * 编译：gcc -shared -O2 -I.. -o strip_tracking.dll strip_tracking.c
 */
#include <string.h>
#include "plugin_api.h"

int fd_plugin_api_version(void) { return FD_PLUGIN_API_VERSION; }
const char* fd_plugin_name(void) { return "Tracking Param Cleaner"; }
const char* fd_plugin_version(void) { return "1.0.0"; }
const char* fd_plugin_description(void) { return "remove utm_*/fbclid/gclid style tracking parameters"; }
const char* fd_plugin_author(void) { return "FastDownloader example"; }

static int starts_with(const char* s, int len, const char* prefix) {
    int n = (int)strlen(prefix);
    return len >= n && strncmp(s, prefix, n) == 0;
}

/* 传入的是 "key=value" 整段，只有 '=' 之前的部分才是参数名 */
static int is_tracking(const char* item, int len) {
    static const char* exact[] = { "fbclid", "gclid", "yclid", "msclkid", "mc_eid",
                                   "igshid", "ref", "ref_src", "spm", "share_source" };
    const char* eq = (const char*)memchr(item, '=', len);
    int klen = eq ? (int)(eq - item) : len;
    int i;
    if (klen <= 0) return 0;
    if (starts_with(item, klen, "utm_")) return 1;
    for (i = 0; i < (int)(sizeof(exact) / sizeof(exact[0])); i++) {
        if ((int)strlen(exact[i]) == klen && strncmp(item, exact[i], klen) == 0) return 1;
    }
    return 0;
}

int fd_plugin_on_url(char* url, int size) {
    char out[4096];
    char* q = strchr(url, '?');
    char* p;
    int used, changed = 0, first = 1;

    if (!q) return 0;                       /* 没有查询串，什么都不做 */
    used = (int)(q - url);
    if (used >= (int)sizeof(out) - 2) return 0;
    memcpy(out, url, used);
    out[used] = 0;

    p = q + 1;
    while (*p) {
        char* amp = strchr(p, '&');
        int len = amp ? (int)(amp - p) : (int)strlen(p);
        if (is_tracking(p, len)) {
            changed = 1;
        } else if (len > 0) {
            /* 还剩多少空间：末尾要留 '?' 或 '&' 和结尾 0 */
            int room = (int)sizeof(out) - used - 2;
            if (len > room) return 0;       /* 放不下就整体放弃，不改坏链接 */
            out[used++] = first ? '?' : '&';
            memcpy(out + used, p, len);
            used += len;
            out[used] = 0;
            first = 0;
        }
        if (!amp) break;
        p = amp + 1;
    }
    if (!changed) return 0;
    if (used >= size) return 0;
    memcpy(url, out, used + 1);
    return 1;
}
