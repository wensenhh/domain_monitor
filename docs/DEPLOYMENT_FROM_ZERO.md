# DevOps Monitor 从 0 部署文档

本文档面向“把本项目部署到其它项目或其它环境”的场景，覆盖环境准备、Docker 部署、Django 初始化、监控平台、Telegram 告警、第三方免费/付费接口、阈值和置信度策略。

> 当前代码是 Django + PostgreSQL + producer/worker 的监控系统。监控平台适配器通过网页/接口采集第三方测速结果，并把失败率超过阈值且复测仍异常的域名推送到 Telegram。

## 1. 部署目标架构

```text
PostgreSQL
  ^
  |
Django Admin / monitor API
  ^
  |
producer: 扫描 MonitorDomainTarget，写入 MonitorWaitingTask
  ^
  |
worker N 个: 抢占等待任务，调用 itdog/17ce/chinaz，写入 MonitorTask 和 MonitorDomainResult
  |
  +-- Telegram sender: /monitor/telegram_sender -> Telegram Bot API
```

核心进程：

- `django-server`：Django 管理后台和 Telegram 发送转发接口。
- `monitor-producer`：不断扫描启用的目标域名，按间隔创建等待任务。
- `monitor-worker1..16`：并发消费等待任务，执行第三方测速并触发告警。
- `db`：PostgreSQL 15。

## 2. 服务器准备

最低建议：

- CPU：2 核起步；如果启用 8 个以上 worker，建议 8 核以上。
- 内存：2 GB 起步；如果多个 worker 同时跑 Playwright，建议 8 GB 以上。
- 磁盘：20 GB 起步；`db_data/` 会保存 PostgreSQL 数据。
- 网络：服务器需要能访问第三方测速站点、`https://api.telegram.org`，如果目标环境访问 Telegram 受限，需要配置代理或放在可访问 Telegram 的网络中。

软件：

- Docker Engine
- Docker Compose v2
- Git

检查命令：

```bash
docker --version
docker compose version
git --version
```

如果你要直接用一键脚本部署，可以先下载脚本再执行。脚本会自动安装缺失的 `git` 和 Docker Compose v2：

```bash
curl -fsSL https://raw.githubusercontent.com/wensenhh/domain_monitor/main/scripts/deploy.sh \
  -o /tmp/deploy-domain-monitor.sh
bash /tmp/deploy-domain-monitor.sh
```

带 Telegram 配置的一次性部署示例：

```bash
TG_BOT_TOKEN='你的机器人 token' \
TG_CHAT_ID='你的 chat id' \
WORKERS=2 \
bash /tmp/deploy-domain-monitor.sh
```

脚本默认安装到 `/opt/domain_monitor`，默认只启动 2 个 worker。需要更多 worker 时可以设置 `WORKERS=4` 到 `WORKERS=16`。

如果你想在后台已经登录后，一次性补配置、加测试域名并跑验收，可以执行：

```bash
cd /opt/domain_monitor
TG_BOT_TOKEN='新的机器人 token' \
TG_CHAT_ID='你的 chat id' \
bash scripts/bootstrap_monitor.sh
```

说明：真实监控告警只有在首测失败率和复测失败率都超过 `ALERT_FAIL_THRESHOLD` 时才会发送。`bootstrap_monitor.sh` 默认带 `FORCE_ALERT_TEST=1`，会额外发送一条“告警链路测试”消息并写入告警表，用于确认 Telegram 告警通道可用。

只写配置和测试域名，不跑告警和采集：

```bash
RUN_CHECKS=0 bash scripts/bootstrap_monitor.sh
```

## 3. 拉取代码

```bash
git clone <你的仓库地址> devops-monitor
cd devops-monitor
```

如果是直接拷贝项目，也要保证目录中包含：

- `Dockerfile`
- `docker-compose.yml`
- `requirements.txt`
- `devops_django_server/`

## 4. 创建 `.env`

在项目根目录创建 `.env`：

```dotenv
# 让 docker compose 生成 vp-monitor-monitor-worker1:latest。
# 当前 docker-compose.yml 的 worker2..16 依赖这个固定镜像名。
COMPOSE_PROJECT_NAME=vp-monitor

# PostgreSQL 对外映射端口。只在宿主机需要直连数据库时使用。
POSTGRES_DB_LISTEN_PORT=5432
POSTGRES_DB=devops_monitor
POSTGRES_USER=devops_monitor
POSTGRES_PASSWORD=请替换为强密码
POSTGRES_HOST=db
POSTGRES_PORT=5432

# Django runserver 端口。
DJANGO_SERVER_PORT=8001

# 日志级别。
LOG_LEVEL=INFO
DJANGO_LOG_LEVEL=INFO
```

注意：

- `POSTGRES_HOST=db` 是容器内连接 PostgreSQL 的服务名，不要改成 `127.0.0.1`。
- `POSTGRES_DB_LISTEN_PORT` 是宿主机端口，若同机已有 PostgreSQL，可改成 `15432`。
- 当前代码中 `SECRET_KEY`、`DEBUG=True`、`ALLOWED_HOSTS=["*"]` 写在 `settings.py` 中。生产公网部署建议先改造成环境变量，并关闭 `DEBUG`。

## 5. 构建镜像

当前 `docker-compose.yml` 的 worker2 到 worker16 使用固定镜像名 `vp-monitor-monitor-worker1:latest`，因此建议显式指定 compose project：

```bash
docker compose -p vp-monitor build
```

等价方案是在 `.env` 中设置 `COMPOSE_PROJECT_NAME=vp-monitor`，但命令里继续带 `-p vp-monitor` 更直观。

构建过程会安装：

- Python 3.11
- Django 5.2
- PostgreSQL 客户端依赖
- Playwright Chromium
- 中文字体和浏览器运行库

## 6. 初始化数据库

先启动数据库：

```bash
docker compose -p vp-monitor up -d db
```

执行迁移：

```bash
docker compose -p vp-monitor run --rm django-server \
  python devops_django_server/manage.py migrate
```

创建管理员：

```bash
docker compose -p vp-monitor run --rm django-server \
  python devops_django_server/manage.py createsuperuser
```

启动全部服务：

```bash
docker compose -p vp-monitor up -d
```

访问后台：

```text
http://服务器IP:8001/admin/
```

如果改了 `DJANGO_SERVER_PORT`，使用对应端口。

## 7. 首次健康检查

查看容器：

```bash
docker compose -p vp-monitor ps
```

查看日志：

```bash
docker compose -p vp-monitor logs -f django-server
docker compose -p vp-monitor logs -f monitor-producer
docker compose -p vp-monitor logs -f monitor-worker1
```

Django 自检：

```bash
docker compose -p vp-monitor exec -T django-server \
  python devops_django_server/manage.py check
```

确认后台可访问：

```bash
curl -I http://127.0.0.1:8001/admin/
```

## 8. Django Admin 基础数据配置

进入 `/admin/` 后，至少配置三类数据：

1. `Monitor platforms`：第三方测速平台。
2. `Monitor configs`：业务参数、阈值、Telegram 配置。
3. `Monitor domain targets`：要监控的域名。

### 8.1 监控平台 `MonitorPlatform`

当前代码支持的平台：

| platform | website_url | 类型 | 建议 |
| --- | --- | --- | --- |
| `itdog` | `https://www.itdog.cn/http/` | 免费网页测速 | 推荐优先启用。当前代码用 Playwright 采集页面表格。 |
| `17ce` | `https://17ce.com/get` | 免费网页测速 | 可作为复测平台，但过于频繁可能返回 `url in black list`。 |
| `chinaz` | `https://tool.chinaz.com/speedtest/` | 免费网页测速 | 可启用；若页面结构变化或限制增强，可能需要维护适配器。 |

建议初始配置：

| platform | enabled |
| --- | --- |
| `itdog` | `true` |
| `17ce` | `true` |
| `chinaz` | `false`，稳定后再启用 |

选择逻辑：

- worker 会从启用平台中按等待任务 ID 分散选择。
- 如果首选平台失败，会尝试其它启用平台。
- 复测优先选择不同平台；只有一个平台时才复测同平台。
- 若同一域名 7 天内在 `17ce` 出现 `black list` 失败记录，后续会尽量跳过 `17ce`。

### 8.2 监控配置 `MonitorConfig`

`MonitorConfig` 的 `value_type` 必须和实际值匹配。例如整数用 `int`，布尔值用 `bool`，浮点数用 `float`。

#### 必填或强烈建议配置

| key | value_type | 推荐值 | 代码默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `HEADLESS` | `bool` | `true` | `true` | Playwright 是否无头运行。服务器部署必须用 `true`。 |
| `DEFAULT_PROXY` | `str` | 空或代理地址 | 空字符串 | worker 访问测速平台时使用的代理。示例：`http://user:pass@host:port`。 |
| `SCREENSHOT_ENABLED` | `bool` | `false` | `false` | 是否截图。排障时临时打开，长期打开会增加磁盘和网络开销。 |
| `SCREENSHOT_DIR` | `str` | `./screenshots` | `./screenshots` | 截图目录，相对容器 `/app`。 |
| `PLAYWRIGHT_NAV_TIMEOUT_MS` | `int` | `60000` | `60000` | 页面导航超时，单位毫秒。网络慢时可调到 `90000`。 |
| `PLAYWRIGHT_ACTION_TIMEOUT_MS` | `int` | `30000` | `30000` | 页面元素操作超时，单位毫秒。 |
| `ALERT_FAIL_THRESHOLD` | `float` | `0.3` | `0.3` | 失败率阈值，`0.3` 表示超过 30% 才进入复测/告警流程。 |
| `TASK_LEASE_SECONDS` | `int` | `300` | `300` | worker 抢占任务后的租约秒数。应大于单次检测最长耗时。 |
| `WORKER_MAX_ATTEMPTS` | `int` | `5` | `5` | 等待任务最大尝试次数，超过后置为失败。 |
| `WORKER_NO_TASK_LOOP_SECONDS` | `int` | `60` | `60` | worker 没任务时休眠秒数。 |
| `PRODUCER_SLEEP_SECONDS` | `int` | `5` | `5` | producer 本轮创建了任务后的休眠秒数。 |
| `PRODUCER_IDLE_SLEEP_SECONDS` | `int` | `30` | `30` | producer 本轮无任务后的休眠秒数。 |

#### Telegram 告警配置

| key | value_type | 示例 | 说明 |
| --- | --- | --- | --- |
| `TG_BOT_TOKEN` | `str` | `123456:ABC...` | BotFather 创建机器人后得到的 token。 |
| `TG_CHAT_ID` | `str` | `-1001234567890` | 目标群、频道或个人 chat id。 |
| `TELEGRAM_SENDER_URL` | `str` | `http://django-server:8001/monitor/telegram_sender` | worker 调用的内部发送接口。为空时默认使用 `http://django-server:${DJANGO_SERVER_PORT}/monitor/telegram_sender`。 |
| `TELEGRAM_SENDER_API_KEY` | `str` | 随机长字符串 | 可选。配置后 `telegram_sender` 会校验 `X-Api-Key`。生产建议配置。 |
| `DJANGO_SERVER_PORT` | `str` | `8001` | 可选。worker 拼默认 sender URL 时会优先读这里，再读 `.env`。 |

Telegram 发送流程：

```text
worker -> django-server /monitor/telegram_sender -> https://api.telegram.org/bot<TOKEN>/sendMessage
```

当前实现：

- `telegram_sender` 接收 JSON 或表单。
- 必填字段：`token`、`groupid` 或 `chat_id`、`text`。
- 单次发送最多重试 5 次。
- 请求 Telegram 时不使用系统代理。
- Telegram 官方 `sendMessage` 需要 `chat_id` 和 `text`，文本长度为 1 到 4096 字符。
- Telegram Bot API 支持 `allow_paid_broadcast` 付费广播参数，但当前代码没有使用这个参数。

## 9. Telegram 机器人从 0 配置

1. 在 Telegram 中打开 `@BotFather`。
2. 输入 `/newbot` 创建机器人。
3. 记录 BotFather 返回的 token，填入 `MonitorConfig.TG_BOT_TOKEN`。
4. 把机器人拉入告警群。
5. 如果是群组，给机器人发送消息权限。
6. 获取 `chat_id`：
   - 方式 A：在群里发一条消息，然后访问 `https://api.telegram.org/bot<TG_BOT_TOKEN>/getUpdates` 查看 `chat.id`。
   - 方式 B：使用第三方 `get id` 机器人获取。
   - 超级群或频道常见格式是 `-100...`。
7. 把 chat id 填入 `MonitorConfig.TG_CHAT_ID`。
8. 手动测试：

```bash
curl -X POST http://127.0.0.1:8001/monitor/telegram_sender \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: 你的 TELEGRAM_SENDER_API_KEY，如果未配置则去掉此行' \
  -d '{
    "token": "你的 TG_BOT_TOKEN",
    "groupid": "你的 TG_CHAT_ID",
    "text": "devops-monitor telegram test",
    "timeout_seconds": 10,
    "max_attempts": 5
  }'
```

返回 `{"ok": true, ...}` 且群里收到消息即配置成功。

## 10. 监控目标配置

在 `Monitor domain targets` 中新增域名：

| 字段 | 说明 | 建议 |
| --- | --- | --- |
| `domain` | 监控域名，可带协议，代码会清洗 | 推荐填 `example.com` 或 `https://example.com`，保持一致 |
| `enabled` | 是否启用 | `true` |
| `priority` | 优先级 | 越大越先被 producer 扫描 |
| `schedule_interval_minutes` | 调度间隔 | 默认 `10`；重要域名可 `5`，普通域名 `10-30` |

后台支持批量添加：

- 每行一个域名。
- 也支持逗号、中文逗号、分号分隔。
- 可先勾选 dry run 预览。

producer 规则：

- 目标未启用则跳过。
- 目标未到 `schedule_interval_minutes` 则跳过。
- 目标已有 waiting 或未过期 running 任务则跳过。
- 创建任务后更新 `last_scheduled_at`。

## 11. 阈值、失败率和置信度

### 11.1 失败率计算

每个第三方检测节点对应一条 `MonitorDomainResult`。

```text
total = 当前 MonitorTask 写入的结果行数
failed = status_code 不是 HTTP 200-399 的行数
failure_rate = failed / total
```

成功：

- `200` 到 `399`

失败：

- `404`、`500` 等 4xx/5xx
- `失败`
- `超时`
- `--`
- 空值
- 非三位数字文本

如果没有任何结果行，失败率按 `1.0` 处理。

### 11.2 告警触发链路

```text
首测失败率 <= ALERT_FAIL_THRESHOLD
  -> 不告警

首测失败率 > ALERT_FAIL_THRESHOLD
  -> sleep 2 秒
  -> 复测
  -> 复测失败率 > ALERT_FAIL_THRESHOLD
      -> 发送 Telegram
      -> 写入 MonitorAlertedDomais
  -> 复测失败率 <= ALERT_FAIL_THRESHOLD
      -> 不告警
```

如果复测任务本身失败：

- 再复测一次同一复测平台。
- 如果仍失败且复测平台不是首测平台，再回退到首测平台测一次。

### 11.3 阈值建议

| 场景 | `ALERT_FAIL_THRESHOLD` | 说明 |
| --- | --- | --- |
| 保守上线期 | `0.5` | 减少误报，适合刚部署、平台稳定性未知时。 |
| 常规生产 | `0.3` | 当前代码默认值。超过 30% 节点失败才复测/告警。 |
| 高敏感业务 | `0.1` 到 `0.2` | 更早告警，但需要至少两个稳定平台，否则误报会增加。 |

### 11.4 置信度分级

这里的“置信度”不是统计学置信区间，而是运维判断告警可信程度的规则。

| 置信度 | 条件 | 运维动作 |
| --- | --- | --- |
| 高 | 首测超过阈值，复测也超过阈值，且复测平台不同于首测平台 | 直接按真实异常处理。 |
| 中 | 首测和复测都超过阈值，但只有一个启用平台，复测仍是同平台 | 先人工抽查一次，再升级处理。 |
| 中 | 两个平台均异常，但节点数较少或平台日志显示部分采集失败 | 结合业务监控、CDN、源站日志确认。 |
| 低 | itdog 标记 incomplete，或平台页面异常导致结果不足 | 当前代码会跳过 incomplete 告警；应排查平台或代理。 |

建议生产至少启用两个平台：`itdog + 17ce` 或 `itdog + chinaz`。这样复测能跨平台，告警可信度明显更高。

## 12. 第三方免费接口配置

### 12.1 itdog

- 配置表：`MonitorPlatform`
- `platform=itdog`
- `website_url=https://www.itdog.cn/http/`
- 鉴权：当前代码不需要账号或 token。
- 当前实现：Playwright 打开页面并采集表格。
- 风险：页面结构、反自动化策略、访问频控变化会影响采集。

### 12.2 17ce

- 配置表：`MonitorPlatform`
- `platform=17ce`
- `website_url=https://17ce.com/get`
- 鉴权：当前代码不需要账号或 token。
- 当前实现：标准库 HTTP + WebSocket 协议采集。
- 风险：测试太频繁可能返回 `url in black list`；代码会在 7 天窗口内尽量跳过该域名的 17ce。

### 12.3 Chinaz 站长工具

- 配置表：`MonitorPlatform`
- `platform=chinaz`
- `website_url=https://tool.chinaz.com/speedtest/`
- 鉴权：当前代码不需要账号或 token。
- 当前实现：Playwright 打开页面并采集 `#tbSort` 表格。
- 风险：页面结构或限制变更时需要维护适配器。

### 12.4 HTTPBin

- 用途：当 `DEFAULT_PROXY` 不为空时，itdog/chinaz/17ce 适配器会访问 `https://httpbin.org/ip` 检测真实代理出口 IP。
- 鉴权：无。
- 失败影响：无法识别真实代理 IP，但主检测流程通常会继续。
- 如果生产环境不能访问 HTTPBin，可接受 `real_proxy_ip` 为空，或后续把检测地址改成自建 IP 回显服务。

## 13. 第三方付费接口和密钥配置

### 13.1 代理服务

当前项目对付费代理的支持是通用代理地址，不绑定具体供应商。

配置：

- `MonitorConfig.DEFAULT_PROXY`
- value_type：`str`

示例：

```text
http://proxy.example.com:8080
http://user:password@proxy.example.com:8080
```

建议：

- 使用稳定机房代理，不建议使用频繁变化的低质量代理。
- 代理如果被测速平台拒绝，代码会在 itdog/17ce 场景下尝试不走代理重试。
- 如果目标环境访问 Telegram 也需要代理，当前代码未把 `DEFAULT_PROXY` 用于 Telegram，需要单独改造。

### 13.2 Telegram 付费广播

Telegram Bot API 有付费广播能力，但当前项目只调用普通 `sendMessage`，未传 `allow_paid_broadcast`。

结论：

- 普通告警不需要付费。
- 如果未来要做高频群发或海量通知，需要改造 `monitor/views.py` 中的 Telegram 请求 payload。
- 当前 `MonitorConfig` 里没有 Telegram Stars 或付费广播相关配置项。

### 13.3 17ce / Chinaz / itdog 付费账号

当前代码没有登录流程，也没有读取这些平台的账号、cookie、token 或付费 API key。

如果要接入付费 API，需要新增：

- 供应商密钥保存位置，例如 `config.CloudAPIAuthKey`。
- 平台适配器中的鉴权逻辑。
- 付费 API 的请求、签名、限额、错误码处理。
- 调用量和费用保护阈值。

### 13.4 `CloudAPIAuthKey` 表

`config.CloudAPIAuthKey` 当前是通用云 API 密钥表，字段包括：

- `vendor`
- `sub_account`
- `email`
- `api_key`
- `api_secret`
- `api_key_1`
- `api_secret_1`
- `status`
- `status_info`

重要说明：

- 当前监控 worker 没有读取 `CloudAPIAuthKey`。
- 这张表适合后续存放 Cloudflare、DNSPod、阿里云、腾讯云或第三方付费 API 凭证。
- 仅在后台填入密钥不会自动改变监控行为。

## 14. 推荐初始化配置清单

### 14.1 MonitorConfig 推荐值

| key | type | value |
| --- | --- | --- |
| `HEADLESS` | `bool` | `true` |
| `DEFAULT_PROXY` | `str` | 空，或你的代理地址 |
| `SCREENSHOT_ENABLED` | `bool` | `false` |
| `SCREENSHOT_DIR` | `str` | `./screenshots` |
| `PLAYWRIGHT_NAV_TIMEOUT_MS` | `int` | `60000` |
| `PLAYWRIGHT_ACTION_TIMEOUT_MS` | `int` | `30000` |
| `ALERT_FAIL_THRESHOLD` | `float` | `0.3` |
| `TASK_LEASE_SECONDS` | `int` | `300` |
| `WORKER_MAX_ATTEMPTS` | `int` | `5` |
| `WORKER_NO_TASK_LOOP_SECONDS` | `int` | `60` |
| `PRODUCER_SLEEP_SECONDS` | `int` | `5` |
| `PRODUCER_IDLE_SLEEP_SECONDS` | `int` | `30` |
| `TG_BOT_TOKEN` | `str` | 你的 token |
| `TG_CHAT_ID` | `str` | 你的 chat id |
| `TELEGRAM_SENDER_URL` | `str` | `http://django-server:8001/monitor/telegram_sender` |
| `TELEGRAM_SENDER_API_KEY` | `str` | 随机长字符串 |
| `DJANGO_SERVER_PORT` | `str` | `8001` |

### 14.2 MonitorPlatform 推荐值

| platform | website_url | enabled |
| --- | --- | --- |
| `itdog` | `https://www.itdog.cn/http/` | `true` |
| `17ce` | `https://17ce.com/get` | `true` |
| `chinaz` | `https://tool.chinaz.com/speedtest/` | `false` |

### 14.3 Worker 数量建议

| 目标域名数量 | 建议 worker 数 | 说明 |
| --- | --- | --- |
| 1 到 50 | 1 到 2 | 先观察第三方平台稳定性。 |
| 50 到 300 | 4 到 8 | 调度间隔建议 10 分钟以上。 |
| 300 以上 | 8 到 16 | 需要关注 CPU、内存、第三方频控和黑名单。 |

当前 compose 默认定义了 16 个 worker。刚部署时可以只启动少量 worker：

```bash
docker compose -p vp-monitor up -d db django-server monitor-producer monitor-worker1 monitor-worker2
```

稳定后再启动更多：

```bash
docker compose -p vp-monitor up -d monitor-worker3 monitor-worker4 monitor-worker5 monitor-worker6
```

## 15. 手动验证一次完整链路

1. 在后台新增一个测试域名，例如 `example.com`。
2. 确认至少一个 `MonitorPlatform.enabled=true`。
3. 手动运行 producer 一次：

```bash
docker compose -p vp-monitor exec -T django-server \
  python devops_django_server/manage.py producer --once
```

4. 手动运行 worker 一次：

```bash
docker compose -p vp-monitor exec -T django-server \
  python devops_django_server/manage.py worker --once
```

5. 在后台检查：

- `Monitor waiting tasks` 中任务状态是否变为 `success`。
- `Monitor tasks` 中是否生成新任务。
- `Monitor domain results` 中是否有检测节点结果。
- `failure_rate` 是否符合预期。

6. 测试 Telegram：

- 可以用第 9 节的 `curl` 手动发一条测试消息。
- 自动告警需要触发超过阈值且复测仍超过阈值，不建议在生产一开始故意压测第三方平台。

## 16. 定时维护命令

项目提供几个管理命令：

### 16.1 更新最近任务失败率

```bash
docker compose -p vp-monitor exec -T django-server \
  python devops_django_server/manage.py update_failure_rate_last_hour --hours 1
```

可用于回补最近一段时间任务的失败率。

### 16.2 汇总最近告警并发送 Telegram

```bash
docker compose -p vp-monitor exec -T django-server \
  python devops_django_server/manage.py alert_fail_rate_last_hour --hours 1 --limit 200
```

先 dry run：

```bash
docker compose -p vp-monitor exec -T django-server \
  python devops_django_server/manage.py alert_fail_rate_last_hour --hours 1 --limit 200 --dry-run
```

### 16.3 清理旧数据

```bash
docker compose -p vp-monitor exec -T django-server \
  python devops_django_server/manage.py delete_old_data
```

当前清理规则：

- 删除昨天 00:00 以前的 `MonitorDomainResult`。
- 删除昨天 00:00 以前的 `MonitorTask`。
- 每批 5000 条。

建议把维护命令放到宿主机 crontab，例如：

```cron
*/15 * * * * cd /opt/devops-monitor && docker compose -p vp-monitor exec -T django-server python devops_django_server/manage.py update_failure_rate_last_hour --hours 1 >> /var/log/devops-monitor-cron.log 2>&1
5 * * * * cd /opt/devops-monitor && docker compose -p vp-monitor exec -T django-server python devops_django_server/manage.py alert_fail_rate_last_hour --hours 1 --limit 200 >> /var/log/devops-monitor-cron.log 2>&1
10 3 * * * cd /opt/devops-monitor && docker compose -p vp-monitor exec -T django-server python devops_django_server/manage.py delete_old_data >> /var/log/devops-monitor-cron.log 2>&1
```

## 17. 升级和迁移到新环境

### 17.1 备份数据库

```bash
docker compose -p vp-monitor exec -T db \
  sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > devops_monitor_backup.sql
```

如果宿主机没有导出 `.env` 变量，直接写实际值：

```bash
docker compose -p vp-monitor exec -T db \
  pg_dump -U devops_monitor devops_monitor > devops_monitor_backup.sql
```

### 17.2 新环境恢复

在新环境完成第 2 到第 6 节后：

```bash
cat devops_monitor_backup.sql | docker compose -p vp-monitor exec -T db \
  sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
```

然后启动服务：

```bash
docker compose -p vp-monitor up -d
```

## 18. 生产安全建议

当前项目能运行，但生产公网部署建议补齐：

- 把 `SECRET_KEY` 改成环境变量。
- 把 `DEBUG` 改成环境变量并在生产设置为 `False`。
- 限制 `ALLOWED_HOSTS`。
- 给 `/admin/` 加反向代理鉴权、IP 白名单或 VPN。
- 不要把 `POSTGRES_DB_LISTEN_PORT` 暴露到公网。
- 给 `TELEGRAM_SENDER_API_KEY` 设置随机强密钥。
- 定期备份 `db_data/` 或使用托管 PostgreSQL。
- 如果截图开启，定期清理 `screenshots/`。

## 19. 常见问题

### worker2..16 启动失败，提示找不到镜像

原因：compose 文件写死了 `vp-monitor-monitor-worker1:latest`。

解决：

```bash
docker compose -p vp-monitor build
docker compose -p vp-monitor up -d
```

或在 `.env` 中确保：

```dotenv
COMPOSE_PROJECT_NAME=vp-monitor
```

### worker 一直没有任务

检查：

- `MonitorDomainTarget.enabled=true`
- `schedule_interval_minutes` 是否未到
- 是否已有 waiting/running 任务
- producer 是否运行

手动触发：

```bash
docker compose -p vp-monitor exec -T django-server \
  python devops_django_server/manage.py producer --once
```

### 没有告警

检查：

- `TG_BOT_TOKEN` 和 `TG_CHAT_ID` 是否配置。
- `TELEGRAM_SENDER_URL` 是否能从 worker 容器访问。
- `ALERT_FAIL_THRESHOLD` 是否过高。
- 失败是否只发生在首测，复测是否恢复。
- itdog 是否标记 `incomplete`，这种情况当前代码会跳过告警。

### Telegram 测试接口返回 401

说明配置了 `TELEGRAM_SENDER_API_KEY`，但请求没带正确的 `X-Api-Key`。

### 第三方测速平台结果为空

可能原因：

- 平台页面结构变化。
- 服务器网络无法访问平台。
- 代理不可用。
- 平台频控或黑名单。
- Playwright 运行资源不足。

排查：

```bash
docker compose -p vp-monitor logs -f monitor-worker1
docker compose -p vp-monitor exec -T django-server python devops_django_server/manage.py worker --once
```

必要时临时开启：

- `SCREENSHOT_ENABLED=true`
- `SCREENSHOT_DIR=./screenshots`

### 17ce 返回 black list

说明该域名在 17ce 上可能被频控或拉黑。当前代码会在 7 天内尽量跳过该域名的 17ce 检测。建议降低目标调度频率，或只用 itdog/chinaz 复测。

## 20. 外部参考入口

- Telegram Bot API：`https://core.telegram.org/bots/api`
- itdog HTTP 测速：`https://www.itdog.cn/http/`
- 17ce 测速：`https://17ce.com/get`
- Chinaz 站长工具测速：`https://tool.chinaz.com/speedtest/`
