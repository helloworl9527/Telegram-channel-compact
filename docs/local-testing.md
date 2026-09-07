# 本机测试指南

本项目当前只允许在本机开发和测试。本机验收全部通过并再次获得用户批准前，不上传或部署到 Ubuntu 服务器。

## 1. 准备配置

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

项目不会自动读取 `.env`；应在当前终端安全加载环境变量，或由进程管理器注入。不要将填写后的 `.env`、Telegram session 或数据库提交版本控制。

Telegram 人工白名单默认包含 `@hezu1`。测试保持只读，不向频道或群组发送、编辑或删除消息。

如需验收特殊频道，在 `allowlist` 和 `shop_url_channels` 中同时列出该来源。`shop_url_channels` 不能包含总白名单之外的来源。

## 2. 安装与静态验证

```bash
uv sync --dev
uv run pytest tests -q
uv run python -m compileall -q src tests
uv run tg-collector --config config.yaml init-db
uv run tg-collector --config config.yaml status
```

## 3. Telegram 联调

配置 Telegram 环境变量后执行：

```bash
uv run tg-collector --config config.yaml login
uv run tg-collector --config config.yaml sources validate
uv run tg-collector --config config.yaml backfill
uv run tg-collector --config config.yaml status
```

验收要点：

- 只解析并采集人工白名单来源；
- 首次同步只保留当前自然日及过去 5 个自然日；
- 重复回填不产生重复消息；
- 消息编辑后，最新内容和各次不同版本均可保留；
- Telegram 删除事件被收到时，原消息及编辑历史立即删除。
- 每条可生成链接的消息保存公开 `t.me/<username>/<message_id>` 或私有超级群组/频道 `t.me/c/...` 链接；
- 特殊频道正文中的带协议 URL 和域名形式链接均被提取并去重；
- 特殊频道只保存消息发送者简介文本，消息记录中的发送者身份为空，且数据库不建立用户主页表；
- 编辑特殊频道消息后，已从正文删除的 URL 不再保留；
- 简介读取失败只使简介为空，不阻塞消息保存。

## 4. 持续采集

```bash
uv run tg-collector --config config.yaml run
```

持续采集命令启动时先注册实时事件，再回填保留窗口；运行期间每 5 分钟根据持久化游标主动补拉一次，用于恢复事件注册、断线或重连期间可能遗漏的消息。随后持续监听新增、编辑和删除事件。

## 5. AI 联调

配置 OpenAI 兼容 `base_url`、API key 和模型后执行：

```bash
uv run tg-collector --config config.yaml summarize
uv run tg-collector --config config.yaml scheduler
```

默认总结窗口（`Asia/Shanghai`）：

- 前一日 22:00 至当日 08:00；
- 当日 08:00 至 12:00；
- 当日 12:00 至 22:00。

窗口使用左闭右开区间。输出为中文固定栏目：主题、重要信息、主要观点、消息来源。

## 6. 清理与备份

先预览：

```bash
uv run tg-collector --config config.yaml cleanup --dry-run
```

确认后清理：

```bash
uv run tg-collector --config config.yaml cleanup
```

到期原始消息无论是否总结成功都会删除，对应总结记录和 Markdown 文件同步删除。默认调度时间为 `Asia/Shanghai` 每周日 03:00；如果程序在该时刻未运行，调度器重启后会补执行本周尚未完成的清理。清理循环独立于 AI 总结：AI 配置缺失、请求失败或请求较慢时，清理仍会继续执行；清理记录包含开始和完成时间，Markdown 删除失败时会保留数据库记录以便下次重试。

创建一致性数据库备份：

```bash
uv run tg-collector --config config.yaml backup --destination backups/telegram.sqlite3
```

Telegram session 不包含在普通数据库备份中，应单独按高敏感凭证管理。CLI 会将 session 目录限制为 `0700`、session SQLite 文件限制为 `0600`。

## 7. 本机验收门禁

完成 `docs/requirements.md` 第 10 节全部验收项后，记录真实测试证据。只有用户再次明确批准，才进入 Ubuntu 上传和部署阶段。
