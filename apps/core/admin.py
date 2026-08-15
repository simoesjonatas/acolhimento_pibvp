from django.contrib import admin

from apps.core.models import QrCodeDinamico


@admin.register(QrCodeDinamico)
class QrCodeDinamicoAdmin(admin.ModelAdmin):
    list_display = ('nome', 'codigo', 'destino', 'ativo', 'total_acessos', 'ultimo_acesso', 'criado_em')
    list_filter = ('ativo',)
    search_fields = ('nome', 'codigo', 'destino')
    readonly_fields = ('codigo', 'total_acessos', 'ultimo_acesso', 'criado_por', 'criado_em', 'atualizado_em')
    ordering = ('-criado_em',)
