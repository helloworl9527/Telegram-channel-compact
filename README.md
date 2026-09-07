# TG 消息收集

Telegram 人工白名单消息采集与按来源 AI 总结工具。当前只进行本机开发和测试；本机验收通过并再次获得用户批准前，不上传或部署到 Ubuntu 服务器。

## 当前能力

- Telethon 用户会话登录；
- 人工白名单来源，默认 `@hezu1`；
- 最近 6 个自然日回填和断线补拉；
- 新增、编辑、删除消息增量处理；
- 普通来源消息保存可生成的 Telegram 消息链接；
- 特殊频道提取正文 URL，并仅保存发送者简介文本；不保存身份和 Telegram 消息链接；
- SQLite WAL、消息去重和完整编辑历史；
- OpenAI 兼容接口及自定义 `base_url`；
- 按来源、无重叠时间窗口分块总结；
- 中文固定栏目：主题、重要信息、主要观点、消息来源；
- 每周日 03:00 到期强制清理；
- 状态查询和 SQLite 一致性备份。

## 快速开始

```bash
uv sync --dev
cp config.example.yaml config.yaml
cp .env.example .env
uv run pytest tests -q
uv run tg-collector --config config.yaml init-db
uv run tg-collector --config config.yaml status
```

项目不会自动读取 `.env`，实际运行前应由当前终端或进程管理器安全注入环境变量。

完整需求见 `docs/requirements.md`，本机联调和验收步骤见 `docs/local-testing.md`。
仅供兼容性检查、不得在当前阶段执行的服务器草案见
`docs/ubuntu-deployment-draft.md`。

## 命令

```text
tg-collector login
tg-collector sources list
tg-collector sources validate
tg-collector backfill
tg-collector run
tg-collector summarize
tg-collector scheduler
tg-collector cleanup --dry-run
tg-collector cleanup
tg-collector backup --destination backups/telegram.sqlite3
tg-collector status
```

所有命令均可在前面添加 `--config config.yaml` 指定配置文件。

特殊频道需先存在于总白名单，再单独启用：

```yaml
telegram:
  allowlist:
    - "@普通频道"
    - "@店铺频道"
  shop_url_channels:
    - "@店铺频道"
```

特殊频道会保存正文中规范化后的 URL 和当次取得的简介文本。不会保存 Telegram 消息链接，不会建立用户表，也不会在该类消息记录中保存发送者身份。

## 敏感文件

以下内容不得提交或发送给他人：

- `.env`；
- Telegram `.session` 文件；
- `data/` 数据库和总结；
- 包含私密消息正文的日志或导出。
