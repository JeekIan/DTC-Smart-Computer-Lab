from django.contrib import admin

from .models import SyncConflictLog, SyncedBooking, SyncQueue


@admin.register(SyncedBooking)
class SyncedBookingAdmin(admin.ModelAdmin):
    list_display = ('sync_uuid', 'origin', 'state', 'session', 'updated_at')
    list_filter = ('origin', 'state')


@admin.register(SyncConflictLog)
class SyncConflictLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'kind', 'message')
    list_filter = ('kind',)


@admin.register(SyncQueue)
class SyncQueueAdmin(admin.ModelAdmin):
    list_display = ('object_type', 'local_id', 'synced', 'attempts', 'created_at')
    list_filter = ('object_type', 'synced')
