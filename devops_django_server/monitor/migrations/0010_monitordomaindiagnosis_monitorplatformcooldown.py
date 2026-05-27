# Generated manually for domain DNS diagnosis and platform cooldown.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("monitor", "0009_monitoralerteddomais"),
    ]

    operations = [
        migrations.CreateModel(
            name="MonitorDomainDiagnosis",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("domain", models.CharField(help_text="诊断域名", max_length=255, verbose_name="诊断域名")),
                (
                    "diagnosis_type",
                    models.CharField(
                        choices=[
                            ("normal", "normal"),
                            ("http_only_failure", "http_only_failure"),
                            ("dns_misconfig", "dns_misconfig"),
                            ("registrar_dns_suspended", "registrar_dns_suspended"),
                            ("registrar_hold", "registrar_hold"),
                            ("inconclusive", "inconclusive"),
                        ],
                        help_text="诊断类型",
                        max_length=64,
                        verbose_name="诊断类型",
                    ),
                ),
                ("confidence", models.FloatField(default=0.0, help_text="0~1", verbose_name="置信度")),
                ("evidence", models.JSONField(blank=True, help_text="DNS/RDAP/平台复测证据", null=True, verbose_name="诊断证据")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                (
                    "target",
                    models.ForeignKey(
                        blank=True,
                        db_column="target_id",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="diagnoses",
                        to="monitor.monitordomaintarget",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        blank=True,
                        db_column="task_id",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="diagnoses",
                        to="monitor.monitortask",
                    ),
                ),
            ],
            options={
                "verbose_name": "域名诊断记录",
                "verbose_name_plural": "域名诊断记录",
                "db_table": "monitor_domain_diagnoses",
                "indexes": [
                    models.Index(fields=["domain", "created_at"], name="idx_mdiag_domain_created"),
                    models.Index(fields=["diagnosis_type", "created_at"], name="idx_mdiag_type_created"),
                ],
            },
        ),
        migrations.CreateModel(
            name="MonitorPlatformCooldown",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cooldown_until", models.DateTimeField(blank=True, help_text="平台冷却截止时间", null=True, verbose_name="冷却截止时间")),
                ("consecutive_failures", models.IntegerField(default=0, help_text="连续平台失败次数", verbose_name="连续平台失败次数")),
                ("reason", models.CharField(blank=True, help_text="冷却原因", max_length=255, null=True, verbose_name="冷却原因")),
                ("last_error_type", models.CharField(blank=True, help_text="最后错误类型", max_length=255, null=True, verbose_name="最后错误类型")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "platform",
                    models.OneToOneField(
                        db_column="platform_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="cooldown",
                        to="monitor.monitorplatform",
                    ),
                ),
            ],
            options={
                "verbose_name": "平台冷却状态",
                "verbose_name_plural": "平台冷却状态",
                "db_table": "monitor_platform_cooldowns",
                "indexes": [
                    models.Index(fields=["cooldown_until"], name="idx_mpc_cooldown_until"),
                ],
            },
        ),
    ]
