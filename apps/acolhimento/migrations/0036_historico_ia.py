"""Preenche o historico da IA nas mensagens recebidas antes de ele existir.

Ate aqui a decisao da IA ficava so na mensagem de saida (metadata_envio['ia'],
com entrada_id) e as opcoes escolhidas pela IA nem isso. Agora a tela Conversas da
IA le de metadata_resposta['ia'] da mensagem da pessoa: copia o que da para saber.
"""
from django.db import migrations


def preencher(apps, schema_editor):
    Mensagem = apps.get_model('acolhimento', 'MensagemContato')
    saidas = Mensagem.objects.filter(direcao='saida', metadata_envio__origem='bot_ia').order_by('enfileirada_em', 'id')
    for saida in saidas:
        ia = (saida.metadata_envio or {}).get('ia') or {}
        entrada = None
        if ia.get('entrada_id'):
            entrada = Mensagem.objects.filter(pk=ia['entrada_id'], direcao='entrada').first()
        if entrada is None:
            # Sem o id (opcao escolhida pela IA, limite): a ultima mensagem da pessoa antes da resposta.
            entrada = (
                Mensagem.objects.filter(
                    pessoa_id=saida.pessoa_id, direcao='entrada', enfileirada_em__lte=saida.enfileirada_em,
                )
                .order_by('-enfileirada_em', '-id')
                .first()
            )
        if entrada is None:
            continue
        metadata = dict(entrada.metadata_resposta or {})
        if 'ia' in metadata:
            continue
        registro = {
            'acao': ia.get('acao') or 'opcao',
            'resposta_id': saida.pk,
            'em': saida.enfileirada_em.isoformat() if saida.enfileirada_em else None,
        }
        for chave in ('modelo', 'tokens_entrada', 'tokens_saida'):
            if ia.get(chave) not in (None, ''):
                registro[chave] = ia[chave]
        if ia.get('primeira'):
            registro['primeira'] = True
        metadata['ia'] = registro
        entrada.metadata_resposta = metadata
        entrada.save(update_fields=['metadata_resposta'])


class Migration(migrations.Migration):
    dependencies = [
        ('acolhimento', '0035_template_responsavel_atribuido'),
    ]

    operations = [
        migrations.RunPython(preencher, migrations.RunPython.noop),
    ]
