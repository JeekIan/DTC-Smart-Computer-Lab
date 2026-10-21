import datetime
import json
import uuid

from django.test import TestCase, override_settings

from labs.models import Lab, PC
from scheduling.models import Session, SessionCheckIn, SessionRequest

from . import conflicts
from .models import SyncConflictLog, SyncedBooking, SyncQueue

D = datetime.date(2026, 11, 2)


def t(h, m=0):
    return datetime.time(h, m)


def snap(code, start, end, rtype='instructor', pcs=0, students=10, lab='Lab 01'):
    return {
        'sync_uuid': str(uuid.uuid4()), 'origin': 'cloud', 'lab_name': lab,
        'instructor_email': None, 'roster': None, 'requester_type': rtype,
        'requester_name': 'Juan', 'requester_id_number': '2023-1', 'reservation_code': code,
        'subject': 'Prog 1', 'date': D.isoformat(), 'start_time': start, 'end_time': end,
        'student_count': students, 'pcs_requested': pcs, 'created_at': None,
    }


class Base(TestCase):
    def setUp(self):
        self.lab = Lab.objects.create(name='Lab 01')
        for i in range(2):
            PC.objects.create(lab=self.lab, pc_id=f'PC0{i + 1}')

    def make_session(self, start, end, code, rtype='instructor', pcs=0):
        return Session.objects.create(
            lab=self.lab, requester_type=rtype, requester_name='X', requester_id_number='1',
            reservation_code=code, subject='S', date=D, start_time=start, end_time=end, pcs_requested=pcs,
        )


class EdgeTests(Base):
    def test_free_slot_is_accepted_with_same_code(self):
        v = conflicts.evaluate_proposal(snap('CABC123', '09:00:00', '11:00:00'))
        self.assertTrue(v['accepted'])
        s = Session.objects.get(reservation_code='CABC123')
        self.assertEqual(s.sync_booking.state, 'confirmed')
        # accepting a cloud booking must not echo it back as a new edge booking
        self.assertFalse(SyncQueue.objects.filter(object_type='session').exists())

    def test_overlap_with_edge_booking_is_rejected(self):
        self.make_session(t(9), t(11), 'EDGE01')
        v = conflicts.evaluate_proposal(snap('CABC123', '10:00:00', '12:00:00'))
        self.assertFalse(v['accepted'])
        self.assertIn('conflict', v['reason'].lower())
        self.assertFalse(Session.objects.filter(reservation_code='CABC123').exists())
        self.assertEqual(SyncConflictLog.objects.filter(kind='rejected').count(), 1)

    def test_same_proposal_twice_gives_same_answer(self):
        s = snap('CABC123', '09:00:00', '11:00:00')
        self.assertTrue(conflicts.evaluate_proposal(s)['accepted'])
        self.assertTrue(conflicts.evaluate_proposal(s)['accepted'])
        self.assertEqual(Session.objects.count(), 1)

    def test_two_overlapping_proposals_only_first_accepted(self):
        a, b = snap('CAAA111', '09:00:00', '11:00:00'), snap('CBBB222', '10:00:00', '12:00:00')
        self.assertTrue(conflicts.evaluate_proposal(a)['accepted'])
        self.assertFalse(conflicts.evaluate_proposal(b)['accepted'])
        self.assertEqual(Session.objects.count(), 1)

    def test_back_to_back_is_not_a_conflict(self):
        self.assertTrue(conflicts.evaluate_proposal(snap('CAAA111', '09:00:00', '10:00:00'))['accepted'])
        self.assertTrue(conflicts.evaluate_proposal(snap('CBBB222', '10:00:00', '11:00:00'))['accepted'])

    def test_walk_ins_share_the_lab_until_pcs_run_out(self):
        r = [conflicts.evaluate_proposal(snap(f'CW{i}', '09:00:00', '10:00:00', 'walk_in', pcs=1))['accepted']
             for i in range(3)]
        self.assertEqual(r, [True, True, False])  # lab has 2 PCs

    def test_exclusive_booking_blocks_walk_in_proposal(self):
        self.make_session(t(9), t(11), 'EDGE01')
        v = conflicts.evaluate_proposal(snap('CW1', '09:30:00', '10:00:00', 'walk_in', pcs=1))
        self.assertFalse(v['accepted'])

    def test_unknown_lab_is_rejected(self):
        self.assertFalse(conflicts.evaluate_proposal(snap('CX', '09:00:00', '10:00:00', lab='Nope'))['accepted'])

    def test_outside_opening_hours_is_rejected(self):
        self.assertFalse(conflicts.evaluate_proposal(snap('CX', '05:00:00', '06:00:00'))['accepted'])

    def test_replay_catches_a_race(self):
        """Simulate two accepts that both committed: the later one must not stand."""
        first = self.make_session(t(9), t(11), 'R1')
        second = self.make_session(t(10), t(12), 'R2')
        self.assertTrue(conflicts.replay_admits(first))
        self.assertFalse(conflicts.replay_admits(second))

    def test_edge_signals_queue_session_delete_and_checkin(self):
        s = self.make_session(t(9), t(11), 'EDGE01')
        self.assertEqual(SyncQueue.objects.filter(object_type='session').count(), 1)
        SessionCheckIn.objects.create(session=s, id_number='2023-1')
        self.assertEqual(SyncQueue.objects.filter(object_type='checkin').count(), 1)
        with self.captureOnCommitCallbacks(execute=True):
            s.delete()
        self.assertEqual(SyncQueue.objects.filter(object_type='session_deleted').count(), 1)


@override_settings(CLOUD_SYNC_ROLE='cloud', CLOUD_SYNC_API_KEY='k3y')
class CloudTests(Base):
    def make_proposal(self, start, end, code):
        s = self.make_session(t(*start), t(*end), code)
        SessionRequest.objects.create(
            lab=self.lab, subject='S', date=D, start_time=t(*start), end_time=t(*end),
            status='approved', reservation_code=code,
        )
        return s

    def test_cloud_booking_is_provisional_and_not_queued(self):
        s = self.make_proposal((9,), (11,), 'CAAA111')
        self.assertEqual(s.sync_booking.state, 'provisional')
        self.assertEqual(s.sync_booking.origin, 'cloud')
        self.assertFalse(SyncQueue.objects.exists())

    def test_rejected_verdict_removes_session_and_declines_request(self):
        s = self.make_proposal((9,), (11,), 'CAAA111')
        u = str(s.sync_booking.sync_uuid)
        conflicts.apply_verdict({'sync_uuid': u, 'accepted': False, 'reason': 'Time slot conflict'})
        self.assertFalse(Session.objects.exists())
        req = SessionRequest.objects.get()
        self.assertEqual(req.status, 'declined')
        self.assertIsNone(req.reservation_code)
        self.assertEqual(SyncedBooking.objects.get(sync_uuid=u).state, 'rejected')

    def test_accepted_verdict_confirms(self):
        s = self.make_proposal((9,), (11,), 'CAAA111')
        conflicts.apply_verdict({'sync_uuid': str(s.sync_booking.sync_uuid), 'accepted': True, 'student_count': 2})
        s.refresh_from_db()
        self.assertEqual(s.sync_booking.state, 'confirmed')
        self.assertEqual(s.student_count, 2)

    def test_edge_booking_evicts_overlapping_provisional(self):
        prov = self.make_proposal((10,), (12,), 'CAAA111')
        edge = snap('EDGE01', '09:00:00', '11:00:00')
        edge['origin'] = 'edge'
        conflicts.apply_edge_session(edge)
        self.assertFalse(Session.objects.filter(pk=prov.pk).exists())
        self.assertEqual(Session.objects.get().reservation_code, 'EDGE01')
        self.assertEqual(SessionRequest.objects.get().status, 'declined')
        self.assertEqual(SyncConflictLog.objects.filter(kind='evicted').count(), 1)

    def test_edge_booking_never_evicts_a_confirmed_booking(self):
        conf = self.make_proposal((10,), (12,), 'CAAA111')
        conflicts.apply_verdict({'sync_uuid': str(conf.sync_booking.sync_uuid), 'accepted': True})
        edge = snap('EDGE01', '09:00:00', '11:00:00')
        conflicts.apply_edge_session(edge)
        self.assertTrue(Session.objects.filter(pk=conf.pk).exists())

    def test_api_requires_key_pull_and_idempotent_push(self):
        prov = self.make_proposal((9,), (11,), 'CAAA111')
        self.assertEqual(self.client.get('/api/sync/pull/').status_code, 403)
        r = self.client.get('/api/sync/pull/', HTTP_AUTHORIZATION='Bearer k3y')
        self.assertEqual([p['reservation_code'] for p in r.json()['proposals']], ['CAAA111'])

        rec = {'edge_id': str(uuid.uuid4()), 'object_type': 'verdict', 'local_id': 0,
               'payload': {'sync_uuid': str(prov.sync_booking.sync_uuid), 'accepted': False, 'reason': 'x'}}
        for _ in range(2):  # re-sent batch (lost response) must be harmless
            r = self.client.post('/api/sync/', data=json.dumps({'records': [rec]}),
                                 content_type='application/json', HTTP_AUTHORIZATION='Bearer k3y')
            self.assertEqual(r.json()['accepted_edge_ids'], [rec['edge_id']])
        self.assertFalse(Session.objects.exists())
        self.assertEqual(self.client.post('/api/sync/', data='{}', content_type='application/json').status_code, 403)

    def test_checkin_applied_with_real_time(self):
        s = self.make_proposal((9,), (11,), 'CAAA111')
        conflicts.apply_checkin({'session_uuid': str(s.sync_booking.sync_uuid), 'id_number': '2023-1',
                                 'checkin_type': 'requester', 'pc_id': 'PC01',
                                 'checked_in_at': '2026-11-02T09:05:00+00:00'})
        c = SessionCheckIn.objects.get()
        self.assertEqual(c.pc.pc_id, 'PC01')
        self.assertEqual(c.checked_in_at.hour, 9)


@override_settings(CLOUD_SYNC_ROLE='edge')
class EdgeHasNoInboundApi(TestCase):
    def test_edge_does_not_expose_sync_endpoints(self):
        self.assertEqual(self.client.get('/api/sync/pull/').status_code, 404)


from django.contrib.auth import get_user_model
from django.core import mail

from notifications import services as notify_service
from .models import DeferredNotice


def _instructor():
    return get_user_model().objects.create_user(email='prof@gmail.com', password='x12345678!')


class NotificationDeferralTests(Base):
    """Students/instructors only RECEIVE notifications: the reservation code
    must never be sent for a booking the lab server has not confirmed."""

    def _provisional(self):
        prof = _instructor()
        s = self.make_session(t(9), t(11), 'CAAA111')
        req = SessionRequest.objects.create(
            lab=self.lab, subject='Prog 1', date=D, start_time=t(9), end_time=t(11),
            status='approved', reservation_code='CAAA111', instructor=prof,
        )
        return s, req

    @override_settings(CLOUD_SYNC_ROLE='cloud')
    def test_code_email_is_held_until_confirmed_then_sent_once(self):
        s, req = self._provisional()
        # the Session above was created on the cloud -> provisional
        self.assertEqual(s.sync_booking.state, 'provisional')
        notify_service.notify_reservation_approved(req, 'CAAA111')
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(DeferredNotice.objects.count(), 1)

        conflicts.apply_verdict({'sync_uuid': str(s.sync_booking.sync_uuid), 'accepted': True})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('CAAA111', mail.outbox[0].body)
        self.assertEqual(DeferredNotice.objects.count(), 0)
        # applying the same verdict again must not email twice
        conflicts.apply_verdict({'sync_uuid': str(s.sync_booking.sync_uuid), 'accepted': True})
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(CLOUD_SYNC_ROLE='cloud')
    def test_rejected_booking_never_sends_the_code_and_sends_decline(self):
        s, req = self._provisional()
        notify_service.notify_reservation_approved(req, 'CAAA111')
        conflicts.apply_verdict({'sync_uuid': str(s.sync_booking.sync_uuid), 'accepted': False, 'reason': 'Time slot conflict'})
        self.assertEqual(DeferredNotice.objects.count(), 0)
        self.assertFalse(any('CAAA111' in m.body for m in mail.outbox))
        from notifications.models import Notification
        self.assertTrue(Notification.objects.filter(title='Lab request declined').exists())

    @override_settings(CLOUD_SYNC_ROLE='cloud')
    def test_evicted_booking_also_sends_decline_not_code(self):
        s, req = self._provisional()
        notify_service.notify_reservation_approved(req, 'CAAA111')
        edge = snap('EDGE01', '08:00:00', '10:00:00')
        conflicts.apply_edge_session(edge)
        self.assertFalse(any('CAAA111' in m.body for m in mail.outbox))
        self.assertEqual(DeferredNotice.objects.count(), 0)

    def test_on_the_lab_server_the_code_is_sent_immediately(self):
        prof = _instructor()
        self.make_session(t(9), t(11), 'EDGE01')  # edge booking: final at once
        req = SessionRequest.objects.create(
            lab=self.lab, subject='Prog 1', date=D, start_time=t(9), end_time=t(11),
            status='approved', reservation_code='EDGE01', instructor=prof,
        )
        notify_service.notify_reservation_approved(req, 'EDGE01')
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(CLOUD_SYNC_ROLE='cloud')
    def test_cloud_never_queues_for_push(self):
        self.make_session(t(9), t(11), 'CAAA111')
        self.assertFalse(SyncQueue.objects.exists())


# ===================== booking REQUEST sync =====================
from .models import SyncedRequest


def req_snap(status='pending', code=None, origin='cloud', start='09:00:00', end='11:00:00', uid=None):
    s = snap(code, start, end)
    s.update({'sync_uuid': uid or str(uuid.uuid4()), 'origin': origin, 'status': status,
              'reservation_code': code, 'notes': '', 'updated_at': '2026-11-01T08:00:00+00:00'})
    return s


class RequestSyncEdgeTests(Base):
    def test_cloud_request_appears_pending_on_the_lab_server(self):
        conflicts.apply_cloud_request(req_snap())
        r = SessionRequest.objects.get()
        self.assertEqual(r.status, 'pending')
        self.assertEqual(r.sync_request.origin, 'cloud')
        self.assertFalse(SyncQueue.objects.filter(object_type='request').exists())  # no echo

    def test_cloud_decline_applies_only_while_pending_here(self):
        s = req_snap()
        conflicts.apply_cloud_request(s)
        conflicts.apply_cloud_request({**s, 'status': 'declined'})
        self.assertEqual(SessionRequest.objects.get().status, 'declined')

    def test_lab_server_decision_is_never_overwritten(self):
        s = req_snap()
        conflicts.apply_cloud_request(s)
        r = SessionRequest.objects.get()
        r.status, r.reservation_code = 'approved', 'EDGE01'
        r.save()
        conflicts.apply_cloud_request({**s, 'status': 'declined'})
        r.refresh_from_db()
        self.assertEqual((r.status, r.reservation_code), ('approved', 'EDGE01'))

    def test_edge_decision_is_queued_for_the_cloud(self):
        conflicts.apply_cloud_request(req_snap())
        r = SessionRequest.objects.get()
        r.status = 'approved'
        r.save()
        q = SyncQueue.objects.get(object_type='request')
        self.assertEqual(q.payload['status'], 'approved')

    def test_accepted_proposal_marks_the_request_approved(self):
        rs = req_snap()
        conflicts.apply_cloud_request(rs)
        prop = snap('CABC123', '09:00:00', '11:00:00')
        prop['request_uuid'] = rs['sync_uuid']
        self.assertTrue(conflicts.evaluate_proposal(prop)['accepted'])
        r = SessionRequest.objects.get()
        self.assertEqual((r.status, r.reservation_code), ('approved', 'CABC123'))

    def test_rejected_proposal_declines_the_request_here(self):
        self.make_session(t(9), t(11), 'EDGE01')
        rs = req_snap()
        conflicts.apply_cloud_request(rs)
        prop = snap('CABC123', '10:00:00', '12:00:00')
        prop['request_uuid'] = rs['sync_uuid']
        self.assertFalse(conflicts.evaluate_proposal(prop)['accepted'])
        self.assertEqual(SessionRequest.objects.get().status, 'declined')

    def test_cloud_cannot_approve_what_the_lab_server_already_declined(self):
        rs = req_snap()
        conflicts.apply_cloud_request(rs)
        r = SessionRequest.objects.get()
        r.status = 'declined'
        r.save()
        prop = snap('CABC123', '09:00:00', '11:00:00')
        prop['request_uuid'] = rs['sync_uuid']
        v = conflicts.evaluate_proposal(prop)
        self.assertFalse(v['accepted'])
        self.assertFalse(Session.objects.exists())

    def test_cloud_cannot_approve_a_request_the_lab_server_approved_with_another_code(self):
        rs = req_snap()
        conflicts.apply_cloud_request(rs)
        r = SessionRequest.objects.get()
        r.status, r.reservation_code = 'approved', 'EDGE01'
        r.save()
        prop = snap('CABC123', '12:00:00', '13:00:00')  # even a free slot: same request, two codes
        prop['request_uuid'] = rs['sync_uuid']
        self.assertFalse(conflicts.evaluate_proposal(prop)['accepted'])


@override_settings(CLOUD_SYNC_ROLE='cloud', CLOUD_SYNC_API_KEY='k3y')
class RequestSyncCloudTests(Base):
    def test_edge_request_is_created_on_the_cloud(self):
        conflicts.apply_edge_request(req_snap(origin='edge'))
        self.assertEqual(SessionRequest.objects.get().status, 'pending')
        self.assertEqual(SyncedRequest.objects.get().origin, 'edge')

    def test_pull_returns_requests_changed_since_the_cursor(self):
        req = SessionRequest.objects.create(
            lab=self.lab, subject='S', date=D, start_time=t(9), end_time=t(11))
        auth = {'HTTP_AUTHORIZATION': 'Bearer k3y'}
        r = self.client.get('/api/sync/pull/', **auth).json()
        self.assertEqual(len(r['requests']), 1)
        later = r['requests'][0]['updated_at']
        r2 = self.client.get('/api/sync/pull/', {'since': later}, **auth).json()
        self.assertEqual(r2['requests'], [])
        req.subject = 'changed'
        req.save()
        r3 = self.client.get('/api/sync/pull/', {'since': later}, **auth).json()
        self.assertEqual([x['subject'] for x in r3['requests']], ['changed'])

    def test_lab_server_approval_replaces_a_cloud_provisional_one_silently(self):
        prof = _instructor()
        s = self.make_session(t(9), t(11), 'CAAA111')  # cloud approved first (provisional)
        req = SessionRequest.objects.create(
            lab=self.lab, subject='S', date=D, start_time=t(9), end_time=t(11), instructor=prof,
            status='approved', reservation_code='CAAA111')
        uid = str(req.sync_request.sync_uuid)
        # ...but the In-Charge approved the same request at the lab with another code
        conflicts.apply_edge_request(req_snap('approved', 'EDGE01', origin='cloud', uid=uid))
        req.refresh_from_db()
        self.assertEqual((req.status, req.reservation_code), ('approved', 'EDGE01'))
        self.assertFalse(Session.objects.filter(pk=s.pk).exists())  # provisional one removed
        self.assertFalse(any('declined' in m.subject.lower() for m in mail.outbox))  # no wrong "declined"
        from notifications.models import Notification
        self.assertFalse(Notification.objects.filter(title='Lab request declined').exists())


# ===================== who may approve =====================
class RolePermissionTests(Base):
    def setUp(self):
        super().setUp()
        U = get_user_model()
        self.instr = U.objects.create_user(email='i@gmail.com', password='pw12345678!', role='instructor')
        self.inch = U.objects.create_user(email='c@gmail.com', password='pw12345678!', role='incharge')
        self.adm = U.objects.create_user(email='a@gmail.com', password='pw12345678!', role='admin')
        self.req = SessionRequest.objects.create(
            lab=self.lab, subject='S', date=D, start_time=t(9), end_time=t(11), instructor=self.instr)

    def _approve(self):
        return self.client.post(f'/schedule/request/{self.req.pk}/approve/')

    def test_anonymous_cannot_approve_decline_or_delete(self):
        sess = self.make_session(t(13), t(14), 'X1')
        from django.urls import reverse
        for url in (reverse('approve_request', args=[self.req.pk]), reverse('decline_request', args=[self.req.pk]),
                    reverse('session_delete', args=[sess.pk])):
            r = self.client.post(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn('/accounts/login/', r['Location'], url)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'pending')
        self.assertTrue(Session.objects.filter(pk=sess.pk).exists())

    def test_instructor_cannot_approve(self):
        self.client.force_login(self.instr)
        self.assertEqual(self._approve().status_code, 403)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'pending')

    def test_incharge_and_admin_can_approve(self):
        # This test is about WHO may approve, not about the email. The email template render is
        # patched out so the test does not depend on Django's template-context copying (which is
        # broken on Django < 5.2 with Python 3.14).
        from unittest import mock
        self.client.force_login(self.inch)
        with mock.patch('notifications.services.notify_reservation_approved'):
            self._approve()
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'approved')


# ===================== reservation code format & collisions =====================
class CodeFormatTests(Base):
    def test_generated_codes_keep_the_original_6_character_format(self):
        from scheduling.utils import generate_reservation_code
        for _ in range(50):
            code = generate_reservation_code()
            self.assertEqual(len(code), 6)
            self.assertTrue(code.isalnum() and code == code.upper())

    def test_same_code_on_both_servers_is_reissued_not_rejected(self):
        self.make_session(t(13), t(14), 'ABC234')  # lab server already uses this code elsewhere
        v = conflicts.evaluate_proposal(snap('ABC234', '09:00:00', '11:00:00'))
        self.assertTrue(v['accepted'])
        self.assertNotEqual(v['reservation_code'], 'ABC234')
        self.assertEqual(len(v['reservation_code']), 6)
        self.assertEqual(Session.objects.filter(reservation_code='ABC234').count(), 1)  # the old one untouched
        self.assertTrue(Session.objects.filter(reservation_code=v['reservation_code'], start_time=t(9)).exists())


@override_settings(CLOUD_SYNC_ROLE='cloud')
class CodeCollisionCloudTests(Base):
    def test_verdict_with_new_code_updates_session_request_and_held_email(self):
        prof = _instructor()
        s = self.make_session(t(9), t(11), 'ABC234')
        req = SessionRequest.objects.create(
            lab=self.lab, subject='Prog 1', date=D, start_time=t(9), end_time=t(11),
            status='approved', reservation_code='ABC234', instructor=prof)
        notify_service.notify_reservation_approved(req, 'ABC234')  # held (provisional)
        conflicts.apply_verdict({'sync_uuid': str(s.sync_booking.sync_uuid), 'accepted': True,
                                 'reservation_code': 'XYZ789'})
        s.refresh_from_db(); req.refresh_from_db()
        self.assertEqual((s.reservation_code, req.reservation_code), ('XYZ789', 'XYZ789'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('XYZ789', mail.outbox[0].body)
        self.assertNotIn('ABC234', mail.outbox[0].body)

    def test_lab_server_booking_with_same_code_makes_the_provisional_one_change(self):
        prov = self.make_session(t(9), t(11), 'ABC234')  # cloud, provisional
        conflicts.apply_edge_session(snap('ABC234', '13:00:00', '14:00:00'))  # edge owns ABC234
        prov.refresh_from_db()
        self.assertNotEqual(prov.reservation_code, 'ABC234')
        self.assertEqual(len(prov.reservation_code), 6)
        self.assertEqual(Session.objects.get(start_time=t(13)).reservation_code, 'ABC234')