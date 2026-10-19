from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from accounts.email_rules import email_domain_error
from accounts.constants import DEPARTMENT_CHOICES, year_level_choices_for

User = get_user_model()


def _check_email_typo(form, email):
    """Gmail only — for every role, on create AND edit (no exemption for an
    account's existing address, so an old non-Gmail email has to be fixed
    the next time that account is saved)."""
    error = email_domain_error(email)
    if error:
        form.add_error('email', error)


def _validate_department_year_level(cleaned):
    """Shared rule: if a year level is set, it must be one of the year
    levels that actually belong to the selected department (departments
    don't all have the same number of year levels)."""
    department = cleaned.get('department')
    year_level = cleaned.get('year_level')
    if year_level and department:
        valid_values = {v for v, _ in year_level_choices_for(department)}
        if year_level not in valid_values:
            raise ValidationError({'year_level': 'That year level isn\'t offered by the selected department.'})


class ProfileUpdateForm(forms.ModelForm):
    """Lets a logged-in user update their own display info and avatar.
    Deliberately excludes email/role/permissions — those stay
    admin-controlled via AdminUserCreateForm/UserUpdateView."""

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'avatar']


def _clean_id_number_uniqueness(form, cleaned):
    """Shared rule: the Student/Instructor ID doubles as a lab-PC login
    credential and a reservation-matching key, so it must be unique across
    every account — not just among students — or check-ins and PC logins
    could match the wrong person. Excludes the form's own instance when
    editing so saving an unchanged record doesn't trip over itself."""
    id_number = (cleaned.get('id_number') or '').strip()
    if not id_number:
        return
    qs = User.objects.filter(id_number__iexact=id_number)
    if form.instance.pk:
        qs = qs.exclude(pk=form.instance.pk)
    if qs.exists():
        form.add_error('id_number', 'A user with that ID Number already exists.')


class AdminUserCreateForm(forms.ModelForm):
    """Used by admins to create accounts for any role.

    Students are a special case: they never sign in to the SCLAMS
    dashboard, so they don't get (or need) a password — their Student ID
    (plus a reservation code) is what they present at a lab PC instead.
    A Gmail address is still required for them though: it's never a
    login credential for a Student, just where roster/reservation
    notifications get sent once an instructor adds them to a Class
    Roster — but every account in the system, Student or not, is
    required to have one on file. Only Admin, Lab In-Charge, and
    Instructor accounts additionally require a password (Students never
    get one).
    """
    password1 = forms.CharField(
        label='Password',
        widget=forms.PasswordInput,
        required=False,
       
    )
    password2 = forms.CharField(
        label='Confirm password',
        widget=forms.PasswordInput,
        required=False,
    )

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'id_number', 'role',
                  'department', 'year_level', 'assigned_lab']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Email is required for every role now, Student included — kept
        # off at the field level (not required=True) so our own
        # clean()-level message below fires instead of Django's generic
        # default, and so switching the Role dropdown around doesn't
        # trip the browser's native required-field popup before clean()
        # gets a say.
        self.fields['email'].required = False
        # This field is hidden client-side for Students via CSS, but
        # browsers can still autofill it with saved credentials (e.g. the
        # logged-in admin's own email/password) before the hide runs.
        # autocomplete="off"/"new-password" stops most browsers from doing
        # that in the first place; the clean() override below is the
        # server-side backstop in case a browser ignores this anyway.
        self.fields['email'].widget.attrs['autocomplete'] = 'off'
        self.fields['password1'].widget.attrs['autocomplete'] = 'new-password'
        self.fields['password2'].widget.attrs['autocomplete'] = 'new-password'

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get('role')
        id_number = (cleaned.get('id_number') or '').strip()
        email = (cleaned.get('email') or '').strip()
        _check_email_typo(self, email)

        if role == 'student':
            # No password for students — clear anything typed in case the
            # role was switched after filling in the fields on the form.
            # Email/Gmail IS required for a Student now too (notifications
            # only, never a login credential) — stored as None, never '',
            # when genuinely blank, so multiple pre-existing Students
            # without one on file don't collide against the unique
            # constraint (NULL, unlike '', isn't equal to another NULL in
            # a uniqueness check). Format + uniqueness for the value
            # itself is enforced the same automatic way as for the other
            # roles (see below).
            cleaned['password1'] = ''
            cleaned['password2'] = ''
            cleaned['email'] = email or None
            cleaned['assigned_lab'] = None
            if not email:
                self.add_error('email', 'Email (Gmail) is required for Student accounts too — it\'s where roster and reservation notifications get sent.')
            if not id_number:
                self.add_error('id_number', 'Student ID is required for Student accounts.')
            if not cleaned.get('department'):
                self.add_error('department', 'Department is required for Student accounts.')
            if not cleaned.get('year_level'):
                self.add_error('year_level', 'Year Level is required for Student accounts.')
        else:
            p1 = cleaned.get('password1')
            p2 = cleaned.get('password2')
            if not email:
                self.add_error('email', 'Email is required for this role.')

            if role in ('incharge', 'instructor'):
                # No password is set here at all — this account is created
                # with set_unusable_password() (see save()) and the new
                # Lab In-Charge/Instructor sets their OWN password by
                # clicking the verification link emailed to them (see
                # verify_email view's "first-time activation" branch).
                # Nobody else — not even the admin creating the account —
                # ever knows it.
                cleaned['password1'] = ''
                cleaned['password2'] = ''
            else:
                # Admin accounts still go through admin-sets-password.
                if not p1:
                    self.add_error('password1', 'Password is required for this role.')
                if not p2:
                    self.add_error('password2', 'Please confirm the password.')
                if p1 and p2 and p1 != p2:
                    self.add_error('password2', "Passwords don't match.")
                if p1:
                    try:
                        validate_password(p1)
                    except ValidationError as e:
                        self.add_error('password1', e)

            # Admin and Lab In-Charge accounts don't use Department/Year
            # Level/Student ID at all — clear out anything left over from
            # switching the role dropdown away from Instructor/Student
            # after those fields were already filled in.
            if role in ('admin', 'incharge'):
                # department is a CharField with no null=True — it must be
                # cleared to '' (not None), or MySQL rejects the insert
                # with "Column 'department' cannot be null". year_level IS
                # nullable (PositiveSmallIntegerField(null=True)), so None
                # is correct there.
                cleaned['department'] = ''
                cleaned['year_level'] = None
                cleaned['id_number'] = ''
            # Assigned Lab is only used by Lab In-Charge accounts — clear
            # any leftover value if the role was switched away from
            # Lab In-Charge after a lab was already picked.
            if role != 'incharge':
                cleaned['assigned_lab'] = None
            # Email format + uniqueness for this role is otherwise enforced
            # automatically: EmailField validates format, and the model
            # field's unique=True triggers Django's built-in uniqueness
            # check during full_clean() since 'email' is on this form.

        _clean_id_number_uniqueness(self, cleaned)
        _validate_department_year_level(cleaned)
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        if user.role in ('student', 'incharge', 'instructor'):
            # Students never authenticate with a Django password at all.
            # Incharge/Instructor accounts start unusable too — on
            # purpose, not a bug: they set their own real password the
            # first time they click the emailed verification link (see
            # verify_email view), instead of the admin choosing one for
            # them.
            user.set_unusable_password()
        else:
            user.set_password(self.cleaned_data['password1'])
        if commit:
            user.save()
        return user


class AdminUserUpdateForm(forms.ModelForm):
    """Used by admins to edit an existing account. Mirrors the Student
    special-casing in AdminUserCreateForm. Unlike that form, this one DOES
    carry password1/password2 — but only as an optional "set/reset
    password" pair, left blank to keep whatever password is already on
    the account.

    Role is deliberately NOT on this form — once an account is created,
    its role is permanent. (It used to be editable here, with the rest of
    the form reacting to the change — e.g. clearing Email when switched
    to Student — but that's gone now: create a new account with the
    correct role instead of switching an existing one.) All the
    role-based rules below key off `self.instance.role`, the account's
    existing, unchangeable role, rather than anything submitted in the
    form.
    """

    password1 = forms.CharField(
        label='New password',
        widget=forms.PasswordInput,
        required=False,
        help_text='Leave blank to keep the current password.'
    )
    password2 = forms.CharField(
        label='Confirm new password',
        widget=forms.PasswordInput,
        required=False,
    )

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'id_number',
                  'department', 'year_level',
                  'assigned_lab', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].required = False
        self.fields['password1'].widget.attrs['autocomplete'] = 'new-password'
        self.fields['password2'].widget.attrs['autocomplete'] = 'new-password'
        # Captured before anything on this form can touch self.instance,
        # so save() below can tell whether email actually changed — by
        # the time save() runs, Django's ModelForm machinery has already
        # applied the new value onto self.instance.email, so comparing
        # against self.instance there would just compare the new value to
        # itself.
        self._original_email = self.instance.email if self.instance.pk else None

    def clean(self):
        cleaned = super().clean()
        # Role can't change on this form — always the account's existing,
        # permanent role, never something submitted by the user.
        role = self.instance.role
        id_number = (cleaned.get('id_number') or '').strip()
        email = (cleaned.get('email') or '').strip()
        _check_email_typo(self, email)

        if role == 'student':
            cleaned['password1'] = ''
            cleaned['password2'] = ''
            # Students don't use Email as a login credential, but it IS
            # required for them too now — it's their notification
            # address (roster/reservation details). Stored as None,
            # never '', only for the rare pre-existing Student who
            # genuinely has none on file yet, so those don't collide on
            # the unique constraint.
            cleaned['email'] = email or None
            cleaned['assigned_lab'] = None
            if not email:
                self.add_error('email', 'Email (Gmail) is required for Student accounts too — it\'s where roster and reservation notifications get sent.')
            if not id_number:
                self.add_error('id_number', 'Student ID is required for Student accounts.')
            if not cleaned.get('department'):
                self.add_error('department', 'Department is required for Student accounts.')
            if not cleaned.get('year_level'):
                self.add_error('year_level', 'Year Level is required for Student accounts.')
        else:
            if not email:
                self.add_error('email', 'Email is required for this role.')
            # Assigned Lab is only used by Lab In-Charge accounts — clear
            # any leftover value for Admin/Instructor accounts.
            if role != 'incharge':
                cleaned['assigned_lab'] = None
            p1 = cleaned.get('password1')
            p2 = cleaned.get('password2')
            if p1 or p2:
                if p1 and p2 and p1 != p2:
                    self.add_error('password2', "Passwords don't match.")
                elif p1 and not p2:
                    self.add_error('password2', 'Please confirm the password.')
                elif p2 and not p1:
                    self.add_error('password1', 'Please enter the new password.')
                if p1:
                    try:
                        validate_password(p1, user=self.instance)
                    except ValidationError as e:
                        self.add_error('password1', e)
            elif not self.instance.has_usable_password():
                # An account with no usable password yet (shouldn't
                # normally happen outside of Students, since role is
                # fixed now) still needs one set to be able to log in.
                self.add_error(
                    'password1',
                    'This account doesn\'t have a password yet. Set one so this user can log in.'
                )
            # Email format + uniqueness for this role is otherwise enforced
            # automatically (see AdminUserCreateForm.clean() for why).

        _clean_id_number_uniqueness(self, cleaned)
        _validate_department_year_level(cleaned)
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        # Role is fixed (not on this form), so this just reflects the
        # account's existing role — no more "switched to Student, wipe
        # the old password" case, but a Student should never carry a
        # usable password regardless of how it ended up on the account.
        if user.role == 'student' and user.has_usable_password():
            user.set_unusable_password()
        elif user.role != 'student' and self.cleaned_data.get('password1'):
            user.set_password(self.cleaned_data['password1'])

        # Changing the email to something new means nobody has proven
        # they actually own THAT address yet — the old verification only
        # covers the old address. Reset the flag so login.is blocked
        # again (see LoginView.form_valid's email_verified check) until
        # a fresh verification link (sent by the view, since that needs
        # `request`) is clicked. self.email_changed is read by
        # UserUpdateView.form_valid() to decide whether to send that link.
        # Students never log in with email at all, so this whole
        # verify-on-change dance doesn't apply to them — a Student's
        # Gmail is just a notification address, no proof-of-ownership
        # link needed.
        self.email_changed = (
            user.role != 'student'
            and bool(self._original_email) and user.email and user.email != self._original_email
        )
        if self.email_changed:
            user.email_verified = False

        if commit:
            user.save()
        return user


class SetPasswordOnVerifyForm(forms.Form):
    """First-time activation for a Lab In-Charge/Instructor account
    created with no password (see AdminUserCreateForm.save()) — shown by
    the verify_email view INSTEAD of the plain verify-and-done flow when
    the account has no usable password yet, so the account owner sets
    their OWN password the first time they click the emailed
    verification link, rather than the admin who created the account
    ever choosing/knowing it."""
    password1 = forms.CharField(label='Password', widget=forms.PasswordInput)
    password2 = forms.CharField(label='Confirm password', widget=forms.PasswordInput)

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('password1')
        p2 = cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            self.add_error('password2', "Passwords don't match.")
        if p1:
            try:
                validate_password(p1, user=self.user)
            except ValidationError as e:
                self.add_error('password1', e)
        return cleaned


class ProfilePasswordChangeForm(forms.Form):
    """Lets a logged-in user (Admin/Lab In-Charge/Instructor) change their
    own password from the My Profile page. Unlike AdminSetPasswordForm
    (admin resetting someone else's password blind), this requires the
    user's CURRENT password first — proving you already know the old one
    is the safeguard for a self-service change. Students never reach this
    form: they have no dashboard password to change (see AdminUserCreateForm)."""
    current_password = forms.CharField(label='Current password', widget=forms.PasswordInput)
    password1 = forms.CharField(label='New password', widget=forms.PasswordInput)
    password2 = forms.CharField(label='Confirm new password', widget=forms.PasswordInput)

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields['current_password'].widget.attrs['autocomplete'] = 'current-password'
        self.fields['password1'].widget.attrs['autocomplete'] = 'new-password'
        self.fields['password2'].widget.attrs['autocomplete'] = 'new-password'

    def clean_current_password(self):
        current_password = self.cleaned_data.get('current_password')
        if current_password and not self.user.check_password(current_password):
            raise ValidationError('Your current password is incorrect.')
        return current_password

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('password1')
        p2 = cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            self.add_error('password2', "Passwords don't match.")
        if p1:
            try:
                validate_password(p1, user=self.user)
            except ValidationError as e:
                self.add_error('password1', e)
            if cleaned.get('current_password') and p1 == cleaned.get('current_password'):
                self.add_error('password1', 'New password must be different from your current password.')
        return cleaned

    def save(self):
        self.user.set_password(self.cleaned_data['password1'])
        self.user.save(update_fields=['password'])
        return self.user


class StudentImportForm(forms.Form):
    """Step one of 'Import Students (Excel/CSV)': pick the department the
    whole file belongs to, then upload the roster file. Every imported
    student is filed under this one department — matching the requirement
    that import is done by department rather than mixed together — and any
    Year Level column in the file is validated against that department's
    own year levels."""
    department = forms.ChoiceField(choices=DEPARTMENT_CHOICES, label='Department')
    file = forms.FileField(
        label='Excel or CSV file',
        help_text='Columns: ID Number, First Name, Last Name, Year Level, Email. '
                   'Email (Gmail) is required for every student — it\'s where roster and '
                   'reservation notifications get sent — even though it\'s never used to sign in.'
    )

    def clean_file(self):
        f = self.cleaned_data['file']
        name = (f.name or '').lower()
        if not (name.endswith('.csv') or name.endswith('.xlsx')):
            raise ValidationError('Please upload a .csv or .xlsx file.')
        return f


class AdminSetPasswordForm(forms.Form):
    """Lets an admin set a brand-new password for an existing user
    (e.g. when the user forgot theirs). Passwords are hashed and can
    never be displayed again once set — this is the only way to help
    a locked-out user."""
    password1 = forms.CharField(label='New password', widget=forms.PasswordInput)
    password2 = forms.CharField(label='Confirm new password', widget=forms.PasswordInput)

    def __init__(self, *args, target_user=None, **kwargs):
        self.target_user = target_user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('password1')
        p2 = cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            self.add_error('password2', "Passwords don't match.")
        if p1:
            try:
                validate_password(p1, user=self.target_user)
            except ValidationError as e:
                self.add_error('password1', e)
        return cleaned

    def save(self):
        self.target_user.set_password(self.cleaned_data['password1'])
        self.target_user.save()
        return self.target_user