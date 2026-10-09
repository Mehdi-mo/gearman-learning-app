import os
from django.apps import AppConfig


class ControlConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'control'

    def ready(self):
        # Start the in-process scheduler once. Under runserver's autoreloader
        # ready() fires in both processes, so only the worker process starts it.
        if os.environ.get('RUN_MAIN') != 'true' and os.environ.get('CONTROL_TICKER') != '1':
            return
        try:
            from . import runner
            runner.start_ticker()
        except Exception:
            pass
