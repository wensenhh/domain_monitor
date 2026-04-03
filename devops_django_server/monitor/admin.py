from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import path, reverse

# Register your models here.
from .models import MonitorDomainTarget,MonitorWaitingTask,MonitorPlatform,MonitorDomainResult,MonitorTask,MonitorConfig, MonitorAlertedDomais
from .domain_utils import clean_domain


def _parse_domains(raw: str) -> tuple[list[str], list[str]]:
    invalid: list[str] = []
    out: list[str] = []
    seen: set[str] = set()

    for line in (raw or "").splitlines():
        s = str(line).strip()
        if not s:
            continue
        for token in s.replace("，", ",").replace(";", ",").replace("；", ",").split(","):
            t = clean_domain(token)
            if not t:
                continue
            if len(t) > 255:
                invalid.append(token.strip())
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append(t)

    return out, invalid


class BulkAddMonitorDomainTargetForm(forms.Form):
    domains = forms.CharField(
        label="域名列表",
        widget=forms.Textarea(attrs={"rows": 18, "cols": 120}),
        help_text="每行一个域名，也支持逗号分隔",
    )
    enabled = forms.BooleanField(label="启用", required=True, initial=True)
    priority = forms.IntegerField(label="优先级", required=False, initial=0)
    schedule_interval_minutes = forms.IntegerField(label="调度间隔(分钟)", required=True, initial=10, min_value=1)
    dry_run = forms.BooleanField(label="仅预览不写入", required=False, initial=False)

def _split_domains(q: str) -> list[str]:
    raw = (q or "").strip()
    if not raw:
        return []
    parts = []
    for line in raw.splitlines():
        for token in line.replace("，", ",").replace(";", ",").replace("；", ",").replace(" ", ",").split(","):
            t = clean_domain(token)
            if t:
                parts.append(t)
    out = []
    seen = set()
    for d in parts:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out

@admin.register(MonitorDomainTarget)
class MonitorDomainTargetAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "domain",
        "enabled",
        "priority",
        "created_at",
        "updated_at",
        "last_scheduled_at",
        "schedule_interval_minutes"
    )
    search_fields = ("domain",)  # 增加搜索功能：支持按 domain 字段搜索
    change_list_template = "admin/monitor/monitordomaintarget/change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "bulk_add/",
                self.admin_site.admin_view(self.bulk_add_view),
                name="monitor_monitordomaintarget_bulk_add",
            )
        ]
        return custom + urls

    def bulk_add_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        result = None
        if request.method == "POST":
            form = BulkAddMonitorDomainTargetForm(request.POST)
            if form.is_valid():
                domains, invalid = _parse_domains(form.cleaned_data["domains"])
                enabled = bool(form.cleaned_data["enabled"])
                priority = int(form.cleaned_data["priority"] or 0)
                schedule_interval_minutes = int(form.cleaned_data["schedule_interval_minutes"] or 10)
                dry_run = bool(form.cleaned_data["dry_run"])

                existing = set(
                    MonitorDomainTarget.objects.filter(domain__in=domains).values_list("domain", flat=True)
                )
                to_create_domains = [d for d in domains if d not in existing]

                if not dry_run and to_create_domains:
                    objs = [
                        MonitorDomainTarget(
                            domain=d,
                            enabled=enabled,
                            priority=priority,
                            schedule_interval_minutes=schedule_interval_minutes,
                        )
                        for d in to_create_domains
                    ]
                    MonitorDomainTarget.objects.bulk_create(objs, ignore_conflicts=True, batch_size=1000)
                    messages.success(
                        request,
                        f"批量添加完成：新增 {len(to_create_domains)}，已存在 {len(existing)}，无效 {len(invalid)}",
                    )
                    return redirect("admin:monitor_monitordomaintarget_changelist")

                result = {
                    "input_count": len([x for x in (form.cleaned_data.get("domains") or "").splitlines() if x.strip()]),
                    "valid_count": len(domains),
                    "invalid": invalid,
                    "existing": sorted(existing),
                    "to_create": to_create_domains,
                    "dry_run": dry_run,
                }
        else:
            form = BulkAddMonitorDomainTargetForm()

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "form": form,
            "result": result,
            "title": "批量添加监控目标域名",
            "changelist_url": reverse("admin:monitor_monitordomaintarget_changelist"),
        }
        return render(request, "admin/monitor/monitordomaintarget/bulk_add.html", context)

    def get_search_results(self, request, queryset, search_term):
        domains = _split_domains(search_term)

        if len(domains) >= 2:
            qs = queryset.filter(domain__in=domains)
            return qs, False

        return super().get_search_results(request, queryset, search_term)    


@admin.register(MonitorPlatform)
class MonitorPlatformAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "platform",
        "website_url",
        "enabled",
        "created_at",
        "updated_at",
    )

@admin.register(MonitorTask)
class MonitorTaskAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "domain",
        "platform",
        "created_at",
        "status",
        "failure_rate",
        "proxy_ip",
        "headless",
        "count",
        "browser_launch_ms",
        "collect_ms",
        "insert_ms",
        "total_ms",
        "error_type",
        "error_message",
    )
    search_fields = ("domain",)  # 增加搜索功能：支持按 domain 字段搜索

@admin.register(MonitorDomainResult)
class MonitorDomainResultAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "domain",
        "task_id",
        "isp",
        "detect_node_location",
        "response_ip",
        "ip_location",
        "status_code",
        "total_time",
        "dns_time",
        "connect_time",
        "download_time",
        "updated_at"
    )
    search_fields = ("domain", "task__id__exact")  # 增加搜索功能：支持按 domain 字段搜索


@admin.register(MonitorWaitingTask)
class MonitorWaitingTaskAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "domain",
        "target",
        "status",
        "lease_until",
        "worker_id",
        "attempts",
        "error_message",
        "created_at",
        "updated_at",
    )
    search_fields = ("domain", "target__domain")  # 增加搜索功能：支持按 domain 字段搜索


@admin.register(MonitorConfig)
class MonitorConfigAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "key",
        "value_type",
        "value_str",
        "value_int",
        "value_float",
        "value_bool",
        "value_json",
        "created_at",
        "updated_at",
        "description",
    )
    list_filter = (
        "key",
        "value_type",
    )
    search_fields = (
        "key",
        "description",
    )

@admin.register(MonitorAlertedDomais)
class MonitorAlertedDomaisAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "domain",
        "alert_time",
        "alert_type",
        "alert_message",
    )
    search_fields = (
        "domain",
        "alert_type",
        "alert_message",
    )