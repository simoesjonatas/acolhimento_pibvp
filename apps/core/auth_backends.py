from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class UsernameOrEmailBackend(ModelBackend):
	"""Autentica por username OU e-mail, ignorando maiusculas/minusculas.

	O identificador digitado e normalizado para minusculo antes da busca, e a
	comparacao usa `iexact`, entao "LOgin", "login" e "LOGIN" chegam ao mesmo
	usuario.
	"""

	def authenticate(self, request, username=None, password=None, **kwargs):
		UserModel = get_user_model()
		if username is None:
			username = kwargs.get(UserModel.USERNAME_FIELD)
		if not username or not password:
			return None

		login = username.strip().lower()
		candidatos = list(
			UserModel._default_manager.filter(
				Q(username__iexact=login) | Q(email__iexact=login)
			)
		)

		if not candidatos:
			# Executa o hasher mesmo sem usuario para dificultar enumeracao por tempo.
			UserModel().set_password(password)
			return None

		for user in candidatos:
			if user.check_password(password) and self.user_can_authenticate(user):
				return user
		return None
