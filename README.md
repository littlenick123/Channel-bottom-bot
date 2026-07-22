# 纯 Bot API 多租户频道置底机器人

机器人监听已经绑定的 Telegram 频道。频道出现新帖子后，它会等待默认 10 秒，将这段时间内的连发和相册事件合并，然后删除上一组置底帖，并按槽位编号从大到小重发。`1号`最后发送，因此位于频道最底部。

## 功能

- 每个用户拥有互相隔离的个人草稿库；频道和超级群组配置由已绑定且当前仍是管理员的用户共享。
- 支持文本、格式、图片、视频、文件、相册和多行 URL 按钮。
- 草稿内容复制到专用私密存储频道，不显示“转发自”；发布到槽位时锁定不可变版本。
- 每频道默认 10 个槽位，可独立启停、替换、清空，并支持总开关、静默发送、延迟和立即刷新。
- 基于 aiogram 与 Telegram HTTP Bot API，仅需 Bot Token，不需要 API ID、API Hash 或用户 session。
- SQLite WAL 持久化、防抖调度、RetryAfter 延期、网络错误退避、半批次恢复和管理员故障通知。
- 频道和超级群组成员统计：当前总人数、今天/昨天的加入、离开和净变化；支持 `/stats` 查询及北京时间每日合并推送。
- `/health` 只向配置的运维账号提供聚合计数，不提供绕过频道权限的内容浏览入口。

## Telegram 准备

1. 通过 BotFather 创建机器人并取得 Bot Token。
2. 创建一个只有部署者可见的私密“草稿存储频道”，将机器人设为管理员，并授予发布消息和删除消息权限；取得其 `-100...` ID。机器人会把待确认和已保存草稿的副本写入此频道，并在放弃或过期时删除待确认副本。
3. 将机器人加入需要管理的**目标频道或超级群组**并设为管理员。频道需授予发布消息、删除消息和查看频道消息权限；超级群组需授予发送消息、删除消息权限。这些权限也是成员变更统计、自动刷新和清理上一组置底帖的前提。
4. 目标频道或超级群组中只要机器人被提升为管理员，机器人就会自动发现并绑定该对象。若提升操作的发起管理员此前已 `/start` 并且机器人可向其私聊，则会收到包含数字 ID 的提示；通知无法送达不会影响自动绑定。此时不必再手动输入 `@username`、`-100...` ID 或转发帖子；这三种方式仍可用于手动绑定。

> 存储频道永远不会被自动发现或自动绑定，即使机器人在其中被提升为管理员。已绑定频道帖子和超级群组的普通成员消息都会触发自动刷新；机器人自身和服务消息会被忽略。

> 服务器所有者能访问本机数据库和存储频道。应用隔离保护的是机器人普通用户之间的数据边界。

## 本地运行（Windows / Linux）

需要 Python 3.12。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

Linux：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
cp .env.example .env
```

编辑 `.env` 后，把变量载入当前进程并启动：

```powershell
Get-Content .env | ForEach-Object {
    if ($_ -match '^[^#].*=') {
        $name, $value = $_ -split '=', 2
        Set-Item -Path "Env:$name" -Value $value
    }
}
python -m bottom_post_bot
```

Linux 可使用：

```bash
set -a
. ./.env
set +a
python -m bottom_post_bot
```

项目故意不自动读取 `.env`，避免部署时意外覆盖由容器、systemd 或密钥管理器注入的值。

### 从旧 Telethon 版本升级

数据库会自动升级并增加 Bot API `file_id` 字段。旧版纯文本草稿可以继续使用；旧版媒体草稿没有可供 Bot API 使用的 `file_id`，需要重新转发给机器人并重新发布到对应槽位。升级后可删除旧的 `telethon.session` 文件。

## Docker

```bash
cp .env.example .env
# 编辑 .env
docker compose up -d --build
docker compose logs -f bottom-post-bot
```

数据库位于 `./data`。Compose 已配置最多五个、每个 10 MB 的日志文件。

升级到含成员统计的版本后，数据库迁移会在容器启动时自动执行。拉取新代码并修改 `.env` 后执行一次：

```bash
docker compose up -d --build
docker compose logs --tail=100 bottom-post-bot
```

### Docker 常见启动错误

先查看容器状态和最近日志：

```bash
docker compose ps
docker compose logs --tail=100 bottom-post-bot
```

#### `project name must not be empty`

Compose 默认使用项目目录名生成项目名。目录名全部为中文等非 ASCII 字符时，过滤后可能得到空名称。当前 `docker-compose.yml` 已通过顶层 `name: bottom-post-bot` 固定项目名；旧版本也可以临时显式指定：

```bash
docker compose -p bottom-post-bot up -d --build
```

#### `sqlite3.OperationalError: unable to open database file`

项目使用 `./data:/app/data` 保存数据库。Linux 上如果 `data` 由 root 创建，容器内 UID `10001` 的 `botuser` 将无法写入。仅创建目录并不够，还需要修正所有权和权限：

```bash
mkdir -p data
chown -R 10001:10001 data
chmod 750 data
docker compose up -d --build --force-recreate
```

同时确认 `.env` 中的数据库路径位于挂载目录：

```env
DATABASE_PATH=data/bot.sqlite3
```

#### `Bad Request: chat not found`

这表示机器人无法访问 `STORAGE_CHANNEL_ID` 指定的草稿存储频道。请确认：

- 已创建专用私密存储频道；
- 已将机器人添加为该频道管理员，并授予发布和删除消息权限；
- `.env` 中填写的是真实频道 ID，而不是 `.env.example` 的示例值；
- 频道 ID 使用完整的 `-100...` 格式。

检查配置并让容器重新载入 `.env`：

```bash
grep '^STORAGE_CHANNEL_ID=' .env
docker compose up -d --force-recreate
docker compose logs --tail=100 -f bottom-post-bot
```

修改 `.env` 不需要重新构建镜像，但必须重新创建容器；单纯执行 `docker compose restart` 不会重新载入环境文件。

## 使用流程

- `/start`：打开主菜单。
- 直接转发帖子：创建一个 10 分钟有效的待确认项，可选择“保存为草稿”、“保存并命名”或“放弃”；也可点击“保存新草稿”后发送内容。
- 草稿页面：预览、添加或清空 URL 按钮、重命名、复制、删除。
- 频道页面：点击槽位编号，选择个人草稿发布为频道快照。
- 频道设置：立即刷新、修改延迟、静默/通知、总开关、恢复暂停、退出管理或二次确认删除共享配置。
- `/status`：显示个人草稿和绑定频道数量。
- `/stats`：仅私聊可用；查看一个已绑定频道/超级群组的成员总数、今天和昨天的统计，并切换自己是否接收每日推送。
- `/cancel`：清除当前多步操作。
- `/health`：仅运维账号可用。

### 成员统计与每日报告

统计从机器人已成为管理员且该对象被绑定时开始，不回溯历史。机器人依靠管理员状态下收到的成员变动更新计算加入和离开人数；管理员升降级但仍属于成员时不会计数。当前成员总数来自 Telegram 的实时口径，调用失败时会显示最后一次成功获取的时间与缓存值。

每天按 `STATS_TIMEZONE`（默认北京时间）在 `STATS_PUSH_TIME`（默认 `00:05`）向每位仍具管理员权限、且开启订阅的用户私聊一条合并日报。新绑定管理员默认订阅，可在 `/stats` 页面为每个对象单独关闭或重新开启。用户未曾 `/start` 或屏蔽机器人时，只会影响该用户的推送，不影响其他管理员。

首次启用当天、机器人权限中断当天，或运行心跳中断超过 5 分钟所覆盖的日期会标为“数据不完整”。这是为了避免把不可观测期间的变化伪装成精确的新增或退出数；恢复后新的完整自然日会正常累计。

### 草稿确认与清理

发送或转发内容后，机器人会先复制到存储频道，并显示三个确认按钮：**保存为草稿**、**保存并命名**、**放弃**。待确认项会在 10 分钟后失效（可用 `PENDING_DRAFT_TTL_SECONDS` 调整），每项只能成功确认一次。

选择“放弃”或到期后，机器人会删除存储频道中的临时副本；删除偶遇网络错误时会保留清理记录，并按 `PENDING_CLEANUP_INTERVAL_SECONDS` 的周期重试，而不会留下可再次确认的草稿。

### 槽位与 URL 按钮

向槽位发布草稿时，空槽会自动采用草稿名作为显示名称。替换该槽位的草稿时，自动名称会同步更新；使用“改名”后的自定义名称会持久保留，不会被后续替换覆盖。

URL 按钮必须严格一行一个、恰好三个字段：

```text
按钮文字 | https://example.com | 行号
```

例如：

```text
官网 | https://example.com | 1
联系支持 | tg://resolve?domain=example | 1
下载 | https://example.com/download | 2
```

允许 `https://`、`http://` 和 `tg://` URL；行号从 1 开始。同一行最多 8 个按钮，一条消息总计最多 100 个按钮；空行会忽略，任何非空行都必须遵循上述三字段格式。批量输入会在完整校验后一次性追加，出错不会只保存其中一部分。

### 自动发现、暂停与恢复

机器人失去目标频道管理员身份，或失去发帖/删除权限时，会暂停该频道的自动置底并取消待执行刷新；频道配置、槽位、槽位名称和已绑定管理员不会被删除。恢复权限后，在频道页面选择“检查权限并恢复”继续使用。连续五次发布失败也会以同样方式暂停并通知已绑定管理员。

## 配置

| 变量 | 必填 | 默认值 | 说明 |
|---|---:|---:|---|
| `TELEGRAM_BOT_TOKEN` | 是 | — | BotFather Token |
| `STORAGE_CHANNEL_ID` | 是 | — | 私密存储频道 ID |
| `OPERATOR_USER_IDS` | 是 | — | 可执行 `/health` 的用户 ID，逗号分隔 |
| `DATABASE_PATH` | 否 | `data/bot.sqlite3` | SQLite 路径 |
| `REFRESH_DELAY_SECONDS` | 否 | `10` | 新频道的默认合并延迟 |
| `MAX_CHANNELS_PER_USER` | 否 | `10` | 每用户频道配额 |
| `MAX_DRAFTS_PER_USER` | 否 | `50` | 每用户草稿配额 |
| `MAX_SLOTS_PER_CHANNEL` | 否 | `10` | 每频道槽位上限 |
| `CONVERSATION_TIMEOUT_SECONDS` | 否 | `900` | 多步操作超时 |
| `PENDING_DRAFT_TTL_SECONDS` | 否 | `600` | 待确认草稿的有效期（秒）；默认 10 分钟 |
| `PENDING_CLEANUP_INTERVAL_SECONDS` | 否 | `60` | 清理被放弃或过期待确认副本的重试周期（秒） |
| `STATS_TIMEZONE` | 否 | `Asia/Shanghai` | 成员统计日期和每日推送使用的 IANA 时区 |
| `STATS_PUSH_TIME` | 否 | `00:05` | 每日成员统计推送时间，24 小时制 `HH:MM` |
| `LOG_LEVEL` | 否 | `INFO` | 日志级别 |

## 测试

```bash
python -m pytest -q
python -m compileall -q src tests
```

自动测试使用假 Telegram 客户端，不需要真实 Token。上线前请人工验收：一个频道和一个超级群组、两名管理员、成员加入/退出/踢出/重新加入、北京时间跨零点、机器人短暂离线或失去管理员权限、超级群组自动置底不循环，以及每日推送只发送一次。

## 备份与恢复

必须一起备份：

- `data/bot.sqlite3`（以及存在时的 `-wal`、`-shm`）；
- `.env` 或外部密钥管理器中的配置；
- 私密存储频道本身不能导出到数据库，部署者不得删除其中的草稿消息。

最安全的文件级备份方式是短暂停止容器后复制整个 `data` 目录：

```bash
docker compose stop bottom-post-bot
cp -a data "backup-$(date +%Y%m%d-%H%M%S)"
docker compose start bottom-post-bot
```

恢复时停止机器人，将备份目录放回 `data`，确认 `.env` 的 Bot Token 和存储频道 ID 匹配后再启动。媒体通过持久 `file_id` 重发；私密存储频道仍用于保留可审计的草稿副本。

## 故障处理

- `RetryAfter`：按 Telegram 指定时间自动延期，不计普通失败次数。
- 网络错误：按 5、15、60、300、900 秒退避；第五次失败后暂停频道并通知已绑定管理员。
- 权限丢失：重新授予机器人发帖和删除权限，再在频道页面点击“检查权限并恢复”。
- 崩溃中断：启动时把“发送中”批次转为清理任务，删除已记录的残留消息后重建完整置底组。
- 成员统计异常：`/stats` 会保留最后一次成功的总人数缓存；日报临时发送失败会自动重试，成员更新或运行中断造成的不可观测日期会明确标记为不完整。
- 日志不记录消息正文、媒体内容或 Bot Token。
