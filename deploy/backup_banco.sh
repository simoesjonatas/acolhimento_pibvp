#!/bin/sh
# Backup diario do banco do Acolhimento (Postgres no Docker).
#
# - Gera um dump (formato custom do pg_dump) em $DESTINO.
# - Confere se o arquivo abre (pg_restore --list) antes de aceitar.
# - Apaga os backups diarios com mais de $MANTER_DIAS dias.
# - Dos backups feitos antes de cada deploy (acolhimento-pre-deploy-*.dump em
#   $PRE_DEPLOY_DIR), guarda so os $MANTER_PRE_DEPLOY mais recentes.
#   Outros arquivos dessa pasta (ex.: o backup da migracao) nao sao tocados.
# - Copia o dump para fora do servidor (Google Drive pelo rclone, remote
#   $RCLONE_REMOTE) e la apaga os com mais de $MANTER_DIAS_REMOTO dias. Sem o
#   remote configurado, so avisa no log. Configurar uma vez com: rclone config
#
# Restaurar (ex.: num banco novo):
#   docker exec -i pibvp-acolhimento-db pg_restore -U acolhimento -d <banco> --clean --if-exists < arquivo.dump
set -eu

CONTAINER="${CONTAINER:-pibvp-acolhimento-db}"
DB_USER="${DB_USER:-acolhimento}"
DB_NAME="${DB_NAME:-acolhimento_pibvp}"
DESTINO="${DESTINO:-/root/backups/diario}"
MANTER_DIAS="${MANTER_DIAS:-14}"
PRE_DEPLOY_DIR="${PRE_DEPLOY_DIR:-/root/backups}"
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive}"
RCLONE_PASTA="${RCLONE_PASTA:-acolhimento-pibvp-backups}"
MANTER_DIAS_REMOTO="${MANTER_DIAS_REMOTO:-30}"
MANTER_PRE_DEPLOY="${MANTER_PRE_DEPLOY:-10}"

umask 077
mkdir -p "$DESTINO"

ARQUIVO="$DESTINO/acolhimento-$(date +%Y%m%d-%H%M).dump"
PARCIAL="$ARQUIVO.parcial"
trap 'rm -f "$PARCIAL"' EXIT

docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$PARCIAL"
docker exec -i "$CONTAINER" pg_restore --list < "$PARCIAL" > /dev/null
mv "$PARCIAL" "$ARQUIVO"

find "$DESTINO" -name 'acolhimento-*.dump' -mtime +"$MANTER_DIAS" -delete

# Pre-deploy: do mais novo para o mais velho, apaga a partir do (N+1)-esimo.
ls -1t "$PRE_DEPLOY_DIR"/acolhimento-pre-deploy-*.dump 2>/dev/null \
    | tail -n +"$((MANTER_PRE_DEPLOY + 1))" \
    | while IFS= read -r antigo; do rm -f -- "$antigo"; echo "removido backup de deploy antigo: $antigo"; done

echo "$(date '+%Y-%m-%d %H:%M:%S') backup ok: $ARQUIVO ($(du -h "$ARQUIVO" | cut -f1))"

# Copia fora do servidor: se o servidor cair, o backup nao vai junto.
if command -v rclone >/dev/null 2>&1 && rclone listremotes 2>/dev/null | grep -qx "$RCLONE_REMOTE:"; then
    if rclone copy "$ARQUIVO" "$RCLONE_REMOTE:$RCLONE_PASTA/"; then
        echo "copia enviada para $RCLONE_REMOTE:$RCLONE_PASTA"
        rclone delete "$RCLONE_REMOTE:$RCLONE_PASTA/" --include 'acolhimento-*.dump' --min-age "${MANTER_DIAS_REMOTO}d" \
            || echo "AVISO: nao consegui apagar as copias antigas no $RCLONE_REMOTE"
    else
        echo "ERRO: a copia para o $RCLONE_REMOTE falhou (o backup local esta ok)"
        exit 1
    fi
else
    echo "AVISO: sem copia fora do servidor (remote $RCLONE_REMOTE do rclone nao configurado)"
fi
