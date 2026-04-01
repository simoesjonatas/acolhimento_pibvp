from django.urls import path

from apps.acolhimento.views import (
    PrimeiroContatoCreateView,
    PrimeiroContatoDeleteView,
    PrimeiroContatoDetailView,
    PrimeiroContatoExportCsvView,
    PrimeiroContatoListView,
    PrimeiroContatoUpdateView,
)

urlpatterns = [
    path('pessoas/', PrimeiroContatoListView.as_view(), name='pessoas-lista'),
    path('pessoas/novo/', PrimeiroContatoCreateView.as_view(), name='pessoas-novo'),
    path('pessoas/<int:pk>/', PrimeiroContatoDetailView.as_view(), name='pessoas-detalhe'),
    path('pessoas/<int:pk>/editar/', PrimeiroContatoUpdateView.as_view(), name='pessoas-editar'),
    path('pessoas/<int:pk>/excluir/', PrimeiroContatoDeleteView.as_view(), name='pessoas-excluir'),
    path('pessoas/exportar-csv/', PrimeiroContatoExportCsvView.as_view(), name='pessoas-exportar-csv'),
]
