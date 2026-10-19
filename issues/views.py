from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.views.generic import ListView, CreateView, UpdateView, DetailView
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from accounts.mixins import RoleRequiredMixin, ModalFormMixin, ModalDetailMixin, modal_redirect, bulk_delete
from issues.models import Issue
from notifications.services import notify_issue_reported, notify_issue_status_changed


# list all open issues
class IssuesView(RoleRequiredMixin, ListView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'issues/issues.html'
    context_object_name = 'issues'

    def get_queryset(self):
        return Issue.objects.select_related(
            'pc', 'lab', 'reported_by'
        ).order_by('-created_at')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['open_count'] = Issue.objects.filter(status='open').count()
        ctx['in_progress_count'] = Issue.objects.filter(status='in_progress').count()
        ctx['resolved_today'] = Issue.objects.filter(
            status='resolved',
            resolved_at__date=timezone.now().date()
        ).count()
        return ctx


# view one issue in detail
class IssueDetailView(RoleRequiredMixin, ModalDetailMixin, DetailView):
    allowed_roles = ['admin', 'incharge']
    model = Issue
    template_name = 'issues/issue_detail.html'
    context_object_name = 'issue'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        User = get_user_model()
        ctx['technicians'] = User.objects.filter(role__in=['admin', 'incharge']).order_by('first_name', 'last_name')
        return ctx


# log a new issue
class IssueCreateView(RoleRequiredMixin, ModalFormMixin, CreateView):
    allowed_roles = ['admin', 'incharge', 'instructor']
    model = Issue
    fields = ['pc', 'lab', 'title', 'description', 'issue_type', 'priority']
    template_name = 'issues/issue_form.html'

    # The Issues list is admin/in-charge only, so an instructor who just
    # reported a problem is sent to their own "My issue reports" page
    # instead (where they can follow its status) -- not to the Issues list
    # (403 Forbidden).
    def get_success_url(self):
        if self.request.user.role == 'instructor':
            return reverse('my_issues')
        return reverse('issues')

    def form_valid(self, form):
        form.instance.reported_by = self.request.user
        form.instance.status = 'open'
        response = super().form_valid(form)  # saves self.object
        # Alert the Lab In-Charge of this lab AND the Admins (see
        # notifications.services.notify_issue_reported).
        notify_issue_reported(self.object, reporter=self.request.user)
        messages.success(self.request, 'Issue logged.')
        return response


# update issue status (in progress, resolved)
class IssueUpdateView(RoleRequiredMixin, ModalFormMixin, UpdateView):
    allowed_roles = ['admin', 'incharge']
    model = Issue
    fields = ['status', 'priority', 'notes']
    template_name = 'issues/issue_form.html'
    success_url = reverse_lazy('issues')

    def form_valid(self, form):
        # form.instance already holds the submitted values, so read the
        # status that is currently stored to know whether it really changed.
        old_status = Issue.objects.filter(pk=form.instance.pk).values_list('status', flat=True).first()
        if form.instance.status == 'resolved' and old_status != 'resolved':
            form.instance.resolved_at = timezone.now()
        response = super().form_valid(form)
        if self.object.status != old_status:
            notify_issue_status_changed(self.object, actor=self.request.user)
        messages.success(self.request, 'Issue updated.')
        return response


# equipment monitor view
class EquipmentView(RoleRequiredMixin, ListView):
    allowed_roles = ['admin', 'incharge']
    template_name = 'issues/equipment.html'
    context_object_name = 'issues'

    def get_queryset(self):
        return Issue.objects.filter(
            status__in=['open', 'in_progress']
        ).select_related('pc', 'lab').order_by('priority')


# assign an issue to a technician (admin or incharge staff member)
def issue_assign(request, pk):
    if not request.user.is_authenticated or request.user.role not in ('admin', 'incharge'):
        raise PermissionDenied
    issue = get_object_or_404(Issue, pk=pk)
    if request.method == 'POST':
        User = get_user_model()
        technician_id = request.POST.get('assigned_technician')
        technician = User.objects.filter(pk=technician_id, role__in=['admin', 'incharge']).first()
        if technician:
            old_status = issue.status
            issue.assigned_technician = technician
            issue.status = 'assigned'
            issue.save(update_fields=['assigned_technician', 'status'])
            if old_status != 'assigned':
                notify_issue_status_changed(issue, actor=request.user)
            messages.success(request, f'Issue assigned to {technician.display_name}.')
        else:
            messages.error(request, 'Please select a valid technician.')
    return modal_redirect(request, 'issue_detail', pk=issue.pk)


# mark an issue resolved, with resolution notes
def issue_resolve(request, pk):
    if not request.user.is_authenticated or request.user.role not in ('admin', 'incharge'):
        raise PermissionDenied
    issue = get_object_or_404(Issue, pk=pk)
    if request.method == 'POST':
        resolution_notes = request.POST.get('resolution_notes', '').strip()
        was_resolved = issue.status == 'resolved'
        issue.resolution_notes = resolution_notes
        issue.status = 'resolved'
        issue.resolved_at = timezone.now()
        issue.save(update_fields=['resolution_notes', 'status', 'resolved_at'])
        if not was_resolved:
            notify_issue_status_changed(issue, actor=request.user)
        messages.success(request, 'Issue marked as resolved.')
    return modal_redirect(request, 'issue_detail', pk=issue.pk)


# ---- Instructor: "My issue reports" ----------------------------------- #
# An instructor can only ever see the issues THEY reported (and never the
# admin/in-charge management tools), with each report's current status:
# Open, Assigned, In Progress or Resolved.

class MyIssuesView(RoleRequiredMixin, ListView):
    allowed_roles = ['instructor']
    template_name = 'issues/my_issues.html'
    context_object_name = 'issues'

    def get_base_queryset(self):
        return Issue.objects.filter(reported_by=self.request.user, hidden_by_reporter=False).select_related(
            'pc', 'lab', 'assigned_technician'
        )

    def get_queryset(self):
        qs = self.get_base_queryset().order_by('-created_at')
        status = self.request.GET.get('status', '')
        if status in dict(Issue.STATUS):
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        base = self.get_base_queryset()
        ctx['selected_status'] = self.request.GET.get('status', '')
        counts = {code: base.filter(status=code).count() for code, _ in Issue.STATUS}
        ctx['status_counts'] = counts
        ctx['status_filters'] = [(code, label, counts[code]) for code, label in Issue.STATUS]
        ctx['total_count'] = base.count()
        return ctx


class MyIssueDetailView(RoleRequiredMixin, ModalDetailMixin, DetailView):
    allowed_roles = ['instructor']
    template_name = 'issues/my_issue_detail.html'
    context_object_name = 'issue'

    # Scoped to the logged-in instructor's own reports: someone else's
    # issue id is simply a 404, not a peek at another person's report.
    def get_queryset(self):
        return Issue.objects.filter(reported_by=self.request.user, hidden_by_reporter=False).select_related(
            'pc', 'lab', 'assigned_technician'
        )


# "Delete" on the instructor's side only removes the report from THEIR list.
# The issue itself stays, so Admin / Lab In-Charge keep it until one of them
# deletes it (see issue_delete below).
def my_issue_delete(request, pk):
    if not request.user.is_authenticated or request.user.role != 'instructor':
        raise PermissionDenied
    issue = get_object_or_404(Issue, pk=pk, reported_by=request.user, hidden_by_reporter=False)
    if request.method == 'POST':
        issue.hidden_by_reporter = True
        issue.save(update_fields=['hidden_by_reporter'])
        messages.success(request, 'Report removed from your list.')
    return modal_redirect(request, 'my_issues')


# Admin / Lab In-Charge: permanently delete an issue. This is the only thing
# that really removes it (it then disappears for the reporter too).
def issue_delete(request, pk):
    if not request.user.is_authenticated or request.user.role not in ('admin', 'incharge'):
        raise PermissionDenied
    issue = get_object_or_404(Issue, pk=pk)
    if request.method == 'POST':
        issue.delete()
        messages.success(request, 'Issue deleted.')
    return modal_redirect(request, 'issues')


# "Delete Selected" on the instructor's My issue reports list: hides every
# checked report from THEIR list only (same as the single Delete button).
def my_issues_bulk_delete(request):
    if not request.user.is_authenticated or request.user.role != 'instructor':
        raise PermissionDenied
    if request.method != 'POST':
        raise PermissionDenied
    ids = [v for v in request.POST.getlist('selected_ids') if v]
    count = Issue.objects.filter(
        pk__in=ids, reported_by=request.user, hidden_by_reporter=False
    ).update(hidden_by_reporter=True)
    if count:
        messages.success(request, f'Removed {count} report{"s" if count != 1 else ""} from your list.')
    else:
        messages.error(request, 'No reports were selected.')
    return redirect('my_issues')


# "Delete Selected" on the Admin / Lab In-Charge Issues list: permanently
# deletes every checked issue (shared bulk_delete helper, same as Inventory).
def issues_bulk_delete(request):
    if not request.user.is_authenticated or request.user.role not in ('admin', 'incharge'):
        raise PermissionDenied
    return bulk_delete(request, Issue.objects.all(), 'issues', item_label='issue')
