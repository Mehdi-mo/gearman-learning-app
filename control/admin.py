from django.contrib import admin
from .models import JobRun, Schedule


@admin.register(JobRun)
class JobRunAdmin(admin.ModelAdmin):
    list_display = ['job', 'status', 'done_count', 'total', 'triggered_by', 'created_at']
    list_filter  = ['job', 'status', 'triggered_by']


@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display  = ['job', 'enabled', 'run_at', 'count', 'last_run_on']
    list_editable = ['enabled', 'run_at', 'count']
