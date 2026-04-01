from django.contrib.auth import views as auth_views
from django.urls import path

from apps.core.views import DashboardView, PerfilView, UsuarioCreateView, UsuarioDeleteView, UsuarioListView, UsuarioUpdateView

urlpatterns = [
    path('login/', auth_views.LoginView.as_view(template_name='login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('perfil/', PerfilView.as_view(), name='perfil'),
    path('usuarios/', UsuarioListView.as_view(), name='usuarios-lista'),
    path('usuarios/novo/', UsuarioCreateView.as_view(), name='usuarios-novo'),
    path('usuarios/<int:pk>/editar/', UsuarioUpdateView.as_view(), name='usuarios-editar'),
    path('usuarios/<int:pk>/excluir/', UsuarioDeleteView.as_view(), name='usuarios-excluir'),
    path('', DashboardView.as_view(), name='dashboard'),
]
