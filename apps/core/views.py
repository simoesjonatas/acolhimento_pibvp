from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from django.shortcuts import render

from apps.acolhimento.models import InteracaoAcolhimento, PrimeiroContato


class DashboardView(LoginRequiredMixin, TemplateView):
	template_name = 'dashboard.html'

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)

		total_pessoas = PrimeiroContato.objects.count()
		total_primeiro_contato = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO
		).count()
		total_acompanhamento = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO
		).count()
		total_membros = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.MEMBRO
		).count()

		ultimos_passos = InteracaoAcolhimento.objects.select_related('pessoa')[:8]

		context.update(
			{
				'total_pessoas': total_pessoas,
				'total_primeiro_contato': total_primeiro_contato,
				'total_acompanhamento': total_acompanhamento,
				'total_membros': total_membros,
				'ultimos_passos': ultimos_passos,
			}
		)

		return context

def forbidden_view(request, exception=None):
	return render(request, '403.html', status=403)
