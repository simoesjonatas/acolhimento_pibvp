from django.contrib.auth import get_user_model
from django.db.models.signals import pre_save
from django.dispatch import receiver


User = get_user_model()


@receiver(pre_save, sender=User)
def normalizar_usuario_minusculo(sender, instance, **kwargs):
	"""Garante que username e e-mail sejam sempre gravados em minusculo.

	Vale para qualquer caminho de gravacao (formularios, admin, createsuperuser,
	shell), pois roda no pre_save do modelo de usuario.
	"""
	if instance.username:
		instance.username = instance.username.strip().lower()
	if instance.email:
		instance.email = instance.email.strip().lower()
