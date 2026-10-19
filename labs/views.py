import json
from datetime import timedelta, datetime, timezone as dt_timezone
from django.views.generic import TemplateView, ListView, UpdateView, CreateView, DeleteView, FormView
from django.http import JsonResponse
from django.shortcuts import redirect, get_object_or_404, render
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.urls import reverse_lazy, reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from accounts.mixins import RoleRequiredMixin, ModalFormMixin, ModalDetailMixin, modal_redirect, is_modal_request, bulk_delete
from labs.models import PC, Lab, InventoryItem, MaintenanceLog, EquipmentIssue, PCActivityLog
from labs.network import refresh_pc_statuses
from labs.privacy import resolve_site_label
from labs.forms import ReservationPCLoginForm, MaintenanceScheduleForm, LabForm, InventoryItemForm, PCForm, PCImportForm
from notifications import services as notify_service


def _local_day_bounds(day):
    """
    Returns (start, end) — timezone-aware datetime bounds [start, end) —
    for one local calendar day, in the project's active timezone
    (Asia/Manila). Accepts either a date object or a 'YYYY-MM-DD' string
    (e.g. straight from request.GET); returns (None, None) for anything
    that can't be parsed so callers can just skip filtering.

    Used INSTEAD OF Django's `__date=`/`__date__gte=` lookups (e.g.
    `captured_at__date=day`) wherever a query needs to match "everything
    on this local day". Those lookups ask the DATABASE to convert each
    stored UTC timestamp into the local timezone before comparing — on
    MySQL that means a `CONVERT_TZ(...)` call, which silently returns
    NULL (matching NOTHING, no error) if the server's named-timezone
    tables aren't loaded. That's the MySQL default on plain XAMPP/Laragon
    installs unless `mysql_tzinfo_to_sql` was run — a very easy thing to
    miss, and the likely cause behind "I filtered by today's date and
    nothing showed up" even though the data is right there.
    Computing the day's UTC boundaries here in Python and filtering with
    __gte/__lt sidesteps CONVERT_TZ entirely, so it works regardless of
    whatever timezone data the MySQL server does or doesn't have.
    """
    if day in (None, ''):
        return None, None
    if isinstance(day, str):
        from django.utils.dateparse import parse_date
        day = parse_date(day)
        if day is None:
            return None, None
    from datetime import datetime as _dt
    start = timezone.make_aware(_dt.combine(day, _dt.min.time()))
    return start, start + timedelta(days=1)


# admin main dashboard
class AdminDashboardView(RoleRequiredMixin, TemplateView):
    allowed_roles = ['admin']
    template_name = 'dashboard/admin.html'

    def get_context_data(self, **kwargs):
        from datetime import timedelta
        from django.contrib.auth import get_user_model
        from django.db.models import Q
        from issues.models import Issue
        from scheduling.models import Session, SessionRequest
        from notifications.models import Notification
        from accounts.models import ActivityLog
        from labs.reports import _session_hours
        User = get_user_model()

        ctx = super().get_context_data(**kwargs)
        now = timezone.localtime(timezone.now())
        today = now.date()
        week_start = today - timedelta(days=today.weekday())  # Monday
        week_days = [week_start + timedelta(days=i) for i in range(7)]

        # --- top metric cards ---
        pcs = PC.objects.select_related('lab').all()
        ctx['pcs'] = pcs
        total_students = User.objects.filter(role='student').count()
        total_instructors = User.objects.filter(role='instructor').count()
        total_pcs = pcs.count()
        online_count = pcs.filter(status='online').count()
        offline_count = pcs.filter(status='offline').count()
        in_use_count = pcs.filter(status='in_use').count()
        maintenance_count = pcs.filter(status='maintenance').count()
        issue_count = pcs.filter(status='issue').count()

        _week_start_bound, _ = _local_day_bounds(week_start)
        ctx['total_students'] = total_students
        ctx['new_students_this_week'] = User.objects.filter(
            role='student', date_joined__gte=_week_start_bound
        ).count()
        ctx['total_instructors'] = total_instructors
        ctx['new_instructors_this_week'] = User.objects.filter(
            role='instructor', date_joined__gte=_week_start_bound
        ).count()
        ctx['total_labs'] = Lab.objects.count()
        ctx['total_pcs'] = total_pcs
        ctx['online_count'] = online_count
        ctx['offline_count'] = offline_count
        ctx['in_use_count'] = in_use_count
        ctx['maintenance_count'] = maintenance_count
        ctx['issue_count'] = issue_count
        ctx['open_issues_count'] = Issue.objects.filter(status='open').count()

        def pct(part):
            return round(part / total_pcs * 100) if total_pcs else 0

        ctx['in_use_pct'] = pct(in_use_count)
        ctx['online_pct'] = pct(online_count)
        ctx['maintenance_pct'] = pct(maintenance_count)
        ctx['issue_pct'] = pct(issue_count)
        ctx['offline_pct'] = pct(offline_count)

        # --- today's overview ---
        todays_sessions_qs = Session.objects.filter(date=today).select_related('instructor', 'lab')
        ctx['todays_sessions'] = todays_sessions_qs
        ctx['todays_sessions_count'] = todays_sessions_qs.count()
        ctx['active_sessions_count'] = todays_sessions_qs.filter(
            start_time__lte=now.time(), end_time__gte=now.time()
        ).count()
        ctx['pending_reservations_count'] = SessionRequest.objects.filter(status='pending').count()

        _today_start, _today_end = _local_day_bounds(today)
        todays_attendance = ActivityLog.objects.filter(
            action='pc_unlock', created_at__gte=_today_start, created_at__lt=_today_end
        ).values('target_identifier').distinct().count()
        ctx['todays_attendance'] = todays_attendance
        ctx['todays_attendance_pct'] = (
            round(todays_attendance / total_students * 100, 1) if total_students else 0
        )

        # --- daily attendance trend (this week, Mon-Sun) ---
        attendance_trend = []
        for d in week_days:
            d_start, d_end = _local_day_bounds(d)
            count = ActivityLog.objects.filter(
                action='pc_unlock', created_at__gte=d_start, created_at__lt=d_end
            ).values('target_identifier').distinct().count()
            attendance_trend.append(count)
        ctx['attendance_trend_labels'] = json.dumps(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])
        ctx['attendance_trend_data'] = json.dumps(attendance_trend)

        # --- weekly lab usage (this week, % of total lab capacity booked) ---
        labs_all = list(Lab.objects.all())

        def day_capacity_hours():
            total = 0
            for lab in labs_all:
                from datetime import datetime, date as date_cls
                open_hours = (
                    datetime.combine(date_cls.min, lab.closing_time)
                    - datetime.combine(date_cls.min, lab.opening_time)
                )
                total += open_hours.total_seconds() / 3600
            return total

        capacity = day_capacity_hours()
        lab_usage_trend = []
        for d in week_days:
            day_sessions = Session.objects.filter(date=d)
            booked_hours = sum(_session_hours(s) for s in day_sessions)
            usage_pct = round(booked_hours / capacity * 100) if capacity else 0
            lab_usage_trend.append(min(usage_pct, 100))
        ctx['lab_usage_labels'] = json.dumps(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])
        ctx['lab_usage_data'] = json.dumps(lab_usage_trend)

        # --- monthly computer utilization (last 4 weeks, % avg) ---
        monthly_util = []
        for w in range(3, -1, -1):
            w_start = week_start - timedelta(days=7 * w)
            w_end = w_start + timedelta(days=6)
            w_sessions = Session.objects.filter(date__gte=w_start, date__lte=w_end)
            booked_hours = sum(_session_hours(s) for s in w_sessions)
            week_capacity = capacity * 7
            util_pct = round(booked_hours / week_capacity * 100) if week_capacity else 0
            monthly_util.append(min(util_pct, 100))
        ctx['monthly_util_labels'] = json.dumps(['Week 1', 'Week 2', 'Week 3', 'Week 4'])
        ctx['monthly_util_data'] = json.dumps(monthly_util)

        # --- recent activity (merged feed) ---
        activity = []
        for log in ActivityLog.objects.select_related('actor', 'pc').order_by('-created_at')[:8]:
            if log.action == 'pc_unlock':
                activity.append({
                    'icon': 'login', 'title': f'Student {log.target_identifier} logged in on {log.pc.pc_id if log.pc else "a PC"}',
                    'sub': log.pc.lab.name if log.pc else '', 'time': log.created_at,
                })
            elif log.action == 'register':
                activity.append({
                    'icon': 'user', 'title': f'New user registered: {log.target_identifier}',
                    'sub': '', 'time': log.created_at,
                })
        for req in SessionRequest.objects.filter(status='approved').select_related('lab').order_by('-created_at')[:5]:
            activity.append({
                'icon': 'calendar', 'title': f'Reservation approved for {req.lab.name}',
                'sub': req.requester_name, 'time': req.created_at,
            })
        for issue in Issue.objects.select_related('pc').order_by('-created_at')[:5]:
            activity.append({
                'icon': 'tool',
                'title': f'Maintenance request submitted for {issue.pc.pc_id if issue.pc else "equipment"}',
                'sub': issue.title, 'time': issue.created_at,
            })
        activity.sort(key=lambda a: a['time'], reverse=True)
        ctx['recent_activity'] = activity[:5]

        # --- notifications (system alerts + this admin's own) ---
        ctx['recent_alerts'] = Notification.objects.filter(
            Q(is_system_alert=True) | Q(user=self.request.user)
        ).distinct().order_by('-created_at')[:5]

        # --- upcoming schedules (today, with ongoing/upcoming status) ---
        upcoming = []
        for s in todays_sessions_qs.order_by('start_time'):
            if s.start_time <= now.time() <= s.end_time:
                status = 'ongoing'
            elif s.start_time > now.time():
                status = 'upcoming'
            else:
                status = 'done'
            upcoming.append({'session': s, 'status': status})
        ctx['upcoming_schedules'] = upcoming

        return ctx


# Shared by ReservationPCLoginView (browser form, used when a student logs
# in from a separate reception/kiosk computer) and PCAgentLoginAPIView (JSON
# endpoint, used when the student types their ID/reservation code directly
# into the lock screen on the PC itself). Keeping this logic in one place
# means both entry points enforce identical rules — same reservation checks,
# same PC-unlock call, same activity log — they just differ in how the
# result gets back to the person (rendered form errors vs. a JSON reply).
def verify_reservation_and_check_in(remote_addr, id_number, code):
    """
    Returns a dict:
      {'ok': True,  'pc': PC, 'session': Session, 'unlock_success': bool, 'unlock_detail': str}
      {'ok': False, 'error': str}
    Does NOT raise on "expected" failures (no PC, no matching reservation,
    etc.) — those come back as {'ok': False, 'error': ...} so callers can
    show them however fits their interface.
    """
    from accounts.models import ActivityLog
    from django.contrib.auth import get_user_model
    from labs.network import unlock_pc
    from scheduling.models import Session, SessionCheckIn

    id_number = (id_number or '').strip()
    code = (code or '').strip()

    pc = PC.objects.filter(ip_address=remote_addr).select_related('lab').first()
    if pc is None:
        # Including the detected remote_addr here (instead of just a generic
        # message) is deliberate: this is the ONE place a mismatched/changed
        # PC IP actually surfaces to a human, and it shows up right on the
        # lock screen the student/in-charge is already looking at — no need
        # to go dig through server console/logs on a separate machine to
        # find out what IP this PC is actually being seen as.
        return {'ok': False, 'error': f"This computer isn't registered in the system yet (server sees this PC's IP as {remote_addr}). Ask your Laboratory In-Charge to add it before it can be unlocked."}

    # A reservation code is shared by different sets of people depending on
    # how the booking was made:
    #  - Individual request (requester_type='student', no roster attached):
    #    only the requester's own ID works.
    #  - Instructor request with their class roster attached: any student on
    #    that roster can check in with the shared code — a whole-class booking.
    #  - Group-of-students request (requester_type='group'): the group isn't
    #    enumerated by name anywhere — ANY ID number works with the shared
    #    code, since one person filed it on behalf of the whole group.
    #  - Any request with a roster attached (regardless of type): roster
    #    members can also check in with the shared code, in addition to
    #    whatever the rule above allows.
    session = Session.objects.filter(
        reservation_code__iexact=code,
        lab=pc.lab,
    ).select_related('lab', 'instructor', 'roster').first()

    if session is None:
        return {'ok': False, 'error': 'No matching reservation for this lab. Check your ID and reservation code, or ask your Lab In-Charge.'}

    if not id_number:
        return {'ok': False, 'error': 'Please enter your Student/Instructor ID.'}

    # Concurrency guard: this ID number may already be actively logged in on
    # a DIFFERENT PC right now. SessionCheckIn.pc always reflects the last
    # machine that ID successfully checked into (see the update_or_create
    # below), so if that PC is still marked 'in_use', this same person is
    # currently occupying it — block a second, simultaneous login elsewhere
    # instead of silently letting them take over a second machine while the
    # first one stays live. Excludes THIS pc so re-submitting the login form
    # on the same machine (e.g. a double click, or refreshing after a
    # successful unlock) still works normally.
    other_active_checkin = SessionCheckIn.objects.filter(
        id_number__iexact=id_number, pc__status='in_use',
    ).exclude(pc=pc).select_related('pc', 'pc__lab').first()
    if other_active_checkin and other_active_checkin.pc:
        other_pc = other_active_checkin.pc
        return {'ok': False, 'error': (
            f"This ID is already logged in on {other_pc.pc_id} ({other_pc.lab.name}). "
            "Please log out there first, or ask your Lab In-Charge for help if that session is stuck."
        )}

    is_group_booking = session.requester_type == 'group'
    # Walk-in and Override reservations now book a specific NUMBER of PCs
    # in advance (see scheduling.utils.capacity_error) rather than a single
    # named person, so — just like Group bookings — their shared code
    # should work for any ID, capped at how many PCs were approved.
    is_capacity_booking = session.requester_type in ('walk_in', 'override')
    is_shared_code_booking = is_group_booking or is_capacity_booking
    is_primary_requester = id_number.lower() == (session.requester_id_number or '').strip().lower()
    roster_student = None
    if not is_primary_requester and session.roster_id:
        roster_student = session.roster.students.filter(id_number__iexact=id_number).first()

    User = get_user_model()
    walk_in_account = None
    if not is_shared_code_booking and not is_primary_requester and roster_student is None:
        # Not the requester, not a group/walk-in/override booking, not on
        # this session's roster — they have no claim on this specific
        # reservation. Only a Class (instructor) reservation has a WALK-IN
        # path: any other registered, active account may still use a PC
        # during that class's slot, but only if the laboratory currently
        # has an available (working, not-already-in-use) PC — a reservation
        # guarantees a slot for the people it was actually made for, not
        # for anyone who happens to be in the room. Individual bookings
        # don't get a walk-in path either: an Individual code is for
        # exactly one person. (Group/Walk-in/Override bookings already
        # accept any ID up to their own cap, handled above.)
        if session.requester_type != 'instructor':
            return {'ok': False, 'error': 'No matching reservation for this lab. Check your ID and reservation code, or ask your Lab In-Charge.'}

        walk_in_account = User.objects.filter(id_number__iexact=id_number).first()
        if walk_in_account is None:
            return {'ok': False, 'error': 'No matching reservation for this lab. Check your ID and reservation code, or ask your Lab In-Charge.'}
        if not walk_in_account.is_active:
            return {'ok': False, 'error': 'Your account has been deactivated. Ask your Admin or Lab In-Charge for assistance.'}

        working_pcs = PC.objects.filter(lab=pc.lab).exclude(status__in=['offline', 'maintenance', 'issue']).count()
        occupied_pcs = PC.objects.filter(lab=pc.lab, status='in_use').count()
        if working_pcs <= 0 or occupied_pcs >= working_pcs:
            return {'ok': False, 'error': (
                'This laboratory is at full capacity right now — no available computers for walk-in access. '
                'Please try again later or ask your Lab In-Charge.'
            )}

    if is_shared_code_booking:
        cap = (session.pcs_requested if is_capacity_booking else session.student_count) or 0
        unit = 'PC' if is_capacity_booking else 'student'
        # Compare against everyone ELSE already checked in (not the raw
        # total), and do this on every attempt — not just a person's first
        # one. Previously this whole check was skipped for anyone with an
        # existing SessionCheckIn row, which meant lowering student_count/
        # pcs_requested after people had already checked in (e.g. editing a
        # reservation from 2 students down to 1) never actually revoked
        # anyone's access: whoever had already checked in could keep
        # re-entering indefinitely regardless of the new, lower cap.
        # Excluding "self" is still necessary so a legitimate repeat login
        # doesn't count its own earlier check-in against itself.
        others_checked_in = session.check_ins.exclude(id_number__iexact=id_number).count()
        if cap > 0 and others_checked_in >= cap:
            return {'ok': False, 'error': (
                f"This reservation's check-in limit ({cap} {unit}{'s' if cap != 1 else ''}) has already been "
                "reached. If that count is wrong, ask your Admin or Lab In-Charge to update the request."
            )}

    if session.instructor is None:
        return {'ok': False, 'error': (
            'Your reservation was found, but this ID isn\u2019t linked to a registered account yet. '
            'Ask your Admin or Lab In-Charge to register your account before you can log in here.'
        )}

    if not session.instructor.is_active:
        return {'ok': False, 'error': 'Your account has been deactivated. Ask your Admin or Lab In-Charge for assistance.'}

    now = timezone.localtime()
    if session.date != now.date():
        return {'ok': False, 'error': f"This reservation is for {session.date.strftime('%B %d, %Y')}, not today."}
    if not (session.start_time <= now.time() <= session.end_time):
        return {'ok': False, 'error': f"This reservation is only valid from {session.start_time.strftime('%H:%M')} to {session.end_time.strftime('%H:%M')}."}

    success, detail = unlock_pc(pc)

    # Human-readable label + resolved account, worked out either way so it
    # can be used both in the success path below and in the ActivityLog
    # entry for a failed unlock.
    if is_primary_requester:
        checkin_type = 'requester'
        checked_in_user = session.instructor
        checked_in_name = session.requester_name
    elif roster_student and roster_student.student_id:
        checkin_type = 'roster'
        checked_in_user = roster_student.student
        checked_in_name = roster_student.full_name
    elif roster_student:
        checkin_type = 'roster'
        checked_in_user = None  # roster entry isn't linked to a registered account
        checked_in_name = roster_student.full_name
    elif is_group_booking:
        checkin_type = 'group'
        checked_in_user = User.objects.filter(id_number__iexact=id_number).first() or session.instructor
        checked_in_name = f'{session.requester_name} — group member'
    elif is_capacity_booking:
        checkin_type = session.requester_type  # 'walk_in' or 'override' — same labels used by the live check-in features
        checked_in_user = User.objects.filter(id_number__iexact=id_number).first() or session.instructor
        label = 'Walk-in' if session.requester_type == 'walk_in' else 'Override'
        checked_in_name = f'{id_number} — {label} reservation ({session.requester_name})'
    else:
        checkin_type = 'walk_in'
        checked_in_user = walk_in_account
        checked_in_name = f"{walk_in_account.display_name} — walk-in"

    if not success:
        # The unlock signal never actually reached this PC's agent — most
        # likely nobody has started the agent on that machine yet (or its
        # IP on file no longer matches). The person's ID/reservation WAS
        # verified, but nobody can be sitting at a screen that's still
        # locked, so don't record a check-in or mark the PC "in use" for a
        # session that doesn't really exist. Previously this recorded the
        # check-in and flipped the PC to "in use" regardless of `success`,
        # which is exactly how a PC could end up stuck showing "in use by
        # <name>" even though nobody ever actually got in — including from
        # this same machine's own browser, if its agent wasn't running.
        ActivityLog.objects.create(
            actor=None,
            action='pc_unlock',
            target_identifier=id_number,
            pc=pc,
            details=(
                f"{checked_in_name} ({id_number}) was verified for {pc.pc_id} ({pc.lab.name}) via "
                f"reservation {session.reservation_code}, but the unlock command didn't run: {detail}. "
                "No check-in was recorded and the PC was left as-is."
            ),
        )
        return {
            'ok': False,
            'error': (
                f"You were verified, but {pc.pc_id} couldn't be unlocked ({detail}). "
                "Ask your Lab In-Charge to check that the PC agent is running there, then try again."
            ),
        }

    # Every successful check-in — not just group bookings — is now recorded
    # as a SessionCheckIn, tagged with how they got in (requester/roster/
    # group/walk-in) and the actual resolved account. This is the canonical
    # "who really used a PC during this reservation" transaction log, used
    # by the Attendance Report to show walk-ins as their own section instead
    # of folding them into the requester's name.
    SessionCheckIn.objects.update_or_create(
        session=session, id_number=id_number,
        defaults={'checkin_type': checkin_type, 'student': checked_in_user, 'pc': pc},
    )

    pc.status = 'in_use'
    pc.last_active = timezone.now()
    pc.current_user = checked_in_user
    pc.current_session = session
    pc.current_login_via_override = False
    pc.save(update_fields=['status', 'last_active', 'current_user', 'current_session', 'current_login_via_override'])

    ActivityLog.objects.create(
        actor=checked_in_user,
        action='pc_unlock',
        target_identifier=id_number,
        pc=pc,
        details=f"{checked_in_name} ({id_number}) checked in with reservation {session.reservation_code} — unlocked {pc.pc_id} ({pc.lab.name})."
    )

    return {
        'ok': True,
        'pc': pc,
        'session': session,
        'checked_in_name': checked_in_name,
        'checkin_type': checkin_type,
        'unlock_success': success,
        'unlock_detail': detail,
    }


# the lock screen shown on a lab computer itself — the person enters the
# Student/Instructor ID and reservation code that the Admin/Lab In-Charge
# gave them when their walk-in request was approved. This is deliberately
# NOT behind RoleRequiredMixin/LoginRequiredMixin: it's the page someone
# sees before they're identified at all, and it never grants access to the
# web management system, only to the physical PC it's running on.
#
# NOTE: this browser-based page is kept for labs that use a separate
# reception/kiosk computer for check-in. If students log in directly on the
# lock screen of the (currently locked) lab PC itself, see
# PCAgentLoginAPIView below instead — the lock screen can't show a normal
# browser page since it deliberately blocks all other access to the PC.
class ReservationPCLoginView(FormView):
    template_name = 'labs/pc_login.html'
    form_class = ReservationPCLoginForm

    def _resolve_pc(self):
        ip = self.request.META.get('REMOTE_ADDR')
        return PC.objects.filter(ip_address=ip).select_related('lab').first()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['pc'] = self._resolve_pc()
        return ctx

    def form_valid(self, form):
        result = verify_reservation_and_check_in(
            self.request.META.get('REMOTE_ADDR'),
            form.cleaned_data['id_number'],
            form.cleaned_data['reservation_code'],
        )

        if not result['ok']:
            form.add_error(None, result['error'])
            return self.form_invalid(form)

        pc = result['pc']
        session = result['session']
        messages.success(self.request, f"Welcome, {result['checked_in_name']}. Unlocking {pc.pc_id}...")

        return redirect('pc_login')


# JSON endpoint used by the lab_pc_agent running ON the lab PC itself, for
# labs where students type their ID/reservation code directly into the lock
# screen (instead of a separate reception/kiosk computer running the
# browser-based ReservationPCLoginView above). The request is expected to
# originate from the same machine's agent process, so REMOTE_ADDR here is
# that PC's own IP — the identical IP-based PC lookup used everywhere else,
# just reached over JSON instead of an HTML form. CSRF-exempt because this
# is a machine-to-machine call from a local agent process, not a browser
# session; PC_AGENT_SHARED_SECRET (checked the same way the agent checks
# incoming unlock/lock requests) is what stands in for a login here.
@csrf_exempt
def pc_agent_login_api(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    from django.conf import settings

    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid JSON body'}, status=400)

    expected_secret = getattr(settings, 'PC_AGENT_SHARED_SECRET', '')
    if not expected_secret or payload.get('secret') != expected_secret:
        return JsonResponse({'ok': False, 'error': 'invalid secret'}, status=403)

    result = verify_reservation_and_check_in(
        request.META.get('REMOTE_ADDR'),
        payload.get('id_number', ''),
        payload.get('reservation_code', ''),
    )

    if not result['ok']:
        return JsonResponse({'ok': False, 'error': result['error']}, status=200)

    return JsonResponse({
        'ok': True,
        'pc_id': result['pc'].pc_id,
        'student_name': result['checked_in_name'],
        'unlock_success': result['unlock_success'],
        'unlock_detail': result['unlock_detail'],
        # HH:MM:SS the reservation ends — the agent uses this to schedule an
        # automatic re-lock locally, without needing to keep polling the
        # server. Combined with today's date since Session.date is always
        # "today" by the time verify_reservation_and_check_in accepts it.
        'session_end_time': result['session'].end_time.strftime('%H:%M:%S'),
        # The server's own current wall-clock time (Asia/Manila), sent so the
        # agent can measure its OWN clock's drift/offset against the server
        # instead of trusting the lab PC's local Windows clock outright.
        # Without this, a lab PC with a wrong/drifted system clock schedules
        # the warning + auto-relock against the wrong moment in real time.
        'server_time': timezone.localtime().strftime('%H:%M:%S'),
    })


# JSON endpoint the lab_pc_agent calls (typically once at startup, and
# whenever it shows the lock screen) to find out its OWN registered name —
# the "PC 01"-style label shown on the lock screen BEFORE anyone has logged
# in, so it never has to be hand-typed into agent_config.json and drift out
# of sync with what's actually set for this PC in the admin panel (Manage
# PCs -> Edit). Same shared-secret + IP-based PC lookup as the other
# pc-agent-* endpoints; GET is fine since this is a read-only lookup, but
# the secret is still required so a random device on the network can't
# fish for lab PC names.
@csrf_exempt
def pc_agent_info_api(request):
    from django.conf import settings

    expected_secret = getattr(settings, 'PC_AGENT_SHARED_SECRET', '')
    secret = request.GET.get('secret') if request.method == 'GET' else None
    if secret is None:
        try:
            payload = json.loads(request.body or b'{}')
        except (ValueError, TypeError):
            payload = {}
        secret = payload.get('secret')

    if not expected_secret or secret != expected_secret:
        return JsonResponse({'ok': False, 'error': 'invalid secret'}, status=403)

    pc = PC.objects.select_related('lab').filter(ip_address=request.META.get('REMOTE_ADDR')).first()
    if pc is None:
        return JsonResponse({'ok': False, 'error': 'PC not registered'}, status=200)

    lab = pc.lab
    now_time = timezone.localtime().time()
    lab_is_open = lab.opening_time <= now_time <= lab.closing_time
    open_lbl = lab.opening_time.strftime('%I:%M %p').lstrip('0')
    close_lbl = lab.closing_time.strftime('%I:%M %p').lstrip('0')

    return JsonResponse({
        'ok': True,
        'pc_id': pc.pc_id,
        'lab_name': lab.name,
        # Used by the lock screen to show a "Laboratory Closed" badge and
        # disable the ID/Reservation Code fields for normal self-service
        # login outside the lab's operating hours (Manage Labs). This is
        # informational/UI-only on the agent side — it does NOT affect
        # Override Check-In or Manual Unlock, which the server sends
        # straight to this same PC's /unlock endpoint regardless of what
        # this lock screen shows or has disabled (see labs/network.py
        # unlock_pc()/lock_pc() and scheduling._slot_error's matching
        # exemption for those two flows on the booking side).
        'lab_is_open': lab_is_open,
        'lab_hours': f'{open_lbl}–{close_lbl}',
    })


# JSON endpoint used by the lab_pc_agent while a PC is UNLOCKED — it calls
# this every `activity_report_interval_seconds` (agent_config.json) with
# the current foreground window's title bar text. This is the closest
# thing this system does to "what is the student doing" tracking: no
# keystrokes, no screenshots, no page HTML/URLs — just whatever text
# Windows itself puts in the active window's title bar (for a browser
# that's normally "Page Title - Browser Name"). Same shared-secret auth
# and same IP-based PC lookup as the other pc-agent-* endpoints.
@csrf_exempt
def pc_agent_activity_api(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    from django.conf import settings

    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid JSON body'}, status=400)

    expected_secret = getattr(settings, 'PC_AGENT_SHARED_SECRET', '')
    if not expected_secret or payload.get('secret') != expected_secret:
        return JsonResponse({'ok': False, 'error': 'invalid secret'}, status=403)

    pc = PC.objects.filter(ip_address=request.META.get('REMOTE_ADDR')).select_related('current_user').first()
    if pc is None:
        return JsonResponse({'ok': False, 'error': 'PC not registered'}, status=200)

    # Log activity for a PC the server thinks is actually in active use —
    # either by an authenticated account (pc.current_user), or by a Manual
    # Unlock / offline-tool walk-in guest (pc.current_guest_name, typed and
    # unverified — see manual_unlock_log_api). Both are blank/None for a PC
    # that isn't currently unlocked for anyone (e.g. a stray call landing
    # right as a session just ended) — skip in that case, there's no one to
    # attribute the sample to.
    if pc.current_user is None and not pc.current_guest_name:
        return JsonResponse({'ok': True, 'skipped': 'no active session for this PC'})

    window_title = (payload.get('window_title') or '').strip()[:500]
    page_url = (payload.get('page_url') or '').strip()[:500]

    PCActivityLog.objects.create(
        pc=pc,
        student=pc.current_user,
        guest_name=pc.current_guest_name or '',
        session=pc.current_session,
        window_title=window_title,
        page_url=page_url,
        via_override=pc.current_login_via_override,
    )

    # Bump last_active on every real activity ping, not just at
    # login/unlock time. Without this, last_active only ever reflects when
    # the session STARTED — so a PC still being genuinely used could get
    # wrongly swept up by release_expired_pc_sessions()'s fallback check
    # (last_active + STALE_PC_FALLBACK_HOURS) for guest/no-session PCs,
    # even while someone is actively on it. update_fields keeps this a
    # cheap single-column write, safe to do on every ping.
    pc.last_active = timezone.now()
    pc.save(update_fields=['last_active'])

    return JsonResponse({'ok': True})


# Called by the agent whenever it re-locks a PC — either because the
# reservation's end_time was reached (automatic) or because the student
# clicked "Lock Now" on the little always-on-visible mini panel (manual,
# for when they're done early). Either way this just clears the PC's
# "currently in use" tracking server-side so status pages/admin reflect
# reality; the actual re-locking of the screen happens locally in the
# agent regardless of whether this call succeeds.
@csrf_exempt
def pc_agent_end_session_api(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    from django.conf import settings
    from accounts.models import ActivityLog

    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid JSON body'}, status=400)

    expected_secret = getattr(settings, 'PC_AGENT_SHARED_SECRET', '')
    if not expected_secret or payload.get('secret') != expected_secret:
        return JsonResponse({'ok': False, 'error': 'invalid secret'}, status=403)

    remote_addr = request.META.get('REMOTE_ADDR')
    pc = PC.objects.filter(ip_address=remote_addr).select_related('lab').first()
    if pc is None:
        return JsonResponse({'ok': False, 'error': 'PC not registered'}, status=200)

    reason = payload.get('reason', 'manual')  # 'manual', 'expired', 'remote_lock', or 'override'
    session = pc.current_session
    user = pc.current_user

    if session is not None or user is not None:
        if reason == 'expired':
            detail_msg = f"{pc.pc_id} ({pc.lab.name}) re-locked — reservation time ended."
        elif reason == 'remote_lock':
            detail_msg = (
                f"{pc.pc_id} ({pc.lab.name}) locked remotely by a Lab In-Charge "
                f"(Manual PC Control tool)."
            )
        elif reason == 'override':
            detail_msg = (
                f"{pc.pc_id} ({pc.lab.name}) session ended — Admin/In-Charge Override "
                f"took over this PC after the on-screen warning countdown."
            )
        else:
            detail_msg = f"{pc.pc_id} ({pc.lab.name}) locked by the student (session ended early)."

        ActivityLog.objects.create(
            actor=user,
            action='pc_lock',
            target_identifier=session.requester_id_number if session else '',
            pc=pc,
            details=detail_msg,
        )

    pc.status = 'online'
    pc.current_user = None
    pc.current_session = None
    pc.current_guest_name = ''
    pc.save(update_fields=['status', 'current_user', 'current_session', 'current_guest_name'])

    return JsonResponse({'ok': True})


# Called by the agent when a student clicks "Log Out" on the PC itself, or
# when the agent's own scheduled auto-relock timer fires because the
# reservation's end_time passed. Either way, this just marks the PC as no
# longer in use server-side — the actual re-locking of the screen is a
# purely local decision the agent already made before calling this; this
# endpoint exists so the admin dashboard / PC status view reflects reality.
@csrf_exempt
def pc_agent_logout_api(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    from django.conf import settings
    from accounts.models import ActivityLog

    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid JSON body'}, status=400)

    expected_secret = getattr(settings, 'PC_AGENT_SHARED_SECRET', '')
    if not expected_secret or payload.get('secret') != expected_secret:
        return JsonResponse({'ok': False, 'error': 'invalid secret'}, status=403)

    pc = PC.objects.filter(ip_address=request.META.get('REMOTE_ADDR')).select_related('lab').first()
    if pc is None:
        return JsonResponse({'ok': False, 'error': 'PC not registered'}, status=200)

    previous_user = pc.current_user
    reason = payload.get('reason', 'manual')  # 'manual' or 'expired'

    # 'locked' is not one of PC.STATUS's choices (only online/offline/in_use/
    # maintenance/issue exist) — using it here left the PC uncounted in every
    # status metric and unstyled on the status pill. 'online' ("Available")
    # is what every sibling endpoint (pc_agent_end_session_api, force-release,
    # the stale-session sweeper) already uses for "screen locked, nobody on
    # it, ready for the next person" — so match that here too.
    pc.status = 'online'
    pc.current_user = None
    pc.current_session = None
    pc.current_guest_name = ''
    pc.last_active = timezone.now()
    pc.save(update_fields=['status', 'current_user', 'current_session', 'current_guest_name', 'last_active'])

    ActivityLog.objects.create(
        actor=previous_user,
        action='pc_lock',
        target_identifier=previous_user.display_name if previous_user else '',
        pc=pc,
        details=(
            f"{pc.pc_id} ({pc.lab.name}) re-locked — reservation time ended."
            if reason == 'expired' else
            f"{pc.pc_id} ({pc.lab.name}) locked by student (logged out)."
        )
    )

    return JsonResponse({'ok': True})


# Called by lab_pc_agent/manual_unlock.html — the standalone, no-login HTML
# tool that Lab In-Charges open to unlock a PC *directly* through its agent
# (IP + shared secret) when the CompuLab server itself is unreachable. That
# tool's own "Unlock PC" button already talks straight to the agent and
# does not depend on this endpoint at all — a PC can still be unlocked for
# a guest even while the server is down. This endpoint's only job is the
# separate, best-effort step of recording that guest access in the system's
# access log, tried right after the direct unlock and again whenever the
# tool is later able to reach the server. Because there is no login on that
# offline tool, the "Lab In-Charge name" here is a typed, unverified string
# (not tied to an authenticated account) — logged as such rather than
# attributed to a real actor, so the audit trail stays honest about what it
# actually knows.
@csrf_exempt
def manual_unlock_log_api(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    from django.conf import settings
    from accounts.models import ActivityLog
    from scheduling.models import Session, SessionCheckIn

    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid JSON body'}, status=400)

    expected_secret = getattr(settings, 'PC_AGENT_SHARED_SECRET', '')
    if not expected_secret or payload.get('secret') != expected_secret:
        return JsonResponse({'ok': False, 'error': 'invalid secret'}, status=403)

    pc_ip = (payload.get('pc_ip') or '').strip()
    guest_name = (payload.get('guest_name') or '').strip()
    incharge_name = (payload.get('incharge_name') or '').strip()

    if not guest_name or not incharge_name:
        return JsonResponse({'ok': False, 'error': 'guest_name and incharge_name are both required'}, status=400)

    pc = PC.objects.select_related('lab').filter(ip_address=pc_ip).first()
    if pc is None:
        return JsonResponse({'ok': False, 'error': 'No PC in the system is registered with that IP address'}, status=200)

    now = timezone.localtime()
    active_session = Session.objects.filter(
        lab=pc.lab, date=now.date(), start_time__lte=now.time(), end_time__gte=now.time(),
    ).order_by('start_time').first()

    guest_id_number = f"GUEST-{pc.pc_id}-{int(timezone.now().timestamp())}"
    SessionCheckIn.objects.update_or_create(
        session=active_session, id_number=guest_id_number,
        defaults={'checkin_type': 'guest', 'student': None, 'guest_name': guest_name, 'pc': pc},
    )

    pc.status = 'in_use'
    pc.last_active = timezone.now()
    pc.current_user = None
    pc.current_guest_name = guest_name
    pc.current_session = active_session
    pc.current_login_via_override = False
    pc.save(update_fields=['status', 'last_active', 'current_user', 'current_guest_name', 'current_session', 'current_login_via_override'])

    ActivityLog.objects.create(
        actor=None,
        action='pc_unlock',
        target_identifier=guest_id_number,
        pc=pc,
        details=(
            f'Emergency Manual Unlock (offline tool — used because the CompuLab server could not be reached '
            f'at the time of unlock): walk-in guest "{guest_name}" was granted access to {pc.pc_id} ({pc.lab.name}). '
            f'Lab In-Charge name entered on the offline tool (typed, not verified via login): "{incharge_name}". '
            + (f'Overlapping reservation {active_session.reservation_code}.' if active_session else 'No active reservation.')
        )
    )

    return JsonResponse({'ok': True})


class PCStatusView(RoleRequiredMixin, TemplateView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/pc_status.html'
    PAGE_SIZE = 10

    def get_context_data(self, **kwargs):
        from django.conf import settings
        from django.core.paginator import Paginator
        from django.db.models import Q
        from accounts.models import ActivityLog
        ctx = super().get_context_data(**kwargs)

        user = self.request.user
        lab_id = self.request.GET.get('lab', '')
        status_filter = self.request.GET.get('status', '')
        day = self.request.GET.get('day', '')
        q = self.request.GET.get('q', '').strip()

        labs_qs = Lab.objects.order_by('name')
        pcs_qs = PC.objects.select_related('lab', 'current_user', 'current_session')

        # Lab In-charge only ever sees their own assigned lab — same scoping
        # rule used by refresh_pc_status_view.
        if user.role == 'incharge' and user.assigned_lab_id:
            labs_qs = labs_qs.filter(pk=user.assigned_lab_id)
            pcs_qs = pcs_qs.filter(lab_id=user.assigned_lab_id)
            if not lab_id:
                lab_id = str(user.assigned_lab_id)

        ctx['all_labs'] = labs_qs
        ctx['status_choices'] = PC.STATUS
        ctx['selected_lab'] = lab_id
        ctx['selected_status'] = status_filter
        ctx['selected_day'] = day
        ctx['search_query'] = q
        ctx['filtered'] = bool(lab_id or status_filter or day or q)
        ctx['day_mode'] = bool(day)
        ctx['auto_check_interval_seconds'] = getattr(settings, 'PC_STATUS_CHECK_INTERVAL_SECONDS', 60)

        # ---- top summary metrics — scoped by lab (if chosen) but not by the
        # status filter, so the cards stay meaningful while the table below
        # is narrowed down to one status. ----
        metric_base = pcs_qs
        if lab_id:
            metric_base = metric_base.filter(lab_id=lab_id)
        total_pcs = metric_base.count()
        available_count = metric_base.filter(status='online').count()
        in_use_count = metric_base.filter(status='in_use').count()
        offline_count = metric_base.filter(status='offline').count()
        maintenance_count = metric_base.filter(status='maintenance').count()
        issue_count = metric_base.filter(status='issue').count()
        online_pcs_count = total_pcs - offline_count  # "reachable" = anything not offline

        def pct(part):
            return round(part / total_pcs * 100) if total_pcs else 0

        ctx.update({
            'total_pcs': total_pcs,
            'online_pcs_count': online_pcs_count, 'online_pcs_pct': pct(online_pcs_count),
            'available_count': available_count, 'available_pct': pct(available_count),
            'in_use_count': in_use_count, 'in_use_pct': pct(in_use_count),
            'offline_count': offline_count, 'offline_pct': pct(offline_count),
            'maintenance_count': maintenance_count, 'maintenance_pct': pct(maintenance_count),
            'issue_count': issue_count, 'issue_pct': pct(issue_count),
        })

        # ---- Closed-lab indicator — which labs are currently outside their
        # opening_time–closing_time (see Lab, set on Manage Labs). Purely
        # informational here (a PC can still be "Available"/"In Use" after
        # hours via Override/Manual Unlock, which are exempt — see
        # scheduling._slot_error) — this just flags it in the PC list/table
        # so an Admin/In-Charge notices at a glance that any activity on a
        # "closed" lab right now is happening outside the normal schedule.
        _now_time = timezone.localtime().time()
        closed_lab_ids = {
            lab.pk for lab in labs_qs
            if not (lab.opening_time <= _now_time <= lab.closing_time)
        }
        ctx['closed_lab_ids'] = closed_lab_ids

        # ---- "Day" lookup mode — which PCs were actually checked in to
        # (unlocked) on a past date, from the login history. PC.last_active
        # gets overwritten by later checks, so it can't answer this. ----
        if day:
            _day_start, _day_end = _local_day_bounds(day)
            logs = ActivityLog.objects.filter(
                action='pc_unlock', pc__isnull=False, created_at__gte=_day_start, created_at__lt=_day_end
            ).select_related('pc', 'pc__lab', 'actor').order_by('pc__lab__name', 'pc__pc_id', 'created_at')
            if lab_id:
                logs = logs.filter(pc__lab_id=lab_id)
            if q:
                logs = logs.filter(
                    Q(pc__pc_id__icontains=q) |
                    Q(target_identifier__icontains=q) |
                    Q(actor__first_name__icontains=q) |
                    Q(actor__last_name__icontains=q) |
                    Q(actor__email__icontains=q)
                )
            labs_usage = {}
            for log in logs:
                labs_usage.setdefault(log.pc.lab, []).append(log)
            ctx['labs_usage'] = [{'lab': lab, 'logs': entries} for lab, entries in labs_usage.items()]
            ctx['total_filtered'] = logs.count()
            return ctx

        # ---- live PC list — filterable + paginated ----
        list_qs = pcs_qs
        if lab_id:
            list_qs = list_qs.filter(lab_id=lab_id)
        if status_filter:
            list_qs = list_qs.filter(status=status_filter)
        if q:
            list_qs = list_qs.filter(
                Q(pc_id__icontains=q) |
                Q(current_user__first_name__icontains=q) |
                Q(current_user__last_name__icontains=q) |
                Q(current_user__id_number__icontains=q) |
                Q(current_user__email__icontains=q)
            )
        list_qs = list_qs.order_by('lab__name', 'pc_id')
        ctx['total_filtered'] = list_qs.count()

        paginator = Paginator(list_qs, self.PAGE_SIZE)
        page_obj = paginator.get_page(self.request.GET.get('page', 1))
        ctx['page_obj'] = page_obj

        # Real "login time" per in-use PC — taken from its most recent
        # pc_unlock activity log entry (PC.last_active alone doesn't tell us
        # when the *current* session started).
        in_use_ids = [pc.pk for pc in page_obj.object_list if pc.status == 'in_use']
        latest_unlocks = {}
        if in_use_ids:
            for log in ActivityLog.objects.filter(action='pc_unlock', pc_id__in=in_use_ids).order_by('pc_id', '-created_at'):
                latest_unlocks.setdefault(log.pc_id, log.created_at)

        # Reservation Type per PC — prefer the actual SessionCheckIn record
        # over pc.current_session. pc.current_session only gets set when
        # there happens to be a Session covering the current lab/time, which
        # an Admin/In-Charge Override often has none of (or, worse, could
        # coincidentally overlap an unrelated Session), so relying on it
        # alone left the Override Check-in row's Reservation Type blank.
        from scheduling.models import SessionCheckIn
        CHECKIN_TYPE_LABELS = {'walk_in': 'Walk-in', 'override': 'Override', 'guest': 'Guest / Walk-in'}
        latest_checkin_types = {}
        if in_use_ids:
            for ci in SessionCheckIn.objects.filter(
                pc_id__in=in_use_ids, checked_out_at__isnull=True
            ).order_by('pc_id', '-checked_in_at'):
                latest_checkin_types.setdefault(ci.pc_id, ci.checkin_type)

        for pc in page_obj.object_list:
            pc.login_time = latest_unlocks.get(pc.pk)
            label = CHECKIN_TYPE_LABELS.get(latest_checkin_types.get(pc.pk))
            if label:
                pc.reservation_type_label = label
            elif pc.current_guest_name:
                pc.reservation_type_label = 'Guest / Walk-in'
            elif pc.current_session:
                pc.reservation_type_label = pc.current_session.get_requester_type_display()
            else:
                pc.reservation_type_label = None

        # ---- live lab usage bars (in-use / total per lab) ----
        usage_labs = labs_qs.filter(pk=lab_id) if lab_id else labs_qs
        lab_usage = []
        for lab in usage_labs:
            lab_total = pcs_qs.filter(lab=lab).count()
            lab_in_use = pcs_qs.filter(lab=lab, status='in_use').count()
            lab_usage.append({
                'lab': lab, 'total': lab_total, 'in_use': lab_in_use,
                'pct': round(lab_in_use / lab_total * 100) if lab_total else 0,
            })
        ctx['lab_usage'] = lab_usage

        # ---- recent activity feed ----
        recent_qs = ActivityLog.objects.filter(action='pc_unlock', pc__isnull=False).select_related('pc', 'actor')
        if lab_id:
            recent_qs = recent_qs.filter(pc__lab_id=lab_id)
        ctx['recent_activities'] = recent_qs.order_by('-created_at')[:8]

        # ---- lab information side panel (only meaningful for one lab) ----
        if lab_id:
            selected_lab_obj = labs_qs.filter(pk=lab_id).first()
            if selected_lab_obj:
                ctx['selected_lab_obj'] = selected_lab_obj
                ctx['selected_lab_total_pcs'] = pcs_qs.filter(lab=selected_lab_obj).count()

        return ctx


# Admin/Lab In-Charge action: manually check a registered account into a
# specific, currently-available PC — the "Override" counterpart to the
# self-service walk-in path in verify_reservation_and_check_in. Used for
# cases the self-service kiosk can't handle on its own (no reservation code
# on hand, PC agent lock-screen unreachable, etc.), while still respecting
# the same "only if the lab has room" rule and leaving the same kind of
# transaction trail (SessionCheckIn + ActivityLog) walk-ins do.
def _modal_aware_lab_redirect(request, view_name, lab_id):
    """Same idea as modal_redirect() in accounts/mixins.py, but for these
    two views' GET target which always carries a `?lab=` query param —
    modal_redirect() itself only supports reverse()'s args/kwargs, not a
    query string. When opened via the app-wide modal (js-modal-link), the
    error/success message set with messages.error()/messages.success()
    just before this is called still gets shown: the JS response handler
    closes the modal and does a real window.location.reload(), and that
    real navigation is what flushes the Django message onto the page."""
    url = f"{reverse(view_name)}?lab={lab_id}"
    if is_modal_request(request):
        return JsonResponse({'success': True, 'redirect': url})
    return redirect(url)


def _finish_override_grant(pc_pk, account_pk, outgoing_session_pk, admin_pk):
    """
    Runs on a daemon background thread a couple seconds after an
    Admin/In-Charge confirmed an Override on a PC that was in use by
    someone else — timed to fire just after the agent's own local
    countdown (see WarningBanner / start_override_countdown in
    lab_pc_agent/agent.py) finishes and the agent re-locks + reports the
    outgoing user's session ended (clearing pc.current_user/
    current_session server-side via pc_agent_end_session_api).

    This is what actually grants the new student/instructor access —
    same mechanics as the immediate-grant path for a free PC, just
    delayed so the outgoing user's warning has time to play out first.
    """
    from django.contrib.auth import get_user_model
    from accounts.models import ActivityLog
    from scheduling.models import Session, SessionCheckIn
    from labs.network import unlock_pc

    User = get_user_model()
    try:
        pc = PC.objects.select_related('lab').get(pk=pc_pk)
        account = User.objects.get(pk=account_pk)
    except (PC.DoesNotExist, User.DoesNotExist):
        return
    admin = User.objects.filter(pk=admin_pk).first()

    # Safety net: if the agent never checked in (offline, unreachable,
    # crashed) the PC may still show the outgoing user here. Don't leave
    # the new student locked out indefinitely just because the on-screen
    # warning/relock didn't complete — proceed with the switch anyway.
    pc.refresh_from_db()

    now = timezone.localtime()
    active_session = Session.objects.filter(
        lab=pc.lab, date=now.date(), start_time__lte=now.time(), end_time__gte=now.time(),
    ).order_by('start_time').first()

    success, detail = unlock_pc(pc)

    id_number = account.id_number or account.display_name
    SessionCheckIn.objects.update_or_create(
        session=active_session, id_number=id_number,
        defaults={'checkin_type': 'override', 'student': account, 'pc': pc},
    )

    pc.status = 'in_use'
    pc.last_active = timezone.now()
    pc.current_user = account
    pc.current_session = active_session
    pc.current_login_via_override = True
    pc.save(update_fields=['status', 'last_active', 'current_user', 'current_session', 'current_login_via_override'])

    ActivityLog.objects.create(
        actor=admin,
        action='pc_unlock',
        target_identifier=id_number,
        pc=pc,
        details=(
            f"Override by {admin.display_name if admin else 'an admin'}: granted {account.display_name} "
            f"({id_number}) access to {pc.pc_id} ({pc.lab.name}) after the warning countdown finished"
            + (f", overlapping reservation {active_session.reservation_code}" if active_session else ", no active reservation")
            + ('.' if success else f' — unlock command did not run: {detail}.')
        )
    )


class OverrideCheckInView(RoleRequiredMixin, ModalFormMixin, TemplateView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/override_checkin.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        labs_qs = Lab.objects.order_by('name')
        if user.role == 'incharge' and user.assigned_lab_id:
            labs_qs = labs_qs.filter(pk=user.assigned_lab_id)
        ctx['all_labs'] = labs_qs
        ctx['selected_lab'] = self.request.GET.get('lab', '')
        if ctx['selected_lab']:
            ctx['available_pcs'] = PC.objects.filter(lab_id=ctx['selected_lab'], status='online').order_by('pc_id')
            # Occupied PCs can ALSO be overridden — the admin/in-charge just
            # gets an extra confirmation step and the current user gets an
            # on-screen warning before the switch happens (see post()).
            ctx['busy_pcs'] = PC.objects.filter(
                lab_id=ctx['selected_lab'], status='in_use'
            ).select_related('current_user').order_by('pc_id')
            ctx['override_warning_seconds'] = getattr(settings, 'OVERRIDE_WARNING_SECONDS', 15)
        return ctx

    def post(self, request, *args, **kwargs):
        from accounts.models import ActivityLog
        from django.contrib.auth import get_user_model
        from labs.network import unlock_pc, send_override_warning
        from scheduling.models import Session, SessionCheckIn
        import threading

        pc_id = request.POST.get('pc', '')
        student_pk = request.POST.get('student_id', '')
        confirmed = request.POST.get('confirm_override') == '1'

        pc = PC.objects.select_related('lab', 'current_user').filter(pk=pc_id).first() if pc_id.isdigit() else None
        if pc is None:
            messages.error(request, 'Select a computer to override into.')
            return _modal_aware_lab_redirect(request, 'override_checkin', request.POST.get('lab', ''))

        User = get_user_model()
        account = User.objects.filter(pk=student_pk, role__in=('student', 'instructor'), is_active=True).first() if student_pk.isdigit() else None
        if account is None:
            messages.error(request, 'Select a registered, active student or instructor account.')
            return _modal_aware_lab_redirect(request, 'override_checkin', pc.lab_id)

        # Re-check occupancy/availability at submit time too — the picker
        # above is just a convenience, this is the real gate (protects
        # against the PC's state changing between page load and submit,
        # or against a stale/forged pc id in the POST body).
        pc.refresh_from_db()

        # OCCUPIED PC: taking it from whoever is currently using it needs an
        # explicit confirmation, a countdown warning shown on that PC's own
        # screen, and only THEN does access actually switch to the new
        # student/instructor — handled by _finish_override_grant() on a
        # delayed background thread, timed to run just after the agent's
        # own local countdown finishes and it re-locks/clears itself.
        if pc.status == 'in_use':
            if not confirmed:
                current_name = pc.current_user.display_name if pc.current_user else (pc.current_guest_name or 'someone')
                messages.error(
                    request,
                    f'{pc.pc_id} is currently in use by {current_name}. Confirm the override to continue — '
                    f'they will see a warning on screen before their session ends.'
                )
                return _modal_aware_lab_redirect(request, 'override_checkin', pc.lab_id)

            seconds = getattr(settings, 'OVERRIDE_WARNING_SECONDS', 15)
            outgoing_user = pc.current_user
            outgoing_session = pc.current_session

            # Record the outgoing user's session as force-ended right now —
            # this is the permanent record of who was using the PC and when
            # their session ended, independent of whether the on-screen
            # warning or the agent's own auto-lock succeeds.
            if outgoing_session is not None and outgoing_user is not None:
                SessionCheckIn.objects.filter(
                    session=outgoing_session,
                    id_number=outgoing_user.id_number or outgoing_user.display_name,
                ).update(checked_out_at=timezone.now(), ended_by=request.user)

            ActivityLog.objects.create(
                actor=request.user,
                action='pc_unlock',
                target_identifier=account.id_number or account.display_name,
                pc=pc,
                details=(
                    f"Override by {request.user.display_name}: {pc.pc_id} ({pc.lab.name}) taken from "
                    f"{outgoing_user.display_name if outgoing_user else (pc.current_guest_name or 'a walk-in guest')} "
                    f"— {seconds}s on-screen warning sent, then access will switch to {account.display_name}."
                )
            )

            warned, warn_detail = send_override_warning(pc, request.user.display_name, seconds)

            def _delayed_grant(pc_pk, account_pk, session_pk, admin_pk, wait_seconds):
                import time
                from django.db import connections
                time.sleep(wait_seconds + 2)  # +2s buffer past the agent's own local countdown
                try:
                    _finish_override_grant(pc_pk, account_pk, session_pk, admin_pk)
                finally:
                    connections.close_all()

            threading.Thread(
                target=_delayed_grant,
                args=(pc.pk, account.pk, outgoing_session.pk if outgoing_session else None, request.user.pk, seconds),
                daemon=True,
            ).start()

            if warned:
                messages.success(
                    request,
                    f'Override started — {pc.pc_id} will switch to {account.display_name} in {seconds} seconds, '
                    f'after a warning is shown on that PC\u2019s screen.'
                )
            else:
                messages.warning(
                    request,
                    f'Override scheduled, but the on-screen warning could not be sent ({warn_detail}). '
                    f'{pc.pc_id} will still switch to {account.display_name} in {seconds} seconds.'
                )
            return _modal_aware_lab_redirect(request, 'override_checkin', pc.lab_id)

        if pc.status != 'online':
            messages.error(request, f'{pc.pc_id} is no longer available — someone else may have just taken it.')
            return _modal_aware_lab_redirect(request, 'override_checkin', pc.lab_id)

        now = timezone.localtime()
        active_session = Session.objects.filter(
            lab=pc.lab, date=now.date(), start_time__lte=now.time(), end_time__gte=now.time(),
        ).order_by('start_time').first()

        success, detail = unlock_pc(pc)

        id_number = account.id_number or account.display_name
        SessionCheckIn.objects.update_or_create(
            session=active_session, id_number=id_number,
            defaults={'checkin_type': 'override', 'student': account, 'pc': pc},
        )

        pc.status = 'in_use'
        pc.last_active = timezone.now()
        pc.current_user = account
        pc.current_session = active_session
        pc.current_login_via_override = True
        pc.save(update_fields=['status', 'last_active', 'current_user', 'current_session', 'current_login_via_override'])

        account_name = account.display_name
        ActivityLog.objects.create(
            actor=request.user,
            action='pc_unlock',
            target_identifier=id_number,
            pc=pc,
            details=(
                f"Override by {request.user.display_name}: granted {account_name} "
                f"({id_number}) access to {pc.pc_id} ({pc.lab.name})"
                + (f", overlapping reservation {active_session.reservation_code}" if active_session else ", no active reservation")
                + ('.' if success else f' — unlock command did not run: {detail}.')
            )
        )

        if success:
            messages.success(request, f'{account_name} was checked into {pc.pc_id} ({pc.lab.name}).')
        else:
            messages.warning(request, f'{account_name} was recorded as checked into {pc.pc_id}, but the unlock command did not run: {detail}')
        return _modal_aware_lab_redirect(request, 'override_checkin', pc.lab_id)


# Lab In-Charge / Admin action: unlock a PC for a first-time walk-in Guest —
# someone with NO registered account in the system at all (as opposed to
# Override Check-in above, which is only for already-registered student/
# instructor accounts). Since there's no account to identify the person by,
# the Lab In-Charge must type in the guest's Full Name before the unlock is
# allowed to go through; that name is what gets recorded in the access log
# (SessionCheckIn + ActivityLog) instead of a student ID.
class ManualUnlockView(RoleRequiredMixin, ModalFormMixin, TemplateView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/manual_unlock.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        labs_qs = Lab.objects.order_by('name')
        if user.role == 'incharge' and user.assigned_lab_id:
            labs_qs = labs_qs.filter(pk=user.assigned_lab_id)
        ctx['all_labs'] = labs_qs
        ctx['selected_lab'] = self.request.GET.get('lab', '')
        if ctx['selected_lab']:
            ctx['available_pcs'] = PC.objects.filter(lab_id=ctx['selected_lab'], status='online').order_by('pc_id')
        return ctx

    def post(self, request, *args, **kwargs):
        from accounts.models import ActivityLog
        from labs.network import unlock_pc
        from scheduling.models import Session, SessionCheckIn

        pc_id = request.POST.get('pc', '')
        guest_name = (request.POST.get('guest_name') or '').strip()

        pc = PC.objects.select_related('lab').filter(pk=pc_id).first() if pc_id.isdigit() else None
        if pc is None:
            messages.error(request, 'Select a computer to unlock.')
            return _modal_aware_lab_redirect(request, 'manual_unlock', request.POST.get('lab', ''))

        if not guest_name:
            messages.error(
                request,
                "Enter the guest's Full Name before unlocking — this is required so the access log can identify who used the computer."
            )
            return _modal_aware_lab_redirect(request, 'manual_unlock', pc.lab_id)

        # Re-check occupancy at submit time too, same reasoning as Override
        # Check-in — the picker above is just a convenience.
        pc.refresh_from_db()
        if pc.status != 'online':
            messages.error(request, f'{pc.pc_id} is no longer available — someone else may have just taken it.')
            return _modal_aware_lab_redirect(request, 'manual_unlock', pc.lab_id)

        now = timezone.localtime()
        active_session = Session.objects.filter(
            lab=pc.lab, date=now.date(), start_time__lte=now.time(), end_time__gte=now.time(),
        ).order_by('start_time').first()

        success, detail = unlock_pc(pc)

        # Guests have no account/ID number, so a stable synthetic one is
        # generated for the SessionCheckIn transaction record — the real,
        # human-readable identifier is guest_name.
        guest_id_number = f"GUEST-{pc.pc_id}-{int(timezone.now().timestamp())}"
        SessionCheckIn.objects.update_or_create(
            session=active_session, id_number=guest_id_number,
            defaults={'checkin_type': 'guest', 'student': None, 'guest_name': guest_name, 'pc': pc},
        )

        pc.status = 'in_use'
        pc.last_active = timezone.now()
        pc.current_user = None
        pc.current_guest_name = guest_name
        pc.current_session = active_session
        pc.current_login_via_override = False
        pc.save(update_fields=['status', 'last_active', 'current_user', 'current_guest_name', 'current_session', 'current_login_via_override'])

        ActivityLog.objects.create(
            actor=request.user,
            action='pc_unlock',
            target_identifier=guest_id_number,
            pc=pc,
            details=(
                f"Manual Unlock by {request.user.display_name}: "
                f"walk-in guest \"{guest_name}\" (no registered account) granted access to {pc.pc_id} ({pc.lab.name})"
                + (f", overlapping reservation {active_session.reservation_code}" if active_session else ", no active reservation")
                + ('.' if success else f' — unlock command did not run: {detail}.')
            )
        )

        if success:
            messages.success(request, f'{guest_name} was checked into {pc.pc_id} ({pc.lab.name}) as a guest.')
        else:
            messages.warning(request, f'{guest_name} was recorded as checked into {pc.pc_id}, but the unlock command did not run: {detail}')
        return _modal_aware_lab_redirect(request, 'manual_unlock', pc.lab_id)


def _pc_activity_app_name(title):
    """Collapses a raw window-title sample down to just the application name,
    e.g. 'index.html - Visual Studio Code' -> 'Visual Studio Code'. Titles
    that don't follow the '<page> - <app>' convention are used as-is.
    Only a fallback for non-browser windows — for browsers, prefer
    _pc_activity_site_label() below, which can use the captured URL."""
    title = (title or '').strip()
    if not title:
        return 'Unknown'
    if ' - ' in title:
        return title.rsplit(' - ', 1)[-1].strip() or title
    return title


def _pc_activity_site_label(title, url):
    """Best label for a raw activity sample: the resolved site ('Chrome —
    ChatGPT') when it's a browser window, otherwise the plain app name."""
    return resolve_site_label(title, url) or _pc_activity_app_name(title)


def _pc_activity_fmt_duration(td, long_form=False):
    """Formats a timedelta as '1h 29m 37s' (default) or, for the session
    detail panel, '1 Hour 29 Minutes 37 Seconds'."""
    if td is None:
        return '—'
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if long_form:
        parts = []
        if hours:
            parts.append(f"{hours} Hour{'s' if hours != 1 else ''}")
        if minutes or hours:
            parts.append(f"{minutes} Minute{'s' if minutes != 1 else ''}")
        parts.append(f"{seconds} Second{'s' if seconds != 1 else ''}")
        return ' '.join(parts)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes} min" if seconds == 0 else f"{minutes}m {seconds}s"
    return f"{seconds} sec"


def _build_pc_sessions(logs, now, gap):
    """Reconstructs login -> app-switch -> logout 'sessions' out of the raw
    window-title samples the lab_pc_agent reports every few seconds. There's
    no explicit login/logout event in this data — a session is inferred as a
    run of consecutive samples for the same PC/student with no gap larger
    than `gap` between them. `logs` must already be ordered by
    ('pc_id', 'captured_at').
    """
    sessions = []
    run = []
    prev_pc_id = prev_student_id = prev_guest_name = prev_time = None

    def flush(run):
        if not run:
            return
        first, last = run[0], run[-1]
        pc, student = first.pc, first.student
        guest_name = first.guest_name

        blocks = []
        bstart = btitle = bend = burl = None
        for log in run:
            if btitle is None:
                bstart = bend = log.captured_at
                btitle = log.window_title
                burl = log.page_url
                continue
            if log.window_title != btitle:
                blocks.append({
                    'title': btitle, 'start': bstart, 'end': bend, 'url': burl,
                    'site_label': _pc_activity_site_label(btitle, burl),
                })
                bstart = log.captured_at
                btitle = log.window_title
                burl = log.page_url
            bend = log.captured_at
        if btitle is not None:
            blocks.append({
                'title': btitle, 'start': bstart, 'end': bend, 'url': burl,
                'site_label': _pc_activity_site_label(btitle, burl),
            })

        is_ongoing = (now - last.captured_at) <= gap
        status = 'active' if is_ongoing else 'completed'
        duration = (now - first.captured_at) if is_ongoing else (last.captured_at - first.captured_at)
        key = f'{pc.pk}-{int(first.captured_at.timestamp())}'

        rows = [{
            'time': first.captured_at, 'student': student, 'guest_name': guest_name, 'pc': pc,
            'activity': 'Logged In', 'application': 'Windows Login',
            'duration': None, 'duration_display': '—', 'status': status, 'session_key': key,
        }]
        for i, b in enumerate(blocks):
            is_last = (i == len(blocks) - 1)
            block_dur = b['end'] - b['start']
            has_dur = block_dur.total_seconds() > 0
            rows.append({
                'time': b['start'], 'student': student, 'guest_name': guest_name, 'pc': pc,
                'activity': 'Opened Application',
                'application': b['title'] or '(Untitled window)',
                'duration': block_dur if has_dur else None,
                'duration_display': _pc_activity_fmt_duration(block_dur) if has_dur else '—',
                'status': 'active' if (is_ongoing and is_last) else 'completed',
                'session_key': key,
            })
        if not is_ongoing:
            rows.append({
                'time': last.captured_at, 'student': student, 'guest_name': guest_name, 'pc': pc,
                'activity': 'Logged Out', 'application': 'Windows Logout',
                'duration': duration, 'duration_display': _pc_activity_fmt_duration(duration),
                'status': 'completed', 'session_key': key,
            })

        sessions.append({
            'key': key, 'pc': pc, 'student': student, 'guest_name': guest_name,
            'login_time': first.captured_at,
            'logout_time': None if is_ongoing else last.captured_at,
            'duration': duration, 'status': status,
            'blocks': blocks, 'rows': rows, 'sample_count': len(run),
            'scheduling_session': first.session,
            'via_override': first.via_override,
            # Raw PCActivityLog rows that make up this one session — kept
            # around so export_pc_activity_log() can build a report scoped
            # to exactly this session (not the whole PC/day) when a
            # 'session' key is passed in. See "View Full Session Report".
            'logs': run,
        })

    for log in logs:
        starts_new_run = (
            prev_pc_id is not None and (
                log.pc_id != prev_pc_id
                or log.student_id != prev_student_id
                or log.guest_name != prev_guest_name
                or (log.captured_at - prev_time) > gap
            )
        )
        if starts_new_run:
            flush(run)
            run = []
        run.append(log)
        prev_pc_id, prev_student_id, prev_guest_name, prev_time = log.pc_id, log.student_id, log.guest_name, log.captured_at
    flush(run)

    return sessions


def _pc_activity_filter_logs(request, base_qs):
    """Applies the shared lab/pc/student/day/time_range/search filters used
    across the PC activity log page, its export, and its delete action."""
    from django.db.models import Q

    lab_id = request.GET.get('lab') or request.POST.get('lab', '')
    pc_id = request.GET.get('pc') or request.POST.get('pc', '')
    student_id = request.GET.get('student') or request.POST.get('student', '')
    day = request.GET.get('day') or request.POST.get('day', '')
    time_range = request.GET.get('time_range') or request.POST.get('time_range', '')
    q = (request.GET.get('q') or request.POST.get('q', '')).strip()

    logs = base_qs
    if lab_id:
        logs = logs.filter(pc__lab_id=lab_id)
    if pc_id:
        logs = logs.filter(pc_id=pc_id)
    if student_id == 'guest':
        logs = logs.filter(student__isnull=True, guest_name__gt='')
    elif student_id == 'override':
        logs = logs.filter(via_override=True)
    elif student_id:
        logs = logs.filter(student_id=student_id)
    if day:
        _day_start, _day_end = _local_day_bounds(day)
        if _day_start:
            logs = logs.filter(captured_at__gte=_day_start, captured_at__lt=_day_end)
    if time_range in ('morning', 'afternoon', 'evening'):
        from django.db.models import F, ExpressionWrapper, DateTimeField
        from django.db.models.functions import ExtractHour
        lo, hi = {'morning': (6, 12), 'afternoon': (12, 18), 'evening': (18, 24)}[time_range]
        # Asia/Manila is a fixed UTC+8 all year (no DST), so instead of
        # asking MySQL to CONVERT_TZ captured_at (stored in UTC) to local
        # time before extracting the hour — which silently returns
        # NULL/matches nothing if the server's named-timezone tables
        # aren't loaded, see _local_day_bounds() above for the same issue
        # on the Date filter — we shift the raw UTC value by the fixed
        # +8h offset ourselves, then extract the hour with tzinfo pinned
        # to UTC so Django does NOT also try to convert it again.
        logs = logs.annotate(
            _local_dt=ExpressionWrapper(F('captured_at') + timedelta(hours=8), output_field=DateTimeField())
        ).annotate(
            _local_hour=ExtractHour('_local_dt', tzinfo=dt_timezone.utc)
        ).filter(_local_hour__gte=lo, _local_hour__lt=hi)
    if q:
        logs = logs.filter(
            Q(window_title__icontains=q) |
            Q(student__first_name__icontains=q) |
            Q(student__last_name__icontains=q) |
            Q(student__id_number__icontains=q) |
            Q(guest_name__icontains=q) |
            Q(pc__pc_id__icontains=q)
        )
    filters = {
        'lab': lab_id, 'pc': pc_id, 'student': student_id,
        'day': day, 'time_range': time_range, 'q': q,
    }
    return logs, filters


# Dashboard page listing the window-title samples collected by
# pc_agent_activity_api — lets admins/in-charges trace what a student was
# doing (per the window titles reported) on a given PC/session. Raw samples
# are reconstructed into login -> app-switch -> logout "sessions" (see
# _build_pc_sessions) to drive the activity timeline, session detail panel,
# and the Most Used Applications / Most Active PCs summaries.
class PCActivityLogView(RoleRequiredMixin, TemplateView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/pc_activity_log.html'

    # A gap this long between two samples on the same PC/student means the
    # PC was locked (or the student switched) in between — used to split
    # the raw sample stream into distinct sessions.
    SESSION_GAP = timedelta(minutes=2)
    PAGE_SIZE = 10

    def get_context_data(self, **kwargs):
        from collections import defaultdict
        from django.contrib.auth import get_user_model

        ctx = super().get_context_data(**kwargs)
        request = self.request
        now = timezone.localtime()
        today = now.date()

        logs, filters = _pc_activity_filter_logs(
            request,
            PCActivityLog.objects.select_related('pc', 'pc__lab', 'student', 'session'),
        )
        logs = logs.order_by('pc_id', 'captured_at')
        sessions = _build_pc_sessions(list(logs[:8000]), now, self.SESSION_GAP)
        sessions.sort(key=lambda s: s['login_time'], reverse=True)

        timeline = [{
            'time': s['login_time'],
            'student': s['student'],
            'guest_name': s['guest_name'],
            'pc': s['pc'],
            'duration_display': _pc_activity_fmt_duration(s['duration']) if s['duration'] else '—',
            'status': s['status'],
            'session_key': s['key'],
        } for s in sessions]

        try:
            page = int(request.GET.get('page', '1'))
        except ValueError:
            page = 1
        total_entries = len(timeline)
        total_pages = max(1, -(-total_entries // self.PAGE_SIZE))
        page = min(max(page, 1), total_pages)
        page_rows = timeline[(page - 1) * self.PAGE_SIZE: page * self.PAGE_SIZE]

        selected_key = request.GET.get('session', '')
        selected = next((s for s in sessions if s['key'] == selected_key), None) or (sessions[0] if sessions else None)

        # ---- Stat cards: read straight off live system state / today's
        # totals rather than the current filter, so they read like a
        # steady dashboard summary instead of jumping around as filters
        # are applied to the table below. ----
        _today_start, _today_end = _local_day_bounds(today)
        today_sessions = _build_pc_sessions(
            list(PCActivityLog.objects.select_related('pc', 'student')
                 .filter(captured_at__gte=_today_start, captured_at__lt=_today_end).order_by('pc_id', 'captured_at')),
            now, self.SESSION_GAP,
        )
        durations_today = [s['duration'] for s in today_sessions if s['duration']]
        avg_duration = (sum(durations_today, timedelta()) / len(durations_today)) if durations_today else None

        total_pcs = PC.objects.count()
        active_pcs_now = PC.objects.filter(status='in_use').count()
        active_students_now = PC.objects.filter(
            status='in_use', current_user__isnull=False
        ).values('current_user').distinct().count()

        ctx['stats'] = {
            'total_sessions_today': len(today_sessions),
            'active_students_now': active_students_now,
            'total_logs_today': PCActivityLog.objects.filter(captured_at__gte=_today_start, captured_at__lt=_today_end).count(),
            'avg_session_duration': _pc_activity_fmt_duration(avg_duration) if avg_duration else '—',
            'active_pcs_now': active_pcs_now,
            'pc_utilization_pct': round(active_pcs_now / total_pcs * 100) if total_pcs else 0,
        }

        # ---- Most Used Applications (today) ----
        app_totals = defaultdict(timedelta)
        for s in today_sessions:
            for b in s['blocks']:
                dur = b['end'] - b['start']
                if dur.total_seconds() == 0:
                    dur = timedelta(seconds=8)  # ~ one report interval
                app_totals[b['site_label']] += dur
        ranked_apps = sorted(app_totals.items(), key=lambda kv: kv[1], reverse=True)
        total_app_time = sum(app_totals.values(), timedelta()) or timedelta(seconds=1)
        palette = ['#a78bfa', '#f2a93b', '#3ed6c4', '#5b9dff', '#ff5d5d']
        most_used_apps = []
        for i, (name, dur) in enumerate(ranked_apps[:5]):
            most_used_apps.append({
                'name': name, 'duration': _pc_activity_fmt_duration(dur),
                'pct': round(dur / total_app_time * 100), 'color': palette[i % len(palette)],
                'raw_seconds': dur.total_seconds(),
            })
        others = sum((d for _, d in ranked_apps[5:]), timedelta())
        if others.total_seconds() > 0:
            most_used_apps.append({
                'name': 'Others', 'duration': _pc_activity_fmt_duration(others),
                'pct': round(others / total_app_time * 100), 'color': '#4d5761',
                'raw_seconds': others.total_seconds(),
            })
        ctx['most_used_apps'] = most_used_apps

        # ---- Most Active PCs (today) ----
        pc_totals = defaultdict(timedelta)
        for s in today_sessions:
            pc_totals[s['pc']] += s['duration'] or timedelta()
        ranked_pcs = sorted(pc_totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
        max_pc_dur = ranked_pcs[0][1] if ranked_pcs else timedelta(seconds=1)
        ctx['most_active_pcs'] = [
            {'pc': pc, 'duration': _pc_activity_fmt_duration(dur),
             'pct': round(dur / max_pc_dur * 100) if max_pc_dur else 0}
            for pc, dur in ranked_pcs
        ]

        ctx['timeline'] = page_rows
        ctx['selected_session'] = selected
        ctx['selected_session_blocks_recent_first'] = (
            list(reversed(selected['blocks'])) if selected else []
        )
        ctx['selected_session_duration_long'] = (
            _pc_activity_fmt_duration(selected['duration'], long_form=True) if selected else None
        )
        ctx['page'] = page
        ctx['total_pages'] = total_pages
        ctx['total_entries'] = total_entries
        ctx['page_start'] = 0 if not total_entries else (page - 1) * self.PAGE_SIZE + 1
        ctx['page_end'] = min(page * self.PAGE_SIZE, total_entries)
        ctx['page_range'] = range(1, total_pages + 1)

        ctx['all_labs'] = Lab.objects.order_by('name')
        ctx['all_pcs'] = PC.objects.select_related('lab').order_by('lab__name', 'pc_id')
        ctx['all_students'] = get_user_model().objects.order_by('first_name', 'last_name')
        ctx['selected_lab'] = filters['lab']
        ctx['selected_pc'] = filters['pc']
        ctx['selected_student'] = filters['student']
        ctx['selected_day'] = filters['day']
        ctx['selected_time_range'] = filters['time_range']
        ctx['search_query'] = filters['q']
        ctx['filtered'] = any(filters.values())
        ctx['total_count'] = PCActivityLog.objects.count()
        return ctx



def delete_pc_activity_log(request):
    """Manually deletes PCActivityLog rows — same filters (lab/pc/student/
    day/time_range/q) as the on-screen view, so 'delete filtered' matches
    what's actually shown. Leaving every filter blank deletes EVERYTHING
    (confirmed client-side via compulabConfirmSubmit before this is ever
    hit)."""
    from labs.reports import _require_report_access

    _require_report_access(request)

    if request.method != 'POST':
        return redirect('pc_activity_log')

    logs, _filters = _pc_activity_filter_logs(request, PCActivityLog.objects.all())

    count, _ = logs.delete()
    messages.success(request, f'Deleted {count} activity log sample{"s" if count != 1 else ""}.')

    # Bounce back to the same filtered view the delete was triggered from,
    # preserving view=grouped/flat too.
    params = request.POST.get('return_qs', '')
    url = reverse('pc_activity_log')
    return redirect(f'{url}?{params}' if params else url)


def export_pc_activity_log(request):
    """Exports PC activity as PDF or Excel. Two modes:

    - Session-scoped (?session=<key>): used by the Session Details panel's
      "View Full Session Report"/"Export Session" buttons. Rebuilds that
      ONE login->logout session (same grouping _build_pc_sessions uses for
      the on-screen panel) and exports only its rows — matches exactly
      what's shown in Session Details / Window & Application Activity,
      not the whole PC's whole day.
    - Filtered (no 'session' param): the original behavior — exports
      the currently filtered PCActivityLog view (lab/pc/student/day/
      time_range/q), same filters as PCActivityLogView but the full
      filtered set rather than just the on-screen page. Handy for keeping
      a copy before the daily auto-wipe deletes everything.
    """
    from labs.reports import _require_report_access, _export_pdf, _export_excel

    _require_report_access(request)

    fmt = request.GET.get('format', 'pdf')
    session_key = request.GET.get('session', '').strip()

    if session_key:
        try:
            pc_pk_str, ts_str = session_key.rsplit('-', 1)
            pc_pk = int(pc_pk_str)
            login_dt = datetime.fromtimestamp(int(ts_str), tz=dt_timezone.utc)
        except (ValueError, TypeError):
            pc_pk = None

        session = None
        if pc_pk is not None:
            login_local_date = timezone.localtime(login_dt).date()
            _day_start, _day_end = _local_day_bounds(login_local_date)
            day_logs = list(
                PCActivityLog.objects.select_related('pc', 'pc__lab', 'student', 'session')
                .filter(pc_id=pc_pk, captured_at__gte=_day_start, captured_at__lt=_day_end)
                .order_by('captured_at')
            )
            sessions = _build_pc_sessions(day_logs, timezone.localtime(), PCActivityLogView.SESSION_GAP)
            session = next((s for s in sessions if s['key'] == session_key), None)

        if session is None:
            messages.error(request, "That session couldn't be found — it may have been auto-wiped already.")
            return redirect('pc_activity_log')

        headers = ['Time', 'PC', 'User', 'Role', 'Session', 'Active window title']
        rows = [
            [
                log.captured_at.strftime('%Y-%m-%d %H:%M:%S'),
                f'{log.pc.lab.name} — {log.pc.pc_id}',
                log.student.display_name if log.student else (log.guest_name or '—'),
                log.student.get_role_display() if log.student else ('Guest' if log.guest_name else '—'),
                f'{log.session.requester_name} ({log.session.date} {log.session.start_time}-{log.session.end_time})' if log.session else '—',
                log.window_title or '—',
            ]
            for log in session['logs']
        ]

        who = session['student'].display_name if session['student'] else (session['guest_name'] or 'Guest')
        period = f"{who} — {session['pc'].pc_id} — {timezone.localtime(session['login_time']).strftime('%Y-%m-%d %H:%M')}"
        if fmt == 'excel':
            return _export_excel('PC Session Report', period, headers, rows, 'pc_session_report.xlsx')
        return _export_pdf('PC Session Report', period, headers, rows, 'pc_session_report.pdf')

    logs, filters = _pc_activity_filter_logs(
        request,
        PCActivityLog.objects.select_related('pc', 'pc__lab', 'student', 'session'),
    )
    logs = logs.order_by('-captured_at')

    headers = ['Time', 'PC', 'User', 'Role', 'Session', 'Active window title']
    rows = [
        [
            log.captured_at.strftime('%Y-%m-%d %H:%M:%S'),
            f'{log.pc.lab.name} — {log.pc.pc_id}',
            log.student.display_name if log.student else (log.guest_name or '—'),
            log.student.get_role_display() if log.student else ('Guest' if log.guest_name else '—'),
            f'{log.session.requester_name} ({log.session.date} {log.session.start_time}-{log.session.end_time})' if log.session else '—',
            log.window_title or '—',
        ]
        for log in logs
    ]

    day = filters['day']
    period = day if day else ('All time' if not filters['pc'] and not filters['q'] else 'Filtered')
    if fmt == 'excel':
        return _export_excel('PC Activity Log', period, headers, rows, 'pc_activity_log.xlsx')
    return _export_pdf('PC Activity Log', period, headers, rows, 'pc_activity_log.pdf')


# API endpoint — returns live PC status as JSON for auto-refresh
def pc_status_api(request):
    pcs = PC.objects.select_related('current_user').values(
        'pc_id', 'status', 'lab__name', 'last_active',
        'current_user__id_number', 'current_user__first_name', 'current_user__last_name',
        'current_guest_name',
    )
    return JsonResponse({'pcs': list(pcs)})


# update a single PC's status (admin/incharge action)
class PCUpdateView(RoleRequiredMixin, ModalFormMixin, UpdateView):
    allowed_roles = ['admin', 'incharge']
    model = PC
    fields = ['status']
    template_name = 'labs/pc_form.html'

    def form_valid(self, form):
        previous_status = PC.objects.get(pk=self.object.pk).status
        # This form only exposes the `status` field, but current_user/
        # current_session/current_guest_name are meaningless once status
        # is manually changed away from 'in_use' — leaving them behind was
        # exactly why the "Current User" column (and other places that
        # check current_user/current_guest_name without also checking
        # status) kept showing someone's name even after an Admin/In-Charge
        # had already corrected the status here.
        if previous_status == 'in_use' and form.instance.status != 'in_use':
            form.instance.current_user = None
            form.instance.current_session = None
            form.instance.current_guest_name = ''
        response = super().form_valid(form)
        if self.object.status in ('maintenance', 'issue') and previous_status not in ('maintenance', 'issue'):
            notify_service.notify_pc_maintenance(self.object, self.object.get_status_display())
        return response

    def get_success_url(self):
        return '/labs/pc-status/'


# actually pings every PC that has an IP address on file and updates their
# real status — this is the "talk to the PCs" action
@login_required
def refresh_pc_status_view(request):
    if request.user.role not in ['admin', 'incharge']:
        raise PermissionDenied
    pcs = PC.objects.exclude(ip_address__isnull=True)
    if request.user.role == 'incharge' and request.user.assigned_lab:
        pcs = pcs.filter(lab=request.user.assigned_lab)
    summary = refresh_pc_statuses(pcs)
    total_with_ip = pcs.count()
    if total_with_ip == 0:
        note = "No PCs have an IP address on file yet — add one in each PC's settings so its real status can be checked."
        if is_modal_request(request):
            return JsonResponse({'ok': True, 'checked': 0, 'online': 0, 'offline': 0, 'message': note, 'level': 'warning'})
        messages.warning(request, note)
    else:
        note = f"Checked {summary['checked']} PC(s): {summary['online']} online, {summary['offline']} offline."
        if is_modal_request(request):
            return JsonResponse({
                'ok': True, 'checked': summary['checked'], 'online': summary['online'],
                'offline': summary['offline'], 'message': note, 'level': 'success',
            })
        messages.success(request, note)
    return redirect(request.META.get('HTTP_REFERER', 'pc_status'))


def pc_force_release(request, pk):
    """
    Immediate, manual version of labs.network.release_expired_pc_sessions()
    — for when Admin/In-Charge can see a PC is obviously stuck showing
    "in use by <name>" (agent crashed, PC lost power, etc.) and don't want
    to wait for the background safety net's grace period, and don't want
    to go through Override (which requires picking a new account to grant
    access to). This just clears the tracking fields outright: no warning
    is sent to the PC's screen, because unlike Override there's no new
    user waiting to take over — we're just correcting stale server-side
    bookkeeping, not actually kicking anyone off a PC they're using.
    """
    if request.method != 'POST':
        raise PermissionDenied
    if request.user.role not in ['admin', 'incharge']:
        raise PermissionDenied

    from accounts.models import ActivityLog

    pc = get_object_or_404(PC.objects.select_related('lab', 'current_user'), pk=pk)
    if request.user.role == 'incharge' and request.user.assigned_lab_id != pc.lab_id:
        raise PermissionDenied

    if pc.status != 'in_use' and not pc.current_user_id and not pc.current_guest_name:
        messages.info(request, f'{pc.pc_id} is not currently marked in use — nothing to release.')
        return redirect(request.META.get('HTTP_REFERER', 'pc_status'))

    outgoing_name = pc.current_user.display_name if pc.current_user else (pc.current_guest_name or 'someone')

    ActivityLog.objects.create(
        actor=request.user,
        action='pc_lock',
        target_identifier=pc.current_user.id_number if pc.current_user else '',
        pc=pc,
        details=(
            f"{pc.pc_id} ({pc.lab.name}) force-released by {request.user.display_name}: "
            f"was stuck showing \"in use by {outgoing_name}\" — cleared manually rather than "
            f"waiting for the automatic stale-session check."
        ),
    )

    pc.status = 'online'
    pc.current_user = None
    pc.current_session = None
    pc.current_guest_name = ''
    pc.save(update_fields=['status', 'current_user', 'current_session', 'current_guest_name'])

    messages.success(request, f'{pc.pc_id} released — was stuck showing "in use by {outgoing_name}".')
    return redirect(request.META.get('HTTP_REFERER', 'pc_status'))


def _pc_power_action(request, pk, action):
    """
    Shared handler for pc_shutdown()/pc_restart()/pc_lock() — sends a
    remote power/lock signal to the PC's agent (see labs/network.py) and
    logs it, same permission/lab-scoping rules as pc_force_release above.
    Fire-and-forget from the server's point of view: for shutdown/restart
    the PC still takes ~30s to actually act after the agent gets the
    signal; lock is near-instant.
    """
    if request.method != 'POST':
        raise PermissionDenied
    if request.user.role not in ['admin', 'incharge']:
        raise PermissionDenied

    from accounts.models import ActivityLog
    from labs.network import shutdown_pc, restart_pc, lock_pc

    pc = get_object_or_404(PC.objects.select_related('lab', 'current_user'), pk=pk)
    if request.user.role == 'incharge' and request.user.assigned_lab_id != pc.lab_id:
        raise PermissionDenied

    send_signal = {'shutdown': shutdown_pc, 'restart': restart_pc, 'lock': lock_pc}[action]
    action_log_name = {'shutdown': 'pc_shutdown', 'restart': 'pc_restart', 'lock': 'pc_lock'}[action]
    success, detail = send_signal(pc)

    ActivityLog.objects.create(
        actor=request.user,
        action=action_log_name,
        target_identifier=pc.current_user.id_number if pc.current_user else (pc.current_guest_name or ''),
        pc=pc,
        details=(
            f"{pc.pc_id} ({pc.lab.name}) remote {action} triggered by {request.user.display_name}."
            + ('' if success else f' Signal did not go through: {detail}')
        ),
    )

    if success:
        if action == 'lock':
            messages.success(request, f'{pc.pc_id} was locked.')
        else:
            verb = 'shut down' if action == 'shutdown' else 'restart'
            messages.success(request, f'{pc.pc_id} will {verb} in about 30 seconds.')
    else:
        messages.error(request, f"Couldn't {action} {pc.pc_id}: {detail}")

    return redirect(request.META.get('HTTP_REFERER', 'pc_status'))


def pc_bulk_power_action(request):
    """
    Bulk version of pc_shutdown/pc_restart/lock — runs the same per-PC
    signal across every PC id submitted from the checkboxes on the PC
    Status page (see the pc-select-checkbox / "Select All" JS in
    pc_status.html), so Admin/In-Charge can shut down, restart, or lock a
    whole lab — or any hand-picked subset — in one action instead of
    clicking each PC one at a time. Skips any PC without an IP address on
    file (nothing to send the signal to) and, for In-Charge, any PC
    outside their assigned lab.
    """
    if request.method != 'POST':
        raise PermissionDenied
    if request.user.role not in ['admin', 'incharge']:
        raise PermissionDenied

    from accounts.models import ActivityLog
    from labs.network import shutdown_pc, restart_pc, lock_pc

    action = request.POST.get('action', '')
    if action not in ('shutdown', 'restart', 'lock'):
        messages.error(request, 'Unknown bulk power action.')
        return redirect(request.META.get('HTTP_REFERER', 'pc_status'))

    raw_ids = request.POST.get('pc_ids', '')
    pc_ids = [int(x) for x in raw_ids.split(',') if x.strip().isdigit()]
    if not pc_ids:
        messages.error(request, 'No PCs were selected.')
        return redirect(request.META.get('HTTP_REFERER', 'pc_status'))

    pcs = PC.objects.select_related('lab', 'current_user').filter(pk__in=pc_ids)
    if request.user.role == 'incharge':
        pcs = pcs.filter(lab_id=request.user.assigned_lab_id)

    send_signal = {'shutdown': shutdown_pc, 'restart': restart_pc, 'lock': lock_pc}[action]
    action_log_name = {'shutdown': 'pc_shutdown', 'restart': 'pc_restart', 'lock': 'pc_lock'}[action]
    verb = {'shutdown': 'shut down', 'restart': 'restart', 'lock': 'lock'}[action]

    ok_count = 0
    skipped_count = 0
    for pc in pcs:
        if not pc.ip_address:
            skipped_count += 1
            continue
        success, detail = send_signal(pc)
        ActivityLog.objects.create(
            actor=request.user,
            action=action_log_name,
            target_identifier=pc.current_user.id_number if pc.current_user else (pc.current_guest_name or ''),
            pc=pc,
            details=(
                f"{pc.pc_id} ({pc.lab.name}) bulk {action} triggered by {request.user.display_name}."
                + ('' if success else f' Signal did not go through: {detail}')
            ),
        )
        if success:
            ok_count += 1
        else:
            skipped_count += 1

    if ok_count:
        messages.success(request, f'Sent {verb} signal to {ok_count} PC(s).')
    if skipped_count:
        messages.warning(request, f'{skipped_count} PC(s) were skipped (no IP on file, offline, or agent unreachable).')

    return redirect(request.META.get('HTTP_REFERER', 'pc_status'))


def pc_shutdown(request, pk):
    return _pc_power_action(request, pk, 'shutdown')


def pc_restart(request, pk):
    return _pc_power_action(request, pk, 'restart')


def pc_lock(request, pk):
    return _pc_power_action(request, pk, 'lock')


# ---- Lab management (create/edit/delete — no more hardcoded/admin-only setup) ----
class LabListView(RoleRequiredMixin, ListView):
    allowed_roles = ['admin']
    template_name = 'labs/lab_list.html'
    context_object_name = 'labs'

    def get_queryset(self):
        qs = Lab.objects.prefetch_related('pcs', 'assigned_users').order_by('name')
        lab_id = self.request.GET.get('lab', '')
        status = self.request.GET.get('status', '')
        q = self.request.GET.get('q', '').strip()

        if lab_id:
            qs = qs.filter(pk=lab_id)
        if status:
            qs = qs.filter(pcs__status=status).distinct()
        if q:
            from django.db.models import Q
            qs = qs.filter(
                Q(name__icontains=q) | Q(location__icontains=q) |
                Q(assigned_users__first_name__icontains=q) |
                Q(assigned_users__last_name__icontains=q)
            ).distinct()
        return qs

    def get_context_data(self, **kwargs):
        from accounts.models import ActivityLog
        ctx = super().get_context_data(**kwargs)

        ctx['selected_lab'] = self.request.GET.get('lab', '')
        ctx['selected_status'] = self.request.GET.get('status', '')
        ctx['search_query'] = self.request.GET.get('q', '').strip()
        ctx['filtered'] = bool(ctx['selected_lab'] or ctx['selected_status'] or ctx['search_query'])
        ctx['all_labs'] = Lab.objects.order_by('name')
        ctx['status_choices'] = PC.STATUS

        # ---- per-lab breakdown used by each card ----
        lab_cards = []
        for lab in ctx['labs']:
            pcs = list(lab.pcs.all())
            total = len(pcs)
            available = sum(1 for p in pcs if p.status == 'online')
            in_use = sum(1 for p in pcs if p.status == 'in_use')
            offline = sum(1 for p in pcs if p.status == 'offline')
            maintenance = sum(1 for p in pcs if p.status == 'maintenance')
            online = total - offline
            incharge = next((u for u in lab.assigned_users.all() if u.role == 'incharge'), None)
            lab_cards.append({
                'lab': lab, 'total': total, 'online': online, 'available': available,
                'in_use': in_use, 'offline': offline, 'maintenance': maintenance,
                'incharge': incharge,
                'utilization_pct': round(in_use / total * 100) if total else 0,
            })
        ctx['lab_cards'] = lab_cards

        # ---- system-wide top metric cards (unfiltered, across every lab) ----
        all_pcs = PC.objects.all()
        ctx['total_labs'] = Lab.objects.count()
        ctx['total_computers'] = all_pcs.count()
        ctx['available_count'] = all_pcs.filter(status='online').count()
        ctx['in_use_count'] = all_pcs.filter(status='in_use').count()
        ctx['maintenance_count'] = all_pcs.filter(status='maintenance').count()
        ctx['offline_count'] = all_pcs.filter(status='offline').count()

        def pct(part):
            return round(part / ctx['total_computers'] * 100, 2) if ctx['total_computers'] else 0
        ctx['available_pct'] = pct(ctx['available_count'])
        ctx['in_use_pct'] = pct(ctx['in_use_count'])
        ctx['maintenance_pct'] = pct(ctx['maintenance_count'])
        ctx['offline_pct'] = pct(ctx['offline_count'])

        # ---- today's lab overview — utilization bars, same metric as the
        # per-card one but for every lab regardless of the current filter ----
        overview = []
        for lab in ctx['all_labs']:
            lab_total = lab.pcs.count()
            lab_in_use = lab.pcs.filter(status='in_use').count()
            overview.append({
                'lab': lab, 'pct': round(lab_in_use / lab_total * 100) if lab_total else 0,
            })
        ctx['lab_overview'] = overview

        ctx['recent_activities'] = ActivityLog.objects.select_related('actor', 'pc', 'pc__lab').order_by('-created_at')[:6]

        # ---- flag PCs sharing one IP address ----
        # Every pc-agent-* endpoint identifies "which PC is this?" by
        # matching the request's IP against PC.ip_address and taking the
        # first row found. If two PC records share an IP, the same one of
        # them always wins that lookup — from the other PC's point of
        # view, the lock screen ends up showing the wrong pc_id.
        from django.db.models import Count
        dupe_ips = (
            PC.objects.exclude(ip_address__isnull=True).exclude(ip_address='')
            .values('ip_address').annotate(n=Count('id')).filter(n__gt=1)
            .values_list('ip_address', flat=True)
        )
        ctx['duplicate_ip_pcs'] = (
            PC.objects.filter(ip_address__in=list(dupe_ips)).select_related('lab').order_by('ip_address', 'pc_id')
            if dupe_ips else []
        )

        return ctx


class LabCreateView(RoleRequiredMixin, ModalFormMixin, CreateView):
    allowed_roles = ['admin']
    model = Lab
    form_class = LabForm
    template_name = 'labs/lab_form.html'
    success_url = reverse_lazy('lab_list')

    def form_valid(self, form):
        messages.success(self.request, f'Lab "{form.instance.name}" created.')
        return super().form_valid(form)


class LabUpdateView(RoleRequiredMixin, ModalFormMixin, UpdateView):
    allowed_roles = ['admin']
    model = Lab
    form_class = LabForm
    template_name = 'labs/lab_form.html'
    success_url = reverse_lazy('lab_list')

    def form_valid(self, form):
        messages.success(self.request, f'Lab "{form.instance.name}" updated.')
        return super().form_valid(form)


class LabDeleteView(RoleRequiredMixin, DeleteView):
    allowed_roles = ['admin']
    model = Lab
    template_name = 'labs/lab_confirm_delete.html'
    success_url = reverse_lazy('lab_list')
    context_object_name = 'lab'

    def form_valid(self, form):
        name = self.object.name
        response = super().form_valid(form)
        messages.success(self.request, f'Lab "{name}" deleted.')
        return response


# ---- PC management (create/edit/delete, including the IP address used for real status checks) ----
class PCCreateView(RoleRequiredMixin, ModalFormMixin, CreateView):
    allowed_roles = ['admin']
    model = PC
    form_class = PCForm
    template_name = 'labs/pc_edit_form.html'
    success_url = reverse_lazy('lab_list')

    def get_initial(self):
        initial = super().get_initial()
        lab_id = self.request.GET.get('lab')
        if lab_id:
            initial['lab'] = lab_id
        return initial

    def form_valid(self, form):
        messages.success(self.request, f'PC "{form.instance.pc_id}" added.')
        return super().form_valid(form)


def pc_import_template(request):
    """Downloadable starter CSV for PCImportView — matches the exact
    headers/order that import expects (see PCImportView.form_valid's
    row.get('lab')/row.get('pc id')/row.get('ip address')/row.get('status')
    lookups), pre-filled with a couple of example rows so it's obvious
    what belongs in each column without having to read the rules list."""
    from django.http import HttpResponse
    import csv

    if not request.user.is_authenticated or request.user.role != 'admin':
        raise PermissionDenied

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="pc_import_template.csv"'
    writer = csv.writer(response)
    writer.writerow(['Lab', 'PC ID', 'IP Address', 'Status'])
    writer.writerow(['Lab 01', 'PC01', '192.168.1.11', 'Available'])
    writer.writerow(['Lab 01', 'PC02', '192.168.1.12', ''])
    return response


class PCImportView(RoleRequiredMixin, FormView):
    """
    Bulk-add PCs from an uploaded CSV or Excel file instead of the one-by-one
    'Add PC' form. Expected columns (header row, any order): Lab, PC ID,
    IP Address, Status (optional — defaults to Offline).
    Rows are validated the same way the single PCForm would validate them
    (IP Address required, loopback IP rejected, lab must already exist,
    duplicate PC ID within the same lab is skipped) and the whole thing
    runs in one DB transaction so a bad file never leaves a half-imported
    mess behind.
    """
    allowed_roles = ['admin']
    form_class = PCImportForm
    template_name = 'labs/pc_import.html'

    def form_valid(self, form):
        import csv
        import io
        import ipaddress as ipaddress_mod
        from django.db import transaction

        uploaded = form.cleaned_data['file']
        status_labels = {label.lower(): key for key, label in PC.STATUS}
        status_keys = dict(PC.STATUS)

        rows = []
        if uploaded.name.lower().endswith('.csv'):
            text = uploaded.read().decode('utf-8-sig', errors='replace')
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                rows.append({(k or '').strip().lower(): (v or '').strip() for k, v in row.items()})
        else:
            import openpyxl
            wb = openpyxl.load_workbook(uploaded, data_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            headers = [str(h or '').strip().lower() for h in next(rows_iter, [])]
            for raw in rows_iter:
                if not any(raw):
                    continue
                rows.append({headers[i]: ('' if raw[i] is None else str(raw[i]).strip())
                             for i in range(len(headers)) if i < len(raw)})

        labs_by_name = {lab.name.strip().lower(): lab for lab in Lab.objects.all()}
        existing_pairs = {(pc.lab_id, pc.pc_id.strip().lower()) for pc in PC.objects.all()}

        created, errors = [], []
        to_create = []
        seen_in_file = set()

        for i, row in enumerate(rows, start=2):  # row 1 is the header
            lab_name = row.get('lab', '')
            pc_id = row.get('pc id') or row.get('pc_id') or row.get('id', '')
            ip_address = row.get('ip address') or row.get('ip_address') or row.get('ip', '')
            status_raw = (row.get('status', '') or '').strip()

            if not lab_name or not pc_id:
                errors.append(f'Row {i}: missing Lab or PC ID — skipped.')
                continue

            if not ip_address:
                errors.append(f'Row {i}: missing IP Address — skipped. (Required for Shutdown/Restart/Lock and automatic online/offline checks.)')
                continue

            lab = labs_by_name.get(lab_name.strip().lower())
            if not lab:
                errors.append(f'Row {i}: lab "{lab_name}" does not exist — skipped.')
                continue

            key = (lab.pk, pc_id.strip().lower())
            if key in existing_pairs or key in seen_in_file:
                errors.append(f'Row {i}: "{pc_id}" already exists in {lab.name} — skipped.')
                continue

            try:
                if ipaddress_mod.ip_address(ip_address).is_loopback:
                    errors.append(f'Row {i}: "{ip_address}" is a loopback address — skipped.')
                    continue
            except ValueError:
                errors.append(f'Row {i}: "{ip_address}" is not a valid IP address — skipped.')
                continue

            status = status_keys.get(status_raw) or status_labels.get(status_raw.lower()) or 'offline'

            to_create.append(PC(lab=lab, pc_id=pc_id, ip_address=ip_address, status=status))
            seen_in_file.add(key)
            created.append(f'{pc_id} → {lab.name}')

        with transaction.atomic():
            PC.objects.bulk_create(to_create)

        if created:
            messages.success(self.request, f'Imported {len(created)} PC{"s" if len(created) != 1 else ""}.')
        if errors:
            messages.warning(self.request, f'{len(errors)} row{"s had" if len(errors) != 1 else " had"} issues — see details below.')

        return self.render_to_response(self.get_context_data(form=self.form_class(), results={'created': created, 'errors': errors}))


class PCEditView(RoleRequiredMixin, ModalFormMixin, UpdateView):
    allowed_roles = ['admin']
    model = PC
    form_class = PCForm
    template_name = 'labs/pc_edit_form.html'
    success_url = reverse_lazy('lab_list')

    def form_valid(self, form):
        # PCForm exposes `status` alongside lab/pc_id/ip_address, so this
        # form can change status away from 'in_use' too — current_user/
        # current_session/current_guest_name are meaningless once that
        # happens. Same guardrail as PCUpdateView (the "Update" button);
        # without it here too, the "Edit" button could leave a stale name
        # on the Current User column even after the status was corrected.
        previous_status = PC.objects.get(pk=self.object.pk).status
        if previous_status == 'in_use' and form.instance.status != 'in_use':
            form.instance.current_user = None
            form.instance.current_session = None
            form.instance.current_guest_name = ''
        messages.success(self.request, f'PC "{form.instance.pc_id}" updated.')
        return super().form_valid(form)


class PCDeleteView(RoleRequiredMixin, ModalFormMixin, DeleteView):
    allowed_roles = ['admin']
    model = PC
    template_name = 'labs/pc_confirm_delete.html'
    success_url = reverse_lazy('lab_list')
    context_object_name = 'pc'

    def form_valid(self, form):
        pc_id = self.object.pc_id
        response = super().form_valid(form)
        messages.success(self.request, f'PC "{pc_id}" removed.')
        return response


# ---- Inventory management (create/edit/delete) ----
class InventoryCreateView(RoleRequiredMixin, ModalFormMixin, CreateView):
    allowed_roles = ['admin']
    model = InventoryItem
    form_class = InventoryItemForm
    template_name = 'labs/inventory_form.html'
    success_url = reverse_lazy('inventory')

    def form_valid(self, form):
        messages.success(self.request, f'"{form.instance.name}" added to inventory.')
        return super().form_valid(form)


class InventoryUpdateView(RoleRequiredMixin, ModalFormMixin, UpdateView):
    allowed_roles = ['admin']
    model = InventoryItem
    form_class = InventoryItemForm
    template_name = 'labs/inventory_form.html'
    success_url = reverse_lazy('inventory')

    def form_valid(self, form):
        messages.success(self.request, f'"{form.instance.name}" updated.')
        return super().form_valid(form)


class InventoryDeleteView(RoleRequiredMixin, DeleteView):
    allowed_roles = ['admin']
    model = InventoryItem
    template_name = 'labs/inventory_confirm_delete.html'
    success_url = reverse_lazy('inventory')
    context_object_name = 'item'

    def form_valid(self, form):
        name = self.object.name
        response = super().form_valid(form)
        messages.success(self.request, f'"{name}" removed from inventory.')
        return response


# admin bulk-removes one or more checked items from the Inventory
# dashboard's "Delete Selected" toolbar — same admin-only rule as
# InventoryDeleteView above, just applied to however many rows were checked.
def inventory_bulk_delete(request):
    if not request.user.is_authenticated or request.user.role != 'admin':
        raise PermissionDenied
    return bulk_delete(request, InventoryItem.objects.all(), 'inventory', item_label='inventory item')


# inventory list — stat cards, search/filter bar, table, and the bottom
# category/condition breakdown + low stock + recent activity panels.
class InventoryView(RoleRequiredMixin, ListView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/inventory.html'
    context_object_name = 'items'
    paginate_by = 10

    # equipment at or below this quantity shows up in the Low Stock panel
    LOW_STOCK_THRESHOLD = 3

    def get_queryset(self):
        qs = InventoryItem.objects.select_related('lab').order_by('lab__name', 'category', 'name')

        self.search = self.request.GET.get('q', '').strip()
        self.lab_id = self.request.GET.get('lab', '').strip()
        self.category = self.request.GET.get('category', '').strip()
        self.condition = self.request.GET.get('condition', '').strip()
        self.status = self.request.GET.get('status', '').strip()

        if self.search:
            qs = qs.filter(name__icontains=self.search)
        if self.lab_id:
            qs = qs.filter(lab_id=self.lab_id)
        if self.category:
            qs = qs.filter(category=self.category)
        if self.condition:
            qs = qs.filter(condition=self.condition)
        if self.status:
            qs = qs.filter(status=self.status)
        return qs

    def get_context_data(self, **kwargs):
        from django.db.models import Count, F, Sum

        ctx = super().get_context_data(**kwargs)
        all_items = InventoryItem.objects.all()

        total_items = all_items.count()
        working = all_items.filter(status='operational').count()
        needs_check = all_items.filter(condition='needs_check').count()
        faulty = all_items.filter(condition='faulty').count()
        under_repair = all_items.filter(status='under_repair').count()

        def pct(n):
            return round(n / total_items * 100, 2) if total_items else 0

        ctx.update({
            'total_items': total_items,
            'working_count': working, 'working_pct': pct(working),
            'needs_check_count': needs_check, 'needs_check_pct': pct(needs_check),
            'faulty_count': faulty, 'faulty_pct': pct(faulty),
            'under_repair_count': under_repair, 'under_repair_pct': pct(under_repair),
        })

        # --- search/filter bar options + currently selected values ---
        ctx['all_labs'] = Lab.objects.order_by('name')
        ctx['all_categories'] = (
            all_items.exclude(category='').values_list('category', flat=True)
            .order_by('category').distinct()
        )
        ctx['condition_choices'] = InventoryItem.CONDITION
        ctx['status_choices'] = InventoryItem.STATUS
        ctx['search_q'] = self.search
        ctx['selected_lab'] = self.lab_id
        ctx['selected_category'] = self.category
        ctx['selected_condition'] = self.condition
        ctx['selected_status'] = self.status
        ctx['filters_active'] = any([self.search, self.lab_id, self.category, self.condition, self.status])

        # --- "Equipment by Category" donut ---
        palette = ['#5b9dff', '#a78bfa', '#3ed6c4', '#f2a93b', '#ff5d5d', '#8b98a3']
        category_breakdown = list(
            all_items.exclude(category='').values('category').annotate(n=Count('id')).order_by('-n')
        )
        for i, row in enumerate(category_breakdown):
            row['pct'] = pct(row['n'])
            row['color'] = palette[i % len(palette)]
        ctx['category_breakdown'] = category_breakdown

        # --- "Equipment Condition" donut (reuses the stat-card numbers) ---
        ctx['condition_breakdown'] = [
            {'label': 'Working', 'n': working, 'pct': pct(working), 'color': '#3ed6c4'},
            {'label': 'Needs Check', 'n': needs_check, 'pct': pct(needs_check), 'color': '#f2a93b'},
            {'label': 'Faulty', 'n': faulty, 'pct': pct(faulty), 'color': '#ff5d5d'},
            {'label': 'Under Repair', 'n': under_repair, 'pct': pct(under_repair), 'color': '#a78bfa'},
        ]

        # --- low stock alert ---
        # A quantity by itself is meaningless if the units behind it are
        # Faulty/Needs Check or already pulled for repair — those aren't
        # available stock, "repair" solves that, not "restock", and it's
        # already tracked by the Faulty/Under Repair counts above. Low
        # Stock should only ever be about USABLE quantity: Good/Fair
        # condition AND Operational status. Grouping by name+lab also
        # matters — the same equipment name often exists as more than one
        # row (a healthy batch and a separate Faulty batch of the same
        # item), and alerting on one broken-out row while a large healthy
        # one sits right next to it is a false alarm.
        usable_items = all_items.filter(condition__in=['good', 'fair'], status='operational')
        low_stock_groups = (
            usable_items.values('name', 'lab_id', 'lab__name')
            .annotate(total_quantity=Sum('quantity'))
            .filter(total_quantity__lte=self.LOW_STOCK_THRESHOLD)
            .order_by('total_quantity')[:5]
        )
        ctx['low_stock_items'] = [
            {'name': g['name'], 'lab_name': g['lab__name'], 'quantity': g['total_quantity']}
            for g in low_stock_groups
        ]

        # --- recent activity (merged feed: added, updated, repaired, reported) ---
        activity = []
        for item in all_items.select_related('lab').order_by('-created_at')[:5]:
            if item.created_at:
                activity.append({'title': f'{item.name} added to {item.lab.name}', 'time': item.created_at})
        for item in all_items.select_related('lab').filter(updated_at__gt=F('created_at')).order_by('-updated_at')[:5]:
            if item.updated_at:
                activity.append({'title': f'{item.name} updated in {item.lab.name}', 'time': item.updated_at})
        for log in MaintenanceLog.objects.filter(completed=True).select_related('equipment', 'equipment__lab').order_by('-completed_at')[:5]:
            if log.completed_at:
                activity.append({'title': f'{log.equipment.name} repaired in {log.equipment.lab.name}', 'time': log.completed_at})
        for issue in EquipmentIssue.objects.select_related('equipment', 'equipment__lab').order_by('-date_reported')[:5]:
            activity.append({'title': f'{issue.equipment.name} reported as faulty in {issue.equipment.lab.name}', 'time': issue.date_reported})
        activity.sort(key=lambda a: a['time'], reverse=True)
        ctx['recent_activity'] = activity[:6]

        return ctx


# analytics — Reporting & Analytics overview. Shared by admin (sees every
# lab) and lab incharge (forced to their own lab) — same layout, scoped data.
class AnalyticsView(RoleRequiredMixin, TemplateView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/analytics.html'

    def get_context_data(self, **kwargs):
        from issues.models import Issue
        from labs.reports import _session_hours, _scoped_lab_id, _visible_labs
        from scheduling.models import Session, SessionCheckIn
        from labs.models import ReportLog
        from django.contrib.auth import get_user_model
        from django.db.models import Q
        from django.utils import timezone
        from datetime import timedelta, date as date_cls, datetime
        import calendar

        User = get_user_model()
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        today = timezone.now().date()

        # --- date range filter (preset dropdown, or custom date_from/date_to) ---
        range_key = self.request.GET.get('range', 'this_month')
        if range_key == 'custom' and self.request.GET.get('date_from') and self.request.GET.get('date_to'):
            try:
                date_from = datetime.strptime(self.request.GET['date_from'], '%Y-%m-%d').date()
                date_to = datetime.strptime(self.request.GET['date_to'], '%Y-%m-%d').date()
            except ValueError:
                range_key = 'this_month'
                date_from = date_to = None
        else:
            date_from = date_to = None

        if date_from is None:
            if range_key == 'last_month':
                last_day_prev = today.replace(day=1) - timedelta(days=1)
                date_from, date_to = last_day_prev.replace(day=1), last_day_prev
            elif range_key == 'this_week':
                date_from, date_to = today - timedelta(days=today.weekday()), today
            elif range_key == 'last_7_days':
                date_from, date_to = today - timedelta(days=6), today
            else:
                range_key = 'this_month'
                date_from, date_to = today.replace(day=1), today

        ctx['range_key'] = range_key
        ctx['date_from'] = date_from
        ctx['date_to'] = date_to

        # --- lab / instructor / status filters ---
        lab_id = _scoped_lab_id(self.request, self.request.GET.get('lab', ''))
        instructor_id = self.request.GET.get('instructor', '')
        status_filter = self.request.GET.get('status', '')

        ctx['selected_lab'] = lab_id
        ctx['selected_instructor'] = instructor_id
        ctx['selected_status'] = status_filter
        ctx['is_incharge'] = (user.role == 'incharge')
        ctx['all_labs'] = _visible_labs(self.request)
        ctx['instructors'] = User.objects.filter(role='instructor').order_by('first_name', 'last_name')
        ctx['status_choices'] = Issue.STATUS

        labs_qs = Lab.objects.filter(pk=lab_id) if lab_id else Lab.objects.all()
        labs_qs = labs_qs.order_by('name')

        # --- top metric cards ---
        pcs_qs = PC.objects.filter(lab__in=labs_qs)
        ctx['total_labs'] = labs_qs.count()
        ctx['total_pcs'] = pcs_qs.count()

        sessions_qs = Session.objects.filter(
            lab__in=labs_qs, date__gte=date_from, date__lte=date_to
        ).select_related('lab', 'instructor')
        if instructor_id:
            sessions_qs = sessions_qs.filter(instructor_id=instructor_id)
        sessions_list = list(sessions_qs)
        ctx['total_reservations'] = len(sessions_list)

        expected = sum(s.student_count for s in sessions_list)
        checked_in = SessionCheckIn.objects.filter(session__in=sessions_list).values('id_number').distinct().count() if sessions_list else 0
        ctx['attendance_rate'] = round(checked_in / expected * 100) if expected else 0

        issues_qs = Issue.objects.filter(lab__in=labs_qs)
        ctx['maintenance_issues_pending'] = issues_qs.filter(status='open').count()

        # --- lab utilization (bars + "most used laboratories") ---
        num_days = (date_to - date_from).days + 1
        utilization_rows = []
        for lab in labs_qs:
            lab_sessions = [s for s in sessions_list if s.lab_id == lab.id]
            total_hours = sum(_session_hours(s) for s in lab_sessions)
            open_hours = (
                datetime.combine(date_cls.min, lab.closing_time) -
                datetime.combine(date_cls.min, lab.opening_time)
            ).total_seconds() / 3600
            capacity_hours = open_hours * num_days
            pct = round(total_hours / capacity_hours * 100) if capacity_hours > 0 else 0
            utilization_rows.append({'lab': lab, 'sessions': len(lab_sessions), 'pct': min(pct, 100)})
        utilization_rows.sort(key=lambda r: r['pct'], reverse=True)
        ctx['utilization_rows'] = utilization_rows
        ctx['lab_utilization_avg'] = (
            round(sum(r['pct'] for r in utilization_rows) / len(utilization_rows))
            if utilization_rows else 0
        )

        # --- equipment status donut: PC health, not inventory items, since
        # the "Total PCs" metric above is what this chart's total matches ---
        working = pcs_qs.filter(status__in=['online', 'in_use']).count()
        maintenance = pcs_qs.filter(status='maintenance').count()
        damaged = pcs_qs.filter(status__in=['offline', 'issue']).count()
        total_for_donut = max(working + maintenance + damaged, 1)
        ctx['equipment_status'] = {
            'working': working, 'maintenance': maintenance, 'damaged': damaged,
            'working_pct': round(working / total_for_donut * 100),
            'maintenance_pct': round(maintenance / total_for_donut * 100),
            'damaged_pct': round(damaged / total_for_donut * 100),
        }

        # --- attendance trend: weekly buckets across the selected range ---
        week_buckets = []
        cursor = date_from
        bucket_index = 1
        while cursor <= date_to and bucket_index <= 6:
            bucket_end = min(cursor + timedelta(days=6), date_to)
            bucket_sessions = [s for s in sessions_list if cursor <= s.date <= bucket_end]
            bucket_expected = sum(s.student_count for s in bucket_sessions)
            bucket_checked_in = SessionCheckIn.objects.filter(
                session__in=bucket_sessions
            ).values('id_number').distinct().count() if bucket_sessions else 0
            week_buckets.append({
                'label': f'Week {bucket_index}',
                'pct': round(bucket_checked_in / bucket_expected * 100) if bucket_expected else 0,
            })
            cursor += timedelta(days=7)
            bucket_index += 1
        ctx['attendance_trend'] = week_buckets

        # --- reservations trend: last 6 calendar months, independent of the
        # date-range filter (a short-term filter shouldn't erase the trend) ---
        trend = []
        # build the last 6 (year, month) pairs ending at the current month
        pairs = []
        y, m = today.year, today.month
        for _ in range(6):
            pairs.append((y, m))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        pairs.reverse()
        for y, m in pairs:
            count = Session.objects.filter(lab__in=labs_qs, date__year=y, date__month=m).count()
            trend.append({'label': calendar.month_abbr[m], 'count': count})
        ctx['reservations_trend'] = trend

        # --- top active instructors (this range) ---
        by_instructor = {}
        for s in sessions_list:
            if not s.instructor_id:
                continue
            entry = by_instructor.setdefault(s.instructor, {'sessions': 0})
            entry['sessions'] += 1
        top_instructors = sorted(
            ({'instructor': i, 'sessions': v['sessions']} for i, v in by_instructor.items()),
            key=lambda r: r['sessions'], reverse=True
        )[:5]
        max_sessions = max((r['sessions'] for r in top_instructors), default=1)
        for r in top_instructors:
            r['bar_pct'] = round(r['sessions'] / max_sessions * 100) if max_sessions else 0
        ctx['top_instructors'] = top_instructors

        # --- maintenance summary ---
        ctx['maintenance_summary'] = {
            'pending': issues_qs.filter(status='open').count(),
            'resolved': issues_qs.filter(status='resolved').count(),
            'under_repair': InventoryItem.objects.filter(lab__in=labs_qs, status='under_repair').count(),
            'total': issues_qs.count(),
        }

        # --- issue breakdown by type, honoring the Status filter ---
        from django.db.models import Count
        breakdown_qs = issues_qs
        if status_filter:
            breakdown_qs = breakdown_qs.filter(status=status_filter)
        ctx['issue_breakdown'] = breakdown_qs.values('issue_type').annotate(
            count=Count('id')
        ).order_by('-count')

        # --- recent generated reports ---
        recent_reports = ReportLog.objects.select_related('generated_by', 'lab')
        if user.role == 'incharge' and user.assigned_lab_id:
            recent_reports = recent_reports.filter(
                Q(lab_id=user.assigned_lab_id) | Q(generated_by=user)
            )
        ctx['recent_reports'] = recent_reports.order_by('-generated_at')[:8]

        return ctx


# Admin sees every notification in the system (per spec: "Admin: View all
# notifications"). Lab In-Charge only sees what notifications/services.py
# actually addressed to them — which is already scoped to their own
# assigned lab, so no extra lab filter is needed here.
class AlertsView(RoleRequiredMixin, ListView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/alerts.html'
    context_object_name = 'alerts'

    def get_base_queryset(self):
        from notifications.models import Notification
        if self.request.user.role == 'admin':
            return Notification.objects.all()
        return Notification.objects.filter(user=self.request.user)

    def get_queryset(self):
        from notifications.filters import apply_filters
        return apply_filters(self.get_base_queryset(), self.request)

    def get_context_data(self, **kwargs):
        from notifications.filters import stat_counts, CATEGORY_CHOICES
        from notifications.models import Notification
        ctx = super().get_context_data(**kwargs)
        ctx['stats'] = stat_counts(self.get_base_queryset())
        # "Unread" must reflect only notifications addressed to THIS user —
        # for Admin, get_base_queryset() above returns every user's
        # notifications (for visibility/moderation), but Admin can only
        # ever mark their own as read (see mark_read/mark_all_read in
        # notifications/views.py). Counting everyone else's unread here
        # made the number impossible to ever clear via "Mark all as read".
        ctx['stats']['unread'] = Notification.objects.filter(user=self.request.user, read=False).count()
        ctx['category_choices'] = CATEGORY_CHOICES
        ctx['selected_category'] = self.request.GET.get('category', 'all')
        ctx['q'] = self.request.GET.get('q', '')
        ctx['sort'] = self.request.GET.get('sort', 'latest')
        return ctx


# equipment health for lab in-charge
class EquipmentView(RoleRequiredMixin, TemplateView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/equipment.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['labs'] = Lab.objects.prefetch_related('pcs').all()
        ctx['items'] = InventoryItem.objects.select_related('lab').all()
        ctx['total_pcs'] = PC.objects.count()
        ctx['online_pcs'] = PC.objects.filter(status='online').count()
        return ctx


# ---------------------------------------------------------------------------
# Equipment (inventory) management: view details, status, maintenance,
# repair history, and equipment-specific issue reporting.
# ---------------------------------------------------------------------------

class InventoryDetailView(RoleRequiredMixin, ModalDetailMixin, TemplateView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/inventory_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        item = get_object_or_404(InventoryItem, pk=kwargs['pk'])
        ctx['item'] = item
        ctx['maintenance_logs'] = item.maintenance_logs.select_related('assigned_technician').order_by('-maintenance_date')[:10]
        ctx['open_issues'] = item.issues.filter(status='open').order_by('-date_reported')
        return ctx


class InventoryStatusListView(RoleRequiredMixin, ListView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/inventory_status_list.html'
    context_object_name = 'items'

    def get_queryset(self):
        return InventoryItem.objects.select_related('lab').order_by('lab__name', 'name')


def inventory_status_update(request, pk):
    if not request.user.is_authenticated or request.user.role not in ('admin', 'incharge'):
        raise PermissionDenied
    item = get_object_or_404(InventoryItem, pk=pk)
    redirect_target = 'inventory_detail' if request.POST.get('next') == 'detail' else 'inventory_status_list'
    redirect_args = [item.pk] if redirect_target == 'inventory_detail' else []
    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status in dict(InventoryItem.STATUS):
            was_already_retired = (item.status == 'retired')
            item.status = new_status
            update_fields = ['status']
            # Retirement traceability — stamped the moment status BECOMES
            # Retired; a re-submit while it's already Retired (nothing
            # actually changed) leaves the original timestamp/who/reason
            # alone instead of overwriting them. Un-retiring (choosing a
            # different status afterward) clears the trail, since it no
            # longer describes the item's current state.
            if new_status == 'retired' and not was_already_retired:
                from django.utils import timezone
                item.retired_at = timezone.now()
                item.retired_by = request.user
                item.retirement_reason = (request.POST.get('reason') or '').strip()
                update_fields += ['retired_at', 'retired_by', 'retirement_reason']
            elif new_status != 'retired' and was_already_retired:
                item.retired_at = None
                item.retired_by = None
                item.retirement_reason = ''
                update_fields += ['retired_at', 'retired_by', 'retirement_reason']
            item.save(update_fields=update_fields)
            messages.success(request, f'Status updated for "{item.name}".')
        return modal_redirect(request, redirect_target, *redirect_args)
    return modal_redirect(request, redirect_target, *redirect_args)


class MaintenanceLogListView(RoleRequiredMixin, ListView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/maintenance_logs.html'
    context_object_name = 'logs'

    def get_queryset(self):
        return MaintenanceLog.objects.select_related('equipment', 'assigned_technician').order_by('completed', '-maintenance_date')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['equipment_list'] = InventoryItem.objects.all().order_by('name')
        ctx['pending_count'] = MaintenanceLog.objects.filter(completed=False).count()
        return ctx


class MaintenanceScheduleCreateView(RoleRequiredMixin, ModalFormMixin, CreateView):
    allowed_roles = ['admin']
    model = MaintenanceLog
    form_class = MaintenanceScheduleForm
    template_name = 'labs/maintenance_schedule_form.html'
    success_url = reverse_lazy('maintenance_logs')

    def form_valid(self, form):
        response = super().form_valid(form)
        notify_service.notify_maintenance_submitted(self.object)
        messages.success(self.request, 'Schedule created.')
        return response


def maintenance_complete(request, pk):
    if request.user.role not in ['admin', 'incharge']:
        raise PermissionDenied
    log = get_object_or_404(MaintenanceLog, pk=pk)
    if request.method == 'POST':
        log.completion_notes = request.POST.get('completion_notes', '')
        log.completed = True
        log.completed_at = timezone.now()
        log.save(update_fields=['completion_notes', 'completed', 'completed_at'])
        notify_service.notify_maintenance_completed(log)
        messages.success(request, 'Maintenance marked as completed.')
        return redirect('maintenance_logs')
    return render(request, 'labs/maintenance_complete_form.html', {'log': log})


class RepairHistoryView(RoleRequiredMixin, ListView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/repair_history.html'
    context_object_name = 'logs'

    def get_queryset(self):
        qs = MaintenanceLog.objects.select_related('equipment', 'assigned_technician').filter(completed=True)
        equipment_id = self.request.GET.get('equipment')
        technician_id = self.request.GET.get('technician')
        date_from = self.request.GET.get('date_from')
        date_to = self.request.GET.get('date_to')
        status = self.request.GET.get('status')

        if equipment_id:
            qs = qs.filter(equipment_id=equipment_id)
        if technician_id:
            qs = qs.filter(assigned_technician_id=technician_id)
        if date_from:
            qs = qs.filter(maintenance_date__gte=date_from)
        if date_to:
            qs = qs.filter(maintenance_date__lte=date_to)
        if status == 'pending':
            qs = MaintenanceLog.objects.none()  # this view only ever shows completed repairs
        return qs.order_by('-completed_at')

    def get_context_data(self, **kwargs):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        ctx = super().get_context_data(**kwargs)
        ctx['equipment_list'] = InventoryItem.objects.all().order_by('name')
        ctx['technicians'] = User.objects.filter(role__in=['admin', 'incharge'])
        ctx['selected_equipment'] = self.request.GET.get('equipment', '')
        ctx['selected_technician'] = self.request.GET.get('technician', '')
        ctx['date_from'] = self.request.GET.get('date_from', '')
        ctx['date_to'] = self.request.GET.get('date_to', '')
        return ctx


class EquipmentIssueListView(RoleRequiredMixin, ListView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/equipment_issues.html'
    context_object_name = 'equipment_issues'

    def get_queryset(self):
        return EquipmentIssue.objects.select_related('equipment', 'reporter').order_by('-date_reported')


class EquipmentIssueCreateView(RoleRequiredMixin, ModalFormMixin, CreateView):
    allowed_roles = ['admin', 'incharge']
    model = EquipmentIssue
    fields = ['equipment', 'description']
    template_name = 'labs/equipment_issue_form.html'
    success_url = reverse_lazy('equipment_issues')

    def get_initial(self):
        initial = super().get_initial()
        equipment_id = self.request.GET.get('equipment')
        if equipment_id:
            initial['equipment'] = equipment_id
        return initial

    def form_valid(self, form):
        form.instance.reporter = self.request.user
        response = super().form_valid(form)
        notify_service.notify_equipment_issue_reported(self.object)
        messages.success(self.request, 'Issue reported.')
        return response


class EquipmentIssueDetailView(RoleRequiredMixin, ModalDetailMixin, TemplateView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'labs/equipment_issue_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['equipment_issue'] = get_object_or_404(EquipmentIssue, pk=kwargs['pk'])
        return ctx


def equipment_issue_resolve(request, pk):
    if request.user.role not in ['admin', 'incharge']:
        raise PermissionDenied
    equipment_issue = get_object_or_404(EquipmentIssue, pk=pk)
    if request.method == 'POST':
        equipment_issue.resolution_notes = request.POST.get('resolution_notes', '')
        equipment_issue.status = 'resolved'
        equipment_issue.resolved_at = timezone.now()
        equipment_issue.save(update_fields=['resolution_notes', 'status', 'resolved_at'])
        notify_service.notify_equipment_issue_resolved(equipment_issue)
        messages.success(request, 'Resolution saved.')
    return modal_redirect(request, 'equipment_issues')