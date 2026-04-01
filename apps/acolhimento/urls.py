from django.urls import path

from apps.acolhimento.views import (
    AutoCadastroCreateView,
    AutoCadastroSuccessView,
    DisparoMensagemMassaView,
    MensagemFilaListView,
    MensagemContatoExcluirView,
    PrimeiroContatoEnfileirarMensagemView,
    PrimeiroContatoCreateView,
    PrimeiroContatoDeleteView,
    PrimeiroContatoDetailView,
    PrimeiroContatoExportCsvView,
    PrimeiroContatoListView,
    PrimeiroContatoMensagensView,
    PrimeiroContatoUpdateView,
)

urlpatterns = [
    path('auto-cadastro/', AutoCadastroCreateView.as_view(), name='auto-cadastro'),
    path('auto-cadastro/sucesso/', AutoCadastroSuccessView.as_view(), name='auto-cadastro-sucesso'),
    path('pessoas/', PrimeiroContatoListView.as_view(), name='pessoas-lista'),
    path('mensagens/fila/', MensagemFilaListView.as_view(), name='mensagens-fila'),
    path('mensagens/disparo/', DisparoMensagemMassaView.as_view(), name='mensagens-disparo-massa'),
    path('mensagens/<int:pk>/excluir/', MensagemContatoExcluirView.as_view(), name='mensagens-excluir'),
    path('pessoas/novo/', PrimeiroContatoCreateView.as_view(), name='pessoas-novo'),
    path('pessoas/<int:pk>/', PrimeiroContatoDetailView.as_view(), name='pessoas-detalhe'),
    path('pessoas/<int:pk>/mensagens/', PrimeiroContatoMensagensView.as_view(), name='pessoas-mensagens'),
    path('pessoas/<int:pk>/enfileirar-mensagem/', PrimeiroContatoEnfileirarMensagemView.as_view(), name='pessoas-enfileirar-mensagem'),
    path('pessoas/<int:pk>/editar/', PrimeiroContatoUpdateView.as_view(), name='pessoas-editar'),
    path('pessoas/<int:pk>/excluir/', PrimeiroContatoDeleteView.as_view(), name='pessoas-excluir'),
    path('pessoas/exportar-csv/', PrimeiroContatoExportCsvView.as_view(), name='pessoas-exportar-csv'),
]
