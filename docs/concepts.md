# 概念速查索引 🛰️

> 这份文件是**索引**，不是教科书。
>
> **从"现象"反查"概念"** —— 你遇到了一个奇怪的现象，
> 在这里查到它叫什么、为什么会这样、去哪儿看详细解释。
>
> ⚠️ 这里原来挂着一个链接，地址是 `https://github.com/` ——
> **它指向 GitHub 首页，哪儿也不是**。所以把链接去掉了，
> 只留这句话本身要说的事。
>
> ⚠️ **单一内容源原则**：概念的长篇讲解会写在专门的文档里（见下方"文档地图"），
> 这份文件只做导航和速查。同一份知识写两遍，迟早会不一致。

---

## 文档地图

⚠️ **开发文档和用户文档分开放**，因为打包时要排除前者：

| 位置 | 给谁看 | 打包？ |
|---|---|---|
| `dev/` | 开发和维护者（**内部笔记，不随发布提供**） | ❌ **不打包**（见 [.packinclude](../.packinclude)） |
| `docs/` | 开发和维护者 | ❌ 不打包 |
| 根目录的 `README.md` | **用户** | ✅ 打包 |

| 文档 | 内容 | 状态 |
|---|---|---|
| **[decisions.md](decisions.md)** | **架构决策记录（ADR）—— 为什么选 X 不选 Y** | ✅ 现在 |
| **concepts.md**（本文） | 概念速查索引 —— 现象反查 | ✅ 现在 |
| [ginan-facts.md](ginan-facts.md) | **Ginan 实测事实** —— 数据源可达性、可用的 URL、最小文件集、`--dry-run` 真实语义、四条取数路径 | ✅ 现在 |
| [THIRD_PARTY.md](../THIRD_PARTY.md) | 第三方组件与许可证 —— ADR-001（绝不打包 Ginan）的**合规依据** | ✅ 现在 |
| ~~architecture.md~~ | ~~分层与依赖规则~~ | 🚧 还没写（分层铁律记在 [decisions.md](decisions.md) 的 ADR-004） |
| ~~reproducibility.md~~ | ~~复现包格式与如何重放~~ | 🚧 还没写（M6） |

> ⚠️⚠️ **这一张表本身就是"指向不存在文件"的活教材，值得留着这一段。**
>
> 它最早有一行写着 `decisions.md` ✅ 现在 —— 而那个文件在磁盘上**不存在**。
> 一份**指向不存在文件**的索引，比没有索引更糟：它让人以为"想看 ADR 就去那儿"，
> 于是**不会去别处找**，而 ADR 当时其实写在 `dev/00-项目全貌.md` 第八节里。
>
> 后来准备公开发布，`dev/` 不随仓库走 —— 那 14 处指向 `dev/` 的链接
> **同时**变成了死链，正是同一个毛病。
>
> 👉 所以做了两件事，而且**第二件比第一件重要**：
>    1. 把 ADR 抽出来放进 [decisions.md](decisions.md)，链接全部改指它
>    2. `scripts/check_paths.py` 加了一条守卫：**文档里的相对链接必须指向
>       存在的文件** —— 否则这种死链只会再次出现，而且没人会发现
>
> （`architecture.md` / `reproducibility.md` 标的是 🚧 —— 那是"还没写"，
> 和"标着 ✅ 却没有"是两回事，那两行保留原样。）

---

## 按主题查

### AI / Agent 侧

| 主题 | 概念 | 相关代码 |
|---|---|---|
| agent 的心脏 | 四步循环：记下来 → 判断 → 执行 → 回填 | [loop.py](../ginan_agent/core/loop.py) |
| 模型怎么用工具 | Function Calling · tool_calls 的 arguments 是**字符串**不是对象 | [types.py](../ginan_agent/core/types.py) |
| 流式输出 | 生成器 · 事件流 · SSE · `yield from` 转发 | [provider.py](../ginan_agent/core/provider.py) |
| **长任务的进度** | ToolContext · emit 旁路出口（生成器执行中不能 yield） | [tools.py](../ginan_agent/core/tools.py) |
| **取消语义** | 协作式取消 · `generator already executing` · 子进程要 `terminate()` | [loop.py](../ginan_agent/core/loop.py) |
| 可靠性与韧性 | 重试策略 · 指数退避 · **抖动** · 幂等性 · 静默的错误 | [provider.py](../ginan_agent/core/provider.py) |
| 上下文与记忆 | 上下文窗口 · 无状态 · Lost in the middle · Prompt Caching | [provider.py](../ginan_agent/core/provider.py) |
| **上下文压缩** | 迟滞（两个阈值）· 切点不能切开工具调用配对 | [loop.py](../ginan_agent/core/loop.py) |
| 安全 | Prompt Injection（数据里藏指令）· 白名单优于黑名单 | [ginan/](../ginan_agent/ginan/) |
| 成本 | 每轮重发全部历史 → 平方级增长 | — |

### Ginan / GNSS 侧

| 主题 | 概念 | 关键事实 |
|---|---|---|
| 配置规模 | 8 个顶层段 · **3000+ 个选项** | 最小示例 311 行 |
| 输入文件 | 文件清单 · `inputs_root` / `gnss_observations_root` | 路径相对 **CWD** 解析 |
| **模型文件 `tables/`** | 上游列了 **34 个**（`products.list` 341–374 行），**不在仓库里**；我们只用其中的 **10 个**（`catalog.TABLES`） | 源是 Ginan 官方 S3 的**全球端点** —— **直连可达，不需要代理**（⚠️ 官方脚本用的区域端点才不通） |
| 预检 | `--dry-run` · `checkValidFiles` | ⚠️ 退出码**不覆盖** sanity checker |
| **静默改写** | 6 个 sanity checker 里 **4 个**会**改配置** | 只发 warning 级日志 |
| 输出格式 | `.POS`（定宽文本）· `.TRACE`（可上 GB）· `$POS`/`$CLK`/`$TROP` | 见 Ginan 源码里的 `Docs/peaUser.md`（不在本仓库） |
| 运行模式 | PPP · POD · RTK · SBAS · SLR · LEO | `process_modes` |
| 数据来源 | GA（免账号）· CDDIS（需账号）· IERS · S3 | 见 [ginan-facts.md](ginan-facts.md) |

---

## 遇到这些现象，去查这些概念

**这是本文档最有用的一张表。**

### A. AI 表现不对

| 你遇到的现象 | 该懂的概念 | 说明 |
|---|---|---|
| 账单涨得比预期快 | 成本平方级增长 · Tokenization | 每轮重发全部历史 |
| 同样的问题问两遍，答案不一样 | 非确定性 · temperature | agent 用 0，要的是可靠不是惊喜 |
| 模型自信地说了一件错事 | 幻觉 | GNSS 术语**要从项目自己的资料里核对**（`docs/ginan-facts.md` 的实测事实、`ginan/diagnostics.py` 的规则表、Ginan 源码里的 `Docs/peaUser.md`），而不是即兴发挥。⚠️ 这里原来写的是「靠 `glossary.py`」—— **全仓库没有这个文件**，那是一个不存在的护栏 |
| 模型不知道今天几号 | 知识截止日期 | 所以"取时间"是个正当的工具 |
| 加了工具它却不用 | description 写法 | 说清"**什么时候该用**"比说清"它是什么"更有用 |
| 工具参数被截断、JSON 不完整 | `finish_reason == "length"` | 见下方"截断防护" |
| 聊久了变慢变贵 | 上下文窗口 · Prompt Caching | 到 75% 自动压缩到 35% |
| 重要指令模型没遵守 | Lost in the middle | 关键约束放系统提示里，且要短 |
| **工具明明注册了，模型却说"没这个工具"** | **声明处与执行处不一致** | ⚠️ 见下方"缺陷 #1" |
| **点了"停止"，程序抛 ValueError** | `generator already executing` | ⚠️ 见下方"取消" |
| **拒绝了工具调用，下一轮 API 报错** | 孤儿 `tool_call` | ⚠️ 见下方"孤儿调用" |

### B. Ginan 跑不起来

| 你遇到的现象 | 该懂的概念 | 说明 |
|---|---|---|
| `Missing <描述> file <路径>` | `checkValidFiles` | 25 类输入文件逐个检查，**相对 CWD** |
| 配置里的值和我写的不一样 | **sanity checker 静默改写** | 4/6 个 checker 会改配置，只发 warning |
| `--dry-run` 说 successful，但配置有问题 | 退出码的真实含义 | ⚠️ 见下方"dry-run 语义" |
| 下载脚本跑完了，但什么都没下到 | `getFiles.sh` 无错误检查 | 依赖 `wget`+`gzip`，Windows 默认没有 |
| 找不到 `tables/xxx` | 模型文件不在仓库里 | 只能下载；源**直连可达**（全球端点），但得先让 `fetch_data` 带上 `with_tables=true`，而且**配置里必须写出那些键** —— 光下到磁盘上 pea 一个都不会打开，也不报错 |
| CDDIS 一直失败 | Earthdata 账号 + `.netrc` | Windows 是 `_netrc`（**下划线开头**） |
| `-q` 之后什么都没看到 | `-q` 只隐藏 info | 要藏警告得用 `-Q` |
| 输出文件写到 2GB 就坏 | Windows 的输出大小上限 | 约等于"两站一天 30 秒采样" |
| 在别的目录跑就报缺文件 | CWD 硬约束 | 所有示例必须从 `exampleConfigs/` 跑 |
| 想跑 POD 却要求 MongoDB | `mongo: enable: primary` | `ppp_example.yaml` 是 `none`，安全 |

---

## 三个我们踩过的坑（含源码位置）

### 缺陷 #1 —— 声明处和执行处不一致

**现象**：模型看到了工具、也调用了，但执行时报"没有这个工具"。

**原因**：两个地方各自看都对，连起来才错。

```
loop.py:656   result = execute(name, arguments)      ← 用参数传进来的工具集了吗？没有
tools.py:267  tool = TOOLS_BY_NAME.get(name)         ← 查的是模块级全局表
```

**为什么难查**：`Agent(tools=[...])` 看起来就是"挂载自定义工具"的写法，
而且 schema 确实发对了（模型看得见），只有在**执行那一刻**才暴露。

**教训**：`声明` 和 `执行` 必须读**同一份状态**。
凡是"传了参数但有别的地方读全局"的地方，都有这个风险。

**守卫**：[check_registry.py](../scripts/check_registry.py) —— 测完 schema 还要测**执行**。

---

### 取消 —— `generator already executing`

**现象**：从界面点"停止"，抛 `ValueError: generator already executing`。

**原因**：循环层是个生成器。取消时它多半正卡在 `stream.__next__()` 里等网络 ——
此时它是**正在执行**状态，对一个正在执行的生成器调 `close()` 是非法操作。

**解**：取消必须是**协作式**的 —— 外部升起一面旗子，生成器在下一个 yield 处自己检查。

```
❌  gen.close()          ← 正在执行时抛 ValueError
✅  cancel_event.set()   ← 生成器自己检查，安全
```

**⚠️ 但协作式取消打断不了子进程。** 一个跑 10 分钟的 `pea` 不会因为你查了旗子就停。
所以 `ToolContext.cancel` 必须一路传到底，真的去 `Popen.terminate()`。

**正确的实现必须同时有两层**，缺一不可。

---

### 孤儿 tool_call —— 只在"拒绝/取消"时才复现

**现象**：用户拒绝了工具调用（或中途取消），**下一次**请求被 API 拒掉。

**原因**：带 `tool_calls` 的 assistant 消息**已经记进历史了**，
但对应的 tool 回复没补上 → 历史里留下"有调用、没结果"的孤儿。

**API 的硬性要求**：每个 `tool_call` 必须**恰好**一条回复。少一条直接报错。

**为什么难查**：平时完全正常，只在用户点"拒绝"或"停止"时坏。

**⚠️ 反直觉的地方**：被拒绝 ≠ 不记录。
"没执行"这件事本身也要作为一条 tool 消息记下来。

```
for call in tool_calls:
    if 被拒绝:
        record(tool_result_message(call.id, "用户拒绝了"))   ← 这一句不能省
        continue                                            ← 是 continue，不是 return
```

**守卫**：[check_core.py](../scripts/check_core.py) 的
`取消后历史仍然合法：N 个调用 / N 条回复`。

---

## Ginan 的两个反直觉语义

### `--dry-run` 的成功语是误导性的

```cpp
void ACSConfig::sanityChecks() {
    sanityManager.runAllChecks(*this);   // ← 返回值被丢弃了（函数是 void）
}

bool pass = configure(argc, argv);       // ← 只看 checkValidFiles
if (acsConfig.dry_run) {
    "Dry-run successful: configuration and sanity checks passed"   // ← 并不可信
}
```

**退出码 0 只保证两件事**：YAML 能解析 ✓、所有声明的输入文件都在 ✓。

**它完全不保证** sanity checker 通过。而且 checkers 里有 4 个会**静默改写配置**
（只发 warning 级日志）。

**推论**：`--dry-run` 之后必须**自己解析 warning 行**，否则用户永远不知道
自己的配置被悄悄改过。这是我们要补的第一层价值。

---

### 「绝对路径」是绕过一堆坑的钥匙

`conditionalPrefix()` 的逻辑是：**路径里含 `:` 或是绝对路径 → 原样返回，不加前缀**。

于是 `inputs_root` 和 `gnss_observations_root` 都可以写成绝对路径，
**完全不需要 `exampleConfigs/` 下面那三个软链接，也不需要 CWD 在特定位置**。

这一条把三个坑一次绕过了：
- macOS/Linux 上是软链接、Windows 解压后变成 0 字节文件的问题
- "必须在 `exampleConfigs/` 里跑"的约束
- "不能写 `ginan-dist/source/` 目录"的洁癖需求

> **通用教训**：遇到一堆"环境要求"时，先找找有没有一个**配置项**能把它们一起消掉，
> 而不是逐个去满足。

---

## 关键数字速查

| 数字 | 含义 | 在哪 |
|---|---|---|
| **3000+** | Ginan 的配置选项总数 | `ginanConfiguration.md` |
| **8** | YAML 顶层段数 | `acsConfig.cpp` |
| **311** | 最小示例 `ppp_example.yaml` 的行数 | — |
| **34** | `tables/` 模型文件数 | `products.list` 341–374 行 |
| **25** | `checkValidFiles` 检查的输入类别数 | `acsConfig.cpp:4454` |
| **6 / 4** | sanity checker 总数 / 会**改配置**的个数 | `ConfigSanityManager.cpp:61-66`（注册处）+ 各 checker 的 `check()`（是否写 config） |
| **2.1 GB** | Windows 输出文件大小上限 | README |
| **64_000** | 默认上下文窗口（token） | `provider.py` |
| **75% → 35%** | 自动压缩的触发线 → 目标 | `loop.py` |
| **8** | 压缩时至少保留的最近消息条数 | `loop.py` |
| **没有上限** | 一轮最多循环几步 —— **这个上限已经删掉了** | `loop.py`（原为 10，见下方说明） |
| **8_000** | 单个工具结果的字符上限 | `tools.py` |

> ⚠️ **上面这两处原来是错的，改过了 —— 记一下错在哪：**
>
> - **`6 / 2` → `6 / 4`**。`ConfigSanityManager::defaultManager()` 注册 **6** 个
>   （`ConfigSanityManager.cpp:61-66`），其中**会写 config 的是 4 个**：
>   `EpochToleranceChecker`（砍 `epoch_tolerance`）、
>   `IonosphericFreeComboChecker`（关 `ionospheric_component2/3`）、
>   `RequiredSiteEccentricityChecker`（打开偏心改正）、
>   `SbasSanityChecker`（只在 SBAS 模式下动）。
>   另外两个（`IonosphericOutageChecker` / `EphemerisTimeDelayChecker`）
>   **只发 warning，不写**。
>
>   ⚠️ 原来那个「2」**在本文档里就自相矛盾**：B 组"Ginan 跑不起来"那张表
>   写的是「4/6 个 checker 会改配置」、底下「`--dry-run` 的成功语是误导性的」
>   那一节写的也是 4 —— **只有速查表里是 2**。
>   **三个地方说同一件事，错的那个一定是最不容易被读到的那个**，
>   而速查表恰恰是给人快速取数的地方。
>   （⚠️ 这里原来写的是"第 60 行 / 第 192 行" —— 那种指法**自己就会过期**：
>   加了二十几行字之后行号全变了。**引用位置要指向内容，不要指向行号。**）
>
> - **「一轮最多循环几步 = 10」→ 没有上限**。`loop.py` 里那个
>   `DEFAULT_MAX_STEPS = 10` 已经删掉了，原地留了一大段说明**为什么删**：
>   上界由**用户**给（停止按钮），而不是由步数给 —— 撞上限的失败方式
>   是"做到一半停下，等你再说一句'继续'"，那对一个说人话的工具来说是坏体验。

---

## 我们做过的判断（一句话版）

详细的记录在 [decisions.md](decisions.md)，这里只列结论，方便快速回忆。

| 判断 | 理由 | ADR |
|---|---|---|
| 核心层**换成对象而不是全局变量** | 全局状态让"传参"和"执行"读的不是同一份 | — |
| 取消用**协作式**而非 `gen.close()` | 正在执行的生成器不能被 close | — |
| 工具结果**必须截断** | `.TRACE` 能到 GB 级，会撑爆上下文 | — |
| **不捆绑 Ginan 二进制** | 它内含 GPL-3 的 JPL Ephemeris，捆了就传染 | [ADR-001](decisions.md#adr-001绝不打包-ginan-二进制) |
| 界面选 **pywebview** 而不是 Qt | 事件流→SSE 天然契合，前端代码将来还能复用于纯 Web | [ADR-002](decisions.md#adr-002界面用-pywebview-而不是-qt) |
| 首个运行 profile **故意做小** | 只依赖实测可达的源，绕开堵住的 | [ADR-003](decisions.md#adr-003源排序由实测决定不由猜测决定) |
| 默认数据源用 **GA 而不是 CDDIS** | GA 免账号、可达，且示例测站本来就是澳站 | [ADR-003](decisions.md#adr-003源排序由实测决定不由猜测决定) |
| 网络层**做厚**（代理+诊断+多源回退） | 目标用户是国内学生，这是刚需 | [ADR-004](decisions.md#adr-004核心层不懂-ginan) |

> ⚠️ 这些 ADR 的链接曾经指向 `decisions.md` —— 而**那个文件当时不存在**。
> 后来它们改指 `dev/00-项目全貌.md` 第八节（那份是内部笔记）。再后来准备公开发布、
> `dev/` 不随仓库走，于是**又一次**全部变成死链。
> 现在 ADR 就住在 [decisions.md](decisions.md) 里，链接直接指它。
> （ADR-001 的**合规依据**另见 [THIRD_PARTY.md](../THIRD_PARTY.md)。）
>
> **一个指向不存在文件的链接，会让人以为"这里没有记录"**，
> 然后自己去重造一份 —— 那比重造之前先读到它要糟得多。
> 所以 `scripts/check_paths.py` 里现在有一条守卫专门盯这件事。

---

## 怎么用这份文档

1. **遇到了怪现象** → 查"遇到这些现象"两张表
2. **想知道某个数字从哪来** → 查"关键数字速查"
3. **想知道当初为什么这么选** → 查"我们做过的判断"，再跳 [decisions.md](decisions.md)
4. **想改代码之前** → 先看对应模块顶部的注释，那里写了**边界**（坚决不做什么）

> ⚠️ 最后一条尤其重要。每个模块顶部的"边界"不是装饰 ——
> 它们是**依赖方向**的守卫。比如 `tools.py` 里写着"不允许 print()"，
> 这条规则是 `core/` 能脱离命令行、跑在无窗口桌面进程里的前提。
> 破坏它不会立刻报错，但会让整个分层慢慢烂掉。
