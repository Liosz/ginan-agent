"""这个项目**自己**在哪里 —— 所有"相对项目"的路径都从这里取。

============================ 为什么单独一个模块 ============================
因为"项目被放在哪"这件事，**用户和开发者完全不同**：

    开发者：克隆到哪就是哪，而且通常不会动
    用户：  可能解压到桌面、D 盘、U 盘、任意一个文件夹

所以代码里**绝不能**假设自己在某个固定位置。

⚠️ 最容易犯的错不是写死 `C:\\xxx`（那个太明显了），而是这些：

    ① `load_dotenv()` 不带路径
       → 它从**当前工作目录**往上找。用户从别的目录启动就找不到 .env，
         而且失败是**静默**的（读不到就当没有），极难诊断。

    ② `Path.cwd()` 当默认值
       → 取的是"进程启动时我在哪"，和"项目在哪"完全无关。

    ③ 相对路径当资源路径
       → `open("frontend/index.html")` 依赖 CWD。

正确做法：**所有项目内资源，都从本文件的 `__file__` 出发算。**

这样不管项目被放在哪、从哪个目录启动，算出来的都是对的。

============================ 和"用户数据目录"的关系 ============================
两者是**上下级**，别混：

    project_root()      项目**代码**在哪
    default_app_root()  它下面的 `userdata/` —— 用户**数据**在哪

⚠️ 数据**就放在项目目录里**（`userdata/` 子目录下），这是刻意的：

        "一个软件 = 一个文件夹。"

   要备份、要整个搬走、要删干净，都只涉及一个地方。
   详见 core/session.py 里 default_app_root 的说明。

   代价是"删软件会连数据一起删掉" —— 这正是用户要的
   （他不想在系统里留下散落的东西）。
"""

from __future__ import annotations

from pathlib import Path


def project_root() -> Path:
    """项目根目录（`ginan-agent/`）。

    ⚠️ 从本文件的位置往上算，不依赖当前工作目录。

    本文件是 `ginan-agent/ginan_agent/locations.py`，
    所以往上走两层就是 `ginan-agent/`：

        locations.py → ginan_agent/ → ginan-agent/
                          ↑ 1 层          ↑ 2 层
    """
    return Path(__file__).resolve().parents[1]


def dotenv_path() -> Path:
    """.env 文件的位置（可能不存在）。

    ⚠️ 为什么要显式给路径？

    因为 `load_dotenv()` 不带参数时，是从**当前工作目录**往上找 ——
    那是"用户在哪个目录敲的命令"，和"项目在哪"是两回事。

    症状很隐蔽：用户在项目目录里跑没事，换个目录（比如从开始菜单快捷方式）
    就变成"没配置 API Key"，而配置文件明明就在那儿。

    （对普通用户这条其实用不上 —— 他们在设置界面里填，存到 config.json。
      这里是给**开发时**用的：把 Key 放 .env 里，不进版本库。）
    """
    return project_root() / ".env"


def frontend_dir() -> Path:
    """前端资源目录（HTML/CSS/JS）。"""
    return Path(__file__).resolve().parent / "frontend"


def load_project_dotenv() -> bool:
    """读项目根目录的 .env（如果存在）。返回是否读到了。

    ⚠️ 文件不存在时**不报错** —— 那只是"没配"，不是"出错"。
       桌面应用要能在没配任何东西的情况下正常启动。
    """
    from dotenv import load_dotenv

    path = dotenv_path()
    if not path.is_file():
        return False
    return load_dotenv(path, override=False)
