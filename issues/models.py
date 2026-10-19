from django.db import models
from django.conf import settings

class Issue(models.Model):
    TYPE = [
        ('hardware', 'Hardware Fault'),
        ('network', 'Network Drop'),
        ('software', 'Software Crash'),
        ('power', 'Power Issue'),
        ('peripheral', 'Peripheral/Accessory'),
        ('facility', 'Facility/Environment'),
        ('aircon', 'Air Conditioning/Ventilation'),
        ('lighting', 'Lighting/Electrical'),
        ('projector', 'Projector/Display'),
        ('furniture', 'Furniture/Room'),
        ('safety', 'Safety/Cleanliness'),
        ('other', 'Other'),
    ]
    PRIORITY = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ]
    STATUS = [
        ('open', 'Open'),
        ('assigned', 'Assigned'),
        ('in_progress', 'In Progress'),
        ('resolved', 'Resolved'),
    ]
    pc = models.ForeignKey('labs.PC', on_delete=models.SET_NULL, null=True, blank=True)
    lab = models.ForeignKey('labs.Lab', on_delete=models.CASCADE)
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    assigned_technician = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='issues_assigned',
        help_text='Staff member (admin or lab in-charge) assigned to fix this issue.'
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    issue_type = models.CharField(max_length=20, choices=TYPE, default='other')
    priority = models.CharField(max_length=10, choices=PRIORITY, default='medium')
    status = models.CharField(max_length=15, choices=STATUS, default='open')
    notes = models.TextField(blank=True)
    resolution_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    hidden_by_reporter = models.BooleanField(
        default=False,
        help_text="Set when the reporter (an Instructor) deletes the report from their own 'My issue reports' list. The record itself is kept so Admin/Lab In-Charge still have it until they delete it.",
    )

    def __str__(self):
        return self.title