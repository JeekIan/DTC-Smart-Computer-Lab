import uuid

from django.db import models


class SyncQueue(models.Model):
    """
    Local outbox: one row per change that still needs to reach the cloud
    server. The edge server is always the source of truth — this table is
    just a to-do list of "things to tell the cloud about once we're online".

    Nothing reads this table to make decisions locally; it exists purely so
    the sync worker (see sync.py) knows what to send, and so nothing gets
    lost if the edge server restarts mid-sync (unsynced rows just stay
    unsynced until the next successful run).
    """
    OBJECT_TYPES = [
        ('reservation', 'Reservation'),
        ('session', 'Session'),
        ('request', 'Booking request'),
        ('session_deleted', 'Session deleted/cancelled'),
        ('verdict', 'Booking verdict (accepted/rejected)'),
        ('checkin', 'Session check-in'),
        ('issue', 'Issue'),
        ('activity_log', 'PC Activity Log'),
        ('notification', 'Notification'),
    ]

    object_type = models.CharField(max_length=20, choices=OBJECT_TYPES)
    # The local primary key of the row this entry describes. Combined with
    # object_type + edge_id below, this is what makes re-sending a record
    # safe (idempotent) on the cloud side instead of creating duplicates.
    local_id = models.PositiveIntegerField()
    # A stable, globally-unique id for this specific change, sent to the
    # cloud as the de-dupe key. Using uuid instead of local_id alone because
    # local_id resets per model and per edge server — this doesn't.
    edge_id = models.UUIDField()

    # Snapshot of the record at queue time, as plain JSON — the sync worker
    # sends this as-is, it never has to re-query the original model (which
    # may have changed again by the time sync actually runs).
    payload = models.JSONField()

    created_at = models.DateTimeField(auto_now_add=True)
    synced = models.BooleanField(default=False)
    synced_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default='')

    class Meta:
        indexes = [
            models.Index(fields=['synced', 'created_at']),
        ]

    def __str__(self):
        return f'{self.object_type}:{self.local_id} ({"synced" if self.synced else "pending"})'


def enqueue(object_type, local_id, payload):
    """
    Call this from wherever the record is created/updated — e.g. at the end
    of a reservation-approval view, or inside notifications/services.py.
    One line, doesn't block on the network, never raises past this call.
    """
    from django.conf import settings
    if getattr(settings, 'CLOUD_SYNC_ROLE', 'edge') == 'cloud':
        return  # the cloud never pushes anything, so never queue
    try:
        SyncQueue.objects.create(
            object_type=object_type,
            local_id=local_id,
            edge_id=uuid.uuid4(),
            payload=payload,
        )
    except Exception:
        # Queueing must never break the actual operation (the reservation
        # still got approved, the log still got written) even if this fails.
        pass


class SyncedBooking(models.Model):
    """
    The cross-server identity + verdict of one Session (booking).

    Each Session gets one of these. `sync_uuid` is the same on the lab
    server (edge) and the cloud, so both sides can talk about "the same
    booking" even though their database ids differ.

    CONFLICT RULE (why two conflicting bookings can never both be accepted):
    only the EDGE can make a booking 'confirmed'. A booking made on the
    cloud starts as 'provisional' and is only a *proposal* until the edge
    runs the normal slot check against its own schedule and accepts it.
    If it overlaps, the edge answers 'rejected' and the cloud removes it.
    """
    ORIGIN = [('edge', 'Lab server'), ('cloud', 'Cloud')]
    STATE = [
        ('provisional', 'Provisional (waiting for the lab server)'),
        ('confirmed', 'Confirmed'),
        ('rejected', 'Rejected (conflict)'),
        ('cancelled', 'Cancelled'),
    ]

    sync_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    session = models.OneToOneField(
        'scheduling.Session', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='sync_booking',
    )
    origin = models.CharField(max_length=5, choices=ORIGIN)
    state = models.CharField(max_length=12, choices=STATE, default='provisional', db_index=True)
    reject_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.sync_uuid} [{self.origin}/{self.state}]'


class SyncConflictLog(models.Model):
    """Audit trail of every booking the sync layer rejected, evicted, or
    flagged — so the In-Charge can see exactly what lost and why."""
    KIND = [
        ('rejected', 'Cloud booking rejected by lab server'),
        ('evicted', 'Provisional booking removed (lab server booking won)'),
        ('warning', 'Accepted with a warning'),
        ('anomaly', 'Unexpected state — needs manual review'),
    ]
    sync_uuid = models.UUIDField(null=True, blank=True)
    kind = models.CharField(max_length=10, choices=KIND)
    message = models.TextField()
    snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.kind}: {self.message[:60]}'


class ReceivedRecord(models.Model):
    """Cloud side only: every edge record already applied, keyed by edge_id,
    so a batch re-sent after a lost response is never applied twice."""
    edge_id = models.UUIDField(unique=True)
    object_type = models.CharField(max_length=20)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.object_type} {self.edge_id}'


class DeferredNotice(models.Model):
    """Cloud side only: a reservation-code notification (email / in-app) that
    was held back because its booking is still provisional. It is sent once
    the lab server confirms the booking, and thrown away if it rejects it —
    so nobody is ever given a code that does not work."""
    booking = models.ForeignKey(SyncedBooking, on_delete=models.CASCADE, related_name='deferred_notices')
    func_name = models.CharField(max_length=60)
    target_model = models.CharField(max_length=10)  # 'request' or 'session'
    target_id = models.PositiveIntegerField()
    code = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.func_name} for {self.booking_id}'


class SyncedRequest(models.Model):
    """Cross-server identity of one booking REQUEST (what an Instructor
    submits). Same idea as SyncedBooking, but requests are only ever
    'pending' until an Admin/In-Charge decides, so this just keeps both
    servers talking about the same request and lets either one approve it.
    `updated_at` only moves when the request is changed on THIS server, which
    is what the other side uses to ask for 'everything new since ...'."""
    sync_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    request = models.OneToOneField(
        'scheduling.SessionRequest', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='sync_request',
    )
    origin = models.CharField(max_length=5, choices=SyncedBooking.ORIGIN)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    def __str__(self):
        return f'{self.sync_uuid} [{self.origin}]'


class SyncState(models.Model):
    """Edge side: small key/value store (e.g. how far it has read the cloud's requests)."""
    key = models.CharField(max_length=40, unique=True)
    value = models.CharField(max_length=100, blank=True, default='')
