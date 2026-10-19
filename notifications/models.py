from django.db import models
from django.conf import settings


class Notification(models.Model):
    # Manual types (created via "+ Create Notification" by Admin/In-Charge)
    # plus the auto-generated system event types listed in the Notifications
    # & Alerts spec. `category` (below) maps every one of these to a single
    # color-coded bucket (Critical/Warning/Information/Success) for display.
    TYPE_CHOICES = [
        # ---- manual / composed ----
        ('booking_confirmation', 'Booking Confirmation'),
        ('schedule_reminder', 'Schedule Reminder'),
        ('maintenance_alert', 'Maintenance Alert'),
        ('system_announcement', 'System Announcement'),
        ('general', 'General'),
        # ---- auto-generated: reservations ----
        ('reservation_submitted', 'New Reservation Request'),
        ('reservation_approved', 'Reservation Approved'),
        ('reservation_declined', 'Reservation Rejected'),
        ('reservation_conflict', 'Reservation Conflict'),
        ('walkin_override_approved', 'Walk-in/Override Approved'),
        ('roster_session_scheduled', 'Class Roster Session Scheduled'),
        ('roster_submitted', 'Class Roster Submitted'),
        ('roster_approved', 'Class Roster Approved'),
        ('roster_rejected', 'Class Roster Rejected'),
        ('session_reminder', 'Session Starting Soon'),
        # ---- auto-generated: PCs ----
        ('pc_offline', 'PC Offline'),
        ('pc_online', 'PC Back Online'),
        ('pc_maintenance', 'PC Needs Maintenance'),
        # ---- auto-generated: maintenance/equipment ----
        ('maintenance_submitted', 'Maintenance Request Submitted'),
        ('maintenance_completed', 'Maintenance Completed'),
        # ---- auto-generated: issue reports ----
        ('issue_reported', 'New Issue Reported'),
        ('issue_status_changed', 'Issue Status Updated'),
        ('issue_resolved', 'Issue Resolved'),
        # ---- auto-generated: security ----
        ('login_security_alert', 'Failed Login Attempts'),
    ]

    # Color-coded bucket per the spec:
    #   Critical – PC offline, failed logins, reservation conflicts
    #   Warning  – Maintenance required, session starting soon
    #   Info     – Reservation approved/rejected/submitted
    #   Success  – Maintenance/session completed, PC back online
    CRITICAL = 'critical'
    WARNING = 'warning'
    INFO = 'info'
    SUCCESS = 'success'

    CATEGORY_BY_TYPE = {
        'booking_confirmation': INFO,
        'schedule_reminder': WARNING,
        'maintenance_alert': WARNING,
        'system_announcement': INFO,
        'general': INFO,
        'reservation_submitted': INFO,
        'reservation_approved': INFO,
        'reservation_declined': INFO,
        'reservation_conflict': CRITICAL,
        'walkin_override_approved': INFO,
        'roster_session_scheduled': INFO,
        'roster_submitted': INFO,
        'roster_approved': INFO,
        'roster_rejected': INFO,
        'session_reminder': WARNING,
        'pc_offline': CRITICAL,
        'pc_online': SUCCESS,
        'pc_maintenance': WARNING,
        'maintenance_submitted': WARNING,
        'maintenance_completed': SUCCESS,
        'issue_reported': WARNING,
        'issue_status_changed': INFO,
        'issue_resolved': SUCCESS,
        'login_security_alert': CRITICAL,
    }

    CATEGORY_LABELS = {
        CRITICAL: 'Critical',
        WARNING: 'Warning',
        INFO: 'Information',
        SUCCESS: 'Success',
    }

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=200)
    message = models.TextField(blank=True)
    notification_type = models.CharField(max_length=30, choices=TYPE_CHOICES, default='general')
    # Which lab this notification concerns, if any. Lets a Lab In-Charge's
    # feed be filtered down to "their" laboratory when needed, and lets the
    # Alert Settings toggles reason about scope consistently.
    lab = models.ForeignKey(
        'labs.Lab', null=True, blank=True, on_delete=models.SET_NULL, related_name='notifications'
    )
    read = models.BooleanField(default=False)
    pinned = models.BooleanField(default=False)
    # True for anything generated automatically by the system (reservation
    # events, PC status, maintenance, security, session reminders). False
    # for notifications a human composed and sent via "+ Create Notification".
    is_system_alert = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-pinned', '-created_at']

    def __str__(self):
        return self.title

    @property
    def category(self):
        return self.CATEGORY_BY_TYPE.get(self.notification_type, self.INFO)

    @property
    def category_label(self):
        return self.CATEGORY_LABELS.get(self.category, 'Information')

    @classmethod
    def types_for_category(cls, category):
        return [t for t, c in cls.CATEGORY_BY_TYPE.items() if c == category]


class AlertSettings(models.Model):
    """Single system-wide row (singleton) letting the Admin turn specific
    categories of AUTO-GENERATED notifications on/off from the Alert
    Settings page. Manual "+ Create Notification" announcements are never
    gated by these -- they're always sent since a human explicitly chose to
    send them."""

    reservation_notifications = models.BooleanField(
        default=True,
        help_text='New reservation requests, approvals/rejections, walk-in/override approvals, and reservation conflicts.'
    )
    pc_status_alerts = models.BooleanField(
        default=True, help_text='A lab PC goes offline unexpectedly, or comes back online.'
    )
    maintenance_alerts = models.BooleanField(
        default=True, help_text='A PC/equipment is flagged for maintenance, and maintenance submitted/completed updates.'
    )
    login_security_alerts = models.BooleanField(
        default=True, help_text='Multiple failed login attempts detected on an account.'
    )
    session_reminders = models.BooleanField(
        default=True, help_text='A laboratory session is about to start (10-15 minutes before).'
    )
    system_announcements = models.BooleanField(
        default=True,
        help_text='Manual announcements composed by Admin/Lab In-Charge always send regardless; this toggle is reserved for future auto-echoed system announcements.'
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Alert Settings'
        verbose_name_plural = 'Alert Settings'

    def __str__(self):
        return 'Alert Settings'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
