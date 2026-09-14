# Автоматический деплой development

Push в `main` запускает существующий workflow `CI`. После его успешного завершения
workflow `Deploy development` передаёт серверу точный проверенный commit SHA.
Секреты приложения остаются только в `/srv/max_admin/.env` на сервере.

Сервер принимает только SSH-команду `deploy <40-символьный SHA>` через отдельный
ключ с `restrict` и forced command. Скрипт проверяет, что SHA равен текущему
`origin/main`, блокирует параллельные деплои, собирает образы, применяет миграции,
перезапускает Compose-проект `max_admin` и проверяет публичный `/health/ready`.
При ошибке запуска скрипт пересобирает и возвращает предыдущую версию приложения.
Миграции должны оставаться обратно совместимыми.

GitHub Environment `development` содержит:

- `DEPLOY_HOST`;
- `DEPLOY_USER`;
- `DEPLOY_SSH_PRIVATE_KEY`;
- `DEPLOY_KNOWN_HOSTS`.

Рабочая копия деплоя находится в `/srv/max_admin`. Последний успешно развернутый
SHA записывается в `/var/lib/max-admin/deployed-sha`. Ручная диагностика:

```bash
docker compose -p max_admin -f /srv/max_admin/compose.yaml ps
curl --fail https://dev.xn--e1aikcel5c5a.online/health/ready
```
