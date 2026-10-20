#!/usr/bin/env bash
#
# Ginan Agent launcher for Linux / macOS
# Ginan Agent 启动器（Linux / macOS）
#
# 用法 / Usage:
#     ./run.sh
#
# ⚠️ Windows 用户请用 run.bat，不要用这个。
#
# ============================================================================
# 和 run.bat 的关系
# ============================================================================
#
# 两者做的事**完全一样**：找到 Python → 交给 bootstrap.py。
#
# 真正的工作（检查依赖、装依赖、打印中文提示）都在 bootstrap.py 里，
# 两个启动器都只是很薄的一层壳。
#
# 这样分的好处：**平台相关的代码只有这几行**。
# 逻辑写在 Python 里，就只需要维护一份。
#
# ⚠️ 但有个必须照做的例外：**Windows 的 .bat 不能有中文**（见 run.bat 里的说明）。
#    Linux/macOS 的 .sh 没这个问题 —— shell 本来就是按 UTF-8 处理的。
#    所以这个文件可以有中文，而 run.bat 不能。

set -e

cd "$(dirname "$0")"

# 找一个够新的 Python
# ⚠️ macOS 上 `python` 可能不存在（只有 python3），所以要都试一遍
PY=""
for C in python3 python; do
    if command -v "$C" >/dev/null 2>&1; then
        if "$C" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
            PY="$C"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    cat <<'EOF'

  ============================================================
   找不到 Python / Python not found
  ============================================================

   这个程序需要 Python 3.10 或更高版本。

   怎么装：
     Ubuntu/Debian:  sudo apt install python3 python3-pip
     Fedora:         sudo dnf install python3 python3-pip
     macOS:          brew install python3   （或到 python.org 下载）

   装完重新运行 ./run.sh
EOF
    exit 1
fi

exec "$PY" "ginan_agent/bootstrap.py" "$@"
