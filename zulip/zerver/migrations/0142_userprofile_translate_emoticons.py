# Generated by Django 1.11.6 on 2018-02-19 22:27

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("zerver", "0141_change_usergroup_description_to_textfield"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="translate_emoticons",
            field=models.BooleanField(default=False),
        ),
    ]
