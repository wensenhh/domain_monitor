Django 项目根目录: devops_django_server ,主要 APP 说明
- monitor: 监控 APP, 主要负责监控系统
- config: 配置 APP, 主要负责系统的配置管理

# config APP 中的表说明
## Vendor
- 供应商表, 主要负责存储供应商的信息
    - 字段说明
        - name: 供应商名称
        - description: 供应商描述
        - created_at: 创建时间
        - updated_at: 更新时间
        - vendor_code: 供应商代码

## CloudAPIAuthKey
- 云 API 认证密钥表, 主要负责存储云 API 的认证密钥
    - 字段说明
        - name: 账号名称，是自定义标识，和账号无实际关联
        - vendor: 供应商, 外键关联 Vendor 表
        - sub_account: 账号，如果没有，无需填写；Cloudflare 如果存在子账号，需要添加子账号
        - email: 账号邮箱，如果没有，无需填写
        - api_key: API 密钥, 如果没有，无需填写
        - api_secret: API 密钥, 如果没有，无需填写
        - status: API 状态, 枚举值, 可选值: active, inactive
        - status_info: API 状态信息, 字符串, 用于存储 API 状态的详细信息
        - created_at: 创建时间
        - updated_at: 更新时间
        - description: 认证密钥描述

# monitor APP 中的表说明
## MonitorTask
- 监控任务表, 主要负责存储监控任务的信息


## MonitorResult
- 监控结果表, 主要负责存储监控任务的结果

## MonitorConfig
- 监控配置表, 主要负责存储监控任务的配置

# producer
负责不断的从 MonitorDomainTarget 中读取待检测域名, 根据 enabled , last_scheduled_at , schedule_interval_minutes 判断是否要插入一条待运行的任务到 MonitorWaitingTask 中. producer 只会有一个

通过 Dgango Command 实现: `python manage.py producer [--once]`

# worker
负责从 MonitorWaitingTask 中获取等待运行的任务, 在 MonitorTask 创建任务,并进行检测, 检测结果写入 MonitorDomainResult ,最后将任务结果返回 MonitorWaitingTask 中
           
如果 MonitorWaitingTask 中没有需要运行的任务,则等待固定时间(WORKER_NO_TASK_LOOP_MINUTES) 后再查询任务
		   
worker 会有多个进程分布式并发运行

worker 做一次“抢占”，逻辑建议:
- status='running' AND lease_until < now() （超时回收）
- 优先抢占并执行最先入库的任务
 
抢到后立刻更新：
- status='running'
- worker_id=<本进程唯一标识> （比如 hostname+pid）
- lease_until=now + LEASE_SECONDS
- attempts += 1
- 若 attempts > MAX_ATTEMPTS ：直接置 failed 并写 error_message='max attempts reached' ，提交事务后进入下一轮

租约（lease_until）保持简单: TASK_LEASE_SECONDS 执行期间不续租

执行任务（事务外执行 Playwright，避免长事务）, 根据平台,执行实际的检测任务
1. 轮询检测平台, 判断 enabled 拿到当前轮询的平台及平台入口, 位于 MonitorPlatform 中(platform, website_url)
2. 创建一条 MonitorTask ：
    - status='running'
    - proxy_ip 、 headless  从 MonitorConfig 读取（ DEFAULT_PROXY / HEADLESS / SCREENSHOT_ENABLED / SCREENSHOT_PATH / 	PLAYWRIGHT_NAV_TIMEOUT_MS / PLAYWRIGHT_TIMEOUT_MS /）
3. 调用 “平台 runner”（抽象化接口，简单即可
    - 输入：domain、proxy、headless、timeout、screenshot_enabled、screenshot_dir 等
    - 输出：一组结果行（用于写入 MonitorDomainResult 多行）+ 原始数据 raw（可塞进每行 raw 或汇总一份）
4. 写入结果：
    - 对每个监测点一行写入 MonitorDomainResult （ task 外键、 domain 、 isp/detect_node_location/.../raw ）
    - 更新 MonitorTask ：
    - 成功： status='success' ， count=<写入行数> ，填 timing 字段
    - 失败： status='failed' ，写 error_type/error_message
5. 最后更新 waiting_task：
    - 成功： status='success' ， error_message 清空或留空， lease_until=NULL （可选）
    - 失败： status='failed' ，写 error_message ， lease_until=NULL （可选，方便快速重试）

# 告警机制
- 从平台获取到域名的检测结果后, 立马计算检测失败率

- 检测失败率: 计算 检测状态码中不是 2XX / 3XX 的比例(total = 本次任务写入的 MonitorDomainResult 所有行数; failed = status_code 不是 2xx/3xx 的行数. status_code 可能是 "200" 、也可能是 "失败"/"--"/"超时" 这类文本, 200-399 状态码视为成功,其他包括 404 等都视为失败), 失败率告警阈值读取 ALERT_FAIL_THRESHOLD

- 在 worker 一次平台任务完成并入库后，做：

    1. 计算第一次失败率 （基于本次 MonitorTask 的结果行）
    2. 若 fail_rate <= threshold ：结束，无告警
    3. 若 fail_rate > threshold ：立即发起一次“复测”, 复测尽量简单, 不要引入更复杂流程
        - 复测优先使用不同平台,如果只有一个平台,则使用相同平台复测
        - 复测与第一次之间可以加一个很短的 sleep（例如 1–3s）
        - 如果复测任务本身异常（比如 itdog 页面打不开），视为“复测失败”, 再执行一次复测
  
    4. 对复测结果计算失败率, 若仍超过阈值, 则发送告警; 否则, 结束, 无告警

- 告警发送到 telegram, 鉴权信息在配置表中(TG_BOT_TOKEN, TG_CHAT_ID)    


告警消息模板如下:
🔥🔥🔥报警实例:  {{ TARGET }}

名称: 域名失败率 > 30% (监控节点数 {{ 总节点数 }} )
时间: {{ 告警时间 }}
级别： Critical
状态: PROBLEM
详情: {{ TARGET }} 检测失败率 {{ 39.18% }} ,{{ 第一次测试得平台 }}:{{ 第一次测试得失败率 }} -> {{ 复测平台 }}:{{ 复测失败率 }}

## 告警域名信息入库
在发送 Telegram 告警消息后(无论是否发送成功), 将告警域名信息写入 MonitorAlertedDomais 表中. 告警域名信息包括:
- 域名
- 告警时间
- 告警平台
- 告警失败率
