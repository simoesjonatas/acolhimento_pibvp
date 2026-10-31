from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('acolhimento', '0026_registro_gabinete'),
    ]

    operations = [
        migrations.AddField(
            model_name='templatewhatsapp',
            name='meta_nome',
            field=models.CharField(blank=True, max_length=512, verbose_name='Nome do template (Meta)'),
        ),
        migrations.AddField(
            model_name='templatewhatsapp',
            name='meta_idioma',
            field=models.CharField(blank=True, default='pt_BR', max_length=15, verbose_name='Idioma do template (Meta)'),
        ),
    ]
