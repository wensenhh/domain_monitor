# itdog 平台监控结果获取流程

本文档记录“方案 A（绕过浏览器的接口化采集）”的实现要点，作为后续演进与排障参考。

## 目标
- 不依赖浏览器渲染，直接以 HTTP + WebSocket 获取 itdog 测试结果
- 降低 CPU 占用与单任务时延，同时保持与页面渲染结果一致

## 总览
1. 提交检测：POST `https://www.itdog.cn/http/`
2. 解析返回 HTML，提取检测上下文：
   - `task_id`
   - `wss_url`（通常为 `wss://www.itdog.cn/websockets`）
   - `check_mode`（如 `fast`）
3. 计算 `task_token`
4. 建立 WebSocket 连接并发送认证负载
5. 持续接收逐节点结果，直到收到完成信号
6. 将消息映射为平台统一结果结构并入库

## 详细步骤
### 1) 提交检测
- 请求：`POST https://www.itdog.cn/http/`
- Form 字段（关键项）：
  - `host`：待检测域名（带协议亦可）
  - `host_s`：域名主机名
  - `check_mode`：`fast`
  - `redirect_num`：`5`
  - `dns_server_type`：`isp`
- 响应：HTML 页面（非重定向接口）

### 2) 从 HTML 中提取上下文
- 在返回 HTML 中查找以下脚本变量：
  - `wss_url='wss://www.itdog.cn/websockets'`
  - `task_id='YYYYMMDDhhmmssxxxxxxxxxxxxxxxxxx'`
  - `check_mode='fast'`
- 若未出现 `wss_url`，默认使用 `wss://www.itdog.cn/websockets`

### 3) 计算 `task_token`
- 规则：`task_token = md5(task_id + SALT)[8:24]`
- `SALT = "token_20230313000136kwyktxb0tgspm00yo5"`
- 该规则来自前端脚本 `frame/js/pages/http_speed.js` 中的 `create_websocket` 逻辑的等价实现

### 4) 建立 WebSocket 并认证
- 连接：`wss_url`（Origin 设为 `https://www.itdog.cn`）
- 首帧发送 JSON：
  ```json
  {
    "task_id": "<步骤2提取的 task_id>",
    "task_token": "<步骤3计算的 token>"
  }
  ```
- 连接建立后，服务端将陆续推送节点检测结果

### 5) 接收与完成判定
- 服务端逐条推送单节点结果，示例字段：
  - `type`：`success`（或其它），`finished` 表示结束
  - `node_id`、`name/region/province/line`（节点位置信息）
  - `ip`、`http_code`、`all_time/dns_time/connect_time/download_time`、`head` 等
- 采集侧持续读帧直至收到 `{"type":"finished"}` 或超时

### 6) 字段映射与入库
- 将 WebSocket 消息映射为统一的结果字段：
  - 位置/运营商：由 `name/region/province/line` 推导
  - 状态码：`http_code`；若缺失但为终态，按平台语义归类为失败/未解析
  - 耗时：`all_time/dns_time/connect_time/download_time`
  - IP：`ip`
- 每个节点一条结果，总计约 159 条；收到 `finished` 后结束本次入库

## 超时与容错
- WebSocket 接收设定总体超时（建议 60–90 秒）
- 若在超时前未收到 `finished`：
  - 标记本次结果为不完整（用于跳过告警）
  - 记录样本帧与上下文以便排障

## 正确性对齐
- 与页面渲染采集对齐的要点：
  - 以 `finished` 作为完成判定的唯一标准
  - 不依赖 DOM 字段是否为 `--` 来判断终态
  - 未解析/失败场景由消息内容直接给出（如 `ip: "Not Found"` 等）

## 性能影响（经验值）
- example.com 实测：
  - 约 30–35 秒收齐 159 条 + `finished`
  - CPU 占用显著低于浏览器渲染方案

## 验证与辅助脚本
- 抓包脚本（用于协议侦测与回归）：
  - `devops_django_server/itdog_xhr_probe.py`
- 无浏览器探针（标准库实现）：
  - `devops_django_server/itdog_ws_nobrowser_probe.py`

## 风险与变更
- itdog 前端脚本可能调整 `SALT` 或校验方式
- 若 `wss_url`/字段名改动，需要同步更新解析与计算逻辑
