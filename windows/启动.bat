@echo off
chcp 65001 >nul
title 树莓派网络麦克风 - Windows 端
cd /d "%~dp0"

rem (1) 优先用打包好的单文件 exe（无需 Python、无控制台窗口）
if exist "树莓派网络麦克风.exe" (
    echo 启动 树莓派网络麦克风.exe ...
    start "" "树莓派网络麦克风.exe"
    exit /b 0
)

rem (2) 没有 exe 就用源码跑（需要 Python + 依赖）
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 exe，也没找到 python。
    echo         要么把「树莓派网络麦克风.exe」放在本目录，
    echo         要么安装 Python 3.8+：https://www.python.org/downloads/
    echo         （安装时勾选 "Add python.exe to PATH"）
    pause
    exit /b 1
)

python -c "import soundcard, numpy" >nul 2>nul
if errorlevel 1 (
    echo 首次运行：正在安装依赖 soundcard / numpy ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败。请手动执行：
        echo         python -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

echo 启动图形界面（关闭这个黑窗口 = 退出程序）...
python btmic_net_gui.py
if errorlevel 1 (
    echo.
    echo 程序异常退出，请把上面的报错内容截图。
    pause
)
