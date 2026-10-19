from django.conf import settings
from django.db import models

class Lab(models.Model):
    name = models.CharField(max_length=100)
    location = models.CharField(max_length=200, blank=True)
    opening_time = models.TimeField(
        default='07:00',
        
    )
    closing_time = models.TimeField(
        default='21:00',
       
    )

    def __str__(self):
        return self.name


class PC(models.Model):
    STATUS = [
        ('online', 'Available'),
        ('offline', 'Offline'),
        ('in_use', 'In Use'),
        ('maintenance', 'Under Maintenance'),
        ('issue', 'Has Issue'),
    ]
    lab = models.ForeignKey(Lab, on_delete=models.CASCADE, related_name='pcs')
    pc_id = models.CharField(max_length=10)
    ip_address = models.GenericIPAddressField(
        null=True, blank=True,
        help_text='Used to actually check whether this PC is reachable on the network.'
    )
    status = models.CharField(max_length=11, choices=STATUS, default='offline')
    last_active = models.DateTimeField(null=True, blank=True)
    current_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='pcs_in_use',
        help_text="The student currently signed in on this PC, set when they unlock it with their Student ID and password. Cleared once the PC goes offline or is reassigned.",
    )
    current_session = models.ForeignKey(
        'scheduling.Session',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        help_text="The reservation that most recently unlocked this PC. Used to auto re-lock the PC once that reservation's end_time passes, and cleared whenever the PC is locked again (by time running out or a manual lock).",
    )
    current_guest_name = models.CharField(
        max_length=150, blank=True, default='',
        help_text="Full name of the walk-in guest currently using this PC via Manual Unlock, when there is no registered account (current_user stays null for guests). Cleared whenever the PC is locked again.",
    )
    current_login_via_override = models.BooleanField(
        default=False,
        help_text=(
            "True if the current session started via an Admin/In-Charge "
            "Override Check-In (see OverrideCheckInView) rather than a "
            "normal student unlock or a Manual Unlock guest walk-in. Set "
            "at grant time on every login/unlock path, then copied onto "
            "each PCActivityLog sample taken while this session is active "
            "so the PC Activity Log's Search/User filter can distinguish "
            "'Override' sessions after the fact."
        ),
    )

    def __str__(self):
        return self.pc_id


class PCActivityLog(models.Model):
    """A single 'what was on screen' sample reported by the lab_pc_agent
    while a PC is unlocked — the active window's title bar text, captured
    every few seconds (see agent_config.json 'activity_report_interval_seconds').

    For browsers this is normally 'Page Title - Browser Name', which used to
    be the closest thing to 'which website' this system tracked. The agent
    can now optionally also read the address bar's URL via Windows UI
    Automation (see lab_pc_agent's uiautomation dependency) for reliable
    site identification — some single-page apps (e.g. ChatGPT) never put
    their own name in the title, only the current conversation/document
    name, so title text alone can't tell them apart from "some other site".
    Still never reads page HTML, form contents, or keystrokes.
    """
    pc = models.ForeignKey(PC, on_delete=models.CASCADE, related_name='activity_logs')
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='pc_activity_logs',
        help_text="Whoever pc.current_user was at the moment this sample was taken.",
    )
    guest_name = models.CharField(
        max_length=150, blank=True, default='',
        help_text=(
            "Whoever pc.current_guest_name was at the moment this sample was "
            "taken, for a Manual Unlock walk-in guest — there is no registered "
            "account to attach via `student` for these, since Manual Unlock "
            "access is never tied to a verified login. Blank for normal "
            "logged-in/override sessions."
        ),
    )
    session = models.ForeignKey(
        'scheduling.Session',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        help_text="Whichever reservation pc.current_session was at the moment this sample was taken.",
    )
    window_title = models.CharField(
        max_length=500, blank=True,
        help_text="The active/foreground window's title bar text at the moment of capture.",
    )
    via_override = models.BooleanField(
        default=False,
        help_text="Copied from pc.current_login_via_override at capture time — True if this sample belongs to a session that started via an Admin/In-Charge Override Check-In.",
    )
    page_url = models.CharField(
        max_length=500, blank=True, default='',
        help_text=(
            "The browser address bar's URL at the moment of capture, when the "
            "agent could read it (requires the UI Automation dependency on the "
            "PC — older/unsupported agents leave this blank and site detection "
            "falls back to guessing from window_title)."
        ),
    )
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['pc', '-captured_at']),
            models.Index(fields=['student', '-captured_at']),
        ]

    def __str__(self):
        return f'{self.pc.pc_id} — {self.window_title[:40]}'


class InventoryItem(models.Model):
    CONDITION = [
        ('good', 'Good'),
        ('fair', 'Fair'),
        ('needs_check', 'Needs Check'),
        ('faulty', 'Faulty'),
    ]
    STATUS = [
        ('operational', 'Operational'),
        ('under_repair', 'Under Repair'),
        ('retired', 'Retired'),
    ]
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=100)
    quantity = models.PositiveIntegerField()
    condition = models.CharField(max_length=20, choices=CONDITION)
    status = models.CharField(max_length=20, choices=STATUS, default='operational')
    lab = models.ForeignKey(Lab, on_delete=models.CASCADE)
    last_checked = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    # Traceability for retirement specifically (not just "last updated") —
    # set automatically by inventory_status_update() the moment status
    # flips to 'retired', cleared if it's ever un-retired. Lets a Retired
    # item's detail page/audit trail show who pulled it and why, instead of
    # a bare status pill with no history behind it.
    retired_at = models.DateTimeField(null=True, blank=True)
    retired_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='retired_inventory_items'
    )
    retirement_reason = models.CharField(max_length=255, blank=True, default='')

    def __str__(self):
        return self.name


class MaintenanceLog(models.Model):
    equipment = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='maintenance_logs')
    maintenance_date = models.DateField()
    assigned_technician = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='maintenance_tasks'
    )
    notes = models.TextField(blank=True)
    completed = models.BooleanField(default=False)
    completion_notes = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-maintenance_date']

    def __str__(self):
        return f'{self.equipment.name} — {self.maintenance_date}'


class EquipmentIssue(models.Model):
    STATUS = [
        ('open', 'Open'),
        ('resolved', 'Resolved'),
    ]
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name='equipment_issues_reported'
    )
    equipment = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='issues')
    description = models.TextField()
    date_reported = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS, default='open')
    resolution_notes = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-date_reported']

    def __str__(self):
        return f'{self.equipment.name} — {self.get_status_display()}'


class ReportLog(models.Model):
    """One row per PDF/Excel report a user has generated from the Reporting
    & Analytics page. Purely a history/audit trail — the export itself is
    still built on the fly from live data, this just remembers that it
    happened (and with which filters) so 'Recent Generated Reports' has
    something real to show, and the same file can be re-downloaded with
    the original filters intact.
    """
    REPORT_TYPES = [
        ('lab_utilization', 'Lab Utilization Report'),
        ('equipment_status', 'Equipment Status Report'),
        ('instructor_usage', 'Instructor Usage Report'),
        ('attendance', 'Attendance Report'),
        ('maintenance', 'Maintenance Report'),
    ]
    FORMAT_CHOICES = [('pdf', 'PDF'), ('excel', 'Excel')]

    report_type = models.CharField(max_length=30, choices=REPORT_TYPES)
    format = models.CharField(max_length=10, choices=FORMAT_CHOICES, default='pdf')
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='generated_reports'
    )
    # Which lab this report was scoped to at the time it was generated.
    # Null means "all labs" (an admin generating an unfiltered report).
    lab = models.ForeignKey(Lab, null=True, blank=True, on_delete=models.SET_NULL)
    # The raw querystring used (date_from/date_to/lab/instructor/etc.) so the
    # download link can reproduce the exact same filtered report.
    params = models.CharField(max_length=255, blank=True, default='')
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-generated_at']

    def __str__(self):
        return f'{self.get_report_type_display()} ({self.format}) — {self.generated_at:%Y-%m-%d %H:%M}'

    @property
    def export_url_name(self):
        return f'report_{self.report_type}_export'

    @property
    def filename(self):
        ext = 'xlsx' if self.format == 'excel' else 'pdf'
        return f'{self.report_type}_report.{ext}'