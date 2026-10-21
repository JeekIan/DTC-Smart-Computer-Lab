import os
import sys
from django.apps import AppConfig


class CloudSyncConfig(AppConfig):
    name = 'cloud_sync'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        # Signals must be connected in every process (incl. the reloader parent).
        from cloud_sync import signals  # noqa: F401

        is_dev_server = 'runserver' in sys.argv
        # With --noreload there is only ONE process (no parent/child), so it must run the worker.
        is_reloader_parent = (
            is_dev_server and '--noreload' not in sys.argv and os.environ.get('RUN_MAIN') != 'true'
        )
        if is_reloader_parent:
            return

        from cloud_sync.sync import start_cloud_sync_worker
        start_cloud_sync_worker()
