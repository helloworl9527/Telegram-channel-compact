# Progress Log

## 2026-08-29 - Task: 编写 Telegram 消息采集与 AI 总结系统需求文档

### What was done

- 明确项目先在本机开发和测试，通过本机验收并再次获得用户批准后，才允许上传和部署到 Ubuntu 服务器。
- 定义每天 1 万至 2 万条纯文本消息、最近 6 个自然日逻辑保留、每周清理以及清理前约 13 个自然日峰值的业务口径。
- 定义 Telegram 登录、来源白名单、首次同步、增量采集、去重、SQLite 存储、AI 分块总结、清理、日志、备份和状态查询需求。
- 定义本机验收标准、当前非目标、待确认事项和服务器上线门禁。

### Testing

- 使用 Python 读取并校验 `docs/requirements.md` 的关键章节、核心业务口径和标识唯一性。
- 校验结果：18 项功能需求、5 项非功能需求、8 项本机验收标准全部存在；无缺失必需章节，无重复需求标识；验证通过。
- 当前目录不是 Git 仓库，因此未执行 Git 差异检查。

### Notes

- `docs/requirements.md`：新增 Telegram 消息采集与 AI 总结系统需求文档。
- `progress.md`：新增本轮正式文档交付和验证记录。
- 回滚方式：删除 `docs/requirements.md`，并删除 `progress.md` 中本任务记录；由于当前目录不是 Git 仓库，不存在可用提交回滚点。

## 2026-08-29 - Task: 根据用户确认更新需求基线

### What was done

- 将来源范围确定为人工白名单，并明确本机验收使用真实频道进行只读采集测试；如白名单包含群组，则同时测试真实群组。
- 将 AI 接口确定为可自定义 `base_url` 的 OpenAI 兼容接口。
- 将默认总结时间确定为 `Asia/Shanghai` 的 08:00、12:00、22:00，并定义连续、不重叠的三个左闭右开时间窗口。
- 将保留规则改为到期强制删除原始消息，不因总结失败延期；对应总结记录和 Markdown 文件随原始消息同步删除。
- 更新相关功能需求、本机验收标准、已确认事项和剩余待确认参数。

### Testing

- 使用 Python 校验需求文档包含人工白名单、OpenAI 兼容 `base_url`、三个总结窗口、强制删除、总结同步删除和真实来源测试等确认内容。
- 校验三个总结窗口时长为 10 小时、4 小时、10 小时，总计完整覆盖 24 小时。
- 检查并确认旧的“总结失败延缓删除”“总结长期保留”等冲突表述均已移除。
- 校验 31 个功能、非功能和验收标识无重复；一致性验证通过。

### Notes

- `docs/requirements.md`：更新已确认的来源、AI、调度、保留和本机测试需求。
- `progress.md`：追加本轮需求基线更新及验证记录。
- 回滚方式：将 `docs/requirements.md` 恢复至本记录之前版本，并删除 `progress.md` 中本任务记录；当前目录不是 Git 仓库，无可用提交回滚点。

## 2026-08-29 - Task: 实施本机可运行的第一版系统

### What was done

- 初始化 Python 包、`uv.lock`、CLI、示例配置、环境变量模板和忽略规则。
- 实现 SQLite WAL 数据库、来源表、消息去重、编辑历史、同步游标、总结任务、总结结果、清理记录和一致性备份。
- 实现 Telegram 人工白名单解析、最近 6 个自然日回填、新增/编辑/删除事件处理，以及基于持久化游标的 5 分钟主动补拉。
- 实现 OpenAI 兼容客户端、可配置 `base_url`/模型、按来源分块总结、固定中文栏目、输入哈希幂等和失败重试上限。
- 实现 `Asia/Shanghai` 可配置总结时间、连续左闭右开窗口、失败来源隔离、每周清理及错过调度后的补执行。
- 实现到期强制删除原始消息、编辑历史、总结记录和对应 Markdown 文件；删除不依赖总结是否成功。
- 实现 Telegram session 目录 `0700` 和文件 `0600` 权限收紧。
- 提供 `init-db`、`status`、`login`、`sources`、`backfill`、`run`、`summarize`、`scheduler`、`backup`、`cleanup` 命令。
- 新增本机测试指南和只供兼容性检查、当前不得执行的 Ubuntu 部署草案。
- 根据独立安全审查补强：重新解析白名单会替换内存来源并禁用已移除来源；来源解析/回填/补拉故障隔离；断线补拉审计近期编辑并遵守保留截止点；支持清空正文的编辑。
- 总结任务增加原子领取、分块结果缓存、递归归并和完整请求长度限制；清理先删除 Markdown 文件，文件删除失败时不提交数据库删除或完成记录；总结调度与强制清理解耦。
- 数据库、备份、总结目录和文件使用受限权限；清理路径校验限定在总结目录；时间比较兼容秒级旧格式；状态增加授权可见性、下一窗口、最近成功总结及清理起止时间。

### Testing

- `uv run pytest tests -q`：47 项测试全部通过。
- `uvx ruff check src tests`：通过。
- `uvx mypy src/tg_collector --ignore-missing-imports --no-error-summary`：通过。
- 测试覆盖率为 86%；存储层 96%、总结层 93%、Telegram 层 87%。
- `uv run python -m compileall -q src tests`：通过。
- `uv build`：成功生成源码包和 wheel。
- 将最新 wheel 安装到全新临时虚拟环境后，CLI `--help` 和包版本导入烟雾测试通过。
- 无凭据烟雾测试通过：初始化数据库、查询状态、列出白名单、清理预览和 SQLite 在线备份。
- 主数据库和备份数据库 `PRAGMA integrity_check` 均返回 `ok`。
- 从备份数据库读取后继续写入消息并推进同步游标的恢复烟雾测试通过。
- 静态安全搜索未发现硬编码凭据、`shell=True`、`eval`、`exec` 或不安全 pickle 反序列化。
- 最终回归后重新执行 `uv run pytest tests -q`、Ruff、Mypy、`compileall` 和 `uv build`，全部通过。
- wheel 在全新临时 Python 3.12 环境安装成功，CLI 帮助和包版本 `0.1.0` 导入测试通过。
- 本机 CLI 无凭据烟雾验证通过：数据库初始化、状态、清理预览、备份；主库和备份 `PRAGMA integrity_check` 均为 `ok`。

### Pending real integration

- 尚未执行 Telegram 交互登录、真实频道来源解析、真实消息回填或持续采集；需要用户在本机终端安全注入 Telegram 配置并完成验证码/两步验证。
- 尚未调用真实 OpenAI 兼容接口；需要实际 `base_url`、模型和安全注入的 API key。
- 因以上真实联调尚未完成，本机第 10 节验收门禁尚未全部通过，当前仍禁止上传或部署到 Ubuntu。
- 当前目录仍不是 Git 仓库，没有提交或 Git 回滚点。

## 2026-08-30 - Task: 新增特殊频道 URL、消息链接与简介采集并修复复审问题

### What was done

- 新增 `telegram.shop_url_channels`，要求特殊来源同时存在于人工 `allowlist`，不得扩大采集范围。
- 特殊频道消息提取带协议 URL 和域名形式链接；无协议域名规范化为 `https://`，同一消息去重，编辑后按最新正文替换 URL 集合。
- 为可生成链接的消息保存公开 `t.me/<username>/<message_id>` 或私有超级群组/频道 `t.me/c/<internal_id>/<message_id>` 链接。
- 特殊频道只保存发送者公开简介文本 `bio`；不建立用户表，不在特殊频道消息中保存发送者身份字段，不保留简介历史。
- 以精确微秒 UTC 比较替换 SQLite `julianday()`，修复微秒编辑覆盖和清理截止边界；跨保留截止点总结窗口按实际保留范围裁剪。
- 离线编辑改为按保留期内已保存消息 ID 分批精确回查，并在采集客户端返回时执行最终补拉。
- 来源解析、回填和补拉错误写入持久状态，状态命令显示来源错误和同步错误。
- 总结任务增加领取 token、续租和 owner 条件；输入变化时删除旧总结、缓存和旧 Markdown；Markdown 使用临时文件、`fsync` 和原子替换。
- 清理增加待删除文件事务日志，文件删除失败或进程中断后可在下次清理恢复；备份拒绝活动数据库的 hard link/同 inode 别名并采用原子替换。
- 配置校验拒绝非法时间、浮点整数、错误列表/映射类型；明确 `max_retries` 表示首次请求之后的重试次数。

### Testing

- `uv run pytest tests -q`：68 项测试全部通过。
- `uvx ruff check src tests`：通过。
- `uvx mypy src/tg_collector --ignore-missing-imports --no-error-summary`：通过。
- `uv run python -m compileall -q src tests`：通过。
- `uv build`：成功生成源码包和 wheel。
- 静态安全搜索未发现 `shell=True`、`os.system`、`eval`、`exec`、不安全 pickle 或硬编码凭据。

### Pending real integration

- 尚未执行 Telegram 交互登录、真实频道来源解析、真实消息回填或持续采集；需要用户在本机终端安全注入 Telegram 配置并完成验证码/两步验证。
- 尚未调用真实 OpenAI 兼容接口；需要实际 `base_url`、模型和安全注入的 API key。
- 本机真实联调完成并再次取得用户明确批准前，禁止上传或部署到 Ubuntu。

## 2026-08-30 - Task: 关闭第二轮独立复审阻断项

### What was done

- 备份目标显式拒绝主数据库及其 `-wal`、`-shm`、`-journal` 路径和同 inode 别名。
- `shop_url_channels` 与 `allowlist` 使用统一来源规范化规则比较，兼容 `@username`、公开 `t.me` 链接和数字 chat_id。
- 总结任务只按来源和时间窗口唯一；模型与提示词变化会更新同一任务，不会刷新其他 owner 的租约。
- 总结 Markdown 使用 owner 锁定的数据库事务发布，stale owner 无法覆盖当前 owner 文件。
- 完成缓存命中前校验 Markdown 不是符号链接、位于配置目录、可读且内容与数据库一致；缺失或损坏时重新生成。
- 临时 Telegram 简介读取失败不再永久缓存为空，后续消息会重试。
- 增加旧 summary_jobs 唯一约束迁移测试、侧车备份防护测试、stale owner 发布测试、输出损坏恢复测试和简介重试测试。

### Testing

- `uv run pytest tests -q`：75 项测试全部通过。
- `uvx ruff check src tests`：通过。
- `uvx mypy src/tg_collector --ignore-missing-imports --no-error-summary`：通过。
- `uv run python -m compileall -q src tests`：通过。
- `uv build`：成功生成源码包和 wheel。

### Gate status

- 最后一轮独立复审进行中。
- 真实 Telegram、真实 AI 接口和 Ubuntu 上传/部署仍未执行。

## 2026-08-30 - Task: 关闭第三轮复审发现的安全边界

### What was done

- 清理和输入变化删除旧总结时改为词法路径校验，只解除总结目录中的符号链接本身，不跟随链接删除目标文件。
- Telegram 来源规范化严格拒绝 `t.me/c`、`t.me/s`、消息路径和邀请路径。
- 白名单和特殊频道比较统一处理大小写；数字 chat_id 与用户名的等价关系延迟到 Telegram 实体解析阶段确认。
- `complete_summary_job` 使用 `BEGIN IMMEDIATE` 和最终更新行数校验，避免 owner 抢占窗口覆盖新执行者结果。
- 备份不改变已存在目标父目录权限。

### Testing

- `uv run pytest tests -q`：87 项测试全部通过。
- `uv run pytest tests --collect-only -q`：精确收集 87 项。
- `uvx ruff check src tests`：通过。
- `uvx mypy src/tg_collector --ignore-missing-imports --no-error-summary`：通过。
- `uv run python -m compileall -q src tests`：通过。
- `uv build`：成功生成源码包和 wheel。

### Gate status

- 最终独立复审进行中。
- 真实 Telegram、真实 AI 接口和 Ubuntu 上传/部署仍未执行。

## 2026-08-30 - Task: 关闭第四轮复审的迁移和路径别名问题

### What was done

- 拒绝 `t.me/c`、`t.me/s` 单段路径及大小写变体，防止误解析为普通用户名。
- 旧 summary_jobs 重复任务对应的 Markdown 文件登记到无外键 orphan 删除队列，由后续清理安全删除。
- 清理路径通过 canonical parent 统一 macOS `/var` 与 `/private/var` 别名，同时不解析最终符号链接组件，确保只删除链接本身。
- 增加旧任务 orphan 文件迁移、macOS 路径别名和保留路径负例回归测试。

### Testing

- `uv run pytest tests -q`：92 项测试全部通过。
- `uv run pytest tests --collect-only -q`：精确收集 92 项。
- `uvx ruff check src tests`：通过。
- `uvx mypy src/tg_collector --ignore-missing-imports --no-error-summary`：通过。
- `uv run python -m compileall -q src tests`：通过。
- `uv build`：成功生成源码包和 wheel。

### Gate status

- 当前代码的最终独立复审进行中。
- 真实 Telegram、真实 AI 接口和 Ubuntu 上传/部署仍未执行。

## 2026-08-30 - Task: 建立总结发布与清理统一持久状态机

### What was done

- 总结任务认领必须原子匹配准备时的 `input_hash`、模型和提示词版本，旧快照不能认领新 revision。
- 发布采用 `running -> publishing -> completed` 两阶段协议；发布目标先写持久文件日志，再在 owner 写锁内替换文件和提交总结记录。
- 清理先将到期任务切换为 `deleting` 并清空 owner，运行中的旧任务无法继续续租或发布。
- 输入变化、损坏文件、迁移淘汰文件和发布中断文件统一进入持久删除日志；正常删除后才确认日志。
- 总结输出恢复为稳定窗口文件名，新 owner 原子替换同一路径，避免 token 文件积累。
- 配置根节点及 `cleanup`、`telegram`、`ai` 子节点拒绝未知键；数值必须有限。
- Telegram 来源只接受合法 `@username`、数字 chat_id 和单段公开 `t.me/<username>`。
- 特殊频道数字/用户名跨形式关系在 Telegram 实体解析后最终确认，未匹配项进入错误状态且不启用特殊采集。

### Testing

- `uv run pytest tests -q`：103 项测试全部通过。
- `uv run pytest tests --collect-only -q`：精确收集 103 项。
- `uvx ruff check src tests`：通过。
- `uvx mypy src/tg_collector --ignore-missing-imports --no-error-summary`：通过。
- `uv run python -m compileall -q src tests`：通过。
- `uv build`：成功生成源码包和 wheel。
- 新数据库 `PRAGMA integrity_check`：`ok`。

### Gate status

- 统一状态机版本的最终独立复审进行中。
- 真实 Telegram、真实 AI 接口和 Ubuntu 上传/部署仍未执行。

## 2026-08-31 - Task: 关闭 deleting 复活和来源 scheme 边界

### What was done

- `deleting` 状态成为不可逆 tombstone；新输入、模型、提示词或 snapshot revision 都不能将其恢复为 pending。
- Telegram 公开来源 URL 只允许 HTTP/HTTPS，拒绝 FTP 和其他 scheme。
- 增加 deleting 期间 revision 变化和 FTP 来源负例回归测试。

### Testing

- `uv run pytest tests -q`：112 项测试全部通过。
- `uv run pytest tests --collect-only -q`：精确收集 112 项。
- `uvx ruff check src tests`：通过。
- `uvx mypy src/tg_collector --ignore-missing-imports --no-error-summary`：通过。
- `uv run python -m compileall -q src tests`：通过。
- `uv build`：成功生成源码包和 wheel。

### Gate status

- 当前最新版本的独立终审进行中。
- 真实 Telegram、真实 AI 接口和 Ubuntu 上传/部署仍未执行。

## 2026-09-01 - Task: 完成本机真实 Telegram 与 AI 验收

### Telegram acceptance

- 真实账号登录成功，session 可复用。
- 人工白名单只解析 `@hezu1`，稳定 chat_id 为 `-1001402371030`，未采集其他来源。
- 首次六日回填处理 909 条消息；重复回填后消息和版本数量不增长，重复主键和重复版本均为 0。
- 909/909 条初始消息均保存 Telegram 消息链接。
- `@hezu1` 按用户确认作为普通来源，不启用店铺 URL 或简介采集。
- 真实实时采集运行后数据库推进至 1135 条消息，最新消息 ID 为 `11190975`，来源错误为 0。
- 实时进程运行 20 秒后经 SIGINT 正常退出，退出码 0，无断线请求错误或 traceback。

### AI acceptance

- 使用真实 OpenAI 兼容接口执行总结。
- 首次执行成功 13 个窗口、3 个窗口超时；后续重试复用分块缓存并最终完成全部 16 个窗口。
- 最终状态：completed 16、failed 0、pending 0、running 0。
- 16 个 Markdown 文件全部存在、四个固定栏目完整，文件内容与数据库一致。
- 再次执行总结后任务、总结和缓存计数不增加，确认幂等和缓存复用。

### Backup and cleanup acceptance

- 创建 `backups/telegram-real-acceptance.sqlite3` 一致性备份。
- 备份与主库消息/总结/任务计数一致，双方 `PRAGMA integrity_check` 均为 `ok`。
- 备份文件权限为 `0600`。
- 清理 dry-run 成功，当前到期消息和总结均为 0，未删除数据。

### Final verification

- `uv run pytest tests -q`：114 项测试全部通过。
- Ruff、Mypy、compileall 和 wheel 构建全部通过。
- 当前数据库：1135 条消息、16 个成功总结、0 个失败总结、无重复键、`integrity_check=ok`。

### Deployment gate

- 本机真实验收通过。
- 尚未上传或部署到 Ubuntu；必须取得用户再次明确批准后才能进入服务器部署阶段。

## 2026-08-31 - Task: 最终本机门禁与独立复审阻塞记录

### Verification

- 当前最新代码：112 项 pytest 全部通过。
- Ruff、Mypy、compileall、wheel 构建全部通过。
- 独立复审两次尝试均因 OpenAI provider 使用额度耗尽返回 HTTP 429，未形成完整结论，不能标记为独立复审通过。

### Gate status

- 本机自动化门禁通过。
- 独立审查门禁因外部 provider 额度阻塞，状态为未完成而非失败或通过。
- 真实 Telegram、真实 AI 接口和 Ubuntu 上传/部署仍未执行。

## 2026-09-02 - Task: 验收未加入的特殊公开群组 URL 与简介采集

### What was done

- 使用独立配置和数据库验收公开群组 `@qiuqiuai1919`，未修改正式白名单及现有真实数据。
- 确认采集账号在回填前后均未加入该群组，Telegram 仍允许读取公开历史消息。
- 特殊来源按要求保存消息链接、提取 URL 和发送者简介，同时不保存发送者身份。
- 真实数据暴露两个边界问题并完成修复：频道身份发言不再错误调用用户简介接口；无协议 URL 提取不再把 `0.85`、`11.30` 等小数价格识别为域名。
- 为上述两个问题分别增加回归测试。

### Testing

- 真实回填完成 2929 条消息；2929 条均有 Telegram 消息链接，发送者身份字段非空数量为 0，2744 条保存了非空简介。
- 提取 273 条 URL，覆盖 273 条消息、6 个不同 URL；不存在纯数字小数域名、重复消息主键或重复消息 URL。
- SQLite `PRAGMA integrity_check` 返回 `ok`。
- 回填结束后再次确认账号未加入群组。
- 全量 pytest、Ruff 和 Mypy 均通过。

### Notes

- 验收配置：`config.qiuqiuai1919-acceptance.yaml`。
- 验收数据库：`data/qiuqiuai1919-acceptance.sqlite3`。
- 当前仍未上传或部署到 Ubuntu。

## 2026-09-02 - Task: 特殊来源停止保存 Telegram 消息链接

### What was done

- 按最新要求调整特殊来源：继续提取正文 URL 和读取用户简介，但 `source_url` 不再保存 Telegram 消息链接。
- 普通来源仍保留 Telegram 消息链接。
- 更新需求文档及回归测试。

### Testing

- 特殊来源测试确认 `message_url` 为空、简介和正文 URL 正常保存、发送者身份为空。
- 全量 pytest、Ruff 和 Mypy 均通过。

## 2026-09-02 - Task: 新增项目级 AGENTS.md

### What was done

- 在项目根目录创建标准 `AGENTS.md`，供兼容的编程代理自动读取。
- 记录项目结构、环境命令、测试门禁、Telegram 只读规则、特殊 URL/简介来源的数据最小化、保留策略、文件安全和 Ubuntu 部署门禁。
- 明确测试配置不能被视为正式采集配置，真实凭据和数据不得进入聊天或版本库。

### Verification

- 校验文件存在，并包含必需的测试、安全、特殊来源和部署章节。
- 已记录用户的长期约定：以后新建软件项目时自动在项目根目录创建 `AGENTS.md`。
