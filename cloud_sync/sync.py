"""
Edge-side sync worker. Every tick, when the cloud is reachable:

  1. PULL  the cloud's provisional bookings and judge each one locally
           (conflicts.evaluate_proposal) -> a 'verdict' is queued.
  2. PUSH  the outbox (confirmed sessions, verdicts, check-ins, ...).

The edge never waits on this; the lab keeps working fully offline and
everything queued here is delivered when the connection returns.
"""
import json
import logging
import threading
import time
import urllib.request

from django.db.models import F
from django.utils import timezone

log = logging.getLogger(__name__)


def _cloud_reachable(base_url, timeout_seconds):
    try:
        req = urllib.request.Request(f'{base_url.rstrip("/")}/api/ping/', method='GET')
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _auth(api_key):
    return {'Authorization': f'Bearer {api_key}'}


def _pull_and_judge(base_url, api_key, timeout_seconds):
    """Fetch what changed on the cloud: (1) booking requests, (2) provisional
    bookings. Requests are applied first, so each provisional booking can be
    tied to its request; then every provisional booking is accepted/rejected here."""
    import urllib.parse
    from cloud_sync.conflicts import apply_cloud_request, evaluate_proposal
    from cloud_sync.models import SyncQueue, SyncState, enqueue

    cursor, _ = SyncState.objects.get_or_create(key='request_cursor')
    qs = urllib.parse.urlencode({'since': cursor.value}) if cursor.value else ''
    req = urllib.request.Request(f'{base_url.rstrip("/")}/api/sync/pull/?{qs}', headers=_auth(api_key), method='GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return 0

    for snap in data.get('requests', []):  # oldest first
        try:
            apply_cloud_request(snap)
        except Exception:
            log.exception('could not apply request %s', snap.get('sync_uuid'))
            break  # do not move the cursor past a request we could not apply
        cursor.value = snap['updated_at']
        cursor.save()

    judged = 0
    for snap in data.get('proposals', []):  # oldest first: first come, first served
        try:
            verdict = evaluate_proposal(snap)
        except Exception:
            log.exception('could not judge proposal %s', snap.get('sync_uuid'))
            continue
        already_queued = SyncQueue.objects.filter(
            object_type='verdict', synced=False, payload__sync_uuid=snap['sync_uuid'],
        ).exists()
        if not already_queued:
            enqueue('verdict', 0, verdict)
        judged += 1
    return judged


def _push_batch(base_url, api_key, timeout_seconds, batch_size=50):
    from cloud_sync.models import SyncQueue

    pending = list(SyncQueue.objects.filter(synced=False).order_by('created_at')[:batch_size])
    if not pending:
        return 0

    records = [
        {
            'edge_id': str(row.edge_id),
            'object_type': row.object_type,
            'local_id': row.local_id,
            'payload': row.payload,
            'created_at': row.created_at.isoformat(),
        }
        for row in pending
    ]
    req = urllib.request.Request(
        f'{base_url.rstrip("/")}/api/sync/',
        data=json.dumps({'records': records}).encode('utf-8'),
        headers={'Content-Type': 'application/json', **_auth(api_key)},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            if not (200 <= resp.status < 300):
                return 0
            result = json.loads(resp.read().decode('utf-8'))
            accepted = set(result.get('accepted_edge_ids', []))
            errors = result.get('errors', {})
    except Exception:
        SyncQueue.objects.filter(id__in=[r.id for r in pending]).update(attempts=F('attempts') + 1)
        return 0

    now = timezone.now()
    done = 0
    for row in pending:
        if str(row.edge_id) in accepted:
            row.synced, row.synced_at = True, now
            row.save(update_fields=['synced', 'synced_at'])
            done += 1
        else:
            row.attempts += 1
            row.last_error = errors.get(str(row.edge_id), '')
            row.save(update_fields=['attempts', 'last_error'])
    return done


_sync_started = False


def start_cloud_sync_worker():
    global _sync_started
    if _sync_started:
        return
    _sync_started = True

    from django.conf import settings
    base_url = getattr(settings, 'CLOUD_SERVER_URL', None)
    api_key = getattr(settings, 'CLOUD_SYNC_API_KEY', '')
    interval = getattr(settings, 'CLOUD_SYNC_INTERVAL_SECONDS', 60)
    timeout_seconds = getattr(settings, 'CLOUD_SYNC_TIMEOUT_SECONDS', 5)

    # The cloud never dials out; only the lab server runs the worker.
    if not base_url or getattr(settings, 'CLOUD_SYNC_ROLE', 'edge') != 'edge':
        return

    def _loop():
        from django.db import connections
        time.sleep(10)
        while True:
            try:
                if _cloud_reachable(base_url, timeout_seconds):
                    _pull_and_judge(base_url, api_key, timeout_seconds)
                    sent = 1
                    while sent > 0:
                        sent = _push_batch(base_url, api_key, timeout_seconds)
            except Exception:
                log.exception('cloud sync tick failed')
            finally:
                connections.close_all()
            time.sleep(interval)

    threading.Thread(target=_loop, name='cloud-sync-worker', daemon=True).start()
