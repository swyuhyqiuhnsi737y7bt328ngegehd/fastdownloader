@echo off
rem 用 MinGW gcc 编译示例插件
rem 需要 gcc 在 PATH 里（Nuitka 自带的那个也可以）
setlocal
set CC=gcc
where %CC% >nul 2>&1 || (
    echo [ERROR] 找不到 gcc，请把 MinGW 的 bin 目录加入 PATH
    exit /b 1
)
if not exist dist mkdir dist
for %%F in (strip_tracking github_mirror task_logger) do (
    echo [CC] %%F.dll
    %CC% -shared -O2 -I.. -o dist\%%F.dll %%F.c || exit /b 1
)
echo.
echo [OK] 生成的插件在 dist\ 目录，可直接拖进主窗口安装
