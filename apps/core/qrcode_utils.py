"""Geracao de imagens de QR Code (SVG para tela/impressao, PNG para download)."""
import io

import qrcode
import qrcode.image.svg
from qrcode.constants import ERROR_CORRECT_M


def _build(dados):
	qr = qrcode.QRCode(
		error_correction=ERROR_CORRECT_M,
		box_size=12,
		border=2,
	)
	qr.add_data(dados)
	qr.make(fit=True)
	return qr


def gerar_qrcode_svg(dados):
	"""Retorna o markup SVG (str) de um QR Code. Escala sem perder qualidade."""
	img = _build(dados).make_image(image_factory=qrcode.image.svg.SvgPathImage)
	buffer = io.BytesIO()
	img.save(buffer)
	return buffer.getvalue().decode('utf-8')


def gerar_qrcode_png(dados):
	"""Retorna os bytes PNG de um QR Code (bom para colar em cartazes/flyers)."""
	img = _build(dados).make_image(fill_color='black', back_color='white')
	buffer = io.BytesIO()
	img.save(buffer, format='PNG')
	return buffer.getvalue()
