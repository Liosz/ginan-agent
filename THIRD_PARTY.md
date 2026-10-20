# 第三方组件与许可证

本项目的代码以 **Apache-2.0** 发布（见 [LICENSE](LICENSE)）。
但本项目**依赖、调用或引用**了以下第三方成果，它们的许可证各自独立。

---

## ⚠️ 最重要的一条：我们不分发 Ginan

**Ginan**（[GeoscienceAustralia/ginan](https://github.com/GeoscienceAustralia/ginan)）
是 Geoscience Australia 开发的 GNSS 分析软件，也是本项目的核心依赖。

| 项 | 说明 |
|---|---|
| Ginan 自身许可证 | Apache-2.0 |
| **但它内含** | **JPL Ephemeris（GPL-3）** |
| 我们的做法 | **以子进程方式调用** `pea` 可执行文件 |
| 我们**不**做 | 不捆绑、不再分发、不修改 Ginan 的任何二进制 |

### ⚠️ 但**配置文件我们打包了** —— 这两件事必须分清

从 2026-09-24 起，`ginan_agent/ginan/example_configs/` 里带着 Ginan 官方仓库
`source/exampleConfigs/` 下的 **18 个 YAML 配置**（逐字节副本，共 106 KB）。

| 项 | 说明 |
|---|---|
| 它们是什么 | Ginan 自己的**配置文本**（`pod_example.yaml`、`ppp_example.yaml` 等） |
| 许可证 | **Apache-2.0** —— 和 Ginan 仓库的声明一致 |
| 为什么可以打包 | **GPL-3 那条约束针对的是二进制**（它静态链了 JPL Ephemeris），不是文本 |
| 为什么需要它们 | POD 的配置必须以官方示例为模板 —— 手写那份缺了半张骨架，实测直接崩 |

⚠️ **界线在这里：`tables/DE436.1950.2050` 那种数据文件我们不打包。**
   配置里**引用**它是可以的（用户自己下），打包它就是另一回事了。

⚠️ 每个文件的 sha256 记在 `ginan/examples.py` 的 `EXAMPLE_SHA256` 里，
   守卫（`scripts/check_toolchain.py::test_example_configs_are_present_and_unmodified`）
   会核对 —— 改它们必须是**有意识**的。

### ⚠️ 还有一份随包的**第三方 JS**：`frontend/plotly.min.js`

从 2026-09-29 起，`ginan_agent/frontend/plotly.min.js` 也在仓库里（4.59 MB）。

| 项 | 说明 |
|---|---|
| 它是什么 | **plotly.js**，画结果图用的（plotly 的浏览器端） |
| 版本 | plotly **7.1.0** 随包的那份（`plotly/package_data/plotly.min.js`） |
| sha256 | `3b6e15d45dbb7fca5bd2094291e961ddc5472cd887009e6009a56dab668d721f` |
| 许可证 | **MIT** —— 可以随包分发 |
| 为什么**必须**随包 | 这个程序**离线可用**。用 CDN 的话，断网/内网环境下图全都不出来，而这个程序的主场景之一正是内网 |

⚠️ **为什么不自己手画**：早先试过手画 SVG 折线（约 60 行），
   但科学作图要的东西（误差棒、置信带、rangeslider 缩放、hover 读数、
   导出 PNG）手画全部要重来一遍，而且做不齐。see `docs/ginan-facts.md` 七点二十八。

⚠️ 它是**从已安装的 plotly 包里拷出来的**，不是从网上下载的 ——
   这样版本和 Python 端严格一致，也有出处可查。

### 为什么这条必须在文档里反复强调

因为「把 `pea.exe` 打包进 release，用户下载即用」是个**非常有诱惑力**的改进，
将来很可能有人（包括我自己）顺手就想做。

**但不能做。** 理由：

Ginan 的发行版里打包了 JPL Ephemeris，而 JPL Ephemeris 是 **GPL-3**。
GPL-3 是**强传染性**许可证 —— 一旦我们再分发包含它的二进制，
整个作品就要以 GPL-3 发布，而 GPL-3 与我们的 Apache-2.0 不兼容。

> 这一条已经记入 [docs/decisions.md](docs/decisions.md) 的 ADR-001，
> 目的是让**将来的人**（包括三个月后的我自己）不会随手破坏它。

### 那用户怎么拿到 Ginan？

**替用户下载**是可以的 —— 那份副本是用户自己的，不由我们分发。

所以设置界面提供的是「**下载 Ginan**」按钮，而不是内置一份。

### 调用而非链接，是这条界线的技术基础

```
我们的进程  ──subprocess──▶  pea.exe  ──▶  独立的地址空间
```

因为两个程序是**各自独立的进程**，不构成"衍生作品"，
所以我们的许可证不受 Ginan 的约束。

⚠️ 反过来说：如果有人把它改成 `import ginan` 或者静态链接，
那条界线就跨过去了。**这是 ADR-001 要守住的真正边界。**

---

## 其他第三方组件

| 组件 | 许可证 | 用途 |
|---|---|---|
| [openai-python](https://github.com/openai/openai-python) | Apache-2.0 | LLM 客户端（DeepSeek 提供 OpenAI 兼容接口） |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | HTTP 传输，`openai` 的依赖 |
| [requests](https://github.com/psf/requests) | Apache-2.0 | 数据产品下载 |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | 生成/解析 Ginan 的 YAML 配置 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | BSD-3-Clause | 从 `.env` 读取配置 |
| [NumPy](https://github.com/numpy/numpy) | BSD-3-Clause | 结果解析与统计 |
| [pywebview](https://github.com/r0x0r/pywebview) | BSD-3-Clause | 桌面窗口（计划中） |
| [Plotly](https://github.com/plotly/plotly.py) | MIT | 结果图表（服务端拼图；前端用随包的 `frontend/plotly.min.js`） |

---

## 数据来源

本项目从以下机构获取公开的 GNSS 数据产品。**数据本身有各自的使用条款**，
本项目的许可证**不覆盖**这些数据：

| 来源 | 提供 | 是否需要账号 |
|---|---|---|
| [Geoscience Australia GNSS 数据门户](https://data.gnss.ga.gov.au/) | 澳大利亚测站 RINEX 观测数据 | 否 |
| [NASA CDDIS](https://cddis.nasa.gov/) | IGS 精密轨道、钟差、偏差 | 是（Earthdata 账号） |
| [IERS](https://datacenter.iers.org/) | 地球定向参数 | 否 |
| [NOAA NCEI](https://www.ncei.noaa.gov/) | 地磁模型系数 | 否 |
| [TU Wien](https://vmf.geo.tuwien.ac.at/) | 对流层延迟格网 | 否 |
| [ICGEM](http://icgem.gfz-potsdam.de/) | 地球重力场模型 | 否 |

> 使用这些数据发表成果时，请按各数据中心的引用要求致谢。
> 引用 Ginan 本身请用其 [README](https://github.com/GeoscienceAustralia/ginan#how-to-cite) 中给出的格式。

---

## 致谢

感谢 Geoscience Australia 把 Ginan 开源 ——
没有它，这个项目根本不会存在。
