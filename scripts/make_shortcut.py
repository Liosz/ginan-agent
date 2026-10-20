"""在项目根目录生成一个「Ginan Agent」快捷方式（Windows）。

跑法：python scripts/make_shortcut.py

============================ 这个文件解决什么问题 ============================
`run.bat` 能双击运行，但它是**批处理** —— 而批处理有个去不掉的毛病：

    按 Ctrl+C 之后，程序自己已经干净退出了，**cmd.exe 还会再问一句**
    "Terminate batch job (Y/N)?"。退出要按两次键。

那句问话是 cmd 的内置行为，从批处理内部关不掉。

👉 而这个快捷方式**完全不经过 cmd** —— 它直接指向 python.exe，
   于是没有批处理，也就没有那句话。按一次 Ctrl+C 就退出。

============================ 为什么是脚本，不是提交一个 .lnk ============================
`.lnk` 是二进制，而且里面存的是**绝对路径**（python 装在哪、项目在哪）。
提交进仓库的话：
  · 换台机器就是坏的，而且**看起来像好的**（图标还在，双击报错）
  · 二进制文件在 diff 里看不出改了什么

所以这里生成它。路径**现算**，换机器重跑一次就行。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 快捷方式的名字。
#
# ⚠️ 用中文名，因为它是**给用户双击的** —— 用户在文件管理器里看到的是它。
#    而 run.bat 必须叫 run.bat（纯 ASCII），两者不冲突。
LINK_NAME = "Ginan Agent.lnk"


def make_shortcut() -> int:
    if sys.platform != "win32":
        print("这个脚本只对 Windows 有意义（别的系统直接用 run.sh）。")
        return 1

    target = Path(sys.executable)          # python.exe
    bootstrap = ROOT / "ginan_agent" / "bootstrap.py"

    if not bootstrap.is_file():
        print(f"找不到 {bootstrap} —— 这个脚本要在项目里跑。")
        return 1

    # ⚠️ 必须是 python.exe，**不能**是 pythonw.exe。
    #    pythonw 没有控制台 —— 用户就看不到 "服务起来后浏览器会自动打开"
    #    那些提示，也看不到任何报错。程序自己是靠那个黑窗口说事情的。
    if target.name.lower().startswith("pythonw"):
        print(f"⚠️ 当前解释器是 {target.name}（无控制台）。"
              f"请用 python.exe 再跑一次这个脚本。")
        return 1

    link = ROOT / LINK_NAME

    # ⚠️ 用 PowerShell 的 WScript.Shell 来写 .lnk ——
    #    .lnk 是二进制格式，手写容易写错，而 Windows 自带能写它的工具。
    #    （不用 pywin32：那是个额外的依赖，而这是个 pip 装完就能跑的项目。）
    ps = f"""
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut('{link}')
$sc.TargetPath = '{target}'
$sc.Arguments = '"{bootstrap}"'
$sc.WorkingDirectory = '{ROOT}'
$sc.IconLocation = '{target},0'
$sc.Description = 'Ginan Agent - GNSS precise positioning workbench'
$sc.Save()
"""

    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"调用 PowerShell 失败：{e}")
        return 1

    if r.returncode != 0 or not link.is_file():
        print("生成失败。PowerShell 说：")
        print((r.stderr or r.stdout or "").strip()[:500])
        return 1

    print(f"已生成：{link}")
    print()
    print("  指向：   " + str(target))
    print("  参数：   " + str(bootstrap))
    print("  工作目录：" + str(ROOT))
    print()
    print("⚠️ 这个快捷方式**不经过 cmd** —— 按 Ctrl+C 一次就退出，")
    print("   不会再有 'Terminate batch job (Y/N)?' 那一问。")
    print()
    print("提示：把它拖到桌面或任务栏，以后就不用开文件夹了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(make_shortcut())
