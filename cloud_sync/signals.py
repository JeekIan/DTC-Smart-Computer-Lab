"""Hooks that feed the sync outbox without touching existing views."""
from django.db import transaction
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver

from scheduling.models import Session, SessionCheckIn, SessionRequest

from .conflicts import request_snapshot, session_snapshot, sync_role
from .models import SyncedBooking, SyncedRequest, enqueue


@receiver(post_save, sender=Session)
def session_saved(sender, instance, created, **kwargs):
    if getattr(instance, '_sync_skip', False):
        return
    if sync_role() == 'cloud':
        # Anything booked on the cloud is only a PROPOSAL until the lab
        # server accepts it. Edits of an already-decided booking are not
        # propagated from the cloud (the lab server is the authority).
        if created:
            SyncedBooking.objects.get_or_create(
                session=instance, defaults={'origin': 'cloud', 'state': 'provisional'},
            )
        return
    booking, _ = SyncedBooking.objects.get_or_create(
        session=instance, defaults={'origin': 'edge', 'state': 'confirmed'},
    )
    enqueue('session', instance.pk, session_snapshot(instance, booking))


@receiver(pre_delete, sender=Session)
def session_deleting(sender, instance, **kwargs):
    if getattr(instance, '_sync_skip', False) or sync_role() == 'cloud':
        return
    booking = SyncedBooking.objects.filter(session=instance).first()
    if booking is None:
        return
    uuid_str, pk = str(booking.sync_uuid), instance.pk
    booking.state = 'cancelled'
    booking.save()
    transaction.on_commit(lambda: enqueue('session_deleted', pk, {'sync_uuid': uuid_str}))


@receiver(post_save, sender=SessionCheckIn)
def checkin_saved(sender, instance, created, **kwargs):
    if sync_role() == 'cloud' or instance.session_id is None:
        return
    booking, _ = SyncedBooking.objects.get_or_create(
        session=instance.session, defaults={'origin': 'edge', 'state': 'confirmed'},
    )
    enqueue('checkin', instance.pk, {
        'session_uuid': str(booking.sync_uuid),
        'id_number': instance.id_number,
        'checkin_type': instance.checkin_type,
        'guest_name': instance.guest_name,
        'pc_id': instance.pc.pc_id if instance.pc_id else None,
        'checked_in_at': instance.checked_in_at.isoformat() if instance.checked_in_at else None,
        'checked_out_at': instance.checked_out_at.isoformat() if instance.checked_out_at else None,
    })


@receiver(post_save, sender=SessionRequest)
def request_saved(sender, instance, created, **kwargs):
    if getattr(instance, '_sync_skip', False):
        return
    sr, made = SyncedRequest.objects.get_or_create(request=instance, defaults={'origin': sync_role()})
    if not made:
        sr.save()  # moves updated_at: "this request changed on this server"
    if sync_role() == 'edge':
        enqueue('request', instance.pk, request_snapshot(instance, sr))
