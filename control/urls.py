from django.urls import path
from . import views

urlpatterns = [
    path('',                views.panel,         name='control_panel'),
    path('status/',         views.status,        name='control_status'),
    path('run/',            views.run_now,       name='control_run'),
    path('cancel/',         views.cancel,        name='control_cancel'),
    path('schedule/',       views.save_schedule, name='control_schedule'),
    path('delete/',         views.delete_rows,   name='control_delete'),
    path('process-one/',    views.process_one,   name='control_process_one'),
    path('clear-cache/',    views.clear_cache,   name='control_clear_cache'),
]
