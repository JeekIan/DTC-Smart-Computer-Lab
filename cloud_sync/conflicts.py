"""
Conflict prevention between the lab server ("edge") and the cloud server.

THE RULE
--------
Only the EDGE can make a booking final. A booking made on the cloud is just
a *proposal* (state='provisional') until the edge accepts it:

  1. The edge pulls the cloud's provisional bookings (edge always dials out,
     so no inbound port is needed at the school).
  2. For each one the edge runs the SAME slot check the web app already uses
     (scheduling.views._slot_error) against its own schedule, with the lab
     row locked so two accepts can't interleave.
  3. Free      -> the edge creates the Session (same reservation code) and
                  answers "accepted".
     Conflict  -> the edge creates nothing and answers "rejected" + reason.
  4. The cloud applies the answer: accepted -> confirmed; rejected -> the
     provisional Session is deleted, its request is declined.

If the edge approves something itself (even while the cloud was unreachable)
and it is later pushed to the cloud, it wins over any overlapping provisional
booking there. A confirmed booking is never removed by sync, so two
overlapping bookings can never both end up accepted.

A last safety net (replay_admits) re-checks right after a commit, in case two
accepts raced: bookings are replayed in a fixed order (confirmed first, then
lowest id) and the one that no longer fits removes itself.
"""
import datetime
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from . import deferral
from .models import SyncConflictLog, SyncedBooking, SyncedRequest

log = logging.getLogger(__name__)


def sync_role():
    return getattr(settings, 'CLOUD_SYNC_ROLE', 'edge')


# --------------------------------------------------------------------------
# Snapshots (Session -> JSON)
# --------------------------------------------------------------------------
def _request_uuid_for(session):
    from scheduling.models import SessionRequest
    if not session.reservation_code:
        return None
    req = SessionRequest.objects.filter(reservation_code=session.reservation_code).select_related('sync_request').first()
    sr = getattr(req, 'sync_request', None) if req else None
    return str(sr.sync_uuid) if sr else None


def session_snapshot(session, booking):
    roster = session.roster
    return {
        'request_uuid': _request_uuid_for(session),
        'sync_uuid': str(booking.sync_uuid),
        'origin': booking.origin,
        'lab_name': session.lab.name,
        'instructor_email': session.instructor.email if session.instructor_id else None,
        'roster': (
            {'name': roster.name, 'course_code': roster.course_code, 'section': roster.section}
            if roster else None
        ),
        'requester_type': session.requester_type,
        'requester_name': session.requester_name,
        'requester_id_number': session.requester_id_number,
        'reservation_code': session.reservation_code,
        'subject': session.subject,
        'date': session.date.isoformat(),
        'start_time': session.start_time.strftime('%H:%M:%S'),
        'end_time': session.end_time.strftime('%H:%M:%S'),
        'student_count': session.student_count,
        'pcs_requested': session.pcs_requested,
        'created_at': session.created_at.isoformat() if session.created_at else None,
    }


def _times(snap):
    return (
        datetime.date.fromisoformat(snap['date']),
        datetime.time.fromisoformat(snap['start_time']),
        datetime.time.fromisoformat(snap['end_time']),
    )


def _lookup_lab(name):
    from labs.models import Lab
    return Lab.objects.filter(name=name).first()


def _lookup_roster(snap):
    from scheduling.models import ClassRoster
    r = snap.get('roster')
    if not r:
        return None
    return ClassRoster.objects.filter(
        name=r['name'], course_code=r['course_code'], section=r['section'],
    ).first()


def _lookup_instructor(snap):
    email = snap.get('instructor_email')
    if not email:
        return None
    return get_user_model().objects.filter(email__iexact=email).first()


def _notify(lab, message):
    try:
        from notifications import services as notify_service
        notify_service.notify_reservation_conflict(lab, message)
    except Exception:  # a notification problem must never break syncing
        log.exception('could not send sync conflict notification')


# --------------------------------------------------------------------------
# Deterministic re-check (the race safety net)
# --------------------------------------------------------------------------
def replay_admits(session):
    """Would `session` still be admitted if every overlapping booking were
    replayed in priority order (confirmed first, then oldest id)? Uses the
    same rules as the web app: whole-lab bookings are exclusive; walk-in/
    override bookings share the lab up to its PC count."""
    from scheduling.models import Session
    from scheduling.utils import CAPACITY_BASED_TYPES

    total = session.lab.pcs.count()
    overlapping = list(
        Session.objects.filter(
            lab=session.lab, date=session.date,
            start_time__lt=session.end_time, end_time__gt=session.start_time,
        ).select_related('sync_booking')
    )

    def rank(s):
        booking = getattr(s, 'sync_booking', None)
        provisional = bool(booking and booking.state == 'provisional')
        return (provisional, s.pk)

    admitted_exclusive = False
    admitted_any = False
    used = 0
    for s in sorted(overlapping, key=rank):
        if s.requester_type in CAPACITY_BASED_TYPES:
            ok = (not admitted_exclusive) and used + s.pcs_requested <= total
            if ok:
                used += s.pcs_requested
        else:
            ok = not admitted_any
            if ok:
                admitted_exclusive = True
        if ok:
            admitted_any = True
        if s.pk == session.pk:
            return ok
    return True


# --------------------------------------------------------------------------
# Booking REQUESTS (Instructors submit them; Admin/In-Charge decide them)
# --------------------------------------------------------------------------
def request_snapshot(req, sr):
    roster = req.roster
    return {
        'sync_uuid': str(sr.sync_uuid), 'origin': sr.origin,
        'lab_name': req.lab.name,
        'instructor_email': req.instructor.email if req.instructor_id else None,
        'roster': ({'name': roster.name, 'course_code': roster.course_code, 'section': roster.section}
                   if roster else None),
        'requester_type': req.requester_type, 'requester_name': req.requester_name,
        'requester_id_number': req.requester_id_number, 'subject': req.subject,
        'date': req.date.isoformat(), 'start_time': req.start_time.strftime('%H:%M:%S'),
        'end_time': req.end_time.strftime('%H:%M:%S'), 'student_count': req.student_count,
        'pcs_requested': req.pcs_requested, 'notes': req.notes, 'status': req.status,
        'reservation_code': req.reservation_code,
        'updated_at': sr.updated_at.isoformat() if sr.updated_at else None,
    }


def _request_fields(snap, lab):
    date, start, end = _times(snap)
    return dict(
        lab=lab, instructor=_lookup_instructor(snap), roster=_lookup_roster(snap),
        requester_type=snap['requester_type'], requester_name=snap['requester_name'],
        requester_id_number=snap['requester_id_number'], subject=snap['subject'],
        date=date, start_time=start, end_time=end, student_count=snap.get('student_count', 0),
        pcs_requested=snap.get('pcs_requested', 0), notes=snap.get('notes', ''),
    )


def _mirror_request(request_uuid):
    if not request_uuid:
        return None
    sr = SyncedRequest.objects.filter(sync_uuid=request_uuid).select_related('request').first()
    return sr.request if sr else None


def _save_quiet(obj):
    obj._sync_skip = True  # applying a remote change must not echo it back
    obj.save()


def apply_cloud_request(snap):
    """Edge only. A request that changed on the cloud. Once the lab server has
    decided a request, the lab server's decision is never overwritten."""
    from scheduling.models import SessionRequest

    lab = _lookup_lab(snap['lab_name'])
    if lab is None:
        raise ValueError(f'Laboratory "{snap["lab_name"]}" does not exist on the lab server.')
    sr = SyncedRequest.objects.filter(sync_uuid=snap['sync_uuid']).select_related('request').first()
    if sr is None:
        if snap.get('origin') != 'cloud':
            return  # an edge-made request we no longer have
        req = SessionRequest(status='declined' if snap['status'] == 'declined' else 'pending',
                             **_request_fields(snap, lab))
        _save_quiet(req)
        SyncedRequest.objects.update_or_create(
            sync_uuid=snap['sync_uuid'], defaults={'request': req, 'origin': snap['origin']})
        return
    req = sr.request
    if req is None or req.status != 'pending':
        return  # deleted, or already decided here: the lab server wins
    for k, v in _request_fields(snap, lab).items():
        setattr(req, k, v)
    if snap['status'] == 'declined':
        req.status = 'declined'
    _save_quiet(req)


def apply_edge_request(snap):
    """Cloud only. A request (or its decision) from the lab server. The lab
    server's decision always wins over anything decided here."""
    from scheduling.models import Session, SessionRequest

    lab = _lookup_lab(snap['lab_name'])
    if lab is None:
        raise ValueError(f'Laboratory "{snap["lab_name"]}" does not exist on the cloud.')
    with transaction.atomic():
        sr = SyncedRequest.objects.filter(sync_uuid=snap['sync_uuid']).select_related('request').first()
        if sr is not None and sr.request is None:
            return  # deleted on the cloud
        if snap.get('reservation_code'):
            _release_code(snap['reservation_code'], keep_request=sr.request if sr else None)
        if sr is None:
            req = SessionRequest(status=snap['status'], reservation_code=snap.get('reservation_code'),
                                 **_request_fields(snap, lab))
            _save_quiet(req)
            SyncedRequest.objects.create(sync_uuid=snap['sync_uuid'], request=req, origin=snap['origin'])
            return
        req = sr.request
        # If the cloud had provisionally approved this request under a different
        # code, that provisional booking is replaced by the lab server's decision.
        old = req.reservation_code
        if old and old.lower() != (snap.get('reservation_code') or '').lower():
            stale = Session.objects.filter(reservation_code=old, sync_booking__state='provisional').first()
            if stale:
                reject_provisional(stale, 'This request was already decided at the laboratory.',
                                   kind='evicted', tell_requester=False)
                req.refresh_from_db()
        for k, v in _request_fields(snap, lab).items():
            setattr(req, k, v)
        req.status = snap['status']
        req.reservation_code = snap.get('reservation_code')
        _save_quiet(req)


# --------------------------------------------------------------------------
# Reservation codes keep the project's original format (6 characters). If the
# same code was ever issued on both servers, ONE side gives a fresh code to
# its not-yet-announced booking — codes are only emailed after confirmation.
# --------------------------------------------------------------------------
def _code_taken(code):
    from scheduling.models import Session, SessionRequest
    return (Session.objects.filter(reservation_code__iexact=code).exists()
            or SessionRequest.objects.filter(reservation_code__iexact=code).exists())


def _recode_session(session, new_code):
    """Cloud only: change a provisional booking's code everywhere it appears."""
    from scheduling.models import SessionRequest
    from .models import DeferredNotice
    old = session.reservation_code
    SessionRequest.objects.filter(reservation_code=old).update(reservation_code=new_code)
    DeferredNotice.objects.filter(booking__session=session).update(code=new_code)
    session.reservation_code = new_code
    _save_quiet(session)
    SyncConflictLog.objects.create(
        kind='warning', message=f'Reservation code {old} was re-issued as {new_code} (same code existed on both servers).',
        snapshot={'old': old, 'new': new_code})


def _release_code(code, keep_session=None, keep_request=None):
    """Cloud only. The lab server owns `code`. If a provisional cloud booking
    holds the same code, that one gets a fresh code instead."""
    from scheduling.models import Session, SessionRequest
    from scheduling.utils import generate_reservation_code

    sessions = Session.objects.filter(reservation_code__iexact=code).select_related('sync_booking')
    if keep_session is not None and keep_session.pk:
        sessions = sessions.exclude(pk=keep_session.pk)
    for s in sessions:
        booking = getattr(s, 'sync_booking', None)
        if booking is None or booking.state != 'provisional':
            raise ValueError(f'Reservation code {code} is already used by a confirmed booking on the cloud.')
        _recode_session(s, generate_reservation_code())
    holders = SessionRequest.objects.filter(reservation_code__iexact=code)
    if keep_request is not None and keep_request.pk:
        holders = holders.exclude(pk=keep_request.pk)
    for r in holders:
        r.reservation_code = generate_reservation_code()
        _save_quiet(r)


# --------------------------------------------------------------------------
# EDGE side: judge a proposal from the cloud
# --------------------------------------------------------------------------
def _verdict(snap, accepted, reason='', warnings=None, student_count=None, code=None):
    return {
        'reservation_code': code,
        'sync_uuid': snap['sync_uuid'],
        'accepted': accepted,
        'reason': reason,
        'warnings': warnings or [],
        'student_count': student_count,
    }


def _reject_incoming(snap, reason):
    SyncedBooking.objects.update_or_create(
        sync_uuid=snap['sync_uuid'],
        defaults={'origin': 'cloud', 'state': 'rejected', 'reject_reason': reason},
    )
    SyncConflictLog.objects.create(
        sync_uuid=snap['sync_uuid'], kind='rejected', message=reason, snapshot=snap,
    )
    mirror = _mirror_request(snap.get('request_uuid'))
    if mirror and mirror.status == 'pending':
        mirror.status = 'declined'
        mirror.notes = (mirror.notes + '\n' if mirror.notes else '') + f'[Auto-declined] {reason}'
        _save_quiet(mirror)
    lab = _lookup_lab(snap.get('lab_name', ''))
    if lab:
        _notify(lab, (
            f'Online booking "{snap.get("subject", "")}" ({snap.get("date")} '
            f'{snap.get("start_time", "")[:5]}–{snap.get("end_time", "")[:5]}) was rejected: {reason}'
        ))
    return _verdict(snap, False, reason)


def evaluate_proposal(snap):
    """Edge only. Accept or reject one provisional booking from the cloud.
    Idempotent: re-sending the same proposal returns the same answer."""
    from labs.models import Lab
    from scheduling.models import Session
    from scheduling.utils import cap_student_count, CAPACITY_BASED_TYPES
    from scheduling.views import _slot_error

    existing = SyncedBooking.objects.filter(sync_uuid=snap['sync_uuid']).first()
    if existing and existing.state in ('confirmed', 'rejected', 'cancelled'):
        return _verdict(snap, existing.state == 'confirmed', existing.reject_reason,
                        code=existing.session.reservation_code if existing.session else None)

    lab = _lookup_lab(snap['lab_name'])
    if lab is None:
        return _reject_incoming(snap, f'Laboratory "{snap["lab_name"]}" does not exist on the lab server.')

    # One request, one decision: if the lab server already decided it, the cloud cannot override.
    mirror = _mirror_request(snap.get('request_uuid'))
    code = (snap.get('reservation_code') or '').strip()
    if mirror and mirror.status == 'declined':
        return _reject_incoming(snap, 'This request was already declined at the laboratory.')
    if mirror and mirror.status == 'approved' and (mirror.reservation_code or '').lower() != code.lower():
        return _reject_incoming(snap, 'This request was already approved at the laboratory.')

    date, start, end = _times(snap)
    warnings = []

    with transaction.atomic():
        # Serialise every accept for this lab (a real row lock on MySQL).
        Lab.objects.select_for_update().get(pk=lab.pk)

        error = _slot_error(
            snap['requester_type'], lab, date, start, end, snap.get('pcs_requested', 0),
            student_count=snap.get('student_count', 0),
        )
        if error is None and code and _code_taken(code):
            from scheduling.utils import generate_reservation_code
            warnings.append(f'Reservation code {code} already existed here; re-issued as a new code.')
            code = generate_reservation_code()  # same 6-character format as always
        if error:
            reason = error
        else:
            reason = ''
            student_count = snap.get('student_count', 0)
            if snap['requester_type'] not in CAPACITY_BASED_TYPES:
                student_count, _note = cap_student_count(lab, student_count)

            roster = _lookup_roster(snap)
            if snap.get('roster') and roster is None:
                warnings.append(
                    'Class roster not found on the lab server — only the requester can use '
                    'this code until the roster is imported there.'
                )
            session = Session(
                lab=lab, instructor=_lookup_instructor(snap), roster=roster,
                requester_type=snap['requester_type'], requester_name=snap['requester_name'],
                requester_id_number=snap['requester_id_number'],
                reservation_code=code or None, subject=snap['subject'],
                date=date, start_time=start, end_time=end,
                student_count=student_count, pcs_requested=snap.get('pcs_requested', 0),
            )
            session._sync_skip = True  # do not echo this back as a new booking
            session.save()
            SyncedBooking.objects.update_or_create(
                sync_uuid=snap['sync_uuid'],
                defaults={'session': session, 'origin': 'cloud', 'state': 'confirmed', 'reject_reason': ''},
            )

    if reason:
        return _reject_incoming(snap, reason)

    # Race safety net: if another booking was committed at the same moment,
    # the replay decides deterministically which one stays.
    if not replay_admits(session):
        reason = 'Another booking took this time slot at the same moment.'
        session._sync_skip = True
        session.delete()
        return _reject_incoming(snap, reason)

    if mirror and mirror.status == 'pending':
        mirror.status = 'approved'
        mirror.reservation_code = session.reservation_code
        mirror.student_count = session.student_count
        _save_quiet(mirror)
    for w in warnings:
        SyncConflictLog.objects.create(sync_uuid=snap['sync_uuid'], kind='warning', message=w, snapshot=snap)
    return _verdict(snap, True, warnings=warnings, student_count=session.student_count,
                    code=session.reservation_code)


# --------------------------------------------------------------------------
# CLOUD side: apply what the edge says
# --------------------------------------------------------------------------
def reject_provisional(session, reason, kind='rejected', tell_requester=True):
    """Remove a losing provisional booking and decline its request."""
    from scheduling.models import SessionRequest

    booking = getattr(session, 'sync_booking', None)
    snap = session_snapshot(session, booking) if booking else {}
    declined = []
    if session.reservation_code:
        for req in SessionRequest.objects.filter(reservation_code=session.reservation_code):
            req.status = 'declined'
            req.notes = (req.notes + '\n' if req.notes else '') + f'[Auto-declined] {reason}'
            req.reservation_code = None
            req.save()
            declined.append(req)
    if booking:
        deferral.discard(booking)  # the held-back "approved + code" notices never go out
        booking.state = 'rejected'
        booking.reject_reason = reason
        booking.save()
        SyncConflictLog.objects.create(sync_uuid=booking.sync_uuid, kind=kind, message=reason, snapshot=snap)
    lab = session.lab
    subject, when = session.subject, f'{session.date} {session.start_time.strftime("%H:%M")}'
    session._sync_skip = True
    session.delete()
    for req in (declined if tell_requester else []):  # tell the requester it did NOT go through
        try:
            from notifications import services as notify_service
            notify_service.notify_reservation_declined(req)
        except Exception:
            log.exception('could not send decline notification')
    _notify(lab, f'Booking "{subject}" ({when}) in {lab.name} was cancelled: {reason}')


def apply_verdict(verdict):
    """Cloud only. The edge's answer for one provisional booking."""
    booking = SyncedBooking.objects.filter(sync_uuid=verdict['sync_uuid']).select_related('session').first()
    if booking is None:
        return
    if verdict['accepted']:
        if booking.state == 'rejected':
            SyncConflictLog.objects.create(
                sync_uuid=booking.sync_uuid, kind='anomaly', snapshot=verdict,
                message='Lab server accepted a booking the cloud had already removed. Review manually.',
            )
            return
        new_code = verdict.get('reservation_code')
        if booking.session and new_code and new_code != booking.session.reservation_code:
            _recode_session(booking.session, new_code)
        booking.state = 'confirmed'
        booking.save()
        if booking.session and verdict.get('student_count') is not None:
            booking.session.student_count = verdict['student_count']
            booking.session._sync_skip = True
            booking.session.save(update_fields=['student_count'])
        deferral.flush(booking)  # NOW the reservation code can be sent out
        return
    if booking.state == 'provisional' and booking.session:
        reject_provisional(booking.session, verdict.get('reason') or 'Rejected by the lab server.')
    elif booking.state == 'provisional':
        booking.state = 'rejected'
        booking.reject_reason = verdict.get('reason', '')
        booking.save()


def apply_edge_session(snap):
    """Cloud only. A booking the edge holds as confirmed. The edge always
    wins: overlapping provisional bookings here are removed."""
    from scheduling.models import Session

    lab = _lookup_lab(snap['lab_name'])
    if lab is None:
        raise ValueError(f'Laboratory "{snap["lab_name"]}" does not exist on the cloud.')
    date, start, end = _times(snap)

    with transaction.atomic():
        booking = SyncedBooking.objects.filter(sync_uuid=snap['sync_uuid']).select_related('session').first()
        session = booking.session if booking and booking.session else Session()
        if booking and booking.state == 'rejected':
            SyncConflictLog.objects.create(
                sync_uuid=booking.sync_uuid, kind='anomaly', snapshot=snap,
                message='Lab server holds a booking the cloud had rejected; restored (lab server wins).',
            )
        if snap.get('reservation_code'):
            _release_code(snap['reservation_code'], keep_session=session)
        session.lab = lab
        session.instructor = _lookup_instructor(snap)
        session.roster = _lookup_roster(snap)
        session.requester_type = snap['requester_type']
        session.requester_name = snap['requester_name']
        session.requester_id_number = snap['requester_id_number']
        session.reservation_code = snap.get('reservation_code') or None
        session.subject = snap['subject']
        session.date, session.start_time, session.end_time = date, start, end
        session.student_count = snap.get('student_count', 0)
        session.pcs_requested = snap.get('pcs_requested', 0)
        session._sync_skip = True
        session.save()
        booking, _ = SyncedBooking.objects.update_or_create(
            sync_uuid=snap['sync_uuid'],
            defaults={'session': session, 'origin': snap.get('origin', 'edge'),
                      'state': 'confirmed', 'reject_reason': ''},
        )

        # Edge wins: evict any provisional booking that no longer fits.
        rivals = Session.objects.filter(
            lab=lab, date=date, start_time__lt=end, end_time__gt=start,
            sync_booking__state='provisional',
        ).exclude(pk=session.pk).select_related('sync_booking').order_by('pk')
        for rival in rivals:
            if not replay_admits(rival):
                reject_provisional(
                    rival,
                    'This time slot was already booked at the laboratory. '
                    'Please choose another time.',
                    kind='evicted',
                )


def apply_edge_session_deleted(payload):
    """Cloud only. The edge cancelled/removed a booking."""
    booking = SyncedBooking.objects.filter(sync_uuid=payload['sync_uuid']).select_related('session').first()
    if booking is None:
        return
    session = booking.session
    booking.state = 'cancelled'
    booking.save()
    if session:
        session._sync_skip = True
        session.delete()


def apply_checkin(payload):
    """Cloud only. A check-in recorded at the lab (possibly while offline)."""
    from labs.models import PC
    from scheduling.models import SessionCheckIn
    from django.utils.dateparse import parse_datetime

    booking = SyncedBooking.objects.filter(sync_uuid=payload.get('session_uuid')).select_related('session').first()
    if booking is None or booking.session is None:
        raise ValueError('Session for this check-in has not reached the cloud yet.')
    session = booking.session
    pc = PC.objects.filter(lab=session.lab, pc_id=payload.get('pc_id')).first() if payload.get('pc_id') else None
    checkin, _ = SessionCheckIn.objects.update_or_create(
        session=session, id_number=payload['id_number'],
        defaults={
            'checkin_type': payload.get('checkin_type', 'requester'),
            'guest_name': payload.get('guest_name', ''),
            'pc': pc,
            'checked_out_at': parse_datetime(payload['checked_out_at']) if payload.get('checked_out_at') else None,
        },
    )
    if payload.get('checked_in_at'):  # auto_now_add: set the REAL time explicitly
        SessionCheckIn.objects.filter(pk=checkin.pk).update(checked_in_at=parse_datetime(payload['checked_in_at']))
