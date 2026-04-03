from django.db import models

class Vendor(models.Model):
    name = models.CharField(max_length=255, verbose_name='供应商名称', blank=False, null=False, unique=True)
    description = models.TextField(verbose_name='供应商描述', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    vendor_code = models.CharField(max_length=255, verbose_name='供应商代码', blank=True, null=True, unique=True)

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'vendor'
        verbose_name = '供应商'
        verbose_name_plural = '供应商'
# Create your models here.

class CloudAPIAuthKey(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "active"
        INACTIVE = "inactive", "inactive"

    name = models.CharField(max_length=255, verbose_name="账号名称", blank=False, null=False, unique=True)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="auth_keys", verbose_name="供应商", blank=False, null=False, help_text="供应商" )
    sub_account = models.CharField(max_length=255, verbose_name="子账号", blank=True, null=True)
    email = models.EmailField(verbose_name="账号邮箱", blank=True, null=True, help_text="账号邮箱")
    api_key = models.TextField(verbose_name="API Key", blank=True, null=True, help_text="API Key, access key")
    api_secret = models.TextField(verbose_name="API Secret", blank=True, null=True, help_text="API Secret, secret key")
    api_key_1 = models.TextField(verbose_name="API Key 1", blank=True, null=True, help_text="API Key 1, access key 1")
    api_secret_1 = models.TextField(verbose_name="API Secret 1", blank=True, null=True, help_text="API Secret 1, secret key 1")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.INACTIVE, verbose_name="API 状态", help_text="API 状态")
    status_info = models.CharField(max_length=1024, verbose_name="API 状态信息", blank=True, null=True, help_text="API 状态信息")
    description = models.TextField(verbose_name="备注", blank=True, null=True, help_text="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "cloud_api_auth_key"
        verbose_name = "云 API 认证密钥"
        verbose_name_plural = "云 API 认证密钥"
        constraints = [
            models.UniqueConstraint(fields=["vendor", "name"], name="uq_caak_vendor_name"),
        ]
        indexes = [
            models.Index(fields=["vendor", "status"], name="ix_caak_vendor_status"),
        ]

    def __str__(self):
        return self.name