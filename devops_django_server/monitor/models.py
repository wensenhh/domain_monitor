from django.db import models
from django.db.models import CheckConstraint, Index, Q, UniqueConstraint
from django.db.models.functions import Length, Trim


class MonitorDomainTarget(models.Model):
    domain = models.CharField(unique=True, verbose_name="域名", blank=False, null=False, max_length=255, help_text="域名")
    enabled = models.BooleanField(default=True, verbose_name="是否启用", help_text="是否启用")
    priority = models.SmallIntegerField(default=0, verbose_name="优先级", help_text="优先级")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间", help_text="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间", help_text="更新时间")
    last_scheduled_at = models.DateTimeField(null=True, blank=True, verbose_name="最后调度时间", help_text="最后调度时间")
    schedule_interval_minutes = models.IntegerField(default=10, verbose_name="调度间隔分钟", help_text="调度间隔分钟")

    class Meta:
        db_table = "monitor_domain_targets"
        verbose_name = "监控目标域名"
        verbose_name_plural = "监控目标域名"
    def __str__(self):
        return self.domain


class MonitorWaitingTask(models.Model):
    target = models.ForeignKey(
        MonitorDomainTarget,
        on_delete=models.CASCADE,
        db_column="target_id",
        related_name="waiting_tasks",
    )
    domain = models.CharField(verbose_name="域名", blank=False, null=False, max_length=255, help_text="域名")
    status = models.CharField(
        max_length=10,
        choices=[
            ("waiting", "waiting"),
            ("running", "running"),
            ("failed", "failed"),
            ("success", "success"),
            ("unknow", "unknow"),
        ],
        default="waiting",
        verbose_name="状态",
        help_text="状态，可选：running, failed, success, unknow",blank=False,null=False
    )
    lease_until = models.DateTimeField(null=True, blank=True, verbose_name="租约过期时间", help_text="租约过期时间")
    worker_id = models.CharField(max_length=255, null=True, blank=True, verbose_name="worker ID", help_text="worker ID")
    attempts = models.IntegerField(default=0, verbose_name="尝试次数", help_text="尝试次数")
    error_message = models.TextField(null=True, blank=True, verbose_name="错误信息", help_text="错误信息")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间", help_text="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间", help_text="更新时间")

    class Meta:
        db_table = "monitor_waiting_tasks"
        indexes = [
            Index(fields=["created_at"], name="idx_waiting_tasks_created"),
            Index(fields=["status", "lease_until"], name="idx_waiting_tasks_status_lease"),
            Index(fields=["target"], name="idx_waiting_tasks_target"),
            Index(fields=["updated_at"], name="idx_waiting_tasks_updated"),
        ]
        verbose_name = "监控等待任务队列"
        verbose_name_plural = "监控等待任务队列"

    def __str__(self):
        return f"{self.target.domain} - {self.status}"

class MonitorPlatform(models.Model):
    platform = models.CharField(unique=True, verbose_name="模拟测试平台", blank=False, null=False, max_length=255, help_text="模拟测试平台")
    enabled = models.BooleanField(default=False, verbose_name="是否启用", help_text="是否启用")
    website_url = models.CharField(max_length=2048, null=False, blank=False, verbose_name="模拟测试平台入口", help_text="模拟测试平台入口",default="")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间", help_text="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间", help_text="更新时间")

    class Meta:
        db_table = "monitor_platforms"
        verbose_name = "模拟测试平台"
        verbose_name_plural = "模拟测试平台"
    def __str__(self):
        return self.platform


class MonitorTask(models.Model):
    platform = models.ForeignKey(
        MonitorPlatform,
        on_delete=models.CASCADE,
        db_column="platform_id",
        related_name="tasks",
    )
    domain = models.CharField(verbose_name="域名", blank=False, null=False, max_length=255, help_text="域名")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间", help_text="创建时间")
    status = models.CharField(
        max_length=10,
        choices=[
            ("success", "success"),
            ("failed", "failed"),
            ("unknow", "unknow"),
            ("running", "running"),
        ],
        verbose_name="状态",
        blank=False,
        null=False,
        help_text="状态，可选：success, failed, unknow, running"
    )
    failure_rate = models.FloatField(null=True, blank=True, verbose_name="域名访问失败率", help_text="此次任务采集数据中，访问失败的比例（0~1）", default=0.0)
    proxy_ip = models.CharField(null=False, blank=False, verbose_name="代理服务器ip", help_text="代理服务器ip",max_length=255)
    headless = models.BooleanField(null=False, blank=False, default=False, verbose_name="浏览器 headless 模式", help_text="是否启用浏览器 headless 模式")
    count = models.IntegerField(default=0,null=False, blank=False, verbose_name="抓取结果数", help_text="抓取结果数")
    browser_launch_ms = models.FloatField(null=True, blank=True, verbose_name="浏览器启动时间", help_text="浏览器启动时间")
    collect_ms = models.FloatField(null=True, blank=True, verbose_name="数据采集时间", help_text="数据采集时间")
    insert_ms = models.FloatField(null=True, blank=True, verbose_name="数据插入时间", help_text="数据插入时间")
    total_ms = models.FloatField(null=True, blank=True, verbose_name="总时间", help_text="总时间")
    error_type = models.CharField(null=True, blank=True, verbose_name="错误类型", help_text="错误类型",max_length=255)
    error_message = models.TextField(null=True, blank=True, verbose_name="错误信息", help_text="错误信息")

    class Meta:
        db_table = "monitor_tasks"
        verbose_name = "监控任务"
        verbose_name_plural = "监控任务"
    def __str__(self):
        return f"{self.domain}"

class MonitorDomainResult(models.Model):
    domain = models.CharField(verbose_name="检测域名", blank=False, null=False, max_length=255, help_text="检测域名",default="")
    task = models.ForeignKey(
        MonitorTask,
        on_delete=models.CASCADE,
        db_column="task_id",
        related_name="results",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间", help_text="更新时间")
    isp = models.CharField(null=True, blank=True, verbose_name="IP 所属运营商", help_text="IP 所属运营商",max_length=255)
    detect_node_location = models.CharField(null=True, blank=True, verbose_name="检测节点位置", help_text="检测节点位置",max_length=255)
    ip_location = models.CharField(null=True, blank=True, verbose_name="响应 IP 位置", help_text="响应 IP 位置",max_length=255)
    response_ip = models.CharField(null=True, blank=True, verbose_name="响应 IP", help_text="响应 IP",max_length=255)
    status_code = models.CharField(null=True, blank=True, verbose_name="检测状态码", help_text="检测状态码",max_length=255)

    download_time = models.FloatField(null=True, blank=True, verbose_name="下载时间", help_text="下载时间")
    connect_time = models.FloatField(null=True, blank=True, verbose_name="连接时间", help_text="连接时间")
    dns_time = models.FloatField(null=True, blank=True, verbose_name="DNS 解析时间", help_text="DNS 解析时间")
    total_time = models.FloatField(null=True, blank=True, verbose_name="数据抓取总时间", help_text="数据抓取总时间")
    
    raw = models.JSONField(null=True, blank=True, verbose_name="原始数据", help_text="原始数据")



    class Meta:
        db_table = "monitor_results"
        verbose_name = "域名检测结果"
        verbose_name_plural = "域名检测结果"
    def __str__(self):
        return f"{self.task.domain}"



class MonitorConfig(models.Model):
    class ValueType(models.TextChoices):
        BOOL = "bool", "bool"
        INT = "int", "int"
        FLOAT = "float", "float"
        STR = "str", "str"
        JSON = "json", "json"

    key = models.CharField(max_length=128, verbose_name="配置键", help_text="配置键", unique=True, blank=False, null=False)
    value_type = models.CharField(max_length=8, choices=ValueType.choices, default=ValueType.STR, verbose_name="值类型", help_text="值类型")

    value_str = models.CharField(null=True, blank=True, verbose_name="字符串值", help_text="字符串值",max_length=255)
    value_int = models.IntegerField(null=True, blank=True, verbose_name="整数值", help_text="整数值")
    value_float = models.FloatField(null=True, blank=True, verbose_name="浮点值", help_text="浮点值")
    value_bool = models.BooleanField(null=True, blank=True, verbose_name="布尔值", help_text="布尔值")
    value_json = models.JSONField(null=True, blank=True, verbose_name="JSON 值", help_text="JSON 值")

    description = models.TextField(null=True, blank=True, verbose_name="说明")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "monitor_config"
        indexes = [
            models.Index(fields=["key"], name="ix_mcfg_key"),
        ]
        verbose_name = "监控 APP 配置项"
        verbose_name_plural = "监控 APP 配置项"

    def __str__(self):
        return self.key

class MonitorAlertedDomais(models.Model):
    domain = models.CharField(verbose_name="告警域名", blank=False, null=False, max_length=255, help_text="告警域名",default="")
    alert_time = models.DateTimeField(auto_now_add=True, verbose_name="告警时间", help_text="告警时间")
    alert_type = models.CharField(null=True, blank=True, verbose_name="告警类型", help_text="告警类型",max_length=255)
    alert_message = models.TextField(null=True, blank=True, verbose_name="告警信息", help_text="告警信息")

    class Meta:
        verbose_name = "告警域名"
        verbose_name_plural = "告警域名"
    def __str__(self):
        return f"{self.domain}-{self.alert_type}-{self.alert_message}"


class MonitorDomainDiagnosis(models.Model):
    class DiagnosisType(models.TextChoices):
        NORMAL = "normal", "normal"
        HTTP_ONLY_FAILURE = "http_only_failure", "http_only_failure"
        DNS_MISCONFIG = "dns_misconfig", "dns_misconfig"
        REGISTRAR_DNS_SUSPENDED = "registrar_dns_suspended", "registrar_dns_suspended"
        REGISTRAR_HOLD = "registrar_hold", "registrar_hold"
        INCONCLUSIVE = "inconclusive", "inconclusive"

    domain = models.CharField(verbose_name="诊断域名", blank=False, null=False, max_length=255, help_text="诊断域名")
    target = models.ForeignKey(
        MonitorDomainTarget,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="target_id",
        related_name="diagnoses",
    )
    task = models.ForeignKey(
        MonitorTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="task_id",
        related_name="diagnoses",
    )
    diagnosis_type = models.CharField(
        max_length=64,
        choices=DiagnosisType.choices,
        verbose_name="诊断类型",
        help_text="诊断类型",
    )
    confidence = models.FloatField(default=0.0, verbose_name="置信度", help_text="0~1")
    evidence = models.JSONField(null=True, blank=True, verbose_name="诊断证据", help_text="DNS/RDAP/平台复测证据")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        db_table = "monitor_domain_diagnoses"
        indexes = [
            models.Index(fields=["domain", "created_at"], name="idx_mdiag_domain_created"),
            models.Index(fields=["diagnosis_type", "created_at"], name="idx_mdiag_type_created"),
        ]
        verbose_name = "域名诊断记录"
        verbose_name_plural = "域名诊断记录"

    def __str__(self):
        return f"{self.domain}-{self.diagnosis_type}-{self.confidence:.2f}"


class MonitorPlatformCooldown(models.Model):
    platform = models.OneToOneField(
        MonitorPlatform,
        on_delete=models.CASCADE,
        db_column="platform_id",
        related_name="cooldown",
    )
    cooldown_until = models.DateTimeField(null=True, blank=True, verbose_name="冷却截止时间", help_text="平台冷却截止时间")
    consecutive_failures = models.IntegerField(default=0, verbose_name="连续平台失败次数", help_text="连续平台失败次数")
    reason = models.CharField(max_length=255, null=True, blank=True, verbose_name="冷却原因", help_text="冷却原因")
    last_error_type = models.CharField(max_length=255, null=True, blank=True, verbose_name="最后错误类型", help_text="最后错误类型")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "monitor_platform_cooldowns"
        indexes = [
            models.Index(fields=["cooldown_until"], name="idx_mpc_cooldown_until"),
        ]
        verbose_name = "平台冷却状态"
        verbose_name_plural = "平台冷却状态"

    def __str__(self):
        return f"{self.platform}-{self.consecutive_failures}-{self.cooldown_until}"
