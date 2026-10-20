"""假 Ginan —— 按剧本表演的 pea。

============================ 为什么需要它 ============================
两个真实的困难：

  ① **真跑一次要下数据**：50 MB 观测 + 几百 KB 产品，还要网络通。
     开发界面时每次改一行都要等下载，根本没法迭代。

  ② **某些情况根本制造不出来**：
     想测"取消"，得在真 pea 跑到一半时点停止 —— 时机很难凑。
     想测"缺文件"，得先把文件删掉再跑。
     想测"跑 10 分钟"，那就要真等 10 分钟。

假 Ginan 把这两件事都解决：**秒级返回、场景可控**。

============================ ⚠️ 它必须和真 pea 足够像 ============================
像到什么程度？**输出格式必须一致** —— 因为 diagnostics.py 就是靠解析这些
文字工作的。如果假的输出格式不对，那些解析规则就从来没被测过，
等接上真 Ginan 才发现全不工作。

所以下面的输出是**从真实 pea 的输出里抄来的**（M0.5 那次实跑的）。

============================ 怎么控制它 ============================
环境变量：

    FAKE_PEA_EPOCHS   处理多少个历元（默认 60）
    FAKE_PEA_DELAY    每个历元睡多少秒（默认 0.005；调大用来测取消）
    FAKE_PEA_SCENARIO 演哪种情况：
                        ""            正常完成
                        "missing"     缺文件
                        "badconfig"   配置无效
                        "mutated"     配置被静默改写（退出码仍是 0）
                        "openblas"    出现 OpenBLAS 警告
                        "crash"       中途崩掉
"""

from __future__ import annotations

import math
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

EPOCHS = int(os.environ.get("FAKE_PEA_EPOCHS", "60"))
DELAY = float(os.environ.get("FAKE_PEA_DELAY", "0.005"))
SCENARIO = os.environ.get("FAKE_PEA_SCENARIO", "")

# ⚠️ 参考位置用 ALIC 站的真实坐标 ——
#    这样产出的 .POS 能被 posfile.py 解析，测出来的结论也是真实的。
REF_XYZ = (-4052052.7249, 4212835.9727, -2545104.5766)
REF_NEU = (-23.670110105748, 133.885521632601, 603.224004273)


def out(msg: str = "") -> None:
    print(msg, flush=True)


def find_config(argv: list[str]) -> Path | None:
    for i, a in enumerate(argv):
        if a in ("--config", "-y") and i + 1 < len(argv):
            return Path(argv[i + 1])
    return None


def banner() -> None:
    out("PEA starting... (HEAD v4.1.4-5033996 from 2026-09-16 17:56:53 +1000)")
    out()


def config_summary(config: Path | None) -> None:
    """打印配置摘要 —— 格式照抄真实 pea。"""
    out("Outputs:")
    out("\ttrace level:                   1")
    out(f"\treceiver trace filename:       {config}")
    out()
    out("Process modes:")
    out("\tPreprocessor:        1")
    out("\tSPP                  1")
    out("\tPPP:                 1")
    out("\tIonospheric:         0")
    out()
    out("Systems:")
    out("\tGPS:     1")
    out("\tGALILEO: 1")
    out("\tBEIDOU:  0")
    out()
    out("===============================")
    out("...End Configuration Summary")
    out("===============================")
    out()


def write_pos(work_dir: Path, config: Path | None, epochs: int) -> Path:
    """写一份真实的 .POS 文件。

    ⚠️ 格式必须和真 Ginan 一致 —— 否则 posfile.py 的解析就没被验证过。
       表头是从真实文件里抄的。
    """
    station = "ALIC"
    fname = work_dir / f"{station}_{config.stem if config else 'fake'}.POS"

    end = datetime(2026, 9, 2, 0, 0, 0) + timedelta(seconds=30 * (epochs - 1))

    lines = [
        "PBO Station Position Time Series. Reference Frame : IGS20",
        "Format Version: 2.0.0",
        f"4-character ID: {station}",
        "First Epoch   : 2026-09-02T00:00:00.01",
        f"XYZ Reference position :  {REF_XYZ[0]:.6f} {REF_XYZ[1]:.6f} {REF_XYZ[2]:.6f} (IGS20)",
        f"NEU Reference position :  {REF_NEU[0]:.12f} {REF_NEU[1]:.12f} {REF_NEU[2]:.9f} (IGS20/WGS84)",
        "Start Field Description",
        "YYYY-MM-DDTHH:MM:SS.SSS  Date and Time of given position epoch (GPS Time)",
        "YYYY.YYYYYYYYY          Decimal year of given position epoch (GPS Time)",
        "X                       X coordinate, Specified Reference Frame, meters",
        "Y                       Y coordinate, Specified Reference Frame, meters",
        "Z                       Z coordinate, Specified Reference Frame, meters",
        "Sx                      Sigma of the X position",
        "Sy                      Sigma of the Y position",
        "Sz                      Sigma of the Z position",
        "Rxy                     Correlation between the X and Y position",
        "Rxz                     Correlation between the X and Z position",
        "Ryz                     Correlation between the Y and Z position",
        "Nlat                    North latitude, WGS-84 ellipsoid, decimal degrees",
        "Elong                   East longitude, WGS-84 ellipsoid, decimal degrees",
        "Height (Up)             Height relative to WGS-84 ellipsoid, meters",
        "dN                      Difference in North component from NEU reference position, meters",
        "dE                      Difference in East component from NEU reference position, meters",
        "dU                      Difference in vertical component from NEU reference position, meters",
        "Sn                      Sigma of dN, meters",
        "Se                      Sigma of dE, meters",
        "Su                      Sigma of dU, meters",
        "End Field Description",
        "*YYYY-MM-DDTHH:MM:SS.SSS YYYY.YYYYYYYYY        X             Y              Z           Sx         Sy         Sz          Rxy       Rxz       Ryz   Nlat                Elong             Height (Up)      dN            dE            dU           Sn         Se         Su          Rne       Rnu       Reu soln",
    ]

    # ⚠️ 模拟真实的收敛过程：开头几米抖动，之后稳定到几厘米。
    #    这样 posfile.py 的"收敛判定"才有东西可判 ——
    #    如果所有历元都是同一个数，收敛逻辑等于没被测。
    for i in range(epochs):
        t = datetime(2026, 9, 2, 0, 0, 0) + timedelta(seconds=30 * i)
        if i < 8:
            # 收敛过程：指数衰减的抖动
            amp = 2.5 * math.exp(-i / 3.0)
            dn = amp * math.sin(i * 1.7)
            de = amp * math.cos(i * 2.3)
            du = amp * 1.8 * math.sin(i * 0.9)
        else:
            # 收敛之后：几厘米的噪声
            dn = 0.012 * math.sin(i * 0.7)
            de = 0.015 * math.cos(i * 1.1)
            du = 0.035 * math.sin(i * 0.4)

        lat = REF_NEU[0] + dn / 111320.0
        lon = REF_NEU[1] + de / (111320.0 * math.cos(math.radians(REF_NEU[0])))
        hgt = REF_NEU[2] + du

        lines.append(
            f" {t.strftime('%Y-%m-%dT%H:%M:%S')}.000 2026.671232306 "
            f"{REF_XYZ[0]+dn:.5f} {REF_XYZ[1]+de:.5f} {REF_XYZ[2]+du:.5f} "
            f"  0.005   0.005   0.008  0.100  0.100  0.100  "
            f"{lat:.10f} {lon:.10f} {hgt:.5f} "
            f"{dn:.5f} {de:.5f} {du:.5f}  0.020  0.020  0.040  0.100  0.100  0.100 ginan"
        )

    fname.parent.mkdir(parents=True, exist_ok=True)
    fname.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fname


def main(argv: list[str]) -> int:
    config = find_config(argv)
    dry_run = "--dry-run" in argv
    max_epochs = EPOCHS
    for i, a in enumerate(argv):
        if a == "-n" and i + 1 < len(argv):
            try:
                max_epochs = int(argv[i + 1])
            except ValueError:
                pass

    work_dir = config.parent if config else Path.cwd()

    banner()

    # ---- 剧本：缺文件 ----
    if SCENARIO == "missing":
        out(f"Error: Missing antenna information (ANTEX file) file {work_dir}/igs20.atx")
        out(f"Error: Missing SINEX file {work_dir}/igs.rnx.snx")
        out("Warning: Invalid SINEX file")
        out("Dry-run failed: configuration is invalid")
        out("PEA finished")
        return 1

    # ---- 剧本：配置无效 ----
    if SCENARIO == "badconfig":
        out("Error: Unknown YAML option 'receiver_options.global.not_a_real_option'")
        out("Incorrect configuration")
        out("PEA finished")
        return 1

    # ---- 剧本：OpenBLAS 警告 ----
    if SCENARIO == "openblas":
        out("OpenBLAS Warning : Detect OpenMP Loop and this application may hang.")
        out("Please use OPENBLAS_NUM_THREADS=1")

    config_summary(config)

    if dry_run:
        # ⚠️ 注意真实 pea 的行为：dry-run 成功时**不管 sanity checker 结果**。
        #    这里如实模仿 —— 好让调用方明白"退出码 0 不等于配置没问题"。
        if SCENARIO == "mutated":
            out("Warning: Epoch tolerance is greater than half the epoch interval. "
                "Adjusting to half the epoch interval.")
            out("Warning: Ionosphere-free combination in use, "
                "disabling 2nd and 3rd order ionospheric components.")
        out("Dry-run successful: configuration and sanity checks passed")
        out("PEA finished")
        return 0

    # ---- 正常处理 ----
    if SCENARIO == "mutated":
        out("Warning: Epoch tolerance is greater than half the epoch interval.")

    start = time.monotonic()
    out(f"PEA started  processing at : {datetime.now()}")
    for i in range(max_epochs):
        time.sleep(DELAY)
        t = datetime(2026, 9, 2, 0, 0, 0) + timedelta(seconds=30 * i)
        out(f"Processed epoch - {t.strftime('%Y-%m-%d %H:%M:%S')}.01 (took 00:00:00.00)")

        # ---- 剧本：中途崩掉 ----
        if SCENARIO == "crash" and i >= max_epochs // 2:
            out("Error: Kalman filter diverged")
            out("PEA finished")
            return 2

    out("Processing complete")
    out()
    out("PEA started  processing at : 2026-09-18 04:19:44.06")
    out("and finished processing at : 2026-09-18 04:19:47.61")
    out(f"Total processing duration  : {time.monotonic()-start:.2f}")
    out()
    out("PEA finished")

    if not dry_run:
        pos = write_pos(work_dir, config, max_epochs)
        # 真实 pea 不打印这个，但我们的测试需要知道产物在哪
        print(f"[fake-pea] wrote {pos}", file=sys.stderr, flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
