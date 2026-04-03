from django.contrib import admin

# Register your models here.
from .models import Vendor, CloudAPIAuthKey


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "vendor_code",  "created_at", "updated_at","description")
    list_filter = ("name",)
    search_fields = ("vendor_code", "name")
    ordering = ("name",)


@admin.register(CloudAPIAuthKey)
class CloudAPIAuthKeyAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "vendor", "email", "sub_account", "api_key", "api_secret", 
    "api_key_1", "api_secret_1",
    "status", "status_info", "updated_at")
    list_filter = ("vendor", "name", "email",  "status")
    search_fields = ("name", "email", "sub_account", "vendor__name", "vendor__vendor_code")
    ordering = ("-updated_at",)