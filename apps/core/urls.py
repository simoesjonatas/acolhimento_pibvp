from django.contrib.auth import views as auth_views
from django.urls import path

from apps.core.forms import LoginForm
from apps.core.views import (
    DashboardView,
    PerfilView,
    QrCodeCreateView,
    QrCodeDeleteView,
    QrCodeDetailView,
    QrCodeImprimirView,
    QrCodeListView,
    QrCodePngView,
    QrCodeUpdateView,
    UsuarioCreateView,
    UsuarioDeleteView,
    UsuarioListView,
    UsuarioUpdateView,
    healthz,
    qr_redirect,
)

urlpatterns = [
    path('healthz/', healthz, name='healthz'),
    path('login/', auth_views.LoginView.as_view(template_name='login.html', authentication_form=LoginForm), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('perfil/', PerfilView.as_view(), name='perfil'),
    path('usuarios/', UsuarioListView.as_view(), name='usuarios-lista'),
    path('usuarios/novo/', UsuarioCreateView.as_view(), name='usuarios-novo'),
    path('usuarios/<int:pk>/editar/', UsuarioUpdateView.as_view(), name='usuarios-editar'),
    path('usuarios/<int:pk>/excluir/', UsuarioDeleteView.as_view(), name='usuarios-excluir'),
    path('qrcodes/', QrCodeListView.as_view(), name='qrcodes-lista'),
    path('qrcodes/novo/', QrCodeCreateView.as_view(), name='qrcodes-novo'),
    path('qrcodes/<int:pk>/', QrCodeDetailView.as_view(), name='qrcodes-detalhe'),
    path('qrcodes/<int:pk>/editar/', QrCodeUpdateView.as_view(), name='qrcodes-editar'),
    path('qrcodes/<int:pk>/excluir/', QrCodeDeleteView.as_view(), name='qrcodes-excluir'),
    path('qrcodes/<int:pk>/imprimir/', QrCodeImprimirView.as_view(), name='qrcodes-imprimir'),
    path('qrcodes/<int:pk>/png/', QrCodePngView.as_view(), name='qrcodes-png'),
    path('r/<str:codigo>/', qr_redirect, name='qr-redirect'),
    path('', DashboardView.as_view(), name='dashboard'),
]
