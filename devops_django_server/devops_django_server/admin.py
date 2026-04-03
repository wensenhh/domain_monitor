from django.contrib.admin.models import LogEntry
from django.contrib import admin


@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    list_display = ['action_time', 'user', 'object_repr', 'object_id', 'action_flag', 'content_type','change_message']
    
