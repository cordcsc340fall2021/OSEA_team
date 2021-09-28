# Generated by Django 1.11.23 on 2019-08-28 00:47

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("zerver", "0242_fix_bot_email_property"),
    ]

    operations = [
        migrations.AddField(
            model_name="archivedmessage",
            name="date_sent",
            field=models.DateTimeField(null=True, verbose_name="date sent"),
        ),
        migrations.AddField(
            model_name="message",
            name="date_sent",
            field=models.DateTimeField(null=True, verbose_name="date sent"),
        ),
    ]
