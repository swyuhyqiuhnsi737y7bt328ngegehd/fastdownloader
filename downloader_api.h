// downloader_api.h - C-compatible interface for Fast Downloader Pro
// 可以被 C++ 直接 #include，也可以通过 Python ctypes 调用
// 未来核心逻辑替换为 C++ DLL 时，保持此接口不变

#ifndef DOWNLOADER_API_H
#define DOWNLOADER_API_H

#ifdef __cplusplus
extern "C" {
#endif

// ---- 错误码 ----
#define DL_OK             0
#define DL_ERR_GENERAL    -1
#define DL_ERR_INIT       -2
#define DL_ERR_NOT_FOUND  -3
#define DL_ERR_BUSY       -4
#define DL_ERR_PARAM      -5

// ---- 任务状态 ----
#define DL_STATUS_READY     0
#define DL_STATUS_RUNNING   1
#define DL_STATUS_PAUSED    2
#define DL_STATUS_COMPLETED 3
#define DL_STATUS_ERROR     4
#define DL_STATUS_STOPPED   5

// ---- 回调事件类型 ----
#define DL_EVENT_ERROR     0
#define DL_EVENT_COMPLETED 1
#define DL_EVENT_PAUSED    2
#define DL_EVENT_STOPPED   3

// 回调函数签名：线程安全，从工作线程调用
typedef void (*DL_EventCallback)(int task_id, int event_type, const char* data, void* userdata);

// ====================================================================
// 以下为 DLL 导出函数，C++ 实现时用 __declspec(dllexport)
// Python 实现时通过 ctypes.CFUNCTYPE / ctypes.cdll 调用
// ====================================================================

// 初始化下载器。config_path=NULL 表示使用默认配置；callback/userdata 可为 NULL
int dl_init(const char* config_path, DL_EventCallback callback, void* userdata);

// 创建下载任务。返回 task_id (>=0) 或负值错误码
int dl_create_task(const char* url, const char* save_path, int num_threads, int speed_limit_kb);

// 开始/恢复任务
int dl_start_task(int task_id);

// 暂停运行中的任务
int dl_pause_task(int task_id);

// 取消/停止任务并移除
int dl_cancel_task(int task_id);

// 获取任务状态码 (DL_STATUS_*)，出错返回负数
int dl_get_status(int task_id);

// 获取任务进度百分比 (0.0 ~ 100.0)，出错返回负数
double dl_get_progress(int task_id);

// 获取任务信息 JSON 字符串，调用者必须用 dl_free_string() 释放
char* dl_get_info_json(int task_id);

// 释放下载器分配的字符串
void dl_free_string(char* str);

// 获取任务错误信息，调用者必须用 dl_free_string() 释放
char* dl_get_error(int task_id);

// 关闭下载器，取消所有任务，释放资源
void dl_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif // DOWNLOADER_API_H
