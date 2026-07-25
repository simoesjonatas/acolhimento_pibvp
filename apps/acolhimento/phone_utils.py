import re


def only_digits(value: str) -> str:
	return re.sub(r'\D', '', (value or '').strip())


def build_phone_candidates(raw_number: str) -> set[str]:
	digits = only_digits(raw_number)
	if not digits:
		return set()

	candidates: set[str] = {digits}
	if digits.startswith('55'):
		br_local = digits[2:]
		candidates.add(br_local)
	else:
		br_local = digits
		candidates.add(f'55{digits}')

	if len(br_local) == 11 and br_local[2] == '9':
		without_ninth = br_local[:2] + br_local[3:]
		candidates.add(without_ninth)
		candidates.add(f'55{without_ninth}')
	elif len(br_local) == 10:
		with_ninth = br_local[:2] + '9' + br_local[2:]
		candidates.add(with_ninth)
		candidates.add(f'55{with_ninth}')

	return {item for item in candidates if item}


def find_pessoa_by_phone(raw_number: str, exclude_pk=None):
	from apps.acolhimento.models import PrimeiroContato

	candidates = build_phone_candidates(raw_number)
	if not candidates:
		return None

	queryset = PrimeiroContato.objects.only('id', 'telefone_whatsapp')
	if exclude_pk is not None:
		queryset = queryset.exclude(pk=exclude_pk)

	for pessoa in queryset:
		pessoa_digits = only_digits(pessoa.telefone_whatsapp)
		if not pessoa_digits:
			continue
		if pessoa_digits in candidates:
			return pessoa
		if pessoa_digits.startswith('55') and pessoa_digits[2:] in candidates:
			return pessoa
		if f'55{pessoa_digits}' in candidates:
			return pessoa

	return None


def phone_for_cadastro(raw_number: str) -> str:
	candidates = sorted(build_phone_candidates(raw_number), key=lambda item: (len(item), item), reverse=True)

	for candidate in candidates:
		if len(candidate) in (10, 11) and not candidate.startswith('55'):
			return candidate

	for candidate in candidates:
		if len(candidate) in (12, 13) and candidate.startswith('55'):
			return candidate[2:]

	digits = only_digits(raw_number)
	if len(digits) in (12, 13) and digits.startswith('55'):
		return digits[2:]
	return digits


def build_auto_nome_from_phone(raw_number: str) -> str:
	digits = only_digits(raw_number)
	suffix = digits[-4:] if len(digits) >= 4 else digits
	return f'Contato WhatsApp {suffix}'.strip()
