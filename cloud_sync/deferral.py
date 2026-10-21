"""
Holds back reservation-code notifications while a booking is provisional.

Staff approve bookings on the cloud, but students only ever *receive*
notifications. If the code email went out immediately and the lab server then
rejected the booking, the student would be holding a code that does not work.
So on the cloud, notify_reservation_approved / notify_roster_session_scheduled
are deferred until the lab server answers (see conflicts.apply_verdict).
"""
import functools
import logging

from django.conf import settings

from .models import DeferredNotice, SyncedBooking

log = logging.getLogger(__name__)


def _is_cloud():
    return getattr(settings, 'CLOUD_SYNC_ROLE', 'edge') == 'cloud'


def defer_until_confirmed(func):
    """Decorator for notify_*(obj, code) functions in notifications.services."""
    @functools.wraps(func)
    def wrapper(obj, code, *args, **kwargs):
        if _is_cloud() and maybe_defer(func.__name__, obj, code):
            return None
        return func(obj, code, *args, **kwargs)
    return wrapper


def maybe_defer(func_name, obj, code):
    booking = SyncedBooking.objects.filter(
        state='provisional', session__reservation_code=code,
    ).first()
    if booking is None:
        return False
    DeferredNotice.objects.create(
        booking=booking, func_name=func_name,
        target_model='request' if obj.__class__.__name__ == 'SessionRequest' else 'session',
        target_id=obj.pk, code=code,
    )
    return True


def flush(booking):
    """The booking is now confirmed: send everything that was held back."""
    from notifications import services
    from scheduling.models import Session, SessionRequest

    for notice in list(booking.deferred_notices.order_by('created_at')):
        try:
            model = SessionRequest if notice.target_model == 'request' else Session
            obj = model.objects.filter(pk=notice.target_id).first()
            session = booking.session
            still_relevant = obj is not None and (session is None or not _already_over(session))
            if still_relevant:
                getattr(services, notice.func_name)(obj, notice.code)
        except Exception:
            log.exception('could not send deferred notification %s', notice.pk)
        notice.delete()


def discard(booking):
    """The booking was rejected: the held-back code notices must never go out."""
    booking.deferred_notices.all().delete()


def _already_over(session):
    """No point emailing a code for a session that has already ended."""
    import datetime
    from django.utils import timezone
    end = datetime.datetime.combine(session.date, session.end_time)
    if settings.USE_TZ:
        end = timezone.make_aware(end)
    return timezone.now() > end
