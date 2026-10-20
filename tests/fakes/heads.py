"""真实文件的头部字节 —— 造测试文件时用。

============================ 为什么要有这个文件 ============================
识别、认领、清理这些逻辑**都看文件内容**，所以造测试文件时必须有
"长得像真的"的头部。而**这段字节必须来自真实文件** —— 编一个看起来
合理的组合，测试会通过，真实文件却认不出来。

⚠️ 这个文件是从 `scripts/check_files.py` 里搬出来的，因为它现在
   **两个自检脚本都要用**（check_files 测识别，check_server 测"新任务
   该删什么"）。各存一份的话，改了一处忘了另一处 ——
   而"两边不一致"的后果是**一边认得出来、另一边认不出来**。

============================ 每个头部的出处 ============================
⚠️ 这是这个文件里最重要的信息。**没有出处的字节等于编的。**

    obs   ✅ 用户自己的 DCMS 文件
             （userdata/data/dcms260101000000.rnx，由 RinexConvert 1.5.2 生成）
             ⚠️ 注意它**没有** `O:` 前缀 —— 那是转换器的写法，真实存在。

    nav   ✅ 实测从 BKG 下载的
             BRDC00IGS_R_20260010000_01D_MN.rnx（753 KB → 解压 12.4 MB）

    sp3 / erp / clk / atx
          ✅ 实测下载到的真实文件头部

============================ ⚠️ 改过一次的教训 ============================
原来那条 `nav` 是**编的**，注释却写着"从实测文件里摘的" ——
**那比没有注释更糟，因为它让下一个人相信它。**
自检因此放过了一个真实文件的识别失败（见 check_files 里的
`test_rinex_version_variants`）。

👉 所以规矩是：往这个字典里加东西，**必须写出它是从哪个文件来的**。
"""

from __future__ import annotations

REAL_HEADS: dict[str, bytes] = {
    "sp3": b"#cP2026  9  2  0  0  0.00000000      96 ORBIT IGc20 HLM  IGS\n",
    "erp": b"version 2\nSource: Xpole,Ypole,Xrt,Yrt,LOD: weighted average\n",
    "clk": (
        b"     3.00           C                                       "
        b"RINEX VERSION / TYPE\n"
    ),
    "atx": b"     1.4            M                                       ANTEX VERSION / SYST\n",
    "obs": (
        b"     3.04           OBSERVATION DATA    M: Mixed            "
        b"RINEX VERSION / TYPE\r\n"
        b"RinexConvert 1.5.2  SINOGNSS            20260914 021901 UTC "
        b"PGM / RUN BY / DATE \r\n"
    ),
    "nav": (
        b"     3.04           N: GNSS NAV DATA    M: MIXED            "
        b"RINEX VERSION / TYPE\n"
    ),
    "sinex": b"%=SNX 2.00 AUS 26:245:00000 AUS 26:245:00000 26:245:00000 00000000\n",
    #   bia   ✅ 实测：用户那份 COD0OPSRAP_20262450000_01D_01D_OSB.BIA
    #            的前 140 字节（2026-09-21 从 userdata/data/ 直接读）
    "bia": (
        b"%=BIA 1.00 COD 2026:246:36196 IGS 2026:245:00000 2026:246:00000 "
        b"A 00007139\n"
        b"*----------------------------------------------------------------"
    ),
    #   met   ✅ 实测：用户那箱气象文件之一
    #            HKWS00HKG_R_20262440000_01D_MM.rnx.gz **解压后**的前 200 字节
    #            （2026-09-21）
    #         ⚠️ **保留 CRLF** —— 那份是 CRLF 的，夹具要贴真实。
    #            识别不能因为行尾不同就认不出来。
    "met": (
        b"     3.02           METEOROLOGICAL DATA"
        b"                     RINEX VERSION / TYPE\r\n"
        b"Spider v7.5.1.8670  SMO                 20260902 005952 UTC "
        b"PGM / RUN BY / DATE\r\n"
        b"Measurements recorded in GPS time    "
    ),
}


# =============================================================================
# 同一类型的**其它写法**
# =============================================================================
#
# ⚠️⚠️ **不要往 `REAL_HEADS` 里塞这些** —— 那个字典的契约是
#     **"键就是 kind"**，而 `check_files.test_identify_by_content`
#     正是照这个契约写的（遍历它、断言 `identify(…).kind == 键`）。
#
#     我第一版就把 ESA 那个变体塞了进去（键叫 `erp_esa`），
#     于是那条断言当场变红 —— 而且红得**没道理**：
#     文件本身没问题，是它**放错了地方**。
#
#     👉 所以变体单独放。**"同一个类型有几种写法"**这件事本身要看得见，
#        而不是混进"类型清单"里冒充一种新类型。

# ✅ ERP 的第二种写法：ESA 用的是**大写** `VERSION 2`。
#
# ⚠️ 实测来源：`ESA0OPSFIN_20262450000_01D_01D_ERP.ERP.gz`
#    （navigation-office.esa.int，230 字节 gzip，解压后以 `VERSION 2` 起头）。
#
# ⚠️ 少了它，识别规则只认小写那种 → ESA 的 ERP 被丢弃 →
#    现象是"ERP 下载失败"，而真正的原因在读文件的那几行规则里。
#    和 nav 那次的教训一模一样：**一种格式的两种写法，必须都列上。**
ERP_ESA = b"VERSION 2\n"
