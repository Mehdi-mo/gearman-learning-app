from django.db import migrations, models


def label_existing_premium(apps, schema_editor):
    """
    Give accounts that already have premium a matching label.

    With an end date they were sold a period, so 'paid'; without one they were
    given open-ended access, so 'lifetime'. Testers cannot be told apart
    retroactively — relabel those few by hand in the panel.
    """
    UserProfile = apps.get_model('accounts', 'UserProfile')
    UserProfile.objects.filter(is_premium=True, premium_until__isnull=False).update(premium_kind='paid')
    UserProfile.objects.filter(is_premium=True, premium_until__isnull=True).update(premium_kind='lifetime')


def unlabel(apps, schema_editor):
    """Nothing to undo — the column goes away with the reverse of AddField."""


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_alter_readarticle_unique_together'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='premium_kind',
            field=models.CharField(
                blank=True, default='', max_length=10,
                choices=[('paid', 'Bezahlt'), ('lifetime', 'Unbegrenzt'), ('tester', 'Tester')],
            ),
        ),
        migrations.RunPython(label_existing_premium, unlabel),
    ]
