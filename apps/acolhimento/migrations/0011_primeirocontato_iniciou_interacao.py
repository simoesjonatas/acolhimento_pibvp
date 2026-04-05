from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('acolhimento', '0010_mensagemcontato_visualizada_equipe_em'),
    ]

    operations = [
        migrations.AddField(
            model_name='primeirocontato',
            name='iniciou_interacao',
            field=models.BooleanField(default=False),
        ),
    ]
