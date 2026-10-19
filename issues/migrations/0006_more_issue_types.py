from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('issues', '0005_alter_issue_issue_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='issue',
            name='issue_type',
            field=models.CharField(
                choices=[
                    ('hardware', 'Hardware Fault'),
                    ('network', 'Network Drop'),
                    ('software', 'Software Crash'),
                    ('power', 'Power Issue'),
                    ('peripheral', 'Peripheral/Accessory'),
                    ('facility', 'Facility/Environment'),
                    ('aircon', 'Air Conditioning/Ventilation'),
                    ('lighting', 'Lighting/Electrical'),
                    ('projector', 'Projector/Display'),
                    ('furniture', 'Furniture/Room'),
                    ('safety', 'Safety/Cleanliness'),
                    ('other', 'Other'),
                ],
                default='other',
                max_length=20,
            ),
        ),
    ]
