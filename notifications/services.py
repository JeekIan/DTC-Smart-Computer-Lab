"""
Central place that creates AUTO-GENERATED notifications for every system
event listed in the Notifications & Alerts spec (new/approved/rejected
reservations, session reminders, PC online/offline, maintenance, walk-in/
override approvals, failed logins, reservation conflicts).

Views/signals should call the small `notify_*` functions below instead of
calling Notification.objects.create() directly, so that:
  1. every auto notification respects the Admin's Alert Settings toggles
  2. every auto notification lands on the right people (Admins always get
     critical/system-wide events; a Lab In-Charge only gets events for
     their OWN assigned lab; an Instructor only gets events tied to their
     own reservations/sessions)
"""
from django.contrib.auth import get_user_model
from notifications.models import Notification, AlertSettings
from django.core.mail import send_mail, EmailMultiAlternatives
from django.conf import settings
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse

User = get_user_model()

# Which AlertSettings toggle gates each auto notification_type.
SETTING_FIELD_BY_TYPE = {
    'reservation_submitted': 'reservation_notifications',
    'reservation_approved': 'reservation_notifications',
    'reservation_declined': 'reservation_notifications',
    'reservation_conflict': 'reservation_notifications',
    'walkin_override_approved': 'reservation_notifications',
    'roster_session_scheduled': 'reservation_notifications',
    'roster_submitted': 'reservation_notifications',
    'roster_approved': 'reservation_notifications',
    'roster_rejected': 'reservation_notifications',
    'roster_student_added': 'reservation_notifications',
    'session_reminder': 'session_reminders',
    'pc_offline': 'pc_status_alerts',
    'pc_online': 'pc_status_alerts',
    'pc_maintenance': 'maintenance_alerts',
    'maintenance_submitted': 'maintenance_alerts',
    'maintenance_completed': 'maintenance_alerts',
    'login_security_alert': 'login_security_alerts',
}


def _event_enabled(notification_type):
    field = SETTING_FIELD_BY_TYPE.get(notification_type)
    if not field:
        return True  # manual/composed types are never gated
    return getattr(AlertSettings.get_solo(), field)


def admins():
    return User.objects.filter(is_active=True, role='admin')


def incharge_for_lab(lab):
    if lab is None:
        return User.objects.none()
    return User.objects.filter(is_active=True, role='incharge', assigned_lab=lab)


def notify(users, title, message, notification_type, lab=None, send_plain_email=True):
    if not _event_enabled(notification_type):
        return []
    seen = set()
    created = []
    for user in users:
        if user is None or user.pk in seen:
            continue
        seen.add(user.pk)
        notification = Notification.objects.create(
            user=user, title=title, message=message,
            notification_type=notification_type, lab=lab, is_system_alert=True,
        )
        created.append(notification)

        from cloud_sync.models import enqueue
        enqueue('notification', notification.id, {
            'user_id': notification.user_id,
            'title': notification.title,
            'message': notification.message,
            'notification_type': notification.notification_type,
            'lab_id': notification.lab_id,
            'created_at': notification.created_at.isoformat(),
        })

        # --- BAGONG PARTE: magpadala rin ng totoong email ---
        # (skipped when the caller is sending its own styled email instead,
        # e.g. the reservation-code emails below)
        if send_plain_email and user.email:
            try:
                send_mail(
                    subject=title,
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    fail_silently=True,
                )
            except Exception:
                pass  # huwag hayaang mapatigil ang notification kung mag-fail ang email
        # -----------------------------------------------------

    return created



def _send_reservation_code_email(user, title, intro, subject, lab, date, time_label, code):
    """Sends the branded 'reservation code' email (approval or roster
    upcoming-session reminder). Separate from notify()'s plain send_mail
    step because this one needs structured context (subject/lab/date/
    time/code laid out in a card) rather than a single message string.
    Silently no-ops for users without an email on file (e.g. students)."""
    if not user or not user.email:
        return
    logo_url = f'{settings.SITE_URL}{static("img/logo.jpg")}'
    context = {
        'user': user, 'title': title, 'intro': intro, 'subject': subject,
        'lab': lab, 'date': date, 'time_label': time_label, 'code': code,
        'logo_url': logo_url,
    }
    text_body = render_to_string('emails/reservation_code.txt', context)
    html_body = render_to_string('emails/reservation_code.html', context)
    try:
        message = EmailMultiAlternatives(
            subject=title, body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL, to=[user.email],
        )
        message.attach_alternative(html_body, 'text/html')
        message.send(fail_silently=True)
    except Exception:
        pass  # never let an email hiccup block the notification itself


def _lab_recipients(lab, include_admins=True):
    recipients = list(incharge_for_lab(lab))
    if include_admins:
        recipients += list(admins())
    return recipients


# ---------------------------------------------------------------- #
# Reservations
# ---------------------------------------------------------------- #

def notify_reservation_submitted(session_request):
    req = session_request
    notify(
        _lab_recipients(req.lab),
        'New reservation request',
        f'{req.requester_name or "A requester"} submitted a request for {req.subject} '
        f'in {req.lab.name} on {req.date} ({req.start_time.strftime("%H:%M")}–{req.end_time.strftime("%H:%M")}).',
        'reservation_submitted', lab=req.lab,
    )


def notify_reservation_approved(session_request, code):
    req = session_request
    recipients = []
    if req.instructor:
        recipients.append(req.instructor)
    notify(
        recipients,
        'Lab request approved',
        f'Your request for {req.subject} on {req.date} has been approved. Reservation code: {code}',
        'reservation_approved', lab=req.lab, send_plain_email=False,
    )
    if _event_enabled('reservation_approved'):
        _send_reservation_code_email(
            req.instructor, 'Lab request approved',
            'Your reservation request has been approved.',
            req.subject, req.lab.name, req.date,
            f'{req.start_time.strftime("%H:%M")}–{req.end_time.strftime("%H:%M")}',
            code,
        )


def notify_reservation_declined(session_request):
    req = session_request
    recipients = []
    if req.instructor:
        recipients.append(req.instructor)
    notify(
        recipients,
        'Lab request declined',
        f'Your request for {req.subject} on {req.date} was declined.',
        'reservation_declined', lab=req.lab,
    )


def notify_walkin_override_approved(session_request, code):
    req = session_request
    label = 'Walk-in' if req.requester_type == 'walk_in' else 'Override'
    notify(
        _lab_recipients(req.lab),
        f'{label} request approved',
        f'{label} access for {req.requester_name or "a requester"} in {req.lab.name} on {req.date} '
        f'({req.start_time.strftime("%H:%M")}–{req.end_time.strftime("%H:%M")}) was approved. Code: {code}',
        'walkin_override_approved', lab=req.lab,
    )


def notify_roster_session_scheduled(session, code):
    """Sent 1 hour before a roster-auto-generated Session starts (see
    notifications.reminders.check_roster_code_reminders) — this is how the
    instructor learns the reservation code for that specific date, since
    they never filed an individual request for it. Every enrolled student
    on that roster with a Gmail on file gets the same reservation code
    emailed to them directly too (no in-app Notification for them, since
    Students never log into the dashboard to see one)."""
    recipients = []
    if session.instructor:
        recipients.append(session.instructor)
    notify(
        recipients,
        'Upcoming class session — reservation code',
        f'{session.subject} in {session.lab.name} starts at {session.start_time.strftime("%H:%M")} '
        f'today ({session.date}), from your class roster schedule. Reservation code: {code}',
        'roster_session_scheduled', lab=session.lab, send_plain_email=False,
    )
    if _event_enabled('roster_session_scheduled'):
        _send_reservation_code_email(
            session.instructor, 'Upcoming class session — reservation code',
            'This class starts soon — from your class roster schedule.',
            session.subject, session.lab.name, session.date,
            session.start_time.strftime("%H:%M"),
            code,
        )
        if session.roster_id:
            roster_students = session.roster.students.exclude(student__isnull=True).select_related('student')
            for roster_student in roster_students:
                student = roster_student.student
                if student and student.email:
                    _send_reservation_code_email(
                        student, 'Upcoming class session — reservation code',
                        'This class starts soon — from your class roster schedule.',
                        session.subject, session.lab.name, session.date,
                        session.start_time.strftime("%H:%M"),
                        code,
                    )


def notify_roster_submitted(roster):
    """Sent to the relevant Lab In-Charge (if the roster already names a
    lab) plus all Admins whenever a class roster reaches/re-reaches
    'pending' — i.e. a new roster from an Instructor, or a Rejected one
    that's just been edited and resubmitted."""
    notify(
        _lab_recipients(roster.lab),
        'Class roster pending approval',
        f'{roster.instructor.get_full_name() if roster.instructor else "An instructor"} submitted '
        f'"{roster.name}" for approval.',
        'roster_submitted', lab=roster.lab,
    )


def notify_roster_approved(roster):
    recipients = []
    if roster.instructor:
        recipients.append(roster.instructor)
    notify(
        recipients,
        'Class roster approved',
        f'Your roster "{roster.name}" has been approved. Its schedule has been added to the official calendar.',
        'roster_approved', lab=roster.lab,
    )


def notify_roster_rejected(roster):
    recipients = []
    if roster.instructor:
        recipients.append(roster.instructor)
    message = f'Your roster "{roster.name}" was rejected.'
    if roster.rejection_reason:
        message += f' Reason: {roster.rejection_reason}'
    message += ' You can edit and resubmit it.'
    notify(
        recipients,
        'Class roster rejected',
        message,
        'roster_rejected', lab=roster.lab,
    )


def notify_student_added_to_roster(roster, student):
    """Sent the moment a Student gets added to a Class Roster (from the
    search-and-add box or the bulk Import Students flow) — lets them know
    which class they're now enrolled in, and its lab/schedule, ahead of
    the per-session reservation-code reminder (see
    notify_roster_session_scheduled). No-ops quietly for a student with
    no Gmail on file, same as every other auto notification here."""
    lab_name = roster.lab.name if roster.lab else 'a lab'
    schedule = f' Schedule: {roster.schedule}.' if roster.schedule else ''
    notify(
        [student],
        'Added to a class roster',
        f'You were added to "{roster.name}" in {lab_name}'
        f'{f" by {roster.instructor.get_full_name()}" if roster.instructor else ""}.'
        f'{schedule} Reservation codes for upcoming sessions will be emailed here '
        f'about an hour before each class.',
        'roster_student_added', lab=roster.lab,
    )


def notify_reservation_conflict(lab, description):
    notify(
        _lab_recipients(lab),
        'Reservation conflict detected',
        description,
        'reservation_conflict', lab=lab,
    )


def notify_session_starting_soon(session):
    recipients = list(incharge_for_lab(session.lab))
    if session.instructor:
        recipients.append(session.instructor)
    notify(
        recipients,
        'Lab session starting soon',
        f'{session.subject} in {session.lab.name} starts at {session.start_time.strftime("%H:%M")} today.',
        'session_reminder', lab=session.lab,
    )


# ---------------------------------------------------------------- #
# PCs
# ---------------------------------------------------------------- #

def notify_pc_offline(pc):
    notify(
        _lab_recipients(pc.lab),
        f'{pc.pc_id} is offline',
        f'The computer {pc.pc_id} in {pc.lab.name} has gone offline unexpectedly.',
        'pc_offline', lab=pc.lab,
    )


def notify_pc_online(pc):
    notify(
        _lab_recipients(pc.lab),
        f'{pc.pc_id} is back online',
        f'The computer {pc.pc_id} in {pc.lab.name} is back online.',
        'pc_online', lab=pc.lab,
    )


def notify_pc_maintenance(pc, reason=''):
    detail = f' ({reason})' if reason else ''
    notify(
        _lab_recipients(pc.lab),
        f'{pc.pc_id} needs maintenance',
        f'{pc.pc_id} in {pc.lab.name} was flagged for maintenance{detail}.',
        'pc_maintenance', lab=pc.lab,
    )


# ---------------------------------------------------------------- #
# Maintenance / equipment
# ---------------------------------------------------------------- #

def notify_maintenance_submitted(log):
    notify(
        _lab_recipients(log.equipment.lab),
        'Maintenance request submitted',
        f'A maintenance task for {log.equipment.name} was scheduled for {log.maintenance_date}.',
        'maintenance_submitted', lab=log.equipment.lab,
    )


def notify_maintenance_completed(log):
    notify(
        _lab_recipients(log.equipment.lab),
        'Maintenance completed',
        f'Maintenance for {log.equipment.name} was marked as completed.',
        'maintenance_completed', lab=log.equipment.lab,
    )


def notify_equipment_issue_reported(issue):
    notify(
        _lab_recipients(issue.equipment.lab),
        'Equipment issue reported',
        f'{issue.equipment.name} in {issue.equipment.lab.name} was reported faulty/needs maintenance: {issue.description[:120]}',
        'pc_maintenance', lab=issue.equipment.lab,
    )


def notify_equipment_issue_resolved(issue):
    notify(
        _lab_recipients(issue.equipment.lab),
        'Equipment issue resolved',
        f'{issue.equipment.name} in {issue.equipment.lab.name} has been repaired/resolved.',
        'maintenance_completed', lab=issue.equipment.lab,
    )


# ---------------------------------------------------------------- #
# Issue reports (issues app)
# ---------------------------------------------------------------- #

def notify_issue_reported(issue, reporter=None):
    """A new issue was logged. Goes to the Lab In-Charge of THAT lab (via
    incharge_for_lab) plus all Admins -- not just Admins. The reporter is
    left out so an Admin/In-Charge who logs an issue themselves doesn't get
    an alert about their own report."""
    reporter = reporter or issue.reported_by
    where = f'{issue.lab.name} \u2014 {issue.pc.pc_id}' if issue.pc else f'{issue.lab.name} (general lab issue)'
    lines = [
        f'Reported by {reporter.display_name} ({reporter.get_role_display()}) at {where}.' if reporter
        else f'Reported at {where}.',
        f'Type: {issue.get_issue_type_display()} \u00b7 Priority: {issue.get_priority_display()}',
    ]
    if (issue.description or '').strip():
        lines.append(issue.description.strip())
    recipients = [u for u in _lab_recipients(issue.lab) if not reporter or u.pk != reporter.pk]
    notify(
        recipients,
        f'New issue: {issue.title}',
        '\n'.join(lines),
        'issue_reported', lab=issue.lab,
    )


def notify_issue_status_changed(issue, actor=None):
    """Tell whoever reported the issue (normally an Instructor) that its
    status changed -- Assigned, In Progress or Resolved. No-ops when there
    is no reporter (account deleted) or when the reporter is the one who
    made the change."""
    reporter = issue.reported_by
    if reporter is None or issue.hidden_by_reporter or (actor is not None and reporter.pk == actor.pk):
        return []
    status_label = issue.get_status_display()
    lines = [f'Your report "{issue.title}" ({issue.lab.name}) is now: {status_label}.']
    if issue.status == 'assigned' and issue.assigned_technician:
        lines.append(f'Assigned to {issue.assigned_technician.display_name}.')
    if issue.status == 'resolved' and (issue.resolution_notes or '').strip():
        lines.append(f'Resolution: {issue.resolution_notes.strip()}')
    if issue.status == 'resolved':
        title, n_type = f'Issue resolved: {issue.title}', 'issue_resolved'
    else:
        title, n_type = f'Issue update: {issue.title} is {status_label}', 'issue_status_changed'
    return notify(
        [reporter], title, '\n'.join(lines), n_type, lab=issue.lab,
    )


# ---------------------------------------------------------------- #
# Security
# ---------------------------------------------------------------- #

def notify_failed_login(identifier, attempt_count):
    notify(
        admins(),
        'Multiple failed login attempts',
        f'{attempt_count} failed login attempts were detected for account "{identifier}".',
        'login_security_alert',
    )


def send_password_changed_email(user):
    """Sends the branded 'Password Change Confirmed' email — fired after
    ANY successful password change, however it happened (self-service
    reset via the Forgot Password flow, or an admin resetting it from
    User & Role Management). This is a security notification, not a
    toggleable one, so unlike notify() above it's not gated by
    AlertSettings and always fires as long as the account has an email
    on file (Students never do, so this is a no-op for them). Failures
    are swallowed the same way notify()'s email step is, so a bad SMTP
    config never blocks the password change itself from succeeding."""
    if not user.email:
        return
    login_url = f'{settings.SITE_URL}{reverse("login")}'
    logo_url = f'{settings.SITE_URL}{static("img/logo.jpg")}'
    context = {'user': user, 'login_url': login_url, 'logo_url': logo_url}
    text_body = render_to_string('emails/password_changed.txt', context)
    html_body = render_to_string('emails/password_changed.html', context)
    try:
        message = EmailMultiAlternatives(
            subject='Password Change Confirmed — SCLAMS',
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        message.attach_alternative(html_body, 'text/html')
        message.send(fail_silently=True)
    except Exception:
        pass  # never let an email hiccup block the password change itself