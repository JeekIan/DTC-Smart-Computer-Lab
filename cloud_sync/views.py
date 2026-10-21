"""
Cloud-side API. The lab server (edge) always dials OUT to these, so the
school needs no open inbound port.

  GET  /api/ping/        reachability probe (no auth)
  GET  /api/sync/pull/   provisional bookings waiting for the lab server's verdict
  POST /api/sync/        edge -> cloud batch: confirmed sessions, verdicts, check-ins...

Active only when CLOUD_SYNC_ROLE == 'cloud'. Auth: 'Authorization: Bearer <CLOUD_SYNC_API_KEY>'.
"""
import datetime
import hmac
import json

from django.conf import settings
from django.db import transaction
from django.http import HttpResponseNotFound, JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from . import conflicts
from .models import ReceivedRecord, SyncedBooking, SyncedRequest


def _authorised(request):
    key = getattr(settings, 'CLOUD_SYNC_API_KEY', '')
    header = request.META.get('HTTP_AUTHORIZATION', '')
    return bool(key) and header.startswith('Bearer ') and hmac.compare_digest(header[7:], key)


@csrf_exempt
def ping(request):
    return JsonResponse({'ok': True, 'role': conflicts.sync_role()})


@csrf_exempt
def pull(request):
    if conflicts.sync_role() != 'cloud':
        return HttpResponseNotFound()
    if not _authorised(request):
        return JsonResponse({'error': 'unauthorised'}, status=403)
    bookings = (
        SyncedBooking.objects.filter(state='provisional', session__isnull=False)
        .select_related('session', 'session__lab', 'session__roster', 'session__instructor')
        .order_by('created_at')[:100]
    )
    since = parse_datetime(request.GET.get('since', '')) or datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    changed = (
        SyncedRequest.objects.filter(updated_at__gt=since, request__isnull=False)
        .select_related('request', 'request__lab', 'request__roster', 'request__instructor')
        .order_by('updated_at')[:100]
    )
    return JsonResponse({
        'requests': [conflicts.request_snapshot(r.request, r) for r in changed],
        'proposals': [conflicts.session_snapshot(b.session, b) for b in bookings],
    })


_HANDLERS = {
    'session': conflicts.apply_edge_session,
    'request': conflicts.apply_edge_request,
    'session_deleted': conflicts.apply_edge_session_deleted,
    'verdict': conflicts.apply_verdict,
    'checkin': conflicts.apply_checkin,
}


@csrf_exempt
def push(request):
    if conflicts.sync_role() != 'cloud':
        return HttpResponseNotFound()
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    if not _authorised(request):
        return JsonResponse({'error': 'unauthorised'}, status=403)
    try:
        records = json.loads(request.body.decode('utf-8')).get('records', [])
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'bad json'}, status=400)

    accepted, errors = [], {}
    for rec in records:
        edge_id = rec.get('edge_id')
        try:
            if ReceivedRecord.objects.filter(edge_id=edge_id).exists():
                accepted.append(edge_id)  # already applied — idempotent
                continue
            with transaction.atomic():
                handler = _HANDLERS.get(rec.get('object_type'))
                if handler:
                    handler(rec['payload'])
                ReceivedRecord.objects.create(
                    edge_id=edge_id, object_type=rec.get('object_type', ''), payload=rec.get('payload', {}),
                )
            accepted.append(edge_id)
        except Exception as exc:  # not accepted -> the edge retries it next tick
            errors[str(edge_id)] = str(exc)[:300]
    return JsonResponse({'accepted_edge_ids': accepted, 'errors': errors})
