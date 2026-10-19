from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db.models import Q


class Command(BaseCommand):
    help = 'Lists accounts whose email is blank or is not a @gmail.com address.'

    def handle(self, *args, **options):
        User = get_user_model()
        bad = User.objects.filter(
            Q(email__isnull=True) | Q(email='') | ~Q(email__iendswith='@gmail.com')
        ).order_by('role', 'id')
        if not bad:
            self.stdout.write(self.style.SUCCESS('All accounts have a @gmail.com email.'))
            return
        self.stdout.write(f'{bad.count()} account(s) need a Gmail address (fix each one with Users > Edit):\n')
        for u in bad:
            self.stdout.write(f'  #{u.pk}  {u.get_role_display():<14} {u.display_name:<30} {u.email or "(no email)"}')
