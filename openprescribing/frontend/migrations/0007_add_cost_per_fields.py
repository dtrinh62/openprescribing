# -*- coding: utf-8 -*-
# Generated by Django 1.9.1 on 2016-09-01 14:04
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('frontend', '0007_auto_20160908_0811'),
    ]

    operations = [
        migrations.AddField(
            model_name='measureglobal',
            name='cost_per_denom',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='measureglobal',
            name='cost_per_num',
            field=models.FloatField(blank=True, null=True),
        ),
    ]