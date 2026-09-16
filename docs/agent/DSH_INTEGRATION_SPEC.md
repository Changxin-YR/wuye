# dsh（DeepSeek Harness）集成规格 —— 实跑验证版

> 本文档是 `agent/bridge.py` / `agent/runtime.py` / `agent/mcp_server.py` 的实现依据。
> **harness 源码一个字节都没有改**：所有定制都通过 `dsh --profile sdk --patch <文件>` 的官方覆盖机制完成。
>
> **证据分级**：每条结论都标注
> - 【实跑】= 在本机真实运行过，命令/输出原文见 `artifacts/agent-spec/`；
> - 【源码】= 读自 harness 源码或安装包内联源码（附文件与行号）；
> - 【推断】= 由前述两条推导，未单独实跑。
>
> 验证时间：2026-09 ｜ 项目：`<PROJECT_ROOT>`

---

## 0. 环境基线

| 项 | 值 | 来源 |
| --- | --- | --- |
| dsh 版本 | `0.1.5-rc.1` | 【实跑】`deepseek-harness-sdk-runtime-win-x64.exe --version` → `0.1.5-rc.1` |
| 运行时载体 | 单文件 exe（pkg 打包 Node） | 【实跑】`<venv>\Lib\site-packages\deepseek_harness_runtime\runtime\deepseek-harness-sdk-runtime-win-x64.exe`（229 MB） |
| Python SDK | `deepseek-harness-sdk 0.1.5rc1` | 【实跑】`pip list` |
| 运行时包 | `deepseek-harness-runtime-bin 0.1.5rc1` | 【实跑】`pip list` |
| Python | 3.14.4（venv：`<PROJECT_ROOT>\.venv\Scripts\python.exe`） | 【实跑】`python -V` |
| MCP（Python 侧） | `mcp 2.2.0`（**FastMCP 已改名 `MCPServer`**） | 【实跑】`from mcp.server.mcpserver import MCPServer` |
| 源码快照 | `<DEEPSEEK_HARNESS_SRC>`，版本 **`0.1.2-alpha.5`** | 【源码】该目录 `package.json` |

> ⚠️ **快照版本落后于运行时**（0.1.2-alpha.5 vs 0.1.5-rc.1）。凡涉及「字段名 / 配置 schema」的结论，
> 本文一律以**运行时实跑与运行时内联源码**为准；快照源码只用于解释 patch 算法。
> 这一点已经造成过一个真实事故，见 [§2 陷阱 G](#陷阱-gpersona-字段已被移除会静默失效)。

启动参数（【源码】`python/sdk` 内 `client.py:458-486`）：

```text
<dsh_bin> --profile sdk --patch <绝对路径1> --patch <绝对路径2> ...
```

- `dsh_bin` 不传时用 `deepseek_harness_runtime.resolve_bundled_launch_args()` 解析出上面那个 exe；
- `dsh_home` **必须显式给**（SDK 拒绝隐式使用 `~/.dsh`），封装成环境变量 `DSH_HOME`；
- `patches` 一律转成绝对路径后跟随 `--patch`，**保持传入顺序**；
- API Key / Base URL 通过 `api_key` / `base_url` 参数或环境变量 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` 注入。

---

## 1. `--patch` 覆盖文件语法与语义

### 1.1 启动器参数（【实跑】`--help` 原文）

```text
Options:
  -V, --version                  output the version number
  --profile <name>               the profile under $DSH_HOME/profiles to boot
  --from-default-profile <name>  initialize a new custom profile from a shipped profile template
  --patch <path>                 extra patch-list overlay applied after the profile layer (repeatable)
  --dump-config                  print the composed profile tree and exit
  --dump-default-config          print the profile tree without its user layer or --patch overlays and exit
```

互斥与约束（【源码】快照 `apps/cli/src/args.ts:83-103`）：

- `--dump-config` 与 `--dump-default-config` **互斥**；
- `--dump-default-config` **不接受** `--patch`；
- 两个 dump 都**不接受** profile 的应用参数（`dsh --profile sdk --dump-config "hi"` 直接报错）；
- `--patch` 是「单值可重复」，**不是变参**（变参会吞掉后面给 app 的参数）。

### 1.2 文件格式

顶层是 **YAML 数组**（JSON 数组同样可解析），每个元素是一条 patch：

```yaml
# ① 覆盖已有行：按 id 定位
- id: <已存在行的 id>
  name: '@deepseek-ai/xxx'      # 可选：断言，写错就跳过并告警
  config: { ... }               # 覆盖字段（整字段替换，见 1.4）
  disabled: true                # 覆盖字段
# ② 新增行：patch 自身不能带 id，必须用 insert
- insert:
    - id: <新行 id>
      name: '@deepseek-ai/dsh-mcp-client'
      config: { ... }
```

解析用 loader 自己的 YAML 方言（【源码】快照 `vendor/include/src/index.ts:9-25`）：
`JSON_SCHEMA` + 自定义 `!!js` 标签，`!!js` 标量在**行激活时**由 Loader 求值，例如：

```yaml
disabled: !!js process.platform === 'win32'
```

【实跑】`--dump-config` 会**原样打印** `!!js` 表达式而不求值，`--dump-default-config` 也一样：

```yaml
- id: tool-pwsh
  name: '@deepseek-ai/dsh-tool-pwsh'
  disabled: !!js process.platform !== 'win32'
```

### 1.3 合并顺序（【源码】快照 `apps/cli/src/profile-boot.ts:136-173, 243-251`）

**从低到高**依次应用，后应用的覆盖前面的：

```text
1. bundle 层：按 profile package.json 的 dsh.profile.bundles 顺序
               sdk profile = ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-sdk-app']
2. profile 自己的 $DSH_HOME/profiles/<name>/cordis.patch.yml
3. home 级用户层 $DSH_HOME/cordis.patch.yml（机器级偏好，优先级高于 profile 层）
4. --patch 覆盖层（按 argv 顺序）
5. 遥测硬开关（DSH_TELEMETRY_DISABLED 非空时自动追加一条 disable patch）
```

所有层在**一次** `applyEntryPatches` 调用里按顺序压平应用，
所以「后一层可以配置/禁用前一层 insert 进来的行」（【源码】`profile.ts:854-860` `composeEntries`）。

【实跑】`--dump-config` 的 provenance 注释直接印证了层级顺序：

```text
# == @deepseek-ai/dsh-base
# == @deepseek-ai/dsh-base, patched by @deepseek-ai/dsh-sdk-app
# == @deepseek-ai/dsh-base, patched by C:\...\patches\20-e2e-final.yml
# == @deepseek-ai/dsh-sdk-app
# == C:\...\patches\20-e2e-final.yml
```

### 1.4 patch 语义（【源码】快照 `vendor/include/src/index.ts:58-128`）

```ts
for (const patch of patches) {
  const { id, insert, name, ...overrides } = patch
  if (insert) { /* id 为空 → 追加到顶层；有 id → 必须指向一个 group，追加进它的 config 数组 */ }
  if (!id) { warn('patch: id is required for non-insert patches'); continue }
  const target = entryMap.get(id)
  if (!target) { warn('patch: entry %C not found', id); continue }
  if (name && name !== target.name) { warn('patch: name mismatch ...'); continue }
  for (const [key, value] of Object.entries(overrides)) { if (key === 'id') continue; target[key] = value }
}
```

要点：

| 语义 | 结论 | 证据 |
| --- | --- | --- |
| 覆盖粒度 | **顶层字段整体替换**，`config` **不是深合并** | 【实跑】只写 `config: {personaPrefix: ...}` 后，原有 `includeHarnessIdentity` 等键全部消失 |
| 新增行 | 必须用 `insert`；patch 自身带 `id` 时 `insert` 会被当成「往 group 里追加」，普通行会报 `not found` | 【源码】上表；【实跑】`- insert:` 生效，见 §1.5 |
| 禁用行 | `disabled: true` 就是覆盖该字段，**不删行、不影响该行的其它字段** | 【实跑】`tool-web` 保留 `config`，仅多出 `disabled: true` |
| 未知 id | **只告警 + 跳过**，进程继续启动、退出码 0 | 【实跑】见下 |
| `name` 断言不匹配 | **跳过该条**并告警 | 【实跑】见下 |
| id 匹配范围 | 会递归进 `group: true` 行的 `config` 数组里找 id | 【源码】`buildMap` |

【实跑】未知 id / name 不符的原文（`--dump-config`，`patches/90-patch-semantics.yml`）：

```text
dsh: [C:\...\patches\90-patch-semantics.yml] patch: entry "this-row-does-not-exist" not found
dsh: [C:\...\patches\90-patch-semantics.yml] patch: name mismatch for "tool-todo"
     (expected "@deepseek-ai/dsh-tool-todo", got "@deepseek-ai/dsh-tool-not-what-it-is"), skipping
```

### 1.5 新增行证据

【实跑】patch：

```yaml
- insert:
    - id: probe-inserted-row
      name: '@deepseek-ai/cordis-plugin-timer'
      disabled: true
```

dump 输出（注意 provenance 注释来自 patch 文件本身）：

```yaml
# == C:\...\patches\90-patch-semantics.yml
- id: probe-inserted-row
  name: '@deepseek-ai/cordis-plugin-timer'
  disabled: true
```

### 1.6 `--dump-default-config` 与 `--dump-config` 对比

两条命令都**不启动插件树**（boot-free），只做「空根 + patch 层」合成，
所以 dump 结果与真实启动的树**逐字段一致**（【源码】`apps/cli/src/dump-config.ts:30-52`）。

| | `--dump-default-config` | `--dump-config` |
| --- | --- | --- |
| 包含 bundle 层 | ✅ | ✅ |
| 包含 profile 的 `cordis.patch.yml` | ❌ | ✅ |
| 包含 `$DSH_HOME/cordis.patch.yml` | ❌ | ✅ |
| 包含 `--patch` 覆盖 | ❌（且禁止传） | ✅ |
| 行数（sdk profile） | 352 行 / 86 行 id | 463 行（带我们的 patch） |
| 用途 | 出问题时的**救急诊断**（用户层写坏了也不受影响） | 核对「最终生效的电路」 |

【实跑】两条命令都成功（exit 0）：

```text
deepseek-harness-sdk-runtime-win-x64.exe --profile sdk --dump-default-config   → 352 行
deepseek-harness-sdk-runtime-win-x64.exe --profile sdk --patch 20-e2e-final.yml --dump-config → 463 行
```

**patch 文件写错路径会显式失败**（不是静默忽略）——【实跑】：

```text
Error: dsh: failed to read overlay C:\...\11-minimal-mcp-probe.yml:
       Error: ENOENT: no such file or directory, open '...'
    at loadOverlayPatches (.../dsh-app-boot/lib/index.js:1165:9)
```

### 1.7 需要人工核对的两处「非 fatal」

1. **未知 id 只告警**。我们的禁用清单里如果写错一个 id，dsh 不会失败，只是那一行照旧生效 →
   模型会突然多出一个内置工具。**必须用 `--dump-config` 或工具清单断言来兜底**（见 §4）。
2. **未知 config 字段被静默忽略**，见下面的陷阱 G。

### 1.8 文档推断（未单独实跑，风险低）

- 【推断】patch 文件支持 `.yml` / `.yaml` / `.json`（【源码】`vendor/include/src/index.ts:27-33` 的 `writable` 表）。
- 【推断】`patchReload: startup` 的 profile（sdk / headless / acp 都是）**不热加载** patch 文件；
  每个运行时进程用当时的 patch 文件内容。这正是「一登录用户一进程」模型需要的语义。

---

## 2. 系统提示词覆盖

### 2.1 行与字段

行 id：`system-prompt`，插件：`@deepseek-ai/dsh-system-prompt`。

**0.1.5-rc.1 的 schema**（【实跑+源码】运行时内联源码 `SystemPrompt.static Config`）：

```ts
const SystemPrompt = class extends Service {
  static Config = z.object({
    includeHarnessIdentity: z.boolean().default(true),
    includeRuntimeContext: z.boolean().default(true),
    personaPrefix: z.string().default(''),
    personaSuffix: z.string().default(''),
    toolOrder: z.array(z.string()).default(void 0),
  })
```

语义（运行时包内联 README 原文与实现一致）：

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `includeHarnessIdentity` | `true` | 是否包含固定首句 `You are an AI agent powered by DeepSeek Harness.`（order `-1000`） |
| `includeRuntimeContext` | `true` | 是否把动态运行时快照（文件策略、审批政策等）作为 user 角色消息注入历史 |
| `personaPrefix` | `''` | 部署人设**前缀**，order `0`（在所有第一方指导之前） |
| `personaSuffix` | `''` | 部署人设**后缀**，order `10200`（在所有第一方指导之后） |
| `toolOrder` | 未设置 | 显式工具顺序，必须**恰好包含一个** `'<unlisted-tools>'` 占位项 |

模板变量（【源码】`agent-loop` 内联实现）：

```ts
ctx.systemPrompt.variable('provider', (context) => context.agent?.options.provider)
ctx.systemPrompt.variable('model',    (context) => context.agent?.options.model)
ctx.systemPrompt.variable('cwd',      (context) => context.agent?.session.header.cwd)
```

插值是**严格**的：`{{name}}` 必须是 `[a-z][a-z0-9_]*`，未注册的变量直接抛错让整轮失败
（【源码】运行时内联 `interpolate()`：`unknown prompt variable "{{x}}" ...`）。

### 2.2 是否支持「文件路径」？

**不支持。**`personaPrefix` / `personaSuffix` 只接受**内联文本字符串**（【源码】schema 是 `z.string()`）。
我们自己的做法：运行时读 `agent/config/wuye_agent.md` → 内联进 patch（YAML `|` 块标量），见 `build_patch.py`。

### 2.3 最小可用 patch（真实生产用的那条）

```yaml
- id: system-prompt
  config:
    includeHarnessIdentity: false        # 去掉 “coding agent powered by DeepSeek Harness”
    includeRuntimeContext: false         # 不注入运行时快照（沙箱/审批政策等噪音）
    personaPrefix: |                     # ← agent/config/wuye_agent.md 全文内联
      你是「云邻AI智脑」的智能助手，帮助物业工作人员和业主用自然语言完成日常工作。
      ...
    personaSuffix: ''
```

### 2.4 【实跑】证据

命令：`python artifacts/agent-spec/build_patch.py artifacts/agent-spec/patches/20-e2e-final.yml --persona-file agent/config/wuye_agent.md`
→ `--dump-config` 输出（`artifacts/agent-spec/dump-config-e2e-final.txt:370-423`）：

```yaml
# == @deepseek-ai/dsh-base, patched by @deepseek-ai/dsh-sdk-app, C:\...\patches\20-e2e-final.yml
- id: system-prompt
  name: '@deepseek-ai/dsh-system-prompt'
  config:
    includeHarnessIdentity: false
    includeRuntimeContext: false
    personaPrefix: |-
      你是「云邻AI智脑」的智能助手，帮助物业工作人员和业主用自然语言完成日常工作。
      ...
    personaSuffix: ''
```

真跑一轮后，`session.event` 里的系统提示词**逐字等于**我们的文件（`e2e-final.notifications.json`，len=1191）：

```json
{
  "type": "system/message",
  "data": {
    "message": {
      "role": "system",
      "content": [{"type": "text", "text": "你是「云邻AI智脑」的智能助手，帮助物业工作人员和业主用自然语言完成日常工作。\n\n## 你是谁\n..."}],
      "source": {"kind": "plugin", "plugin": "@deepseek-ai/dsh-system-prompt"}
    }
  }
}
```

### 2.5 陷阱 A：`config` 不是深合并

只写 `config: {includeHarnessIdentity: false}` 会把 `personaPrefix` / `personaSuffix` / `toolOrder`
一起丢掉（变成默认 `''`）。**必须把该行想保留的字段全部写全**。

### 2.6 陷阱 B：`persona` 字段已被移除，会静默失效

- 快照源码（0.1.2-alpha.5）用的是 `persona`；**0.1.5-rc.1 已改成 `personaPrefix` / `personaSuffix`**。
- 【实跑】写 `config: {persona: 'LEGACY_PERSONA_SHOULD_NOT_EXIST'}` ：
  - `--dump-config` 照样把它打印出来（**dump 不做 schema 校验**）；
  - 真跑一轮**不报任何错**，模型收到的系统提示词是默认的
    `You are an AI agent powered by DeepSeek Harness.\n\n...`（首句仍在，因为 `includeHarnessIdentity` 默认 true）。
- 结论：**`persona` 这类已删除字段是静默无效的**，唯一可靠的验证方法是读 `request/header` 的工具清单
  和 `system/message` 的正文（本文所有结论都按这个方式验证）。
- 📌 仓库里 `agent/runtime.py` 的 `build_patch()` 目前仍写 `"persona": persona`，
  属于**已失效字段**，请一并改成 `personaPrefix` + `includeHarnessIdentity: false`。

### 2.7 陷阱 C：内置工具会往系统提示词里插「使用指导」

第一方工具插件会 `ctx.systemPrompt.section({order: 1000+})` 注册工具使用指导。
**只要那一行还启用着，我们的 persona 后面就会跟着一大段英文指导**
（【实跑】保留 25 个内置工具时，系统提示词里出现 `Use the read tool — not shell commands like cat ...` 等 1.5 KB 英文）。

- 想让系统提示词 100% 等于 `wuye_agent.md` → **同时禁用这些工具行**（§4 的清单就是这么做的，实跑 len=1191 完全一致）；
- 只 `includeHarnessIdentity: false` 而不禁用工具行 → 只能是「persona + 一堆工具指导」。

---

## 3. MCP 接入

### 3.1 插件与 schema（【实跑+源码】运行时内联 `Config = z.union([...])`）

行示例：

```yaml
- insert:
    - id: mcp-wuye
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        transport: stdio
        serverName: wuye
        command: <PROJECT_ROOT>\.venv\Scripts\python.exe
        args: ['-m', 'agent.mcp_server']
        env:
          WUYE_AGENT_TOKEN: <短期令牌>
        cwd: <PROJECT_ROOT>
        toolCallTimeoutMs: 60000
        failOnStartupError: true
```

**完整字段表**（两个 transport 是判别联合，字段互斥）：

| transport | 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| 两者 | `transport` | ✅ | — | `stdio` \| `streamable-http` |
| 两者 | `serverName` | ✅ | — | 工具名前缀命名空间；正则 `^[A-Za-z0-9_-]{1,32}$`；同一注册作用域内唯一 |
| 两者 | `toolCallTimeoutMs` | ❌ | `60000` | 单次 `tools/call` 超时 |
| 两者 | `failOnStartupError` | ❌ | `false` | ⚠️ **`true` 时首次连接/工具同步失败会让整个 dsh 启动失败**；`false` 时「harness 正常启动但一个工具都没有」 |
| 两者 | `reconnect.enabled` | ❌ | `true` | 断线自动重连 |
| 两者 | `reconnect.initialDelayMs` | ❌ | `500` | 首次重连延迟，失败翻倍 |
| 两者 | `reconnect.maxDelayMs` | ❌ | `30000` | 退避上限 |
| 两者 | `reconnect.maxAttempts` | ❌ | `10` | 连续失败上限，超了工具会被摘掉 |
| `stdio` | `command` | ✅ | — | 可执行文件（**不经 shell**，不解析引号） |
| `stdio` | `args` | ❌ | `[]` | 参数数组 |
| `stdio` | `env` | ❌ | `{}` | 叠加在**已脱敏**的父进程环境之上 |
| `stdio` | `cwd` | ❌ | `''` | 子进程工作目录；留空时由 MCP SDK 决定（实践中不要留空） |
| `streamable-http` | `url` | ✅ | — | MCP 端点 |
| `streamable-http` | `headers` | ❌ | `{}` | 附加请求头 |

### 3.2 工具命名规则（【实跑】）

> `mcp__<serverName>__<rawName>`

- 【实跑】`serverName: wuye` + MCP 工具的 `echo` → 模型看到的工具名是 **`mcp__wuye__echo`**；
- 只允许 `[A-Za-z0-9_-]`，最长 64 字符；有损归一化时追加 **12 位 SHA-256 十六进制后缀**（【源码】运行时内联
  `MAX_PUBLIC_NAME_LENGTH = 64` / `INVALID_NAME_CHARS` / `HASH_LENGTH = 12`）；
- `tools/call` 线上永远发**原始名**（`echo`），公开名从不回传、也从不被反解析；
- 两个 server 可以有同名工具（`mcp__a__search` / `mcp__b__search`）；同一个作用域里 `serverName` 重复会让**后加载的那个**行加载失败。

**结论**：`agent/mcp_server.py` 里 `mcp.tool(name="create_work_order")` 定义的原始名，
模型侧就是 `mcp__wuye__create_work_order`。SSE 桥要把 `mcp__wuye__` 前缀剥掉再查 `agent/tools.py` 的清单。

### 3.3 子进程环境（**安全关键**，【源码+实跑】）

stdio 子进程的环境 = `scrubbedParentEnv()` + 我们显式给的 `env`，其中 scrub 规则是
（【源码】运行时内联 `dsh-subprocess`）：

```ts
const SENSITIVE_ENV_PATTERN = /KEY|PASSWORD|SECRET|TOKEN/i
function scrubbedParentEnv() {
  const env = {}
  for (const [key, value] of Object.entries(process.env))
    if (value !== undefined && !SENSITIVE_ENV_PATTERN.test(key) && !key.toUpperCase().startsWith('DSH_'))
      env[key] = value
  // 代理变量按需补回
  return env
}
```

【实跑】用探针工具 `child_info` 读到（`mcp-envprobe.notifications.json`）：

```text
executable=<PROJECT_ROOT>\.venv\Scripts\python.exe
cwd=<PROJECT_ROOT>\artifacts\agent-spec
env_count=88
has_WUYE_AGENT_TOKEN=True          ← 显式 env 生效
has_DEEPSEEK_API_KEY=False         ← KEY 形状被脱敏
has_DSH_HOME=False                 ← DSH_ 前缀被脱敏
```

**落地要求**：

1. 令牌变量名随便叫（`WUYE_AGENT_TOKEN` 也可以），但**必须显式写在 `env:` 里**；
   父进程环境里的同名变量会被 `TOKEN` 规则**滤掉**，别指望继承。
2. `PATH` / `HOME` / locale / 代理变量会保留，所以 `command` 用**绝对路径**最稳
   （本机 `sys.executable` = `<PROJECT_ROOT>\.venv\Scripts\python.exe`）。
3. `DEEPSEEK_API_KEY` **不会**泄漏进 MCP 子进程 —— 这正好符合我们「MCP 子进程只需要业务令牌」的设计。
4. 模型上下文里只有工具的 `name` / `description` / `parameters`，**永远不会出现 `env` 里的值**（令牌不进提示词）。

### 3.4 启动失败的行为（【实跑】）

`failOnStartupError: true` 时，MCP 起不来会**直接让整个 dsh 启动失败**，SDK 侧抛 `TransportClosedError`：

```text
<PROJECT_ROOT>\.venv\Scripts\python.exe: No module named probe_mcp_server
Error: dsh: plugin tree failed to load: failed to apply loader entry include (cordis:include):
       failed to apply loader entry mcp-wuye (@deepseek-ai/dsh-mcp-client):
       mcp-client(wuye): initial connection or tool synchronization failed
McpError: MCP error -32000: Connection closed
```

这对接线期是**好事**（「能聊天但没工具」的假象被消除），
但上线前必须确认 `python -m agent.mcp_server` 在**脱敏环境**下也能起来（令牌、模块可导入、编码正确）。
建议：`env` 里固定加 `PYTHONIOENCODING: utf-8`、`PYTHONUNBUFFERED: "1"`（中文工具名/结果在 Windows 上必须）。

### 3.5 Python 侧最小 MCP server（本项目 venv 实际可用）

`mcp 2.2.0` **已把 `FastMCP` 改名 `MCPServer`**，`agent/mcp_server.py` 必须兼容两种导入：

```python
try:      # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

mcp = _Server("wuye")

@mcp.tool(name="echo", description="回显文本")
def echo(text: str) -> str:
    return text

if __name__ == "__main__":
    mcp.run("stdio")
```

【实跑】`MCPServer.tool` 的调用形态（`server.tool(description=...)(fn)` 或 `@mcp.tool(...)`）在 2.2.0 下均可用，
`tools/list` 返回的 `inputSchema` 由函数签名生成（`required: ["text"]`）。
**约束**：stdio 上只走 JSON-RPC，任何日志必须 `print(..., file=sys.stderr)`。

### 3.6 http（Streamable HTTP）形态

```yaml
- insert:
    - id: mcp-wuye
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        transport: streamable-http
        serverName: wuye
        url: http://127.0.0.1:8765/mcp
        headers:
          Authorization: !!js '`Bearer ${process.env.WUYE_TOKEN ?? ""}`'
```

【推断】本项目**不采用** http：多一个端口就多一份鉴权/生命周期管理成本，而 stdio 已经把
「进程随用户、令牌走环境变量」的隔离做干净了。http 形态只在需要跨机器共享 MCP 服务时才值得。

### 3.7 工具数量多也没问题（v2 起工具会到 ~50–76 个）

【实跑】工具 schema 是**每轮请求都要带上的固定开销**。本机实测对照：

| 工具集合 | schema 字符数 | ≈ tokens（÷4） |
| --- | --- | --- |
| dsh 内置 25 个工具 | 25,907 | 6,476 |
| 我们的 3 个 MCP 探针工具 | 668 | 167 |
| **本项目 73 个业务工具（按 `agent/tools.py` 声明实算）** | **19,758** | **4,940** |

（73 个工具的核算脚本：`artifacts/agent-spec/estimate_tool_tokens.py`，结果 `tool-token-estimate.json`；
平均 271 字符/工具，最大 `create_work_order` 586 字符。）

结论（**v2 契约把工具扩到 ~50 个也不用担心**）：

1. **即使 73 个工具全上，也比 dsh 内置那 25 个工具省 6,476 → 4,940 tokens**，
   因为业务工具的 description 短、参数少；
2. 按 v2 的 ~50 个估算，约 **3,400 tokens**（~13.5 K 字符），占 100 万上下文不到 0.4%；
3. **真正要盯的不是 token，是启动耗时与握手稳定性**：`tools/list` 是 SDK 默认 60 s 超时的普通请求，
   工具越多、description 越大，握手与重同步越久（本项目 3 工具实测启动 ~3 s，模型问答 4~5 s）；
4. 工具定义前缀在工具集合不变时**保持 KV cache 稳定**；一旦集合变化（增删工具）就会失效，
   所以**不要频繁改工具清单**（§12.4 的 `list_changed` 也是同样道理）。
5. 建议 `agent/tools.py` 的 description 保持「一句话 + 必要约束」，别写小作文 —— 每个工具都在每轮付费。

---

## 4. 工具裁剪

### 4.1 裁剪目标与手段

- 目标：**模型工具面板里只剩 `mcp__wuye__*`**，不多花 token、不给模型多余的破坏面；
- 手段：把「能力行」`disabled: true`（patch 语义见 §1.4）。禁用行**不会**让 dsh 报错，只是不加载。

### 4.2 需要禁用的行 id 清单（**基于 `--dump-default-config` 的真实行 id**）

以下 32 个 id **全部在 sdk profile 的 dump 里存在**（86 行 id 中挑出来的），
直接抄进 patch 即可；每一条都经过 §4.3 的实跑验证。

```yaml
# ── 命令行 / 终端 ─────────────────────────────
- id: tool-bash            # Windows 下本来就被 !!js 关掉，显式再关一层
  disabled: true
- id: tool-pwsh            # Windows 上真正在跑的那个 shell 工具（单文件 exe 下自带 pwsh 又报 --profile 缺失）
  disabled: true
# ── 文件系统 / 项目指令 / 技能 ───────────────
- id: tool-fs              # read / write / edit 工具
  disabled: true
- id: tool-fs-search       # glob / grep / read_image
  disabled: true
- id: agent-instructions   # 读 AGENTS.md 塞进上下文
  disabled: true
- id: skill
  disabled: true
- id: skill-filesystem
  disabled: true
- id: skill-badge
  disabled: true
- id: tool-skill           # skill 工具（本例中它同时是「可用技能目录」提示词的来源）
  disabled: true
# ── 联网 ─────────────────────────────────────
- id: web
  disabled: true
- id: web-search-deepseek
  disabled: true
- id: web-fetch-http
  disabled: true
- id: tool-web             # web_search / web_fetch
  disabled: true
# ── 计划 / 目标 / 子智能体 / 工作流 / 后台任务 ──
- id: plan-mode            # exit_plan_mode
  disabled: true
- id: goal
  disabled: true
- id: goal-round-driver
  disabled: true
- id: command-goal
  disabled: true
- id: tool-goal            # create_goal / get_goal / update_goal
  disabled: true
- id: tool-ralph           # ralph
  disabled: true
- id: tool-todo            # todo_write
  disabled: true
- id: subagent
  disabled: true
- id: subagent-spawn-in-process
  disabled: true
- id: subagent-fork-in-process
  disabled: true
- id: tool-subagent             # subagent
  disabled: true
- id: tool-subagent-control     # interrupt_agent / send_message
  disabled: true
- id: tool-subagent-list-agents # list_agents
  disabled: true
- id: tool-subagent-fork        # subagent_fork
  disabled: true
- id: workflow-worker-thread
  disabled: true
- id: tool-workflow             # workflow
  disabled: true
- id: jobs
  disabled: true
- id: tool-jobs                 # job_list / job_output / job_kill
  disabled: true
# ── 交互式追问（SDK 无问答面板）──────────────
- id: user-questions            # ask_user_question
  disabled: true
```

【实跑】**不存在的行 id**（写了会打印 `patch: entry "xxx" not found` 警告，无害但要避免）：

```text
time-context              → not found
tool-present              → not found
tool-str-replace-editor   → not found（默认不在 sdk profile 里，需要显式 insert 才会出现）
```

### 4.3 裁剪效果（【实跑】`request/header.tools` 为准）

| 场景 | patch | 模型看到的工具数 | 工具名 |
| --- | --- | --- | --- |
| 基线（不裁剪） | `14-approval-probe.yml`（只换提示词） | **25** | `create_goal, edit, exit_plan_mode, get_goal, glob, grep, interrupt_agent, job_kill, job_list, job_output, list_agents, pwsh, ralph, read, read_image, send_message, skill, subagent, subagent_fork, todo_write, update_goal, web_fetch, web_search, workflow, write` |
| 只裁剪 | `10-full-disable-nomcp.yml` | **0** | `[]` |
| 裁剪 + MCP | `13-mcp-probe-full.yml` / `20-e2e-final.yml` | **3** | `mcp__wuye__echo, mcp__wuye__env_probe, mcp__wuye__child_info` |

【实跑】裁剪后的 `request/header`（这就是「模型真正拿到的工具面板」）：

```json
{"provider": "deepseek-official", "model": "deepseek-v4-flash",
 "maxTokens": 256000, "reasoningEffort": "high",
 "tools": [{"name": "mcp__wuye__child_info"}, {"name": "mcp__wuye__echo"}, {"name": "mcp__wuye__env_probe"}]}
```

结论：**禁用后仍然能正常调用 MCP 工具**（§8 有完整 tool/call + tool/result 原文）。

### 4.4 `ask_user_question` 是否可用？

**不可用，也不建议启用。**

- 【实跑】`user-questions` 启用时，默认 25 个工具里**没有** `ask_user_question`
  （最小底座里该工具依赖 Web 界面的问答面板；单文件 exe / headless SDK 下没有可用的 UI 通道）；
- 【实跑】禁用 `user-questions` 后启动、对话、工具调用一切正常；
- 方案：**让模型直接用文字问**。`agent/config/wuye_agent.md` 已经写了
  「不确定就问……一次只问最关键的一个问题」，实测模型会正常用文字追问。
- 【推断】即使把它点亮，问题请求也会走 `user-questions/request` waterfall；
  无人应答时按「fail closed」处理，只会变成一次失败的问答，白花一轮 token。

### 4.5 不建议禁用的行（保持启用）

| 行 id | 为什么留着 |
| --- | --- |
| `session-persistence-jsonl` | 会话落盘（`$DSH_HOME/sessions/...`），多轮上下文靠它 |
| `session-projection` / `session-projection-cache` | Web/CLI 读会话状态 |
| `compaction-basic` / `tool-result-pruner` | 长对话压缩（业务工单列表可能很长） |
| `spill-local` / `spill-policy` | 大工具结果溢写，避免撑爆上下文 |
| `timeout-policy` | 工具调用超时策略 |
| `sandbox-policy` / `sandbox` / `fs-sandbox` / `approval` / `permission` | 权限与沙箱基础设施；`permission` 行注入 `prompts` 要求 `ctx.shell`/`ctx.approval`，**禁用有加载失败风险**（【源码】`static inject = ['shell','approval',...]`） |
| `tools` | 工具注册表本身，禁了 MCP 工具也上不来 |
| `llm*` / `credentials` / `settings` | 模型连接与凭据 |
| `token-meter` | 用量统计（我们可以在 SSE 里顺带展示） |

### 4.6 可选裁剪（按需，本文未实跑）

- `repeat-tool-reminder`：同一个工具重复调用时插入提醒；对业务场景意义不大，可用 patch 关掉。
- `session-telemetry-otel`：默认 `FEEDBACK_ONLY`。若要彻底不上报，用官方开关
  `DSH_TELEMETRY_DISABLED=1`（launcher 会自动追加一条 disable patch，【源码】`profile-boot.ts:100-103`），
  **不要**手写 patch 去禁用它。

---

## 5. 授权 / 审批行为

### 5.1 结论

> **SDK（headless）模式下，我们这套组合里的工具调用不需要人工审批，也确实没有触发过审批。**

### 5.2 依据

**（1）审批服务默认是 fail-closed，但只有「显式发起审批」的工具会走到它。**
【源码】运行时内联 `@deepseek-ai/dsh-user-approval`：

```ts
static Config = z.object({ policy: z.union(['ask', 'never']).default('ask') })

async decide(req, session) {
  if (signal?.aborted) return 'cancelled'
  if (this.effectivePolicy(session) === 'never') return 'rejected'      // ← 注意：never = 自动拒绝，不是自动放行
  const answer = Promise.resolve()
    .then(() => this.ctx.waterfall(scopeTarget(...), 'approval/request', req, () => Promise.resolve('unavailable')))
    .then(o => OUTCOMES.includes(o) ? o : 'unavailable', () => 'unavailable')  // ← 没有 answerer 就是 unavailable（fail closed）
}
```

**（2）谁会发起审批？** 只有两类：
- `bash` / `pwsh` 的 `sandbox_permissions` 提权；
- 文件工具的 `sandbox_permissions` 提权（`fs-sandbox`）。

两者都在我们**已禁用**的行里，所以模型连工具都看不见。

**（3）MCP 工具不经过审批。**【源码】运行时内联 `dsh-tools` 的审批闸门是
`ToolRuntime.serviceAsk(exec, ask)`，只有工具自己声明了 `ask` 才会调用 `ctx.get('approval')`；
`dsh-mcp-client` 的执行路径里**没有任何 `approval` 调用**。

**（4）【实跑】沙箱内的正常写操作直接成功、无审批。**
用默认 25 工具、`sandbox=mode workspace-write / approval=ask`（会话投影实测值）跑：

```json
// tool/call
{"turn":1,"step":1,"callId":"call_00_ET_sKFlIbdzoPULIFUApBrh7814","name":"write",
 "arguments":"{\"file_path\": \"probe-write.txt\", \"content\": \"HELLO\"}"}
// tool/result
{"content":[{"type":"tool-result","content":[{"type":"text",
  "text":"<path>C:\\...\\probe-workdir\\probe-write.txt</path>\n<type>file</type>\n<content>\nCreated file\n</content>"}],"isError":false}]}
```

文件确实落盘（`artifacts/agent-spec/probe-workdir/probe-write.txt` = `HELLO`），全程没有 `approval/asked` 事件。

**（5）会话投影里能看到权限档位**【实跑】`$DSH_HOME/storages/session_projcache/sessions/<id>.json`：

```json
"permissions": {"ver": 2, "val": {"preset": "workspace-write", "sandbox": "workspace-write", "approval": "ask", "seeded": false}}
```

### 5.3 如何配置成「自动放行」

| 目标 | 做法 | 评价 |
| --- | --- | --- |
| 工具不需要审批（我们的现状） | 禁用 `tool-bash` / `tool-pwsh` / `tool-fs` / `tool-fs-search` + 只用 MCP 工具 | ✅ **推荐**，实跑验证：0 次审批 |
| 彻底关闭审批提示 | 环境变量 `DSH_PERMISSION_MODE=danger-full-access` → launcher 生成 `approval.policy: never` + `sandbox: danger-full-access` | ⚠️ 注意语义：`never` 是**自动拒绝**需要审批的动作，不是自动放行；而且沙箱全开对生产不合适 |
| 想改某一轮生效的档位 | `DSH_PERMISSION_MODE` 只接受 `read-only` / `workspace-write` / `danger-full-access` | 见下 |

【源码】base 层的默认值（`--dump-default-config` 原文）：

```yaml
- id: sandbox-policy
  config:
    mode: !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
    workspaceRoot: !!js process.cwd()
- id: approval
  config:
    policy: !!js (process.env.DSH_PERMISSION_MODE ?? 'workspace-write') === 'danger-full-access' ? 'never' : 'ask'
- id: permission
  config:
    presets:
      read-only:      {sandbox: read-only,      approval: ask}
      workspace-write:{sandbox: workspace-write, approval: ask}
      danger-full-access: {sandbox: danger-full-access, approval: never}
```

### 5.4 【推断】注意事项

- 【推断】如果**将来**要放开 `bash`/`pwsh`，就必须自己处理审批：
  headless 下没有 answerer，`sandbox_permissions` 提权会得到
  `tool "bash" requires approval, but no approval channel is available`（工具以 isError 返回）。
  本轮不需要。
- 【推断】子智能体（subagent）会继承父会话的沙箱覆盖，但审批策略被**钉死为 `never`**
  （【源码】`captureDelegatedPolicyOverrides`：`approvalPolicy: ... 'never'`）。我们已禁用子智能体行。

---

## 6. 多用户 / 会话隔离

### 6.1 `dsh_home` 目录结构（【实跑】`artifacts/agent-spec/dsh-home-mcp-e2e/`）

```text
<dsh_home>/
├── .anonymous-user-id                 # 首次启动生成的匿名 UUID（37 字节）
├── profiles/
│   ├── node_modules/                  # 安装级模块回退（大量 junction / 代理，约 1657 个文件 0.75 MB）
│   └── sdk/                           # sdk profile 目录，首次启动自动创建
│       ├── package.json               # {"dsh":{"profile":{"bundles":["@deepseek-ai/dsh-base","@deepseek-ai/dsh-sdk-app"],"patchReload":"startup"}}}
│       ├── cordis.patch.yml           # profile 自己的用户层（默认 []）
│       ├── cordis.yml                 # 空根（每次启动被重写，不要手改）
│       ├── pnpm-workspace.yaml
│       └── node_modules/  .dsh-module-fallback/
├── sessions/                          # 会话日志（jsonl.zstd，按 cwd + sessionId 分目录）
│   └── --C--Users-...-probe-workdir--/
│       └── user1-session-A/session.v3.jsonl.zstd
└── storages/
    └── session_projcache/sessions/<sessionId>.json   # 会话投影缓存（标题/用量/权限档位…）
```

【实跑】体积：一个 `dsh_home` 首次使用后 **≈0.75 MB / 1657 个文件**
（其中绝大部分是 `profiles/node_modules` 的回退链接，不是真实文件内容）。

**`dsh_home` 里没有任何凭据文件**：API Key 只通过 SDK 的环境变量注入，不落盘（【实跑】目录里只有上述 4 项）。

### 6.2 同一 `dsh_home` 下多 `session_id` 是否隔离？

**是，隔离的。**【实跑】`probe_multisession.py`：

```text
A_turn1: [WUYE-PERSONA-OK]\n好                       # 会话 A：记住「紫色小猫」
B_turn1: [WUYE-PERSONA-OK]\n不知道。我们当前对话中没有您让我记词的相关记录…
A_turn2: [WUYE-PERSONA-OK]\n您刚才让我记的词是「紫色小猫」。
```

会话日志落在各自的目录里，互不覆盖：

```text
sessions\--C-...-probe-workdir--\user1-session-A\session.v3.jsonl.zstd
sessions\--C-...-probe-workdir--\user2-session-B\session.v3.jsonl.zstd
```

**但会话隔离 ≠ 身份隔离**：同一个 `dsh_home` 里的 session 共享同一份 profile 配置和 MCP 子进程定义。
真正的身份边界来自**我们**：

1. 每个登录用户一个**独立 dsh_home**（§6.4），所以 MCP 行的 `env.WUYE_AGENT_TOKEN` 天然不同；
2. `session_id` 由我们的 `agent_session.dsh_session_id` 决定，用户永远不能自己指定
   （否则同一个 home 里 A 能续上 B 的会话）。

### 6.3 【实跑】会话日志路径推导规则

```text
<sessions>/<cwd 的 ':'、'\'、'/' 全替换成 '-'，两端加 '--'>/<sessionId>/session.v3.jsonl.zstd
例：cwd = C:\...\probe-workdir →
    --C-Users-<user>-...-probe-workdir--
```

推论：**同一 dsh_home 下，cwd 不同会让会话落到不同目录树**。所以同一用户的运行时必须固定 `cwd`，
否则「续同一个 session_id」会找不到历史（会话是按 cwd 分桶的）。

### 6.4 不同登录用户是否必须各自 `dsh_home`？

**必须，理由有三条**（前两条是硬约束）：

1. **MCP 行是按 profile/覆盖文件配置的，不是按会话配置的。** 令牌写在 patch 的 `env` 里，
   而 patch 是**进程级**的（一个 `dsh_home` + 一组 `--patch` 对应一个进程）。
   两个用户共用一个 home ⇒ 共用一份 patch ⇒ **共用同一个 `WUYE_AGENT_TOKEN`**，
   MCP 工具就会用错人的身份去查业务库 —— 这是**越权**，不是性能问题。
2. **会话 ID 在同一 home 里是全局命名空间**，没有跨用户鉴权；共用 home 等于把 A 的会话暴露给 B 的运行时。
3. **生命周期与配额**：一用户一进程（懒启动 + 空闲回收）是最简单的隔离与限流单元；
   共用进程还要处理「用户切换 → 换令牌 → 换 MCP 子进程」的重建，复杂度反而更高。

【实跑】温度对照：单文件 exe 首次启动约 **3 秒**，一次完整问答（含 1 次工具调用）**4~5 秒**，
所以「每个登录用户懒启动一个运行时」在当前部署规模下完全够用。

### 6.5 推荐目录布局

```text
wuye/
├── instances/                              # 运行期状态，不进版本库
│   ├── dsh-home/
│   │   └── u<user_id>/                     # ← 一登录用户一个 dsh_home
│   │       ├── profiles/{node_modules,sdk}/    （dsh 自动创建）
│   │       ├── sessions/<cwd-bucket>/<session_id>/
│   │       └── storages/session_projcache/
│   ├── dsh-workspace/
│   │   └── u<user_id>/                     # ← 一登录用户一个会话工作目录（= SDK 的 cwd）
│   └── dsh-patch/
│       └── u<user_id>-<auth_version>.yml    # 每次启动重新生成（含当次令牌）
└── agent/config/wuye_agent.md
```

约定：

- `DshRuntime(settings).home_for(user_id) = settings.home_root / f"u{user_id}"`；
- **`cwd` 固定为该用户工作目录的绝对路径**（不要用项目根，避免会话桶漂移）；
  该目录里不放业务代码，只当会话工作区（我们已禁用文件工具，正常不会被写入）；
- patch 文件**每次启动重新生成并覆盖**：令牌是短期的，旧的不能复用；文件名带 `auth_version`
  便于「角色/数据范围变了 → 换 home 或换 patch」时一眼看出；
- 单用户一份 home 的磁盘成本 ≈0.75 MB（首次），5 个演示账号 ≈4 MB，可忽略；
- `instances/` 必须进 `.gitignore`；
- 令牌失效（登出 / 角色变化 / 过期）时，最稳的做法是**关掉该用户的运行时**并让下一次提问重建
  （MCP 子进程是启动时拉起的环境变量，进程活着就换不了令牌）。

---

## 7. provider / model / 推理档位

### 7.1 可用模型 id 从哪来

【源码+实跑】单文件 exe 内联了 pi-ai 的模型目录（`providers/data/deepseek.json`），
`deepseek-official` 路由到该目录中的 DeepSeek 条目。`deepseek` provider 的原始定义：

```js
export function deepseekProvider() {
  return createProvider({
    id: 'deepseek', name: 'DeepSeek',
    baseUrl: 'https://api.deepseek.com',
    auth: { apiKey: envApiKeyAuth('DeepSeek API key', ['DEEPSEEK_API_KEY']) },
    models: Object.values(DEEPSEEK_MODELS),
    api: openAICompletionsApi(),
  })
}
```

目录里 DeepSeek 侧的可用模型（原文摘录）：

```json
{"openai-completions":{
  "deepseek-v4-flash":{
    "id":"deepseek-v4-flash","name":"DeepSeek V4 Flash","api":"openai-completions",
    "baseUrl":"https://api.deepseek.com","provider":"deepseek","reasoning":true,
    "input":["text"],"contextWindow":1000000,"maxTokens":384000,
    "compat":{"maxTokensField":"max_tokens","thinkingFormat":"deepseek",
              "requiresReasoningContentOnAssistantMessages":true},
    "thinkingLevelMap":{"minimal":null,"low":"low","medium":null,"high":"high","max":"max"}},
  "deepseek-v4-flash-vision-exp":{"id":"deepseek-v4-flash-vision-exp", "...": "...","input":["text","image"]},
  "deepseek-v4-pro":{"id":"deepseek-v4-pro","name":"DeepSeek V4 Pro", "...": "...",
    "thinkingLevelMap":{"minimal":null,"low":null,"medium":null,"high":"high","max":"max"}}
}}
```

**结论**：`deepseek-official` 下可选 `deepseek-v4-flash`（默认，文本）、
`deepseek-v4-pro`（更贵更强）、`deepseek-v4-flash-vision-exp`（多模态）。
三者 `contextWindow` 都是 1,000,000，`maxTokens` 都是 384,000。

### 7.2 API Key / Base URL 怎么给

两条路都可以，**任选一条**：

| 方式 | 写法 | 适用 |
| --- | --- | --- |
| SDK 参数 | `DeepSeekHarness(api_key="...", base_url="https://api.deepseek.com")` | 从 `.env` 读出后显式传入（**推荐**，不污染进程环境） |
| 环境变量 | 进程环境里放 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` | 本地调试 |

【源码】`api.py:70-74`：`base_url` / `api_key` 参数会被写成 `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`
再注入子进程环境；运行时再用 `envApiKeyAuth(..., ['DEEPSEEK_API_KEY'])` 读取。
`.env` 里已有 `DEEPSEEK_BASE_URL=https://api.deepseek.com`，与 provider 默认一致。

### 7.3 `reasoning_effort` / `max_tokens` 有效取值

SDK 的两个参数（【源码】`client.py:133-150`）：

```python
def initialize(self, *, cwd, provider, model, reasoning_effort=None, max_tokens=None):
    payload = {"cwd": ..., "provider": ..., "model": ...}
    if reasoning_effort is not None: payload["reasoningEffort"] = reasoning_effort
    if max_tokens is not None:       payload["maxTokens"] = max_tokens
```

- `reasoning_effort`：字符串；实际可用值由模型的 `thinkingLevelMap` 决定。
  `deepseek-v4-flash` → **只有 `low` / `high` / `max`**；
  不传时用路由默认 **`high`**（【实跑】`request/header.config.reasoningEffort == "high"`）。
  传不支持的值会**直接拒绝初始化**（不是静默忽略），【实跑】原文：

  ```text
  deepseek_harness.errors.JsonRpcError: provider "deepseek-official" model "deepseek-v4-flash"
      does not support reasoning effort "minimal"
  deepseek_harness.errors.JsonRpcError: provider "deepseek-official" model "deepseek-v4-flash"
      does not support reasoning effort "medium"
  ```

  对照 `thinkingLevelMap`（`minimal:null, low:"low", medium:null, high:"high", max:"max"`）完全一致。
- `max_tokens`：整数；不传时用模型/适配器默认（【实跑】`request/header.config.maxTokens == 256000`）。
- 【实跑】显式传 `reasoning_effort="low"` / `"max"`、`max_tokens=4096`：

  ```json
  {"provider":"deepseek-official","model":"deepseek-v4-flash","reasoningEffort":"low","maxTokens":256000}
  {"provider":"deepseek-official","model":"deepseek-v4-flash","reasoningEffort":"max","maxTokens":4096}
  ```

- 【推断】`max_tokens` 语义受环境变量 `DSH_MAX_TOKENS_AS_SUCCESS` 影响
  （base 行默认 `true`，即「撞到 max tokens 也算正常结束」），这是为了让长回答不被判成失败——
  本项目的回答都很短，保持默认即可。
- 【推断】`max_tokens` 不要设太小：`wuye_agent.md` 要求「先查后做 + 做完回读」，
  一轮里可能出现多步工具调用，每个 step 都会吃 token 预算。

---

## 8. 端到端证据（真实运行日志）

### 8.1 装的东西

| 组件 | 内容 |
| --- | --- |
| 被测 patch | `artifacts/agent-spec/patches/20-e2e-final.yml`（35 条 patch：1 条系统提示词 + 1 条 `session-title-llm` 禁用 + 1 条 `insert` MCP + 32 条工具禁用） |
| 系统提示词 | **项目真实文件** `agent/config/wuye_agent.md`（内联进 patch） |
| MCP server | `artifacts/agent-spec/probe_mcp_server.py`（最小 echo MCP，`mcp 2.2.0` 的 `MCPServer`，stdio） |
| 驱动 | `artifacts/agent-spec/probe_run.py`（DeepSeekHarness + 通知全量落盘） |
| 执行 | `.venv\Scripts\python.exe artifacts/agent-spec/probe_run.py patches/20-e2e-final.yml e2e-final "请调用 mcp__wuye__echo 工具…"` |

生成的 patch 关键片段：

```yaml
- insert:
  - id: mcp-wuye
    name: '@deepseek-ai/dsh-mcp-client'
    config:
      serverName: wuye
      transport: stdio
      command: <PROJECT_ROOT>\.venv\Scripts\python.exe
      args: ['-m', 'probe_mcp_server']
      cwd: <PROJECT_ROOT>\artifacts\agent-spec
      env:
        WUYE_AGENT_TOKEN: DEMO-TOKEN-e2e-1234
        PYTHONIOENCODING: utf-8
        PYTHONUNBUFFERED: '1'
      toolCallTimeoutMs: 60000
      failOnStartupError: true
```

### 8.2 一次成功运行的全部关键通知原文

命令输出：

```text
ELAPSED=4.3s FINISH=completed
TOOLS(3): ['mcp__wuye__child_info', 'mcp__wuye__echo', 'mcp__wuye__env_probe']
```

**（a）系统提示词 = 我们的文件**（`session.event` / `system/message`）

```json
{
  "sessionId": "e2e-final-s1",
  "event": {
    "type": "system/message", "seq": 7,
    "data": {"turn": 1, "step": 1, "message": {
      "role": "system",
      "content": [{"type": "text",
        "text": "你是「云邻AI智脑」的智能助手，帮助物业工作人员和业主用自然语言完成日常工作。\n\n## 你是谁\n\n- 你服务的是**当前登录用户**。…"}],
      "source": {"kind": "plugin", "plugin": "@deepseek-ai/dsh-system-prompt"},
      "id": "…"}},
    "surfaceOp": "append"
  }
}
```

**（b）工具面板只有 MCP 工具**（`request/header`）

```json
{
  "sessionId": "e2e-final-s1",
  "event": {
    "type": "request/header", "seq": 9,
    "data": {
      "header": {
        "config": {"provider": "deepseek-official", "model": "deepseek-v4-flash",
                   "maxTokens": 256000, "reasoningEffort": "high"},
        "adapterDefaults": {"reasoningEffort": true, "maxTokens": true},
        "tools": [
          {"name": "mcp__wuye__child_info", "description": "Report the interpreter, cwd and how many ambient env vars this child inherited.", "parameters": {"type":"object","properties":{},"title":"child_infoArguments"}},
          {"name": "mcp__wuye__echo", "description": "Echo back the given text verbatim.", "parameters": {"type":"object","properties":{"text":{"title":"Text","type":"string"}},"required":["text"],"title":"echoArguments"}},
          {"name": "mcp__wuye__env_probe", "description": "Return one environment variable value exactly as this child process sees it.", "parameters": {"type":"object","properties":{"name":{"title":"Name","type":"string"}},"required":["name"],"title":"env_probeArguments"}}
        ]
      },
      "reason": "initial"
    }
  }
}
```

**（c）模型发起工具调用**（`assistant/message` 里的 `tool-call` 块 + `tool/call` 事件）

```json
{"sessionId":"e2e-final-s1","event":{"type":"tool/call","seq":13,
 "data":{"turn":1,"step":1,
         "callId":"call_00_nr40Un8UenMz6SocCPBU5413",
         "name":"mcp__wuye__echo",
         "arguments":"{\"text\": \"云邻AI智脑联通测试\"}"}}}
```

**（d）工具执行结果**（`tool/result`）

```json
{"sessionId":"e2e-final-s1","event":{"type":"tool/result","seq":14,
 "data":{"turn":1,"step":1,
  "message":{
    "source":{"kind":"tool","callId":"call_00_nr40Un8UenMz6SocCPBU5413"},
    "content":[{"type":"tool-result","toolCallId":"call_00_nr40Un8UenMz6SocCPBU5413",
      "content":[{"type":"text","text":"probe-echo:云邻AI智脑联通测试"}],
      "isError":false}],
    "role":"user","id":"faa92790-f3ad-44b8-aed1-18041bfd1b80"}}}}
```

**（e）最终答复**（`assistant/message`，第二 step）

```json
{"type":"assistant/message","data":{"turn":1,"step":2,"message":{
  "role":"assistant",
  "content":[{"type":"text","text":"调用成功了，工具返回的原文是：\n\n```\nprobe-echo:云邻AI智脑联通测试\n```\n\n可以看到返回内容前面多了个 `probe-echo:` 前缀…"}],
  "source":{"kind":"model","provider":"deepseek-official","model":"deepseek-v4-flash"}},"usage":{...}}}
```

**（f）回合正常结束**

```json
{"type":"turn/end","seq":19,"data":{"turn":1,"reason":{"kind":"completed"}}}
```

**（g）流式粒度的实测结论（补登「assistant 无 token 级增量」）**

`assistant/message` 里带一个 `stream` 数组。实测其 `chunk.type` 只有
`block-start` / `block-end` / `usage` / `finish` 四种 —— **没有 `*-chunks` 增量块**：

```text
stream entries: 8
chunk kinds: {'block-start': 2, 'block-end': 2, 'usage': 1, 'finish': 1}
block-start {"type":"block-start","index":0,"blockType":"reasoning"}
block-start {"type":"block-start","index":1,"blockType":"tool-call"}
block-end   {"type":"block-end","index":0,"block":{"type":"reasoning","text":"The user wants me to call the echo tool…"}}
block-end   {"type":"block-end","index":1,"block":{"type":"tool-call","id":"call_00_…","name":"mcp__wuye__echo","arguments":"{\"text\": \"WUYE-MCP-OK\"}"}}
usage       {"type":"usage","usage":{"inputTokens":169,"outputTokens":88,"totalTokens":641,"cacheReadTokens":384,"reasoningTokens":39}}
finish      {"type":"finish","reason":{"kind":"tool-calls"}}
```

第二段（最终答复）同理：

```text
chunk kinds: {'block-start': 1, 'block-end': 1, 'usage': 1, 'finish': 1}
block-start {"type":"block-start","index":0,"blockType":"text"}
block-end   {"type":"block-end","index":0,"block":{"type":"text","text":"[WUYE-PERSONA-OK]\n工具返回原文：probe-echo:WUYE-MCP-OK\n\n…"}}
finish      {"type":"finish","reason":{"kind":"stop"}}
```

**对 SSE 桥的直接结论**：

1. 助手正文是**整段**到达的；`{"type":"delta",...}` 的「打字机效果」只能在应用层做
   （拿到整段后自己切片推送），不是真流式。
2. 但 `block-start(blockType=tool-call)` + `finish(reason.kind=tool-calls)` 来得**早且可靠**，
   足够实时驱动「正在执行：创建报修工单」这类前端提示（`tool/call` 事件里带完整 `arguments`）。
3. `usage`（含 `reasoningTokens` / `cacheReadTokens`）就在 `stream` 里，可直接用于页面上显 token 用量。
4. 若将来需要真正的逐字流，得换 `--profile acp` 或 Web 版（走 ACP / Remote Event 的增量通道），
   代价是放弃 SDK 这条最简依赖路径 —— 本项目**不做**。

**（h）环境凭据隔离**（同一次配置下的 `child_info` 工具结果，`mcp-envprobe.notifications.json`）

```text
tool/result content:
executable=<PROJECT_ROOT>\.venv\Scripts\python.exe
cwd=<PROJECT_ROOT>\artifacts\agent-spec
env_count=88
has_WUYE_AGENT_TOKEN=True
has_DEEPSEEK_API_KEY=False
has_DSH_HOME=False
```

### 8.3 原始文件清单（可自行复核）

| 文件 | 内容 |
| --- | --- |
| `artifacts/agent-spec/e2e-final.notifications.json` | §8.2 全部通知原文（19 条） |
| `artifacts/agent-spec/e2e-final.summary.json` | 工具清单 + 系统提示词 + 最终答复 摘要 |
| `artifacts/agent-spec/mcp-e2e.notifications.json` | 更早一次 MCP 端到端（含完整 `assistant/message.stream`） |
| `artifacts/agent-spec/mcp-envprobe.notifications.json` | 凭据隔离证据 |
| `artifacts/agent-spec/multisession.result.json` | 多会话隔离证据 |
| `artifacts/agent-spec/dump-default-sdk.txt` | `--dump-default-config` 输出（352 行） |
| `artifacts/agent-spec/dump-config-e2e-final.txt` | `--dump-config` 输出（463 行，含我们的 patch 生效痕迹） |
| `artifacts/agent-spec/dump-patch-semantics.txt` | patch 语义验证输出（含未知 id 告警原文） |
| `artifacts/agent-spec/patches/*.yml` | 全部 patch（含 `20-e2e-final.yml`） |
| `artifacts/agent-spec/build_patch.py` | **patch 生成参考实现**（可直接被 `agent/runtime.py` 借鉴） |
| `artifacts/agent-spec/probe_mcp_server.py` | 最小 echo MCP server（教学/探针用，不是业务实现） |
| `artifacts/agent-spec/probe_run.py` / `probe_mcp_connect.py` / `probe_multisession.py` | 验证脚本 |

---

## 9. 给实现者的落地清单（TL;DR）

1. **一个登录用户一个** `dsh_home`（`instances/dsh-home/u<id>`）+ 一个固定 `cwd`（`instances/dsh-workspace/u<id>`）。
2. patch 文件**每次启动重新生成**：`system-prompt`（`includeHarnessIdentity:false` + `includeRuntimeContext:false` + `personaPrefix=<wuye_agent.md>`）、
   `insert` 一条 `mcp-wuye`（stdio，`env.WUYE_AGENT_TOKEN`，`failOnStartupError: true`）、32 条工具禁用。
   —— **不要再写 `persona` 字段（已失效）**。
3. 工具名 = `mcp__wuye__<原始名>`；SSE 桥按前缀剥离后映射中文标签。
4. 令牌只在 `env` 里；父进程环境里的同名变量会被 scrub 掉，**必须显式写**。
5. 不需要审批配置；不要启用 `bash`/`pwsh`/文件/网络工具（否则审批会 fail closed 变成一堆 isError）。
6. `reasoning_effort` 只在 `low`/`high`/`max` 里选；不传默认 `high`。
7. **兜底断言**（强烈建议进测试）：每轮 `request/header.tools` 的名字集合必须 ⊆ `{mcp__wuye__*}` 且非空，
   否则视为「配置漂移」，日志告警。dsh 对未知行 id 只告警不报错，这个断言是唯一可靠的防线。
8. 会话日志是 `session.v3.jsonl.zstd`（压缩），前端回看**必须**用我们自己的 `agent_session` / `agent_message` 表，不要直读 dsh 日志。
9. 助手正文是**整段**到达的（`stream` 里只有 block 级事件）；「正在执行」提示靠 `tool/call` 驱动，
   打字机效果在前端/桥里自己实现（详见 §8.2(g)）。
10. **换 provider（百炼/DeepSeek）不用改代码**：patch 里 patch 掉 `llm-pi-ai` 已有行、声明两条路由，
   运行时按 `AI_PROVIDER` 选 `initialize(provider=...)`（§12）。百炼那条**接入后要补跑一次 hello 验收**。
11. **权限变化要回收运行时**（现方案正确）：dsh 只首批一次 + 服务端主动 `list_changed` 才刷新；
    权限变小不会自动通知（§13.4）。安全上不会越权，因为 `tools/call` 每次都用令牌二次校验。
12. v2 把工具扩到 ~50–76 个**没有 token 风险**（73 个实测仅 ≈4,940 tokens，比 dsh 内置 25 个还省），
    但 description 要短、工具清单别频繁改（KV cache）（§3.7）。

---

## 10. 追加：可直接复制粘贴的 patch 片段与路径解析规则

> 本节是按 captain 的明确要求补的「粘贴即用」版本。**所有片段都已实跑验证过**（见 §10.3 的命令）。
> 生成逻辑的参考实现见 `artifacts/agent-spec/build_patch.py`。

### 10.1 路径解析三条铁律（【实跑】）

| 路径 | 相对谁解析 | 实测证据 | 结论 |
| --- | --- | --- | --- |
| `dsh_home`（→ `DSH_HOME`） | 无歧义，SDK 会 `.expanduser().resolve()` 成绝对路径再注入 | — | **必须绝对**（本项目用 `instances/dsh-home/u<id>` 的绝对路径） |
| `--patch <path>` | **相对「dsh 进程的当前工作目录」**（也就是 SDK 的 `runtime_cwd`），**不是**相对 patch 文件本身 | 在 `artifacts/agent-spec/probes` 下传 `patches/12-prompt-only.yml` → `failed to read overlay C:\...\probes\patches\12-prompt-only.yml ... ENOENT`，退出码 1 | **必须传绝对路径**。SDK 已经把 `patches` 里的每一项 `.resolve()` 成绝对路径（`client.py:481-486`），我们自己拼路径时也要 `Path(...).resolve()` |
| MCP 行 `config.command` | **不解析**：直接作为可执行文件交给 spawn（不经 shell、不展开 `~`/变量） | 传 `python` 时成功启动了 `D:\python\python.exe`（PATH 里的**系统** Python，不是我们的 venv） | **必须绝对路径**，用 `sys.executable`（= `…\.venv\Scripts\python.exe`） |
| MCP 行 `config.cwd` | 子进程工作目录 = **Python 的模块搜索起点** | `cwd` 指向不存在的目录 → 子进程报 `No module named probe_mcp_server` → 整个 dsh 启动失败 | **必须绝对**，用项目根（`-m agent.mcp_server` 才找得到包） |
| MCP 行 `config.args` | 不解析；逐项传给子进程 | `['-m', 'agent.mcp_server']` 正常 | 用数组，不要拼字符串、不要带引号 |

⚠️ **`command: python` 是最隐蔽的坑**：它不给报错，直接跑系统 PATH 上的另一个解释器。
【实跑】用 `command: python` 启动，MCP 工具 `interpreter_probe` 回报：

```text
sys.executable=D:\python\python.exe  prefix=D:\python  version=3.14.4
mcp=D:\python\Lib\site-packages\mcp\__init__.py
```

也就是**根本没有用到项目的 venv**。本机那个解释器恰好也装了 `mcp`，所以探针「跑通了」——
但生产里它不会有我们项目的依赖（或版本不同），会以「工具全丢 / 导入报错 / 连不上库」的形式表现。
**一律写 `str(Path(sys.executable).resolve())`。**

### 10.2 完整 patch 模板（复制即用）

下面就是 `artifacts/agent-spec/patches/20-e2e-final.yml` 的实际形状（35 条）。
占位符只有一个：`__PERSONA__`（= `agent/config/wuye_agent.md` 全文）、`__TOKEN__`（当次短期令牌）。

```yaml
# ─────────────────────────────────────────────────────────────
# ① 系统提示词：替换成物业助手（字段名必须是 personaPrefix）
#    config 是整字段替换，想保留的字段必须写全
# ─────────────────────────────────────────────────────────────
- id: system-prompt
  config:
    includeHarnessIdentity: false      # 去掉 "You are an AI agent powered by DeepSeek Harness."
    includeRuntimeContext: false       # 不注入沙箱/审批政策快照
    personaPrefix: |
      __PERSONA__
    personaSuffix: ''

# ② 会话标题用首条消息即可，省一次模型调用
- id: session-title-llm
  disabled: true

# ③ MCP stdio 工具服务（模块名 = agent.mcp_server，cwd = 项目根）
- insert:
    - id: mcp-wuye
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: wuye                 # → 模型侧工具名 mcp__wuye__<原始名>
        transport: stdio
        command: <PROJECT_ROOT>\.venv\Scripts\python.exe   # ← 用 sys.executable
        args:
          - '-m'
          - agent.mcp_server           # ← cwd 在 sys.path 里，所以能 import 到
        cwd: <PROJECT_ROOT>                                # ← 项目根，绝对路径
        env:
          WUYE_AGENT_TOKEN: __TOKEN__  # ← 唯一注入令牌的通道（父环境同名变量会被 scrub）
          PYTHONIOENCODING: utf-8      # Windows 中文必需
          PYTHONUNBUFFERED: '1'
        toolCallTimeoutMs: 60000
        failOnStartupError: true       # 起不来就整体启动失败，杜绝"能聊没工具"
        reconnect:
          enabled: true
          initialDelayMs: 500
          maxDelayMs: 30000
          maxAttempts: 10

# ④ 关闭与业务无关的 32 行（名单与理由见 §4.2）
#    下面这段在 build_patch.py 里是循环生成的，等价展开：
- {id: tool-bash,                   disabled: true}
- {id: tool-pwsh,                   disabled: true}
- {id: tool-fs,                     disabled: true}
- {id: tool-fs-search,              disabled: true}
- {id: agent-instructions,          disabled: true}
- {id: skill,                       disabled: true}
- {id: skill-filesystem,            disabled: true}
- {id: skill-badge,                 disabled: true}
- {id: tool-skill,                  disabled: true}
- {id: web,                         disabled: true}
- {id: web-search-deepseek,         disabled: true}
- {id: web-fetch-http,              disabled: true}
- {id: tool-web,                    disabled: true}
- {id: plan-mode,                   disabled: true}
- {id: goal,                        disabled: true}
- {id: goal-round-driver,           disabled: true}
- {id: command-goal,                disabled: true}
- {id: tool-goal,                   disabled: true}
- {id: tool-ralph,                  disabled: true}
- {id: tool-todo,                   disabled: true}
- {id: subagent,                    disabled: true}
- {id: subagent-spawn-in-process,   disabled: true}
- {id: subagent-fork-in-process,    disabled: true}
- {id: tool-subagent,               disabled: true}
- {id: tool-subagent-control,       disabled: true}
- {id: tool-subagent-list-agents,   disabled: true}
- {id: tool-subagent-fork,          disabled: true}
- {id: workflow-worker-thread,      disabled: true}
- {id: tool-workflow,               disabled: true}
- {id: jobs,                        disabled: true}
- {id: tool-jobs,                   disabled: true}
- {id: user-questions,              disabled: true}
```

### 10.3 三条自检命令（**不花模型配额**）

```powershell
$DSH = "<PROJECT_ROOT>\.venv\Lib\site-packages\deepseek_harness_runtime\runtime\deepseek-harness-sdk-runtime-win-x64.exe"
$env:DSH_HOME = "<PROJECT_ROOT>\instances\dsh-home\u1"

# 1) 只看「默认底座」，用来确认行 id 清单没过期（不含我们的 patch，也不会被我们写坏的 patch 拖累）
& $DSH --profile sdk --dump-default-config

# 2) 看「加上我们的 patch 之后最终生效的树」：确认 system-prompt 字段、mcp-wuye 行、
#    32 行 disabled: true 都在（provenance 注释会标出是我们的 patch 改的）
& $DSH --profile sdk --patch "C:\...\20-e2e-final.yml" --dump-config

# 3) 校验 YAML 语法 + 行的存在性（未知 id 会打印 patch: entry "x" not found）
& $DSH --profile sdk --patch "C:\...\20-e2e-final.yml" --dump-config 2>&1 | Select-String "not found"
```

【实跑】三条命令在本机都通过（exit 0，`2)` 输出 463 行，无 `not found`）。

**不要**用 `--dump-config` 证明「提示词生效了」——它不做 schema 校验，
已删除的字段（如 `persona`）也会被原样打印出来（§2.6 的坑）。
提示词/工具清单的唯一可靠验证是**真跑一轮读 `system/message` 与 `request/header.tools`**（§8）。

---

## 11. 追加：MCP 工具命名规则与映射表（实测）

### 11.1 规则（**版本无关，不会变**）

```text
模型侧工具名 = "mcp__" + serverName + "__" + MCP 服务端声明的原始工具名
```

- 【实跑】`serverName: wuye` + 原始名 `echo` → **`mcp__wuye__echo`**（三段，两个下划线各两条）；
- 【实跑】`serverName: wuye` + 原始名 `create_work_order` → **`mcp__wuye__create_work_order`**
  （与契约 §6 一致）；长度 32，远低于 64 上限（本项目 73 个工具里最长 30）；
- 【源码】字符集只允许 `[A-Za-z0-9_-]`，总长 ≤ 64；出现其它字符时做**有损归一化并追加 12 位 SHA-256 十六进制后缀**
  —— **只要坚持用下划线命名就不触发**；
- 【源码】`tools/call` 上线**永远发原始名**，公开名不发送、也不被反解析；
- 【实跑】同一 `serverName` 在同一注册作用域重复 → 后加载的那一行加载失败。

### 11.2 SSE 桥要用到的两个常量

```python
MCP_SERVER_NAME = "wuye"
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"     # "mcp__wuye__"

def raw_tool_name(model_facing: str) -> str | None:
    """把模型侧工具名还原成 tools.py 里的原始名；不是我们的工具就返回 None。"""
    return model_facing[len(MCP_TOOL_PREFIX):] if model_facing.startswith(MCP_TOOL_PREFIX) else None
```

- `tool/call` / `tool/result` 事件里的 `name` 字段就是 **`mcp__wuye__create_work_order`** 这种形态
  （【实跑】`{"type":"tool/call","data":{"name":"mcp__wuye__echo",...}}`），
  所以桥在查 `agent/tools.py` 的中文 label 之前**必须先剥前缀**；
- tooltip / 卡片文案一律用 `ToolSpec.label`（"创建报修工单"），原始名只在服务端和日志里出现；
- 建议加断言：`request/header.tools` 里每个名字都以 `mcp__wuye__` 开头且剥前缀后能在 `TOOLS_BY_NAME` 里找到。

### 11.3 映射示例

| 原始名（MCP 服务端 / tools.py） | 模型侧工具名 | 中文标签 |
| --- | --- | --- |
| `whoami` | `mcp__wuye__whoami` | 查看当前身份 |
| `list_communities` | `mcp__wuye__list_communities` | 查询小区 |
| `list_houses` | `mcp__wuye__list_houses` | 查询房屋 |
| `list_work_orders` | `mcp__wuye__list_work_orders` | 查询工单 |
| `create_work_order` | `mcp__wuye__create_work_order` | 创建报修工单 |
| `assign_work_order` | `mcp__wuye__assign_work_order` | 派单 |
| `finish_work_order` | `mcp__wuye__finish_work_order` | 提交完工 |
| `verify_work_order` | `mcp__wuye__verify_work_order` | 验收关闭 |
| `delete_house` | `mcp__wuye__delete_house` | 删除房屋 |
| `bind_relation` | `mcp__wuye__bind_relation` | 绑定房屋关系 |

完整对照表（含中文标签、query/command 类型）已由脚本落盘：
`artifacts/agent-spec/tool-name-map.json`（生成脚本 `artifacts/agent-spec/extract_tool_names.py`）。

### 11.4 ⚠️ 工具清单口径不一致，落地前必须对齐

我在核对时发现**三份清单互不相同**，请 captain 拍板一个权威版本：

| 来源 | 工具数 | 特征 |
| --- | --- | --- |
| **我收到的任务书 §4 表格** | **26** 行 | 最小闭环：无 `list_units` / `list_leases` / `check_in_lease` / `check_out_lease` / `reopen_work_order` / `work_order_stats`（任务书写的是「27 个」，与表内行数对不上） |
| **现网 `docs/REWRITE_CONTRACT.md`** | §4 命令 **51** + 查询 **25** = **76** | 含单元/租赁/账单/投诉/访客/车辆/车位/设备/巡检/收费；§3 是 **35 个权限点 + 6 个角色**；§6 明说工具清单"与 §4 一一对应" |
| **现网 `agent/tools.py`（旧实现）** | **73** | 覆盖全部业务域，但比契约 §4 少 3 个（无 `update_community/update_building/update_unit` 等更新态） |

也就是说：**「27」/「26」/「76」/「73」是四个不同的口径**，
而 `docs/REWRITE_CONTRACT.md` 目前描述的是「全量版」，不是任务书里的「最小闭环」。

**好消息：这不阻塞实现。** 命名规则（`mcp__wuye__<原始名>`）、patch 结构、路径规则、
裁剪清单都与工具数量无关。`agent/mcp_server.py` 按 `TOOLS` 动态注册，
无论最终是 26 / 51 / 73 / 76 个，模型侧名字都自动正确。
**唯一要保证的是**：`agent/tools.py` 是唯一真相源，`agent/bridge.py` 的 label 映射从它生成，
`tests/test_mcp_tools.py` 按它断言 —— 这样将来加减工具不会漏改桥。

**建议 captain 做两件事**：① 明确本轮是否只做 26 个最小闭环工具（若是，请把
`docs/REWRITE_CONTRACT.md` §4/§6 一起收敛，否则前端与测试会按 76 个来写）；
② 无论选哪个，把最终清单落到 `agent/tools.py` 并让 `extract_tool_names.py` 生成映射表。

### 11.5 动态能力目录对命名的影响

【推断，逻辑清晰】`agent/mcp_server.py` 按登录用户的权限**只在 `tools/list` 里返回他有权限的工具**。
所以同一个 `mcp__wuye__` 命名空间下，不同用户看到的工具**数量不同**（模型侧只看到子集）。
这带来两个实现要求：

1. **`request/header.tools` 会随用户变化**，兜底断言要写成「非空 且 ⊆ `{mcp__wuye__*}`」，
   不能写死"必须等于 27 个"；
2. 运行时是**按用户缓存复用**的（一个 dsh 进程一个 patch 文件 → 一个令牌），
   所以权限变化（`auth_version` 变）必须**回收该用户的运行时**再重建，否则模型侧工具目录会停留在旧权限
   —— 见 §6.5 的目录布局与回收策略。**§12.6 给出了这条策略的实测依据与一个可选优化。**

---

## 12. 追加：自定义 OpenAI 兼容 provider 路由（百炼）

> 目标：`.env` 里配 `BAILIAN_API_KEY` 就切百炼，配 `DEEPSEEK_API_KEY` 就用 DeepSeek，`agent/*.py` 不用改。

### 12.1 结论：可以，路由由 `@deepseek-ai/dsh-llm-pi-ai` 声明，**必须 patch 已有行**

【源码】运行时内联 `@deepseek-ai/dsh-llm-pi-ai` 的模块文档：
「One plugin instance owns a dict of provider routes；a route naming an installed pi-ai provider
inherits that provider's catalog，a route pi-ai does not ship is declared outright」——
即 **`providers` 下的键就是 provider id**，pi-ai catalog 里没有的 id 可以完全手写。

**关键前提：`llm-pi-ai` 行在 sdk profile 里已经存在**（`--dump-default-config`）：

```yaml
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
```

所以必须 **patch 已有行**，不能 `insert` 新实例。实测反例（我先试过 insert）：

```text
Error: dsh: plugin tree failed to load: ... failed to apply loader entry llm-pi-ai-compat
       (@deepseek-ai/dsh-llm-pi-ai): configurable provider "amazon-bedrock" is already declared
LlmError: configurable provider "amazon-bedrock" is already declared
```

原因：每个实例都会把 pi-ai 自带的全部 catalog provider 注册进「可配置 provider 目录」，两个实例就撞了。

### 12.2 可复制 patch 片段（DeepSeek 那条**实跑通过**）

```yaml
# 注意：config 是整字段替换 → providers 里要写全你想用的所有手写路由
- id: llm-pi-ai
  config:
    providers:
      # ① DeepSeek（演示手写能力；生产直接用内置 deepseek-official 路由也可以）
      deepseek-compat:
        displayName: DeepSeek Compatible
        apiKeyEnv: DEEPSEEK_API_KEY          # 凭据引用：从进程环境或凭据服务解析
        api: openai-completions              # OpenAI 兼容协议
        baseURL: https://api.deepseek.com/v1
        models:
          - id: deepseek-v4-flash
            name: DeepSeek V4 Flash (compat)
            contextWindow: 1000000
            maxTokens: 384000
      # ② 阿里云百炼（OpenAI 兼容端点）
      bailian:
        displayName: 阿里云百炼
        apiKeyEnv: BAILIAN_API_KEY
        api: openai-completions
        baseURL: https://dashscope.aliyuncs.com/compatible-mode/v1
        models:
          - id: qwen-plus
            name: Qwen Plus
            contextWindow: 131072
            maxTokens: 8192
```

字段口径（【源码】pi-ai 的 provider profile schema + 模块文档示例）：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `<provider-id>:` | ✅ | 路由键 = SDK `initialize(provider=...)` 要传的值 |
| `displayName` | ❌ | 配置界面显示名 |
| `apiKeyEnv` | ❌ | **凭据引用名**（不写明文 key）。从 `credentials` 服务或进程环境解析 |
| `api` | 手写路由必填 | `openai-completions`（OpenAI 兼容） |
| `baseURL` | 手写路由必填 | 端点前缀，**不含** `/chat/completions`（客户端自己拼） |
| `models[].id` | ✅ | 模型 id，必须与端点真实模型名一致 |
| `models[].name` / `contextWindow` / `maxTokens` | ❌ | 展示名与容量 |
| `models[].reasoningEfforts` | ❌ | 可选推理档位：`{off: null, high: high, max: ultra}`（key=可选值，value=线上拼写） |
| `retryPolicy` | ❌ | 重试策略（改动会触发路由原地重注册） |
| `compat.thinkingFormat` | ❌ | 端点无法被自动识别时的推理方言（如 `deepseek`） |

### 12.3 【实跑】自定义路由验证（拿 DeepSeek 端点当靶子）

```powershell
python artifacts/agent-spec/probe_provider.py `
  artifacts/agent-spec/patches/31-provider-compat-existing-row.yml `
  deepseek-compat deepseek-v4-flash provider-compat3 "只回复两个字：你好"
```

输出：

```text
OK elapsed=4.8s finish=completed
FINAL: 你好
```

落盘 `provider-compat3.provider-probe.json`：

```json
{
  "provider": "deepseek-compat",
  "model": "deepseek-v4-flash",
  "elapsed_s": 4.8,
  "finish_reason": "completed",
  "final_response": "你好",
  "request_config": {"provider": "deepseek-compat", "model": "deepseek-v4-flash", "maxTokens": 384000},
  "tool_count": 25
}
```

即：**完全手写的 provider id + 自定义 baseURL + 从环境变量取 key，真实跑通了**。
`maxTokens: 384000` 正是 patch 里为这个路由声明的值 → 证明配置确实生效。

⚠️ **未实跑项**：百炼本身没跑（`.env` 里 `BAILIAN_API_KEY` 为空）。
`baseURL: https://dashscope.aliyuncs.com/compatible-mode/v1` 是按百炼官方 OpenAI 兼容端点填的，
**接入后请用同一脚本跑一次 hello 作为验收**（只换 provider/model 两个参数，脚本不用改）。

### 12.4 落地写法：一套代码两条端点

```python
# config.py（读 .env）
AI_PROVIDER = os.environ.get("AI_PROVIDER", "deepseek")     # 'deepseek' | 'bailian'

# agent/runtime.py：构建 DeepSeekHarness 时按开关选路
ROUTES = {
    "deepseek": ("deepseek-official", os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")),
    "bailian":  ("bailian",           os.environ.get("BAILIAN_MODEL", "qwen-plus")),
}
provider, model = ROUTES[AI_PROVIDER]      # → DeepSeekHarness(provider=..., model=...)
```

要点：

1. patch 里 **两条路由同时声明**；哪条生效由 `initialize` 的 `provider` 参数决定
   （【源码】SDK `client.py:133-150` 把 `provider`/`model` 放进 `initialize` payload）；
2. `deepseek-official` 是 `@deepseek-ai/dsh-llm-deepseek` **独占**的路由
   （【源码】`const PROVIDER = 'deepseek-official'`），走 `DEEPSEEK_BASE_URL`/`DEEPSEEK_API_KEY`；
   百炼必须走 pi-ai 手写路由；
3. **没被选中的路由不会因为缺 key 报错** —— key 是**每请求**惰性解析的
   （【源码】`resolveApiKey` 在发请求时调用）。所以 `.env` 里只有一把 key 也能正常启动；
4. 路由键就是模型上下文里的 provider 名，`request/header.config.provider` 会如实显示
   —— 演示时可用它证明「这次真的走的是百炼」；
5. 【源码】provider/模型/端点/key 都是**每请求**解析，改 `.env` 后对下一次请求即生效；
   只有「路由集合」或 `retryPolicy` 变化才会原地重注册适配器。

---

## 13. 追加：MCP 工具列表的同步时机（实测）

> 结论先行：**是「启动时一次 + 之后按需刷新」，不是每次提问都拉。刷新只由两类事件触发：
> 首次连接、以及服务端主动发 `notifications/tools/list_changed`（或断线重连）。**

### 13.1 【实跑】证据：会话中途工具列表变小，dsh 当场重同步了

我写了一个会**动态改自己工具列表**的 MCP 服务器
（`artifacts/agent-spec/probe_dynmcp_server.py`：`switch_mode(shrunk)` 移除 `echo` 并发 `list_changed`），
在**一次会话**里让模型连着调两个工具（脚本 `probe_dyn_resync.py`）：

```text
request/header #1 tools(3): ['mcp__wuye__echo', 'mcp__wuye__list_my_tools', 'mcp__wuye__switch_mode']
  tool/call -> mcp__wuye__switch_mode {"mode": "shrunk"}
  tool/result -> mode switched to shrunk; advertised tools: list_my_tools,switch_mode
  tool/call -> mcp__wuye__list_my_tools {}
  tool/result -> mode=shrunk tools=list_my_tools,switch_mode

RESYNC: CHANGED (重新同步发生了)
```

**step2 的 `request/header` 里 `mcp__wuye__echo` 已经消失** → dsh 收到 `tools/list_changed` 后
真的重拉了 `tools/list` 并替换了注册的工具代。

方向也验证了：`list_my_tools` 自己只认 2 个工具（因为它读的是实时 `list_tools()`，与 dsh 侧同步一致）。

### 13.2 同步算法（【源码】运行时内联 `dsh-mcp-client` 的 `syncTools`）

两阶段，注释原文说明了每个分支：

```text
1. Fetch 阶段：把 tools/list 全部分页拉完，在内存里构造「下一代」ToolDefinition。
   任何失败（网络错误 / 服务端重复列出同名工具 / 重复的 continuation cursor）
   → reject，**上一代工具原封不动保留**。
2. Swap 阶段：先 dispose 上一代，再注册新一代。
   如果注册时报冲突（只可能是外部注册者占了 mcp__<serverName>__ 命名空间）
   → **整个新代回滚，本 server 注册 0 个工具**并记 error 日志（不会出现"半套工具"）。
```

`suspend/resume` 的序列化：supervisor 用**一条队列**串行化所有同步
（首次、通知触发、重连触发），两代之间不会交错 dispose/register。
插件卸载或重连预算耗尽（默认连续失败 10 次）→ 工具全部注销。

### 13.3 对 README 那句话的准确修正

运行时 README 写的是「An update that conflicts with an already-registered tool name is rejected
entirely — you never get a partial tool set from that server」。**实测与源码一致，但要说清三点**：

1. 这句说的是**命名冲突**（命名空间被别的插件占了），不是「工具集合变化被拒绝」。
   **正常的增删工具不会被拒绝** —— 上面 §13.1 就是列表变小并成功替换的实证；
2. 真正会「保留上一版」的是 **Fetch 阶段失败**（连不上、列表非法），不是集合变化；
3. fetch 失败保留上一代之后，**它还是会被下一轮模型看到并调用**，调用时要么打旧代（还有连接）要么失败
   —— 不会因为 fetch 失败就自动摘掉工具。

**所以：本项目的动态能力目录不会被 dsh 单方面「拒绝」，只要服务端在列表变化时发 `list_changed`。**

### 13.4 对本项目「权限变化要重建运行时吗」的结论

| 场景 | 模型侧工具目录会怎样 | 是否需要重建运行时 |
| --- | --- | --- |
| 用户登录后首次提问 | 启动时握手拉一次，按该用户权限返回子集 | 首次必然新建 |
| **用户角色/数据范围变了**（`auth_version` 变） | **不会自动变**：服务端不会主动重发列表，dsh 也没有轮询 | **是**（现方案正确） |
| 用户登出后再登录 | 同上一行 | 是（并在重建时换新令牌） |
| 同一用户权限参数被管理员改动、但 `auth_version` 也变了 | 同上 | 是 |
| 工具清单更新（改代码加了工具） | 同上 | 是（重启后自然生效） |
| **服务端在一次工具调用里主动发 `list_changed`** | **当场重同步**（§13.1 实证） | **否** |

**你现在的做法（权限变化就回收运行时）是对的，依据就在上表第二行**：
dsh 只在「首次连接」和「服务端主动通知」时刷新；而*权限变小*这件事我们的 MCP server 不会主动通知，
所以模型侧的目录会停留在旧权限。

**安全上不必担心**（两层防线）：即使用户权限变小、模型侧还留着旧工具，
`agent/mcp_server.py` 每次 `tools/call` 都会用令牌重新 `read_token` → 校验 `auth_version` →
`Policy.require` 二次拒绝。所以**最多是「模型以为自己能调、调用被拒」**，不会越权。

**一个可选优化**（若将来允许权限热变更而不重启）：把 `auth_version` 变化做成事件，
由服务端在**下一次该用户的工具调用里**（或由一个后台 SSE 通道）发
`notifications/tools/list_changed`，dsh 会当场重拉并按新权限替换工具代
—— 这样就能省掉一次约 3 秒的进程重建。本轮**不做**，保持「重建最简单、最不容易错」。

### 13.5 复现命令

```powershell
# 起一个动态工具列表的 MCP server + 一次会话内触发 list_changed
python artifacts/agent-spec/probe_dyn_resync.py
# 原始通知：artifacts/agent-spec/dyn-mcp.notifications.json
```

依赖的 patch：`artifacts/agent-spec/patches/32-dyn-mcp.yml`（同样是「换提示词 + 挂 MCP + 32 条禁用」）。

