from django import forms
from django.contrib.auth import get_user_model
from labs.models import MaintenanceLog, Lab, InventoryItem, PC
import ipaddress


class PCImportForm(forms.Form):
    file = forms.FileField(
        label='CSV or Excel file',
        help_text='Columns: Lab, PC ID, IP Address, Status (optional).'
    )

    def clean_file(self):
        f = self.cleaned_data['file']
        name = f.name.lower()
        if not (name.endswith('.csv') or name.endswith('.xlsx')):
            raise forms.ValidationError('Please upload a .csv or .xlsx file.')
        return f


class LabForm(forms.ModelForm):
    class Meta:
        model = Lab
        fields = ['name', 'location', 'opening_time', 'closing_time']
        widgets = {
            'opening_time': forms.TimeInput(attrs={'type': 'time'}),
            'closing_time': forms.TimeInput(attrs={'type': 'time'}),
        }


class PCForm(forms.ModelForm):
    """
    Same fields as the plain PC ModelForm, plus two guardrails:
    - IP Address is required (even though the model field itself stays
      optional for backward compatibility with older records) — remote
      Shutdown/Restart/Lock and the automatic online/offline checker all
      need it, so a PC without one can't actually be monitored/managed.
    - Loopback/reserved IPs (127.0.0.0/8, 0.0.0.0, etc) are rejected.
      Those always "ping back" the server itself, not the actual lab PC,
      so the status checker would report the PC as online forever
      regardless of whether it's really on — a confusing, hard-to-debug
      false positive.
    """
    class Meta:
        model = PC
        fields = ['lab', 'pc_id', 'ip_address', 'status']

    # Accessibility: wire each widget's aria-describedby to its helptext /
    # error message ids (see labs/pc_edit_form.html, which renders those
    # ids as "id_<field>_help" / "id_<field>_error"), and flag aria-invalid
    # on fields that failed validation, so the "Add PC" modal announces
    # errors properly to screen readers instead of relying on color alone.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Model field stays optional (blank=True) so existing PCs added
        # before this rule took effect don't break — but every NEW/edited
        # PC going through this form must supply one, since remote
        # Shutdown/Restart/Lock and the automatic online/offline checker
        # all depend on it.
        self.fields['ip_address'].required = True
        help_ids = {'ip_address': 'id_ip_address_help', 'status': 'id_status_help'}
        for name, field in self.fields.items():
            described_by = []
            if name in help_ids:
                described_by.append(help_ids[name])
            if self.is_bound and self.errors.get(name):
                field.widget.attrs['aria-invalid'] = 'true'
                described_by.append(f'id_{name}_error')
            if described_by:
                field.widget.attrs['aria-describedby'] = ' '.join(described_by)

    def clean_ip_address(self):
        ip_address = self.cleaned_data.get('ip_address')
        if not ip_address:
            return ip_address
        try:
            addr = ipaddress.ip_address(ip_address)
        except ValueError:
            return ip_address  # let the model field's own validator handle malformed input
        if addr.is_loopback:
            raise forms.ValidationError(
                'This is a loopback address (127.x.x.x) — it always pings back the '
                'server itself, not the lab PC, so status checks would be meaningless. '
                'Enter the PC\'s real network IP address, or leave this field blank.'
            )
        # The pc-agent-* endpoints (labs/views.py) resolve "which PC is this?"
        # with PC.objects.filter(ip_address=...).first() — if two PC records
        # share one IP, that lookup always returns the SAME one (whichever
        # sorts first), so every other PC on that IP silently gets treated
        # as that one PC (e.g. the lock screen keeps showing "PC 01").
        # Block that at the source instead of letting it happen silently.
        dupe = PC.objects.filter(ip_address=ip_address)
        if self.instance and self.instance.pk:
            dupe = dupe.exclude(pk=self.instance.pk)
        existing = dupe.select_related('lab').first()
        if existing:
            raise forms.ValidationError(
                f'This IP address is already assigned to "{existing.pc_id}" ({existing.lab.name}). '
                f'Each PC needs its own unique IP address — the system identifies which PC is talking '
                f'to it by IP, so two PCs sharing one IP will make the agent show the wrong PC ID.'
            )
        return ip_address


class MaintenanceScheduleForm(forms.ModelForm):
    class Meta:
        model = MaintenanceLog
        fields = ['equipment', 'maintenance_date', 'assigned_technician', 'notes']
        widgets = {
            'maintenance_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        User = get_user_model()
        self.fields['assigned_technician'].queryset = User.objects.filter(role__in=['admin', 'incharge'])
        self.fields['assigned_technician'].required = False



class InventoryItemForm(forms.ModelForm):
    class Meta:
        model = InventoryItem
        fields = ['name', 'category', 'quantity', 'condition', 'lab', 'last_checked']
        widgets = {
            'last_checked': forms.DateInput(attrs={'type': 'date'}),
        }

    def save(self, commit=True):
        item = super().save(commit=False)
        # Condition and Status are separate fields on InventoryItem, and
        # Status isn't even exposed on this form — so without this, marking
        # an item "Faulty"/"Needs Check" here left Status stuck wherever it
        # already was (almost always the 'operational' default), which the
        # inventory list badge renders as "Working" regardless of Condition.
        # Auto-escalate Status when Condition turns bad; never auto-revert it
        # back to 'operational' when Condition looks fine again — that stays
        # a deliberate action on the Equipment Status page, in case someone
        # marked it Under Repair for a reason Condition doesn't capture (a
        # part on order, say). A Retired item is never bumped either way.
        if item.condition in ('faulty', 'needs_check') and item.status == 'operational':
            item.status = 'under_repair'
        if commit:
            item.save()
        return item


class ReservationPCLoginForm(forms.Form):
    """
    Shown on a locked lab computer. The person enters the Student/Instructor
    ID and reservation code that the Admin or Lab In-Charge gave them when
    the reservation was approved. This never signs anyone into the web
    system — labs.views.ReservationPCLoginView matches it against the
    official schedule to decide whether to unlock the machine they're
    sitting at.
    """
    id_number = forms.CharField(label='Student ID / Instructor ID', max_length=50)
    reservation_code = forms.CharField(label='Reservation code', max_length=12)