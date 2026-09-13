# Локальная разработка backend

Инструкция для разработчика: запустить API с PostgreSQL и Redis, применить
миграции и выполнить проверки. `compose.yaml` предназначен для локального
окружения. Настройка сервера, HTTPS и production-секретов выполняется при деплое.

## Подготовьте окружение

Нужны Docker Engine с плагином `docker compose`, Python 3.12.14 и uv 0.12.13.
Выполняйте команды из корня репозитория. Для установки uv в отдельное Python
окружение используйте [инструкцию uv](https://docs.astral.sh/uv/getting-started/installation/).

Проверьте инструменты:

```bash
docker version
docker compose version
uv --version
```

Если `.env` ещё нет, создайте его из шаблона:

```bash
test -f .env || cp .env.example .env
chmod 600 .env
```

Сгенерируйте пароль только для локальной PostgreSQL:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Запишите результат в `POSTGRES_PASSWORD` в `.env`. Hex-строка безопасна для
подстановки в URL подключения; произвольные символы `@`, `:`, `/` в этом
параметре потребуют URL-кодирования и не подходят для готового Compose-файла.
Не добавляйте `.env` в Git. Реальные токены MAX для сборки, health-проверок и
автотестов не требуются. Без настройки MAX API отклоняет попытку входа с HTTP
`503`; реальный вход настраивается при деплое.

Параметры Compose задаются в `.env`:

| Переменная | Значение по умолчанию | Назначение |
|---|---|---|
| `POSTGRES_USER` | `crm` | Пользователь локальной PostgreSQL |
| `POSTGRES_DB` | `crm` | База локального приложения |
| `POSTGRES_PASSWORD` | Обязательна | Локальный пароль, без значения в шаблоне |
| `API_PORT` | `8000` | Порт API на `127.0.0.1` |
| `TEST_POSTGRES_PORT` | `15432` | Порт отдельной тестовой PostgreSQL |
| `TEST_REDIS_PORT` | `16379` | Порт отдельного тестового Redis |

Compose формирует `DATABASE_URL` и `REDIS_URL` для API с адресами `postgres` и
`redis` внутри сети Docker. Порты основной PostgreSQL и Redis не публикуются на
хосте. Если запускаете несколько копий проекта, задайте отдельные порты и
одинаковый для всех команд каждой копии `--project-name`.

Для входа через MAX Compose передаёт `MAX_STAFF_TOKEN`, `MAX_OWNER_IDS` и
`MAX_EMPLOYEE_IDS` из `.env`. Списки ID содержат положительные целые числа через
запятую. `MAX_OBSERVER_TOKEN` используется отдельно для Observer. Оставляйте
реальные токены и ID пустыми до подготовки окружения интеграции. Подписанные
данные запуска Mini App проверяются backend; ID из обычного HTTP-запроса не
подтверждает личность сотрудника.

## Запустите приложение

1. Проверьте конфигурацию без вывода значений секретов:

   ```bash
   docker compose config --quiet
   ```

2. Соберите образ с зависимостями из `uv.lock`:

   ```bash
   docker compose build api
   ```

3. Запустите PostgreSQL и Redis и дождитесь успешных healthcheck:

   ```bash
   docker compose up --detach --wait postgres redis
   ```

4. Примените миграции к локальной базе:

   ```bash
   docker compose run --rm api alembic upgrade head
   ```

5. Запустите API:

   ```bash
   docker compose up --detach --wait api
   ```

6. Проверьте процесс и доступность зависимостей:

   ```bash
   curl --fail http://127.0.0.1:8000/health/live
   curl --fail http://127.0.0.1:8000/health/ready
   ```

   Обе команды должны вернуть HTTP `200`. Если меняли `API_PORT`, замените
   `8000` в URL. OpenAPI доступен по `http://127.0.0.1:8000/docs`.

Миграции запускаются отдельной командой. Контейнер приложения сам не изменяет
схему при старте. Образ использует непривилегированного пользователя; Compose
делает его файловую систему доступной только для чтения, кроме `/tmp`.

Uvicorn запускается с `--no-access-log`: HTTP-журнал приложения сохраняет
шаблон маршрута без query string, чтобы данные запуска MAX не попали в журнал.

После изменения кода пересоберите API:

```bash
docker compose up --detach --build --wait api
```

Если изменение включает миграцию, сначала выполните шаги сборки и применения
миграций. Данные PostgreSQL сохраняются в томе `postgres_data`. Redis хранит
временное состояние в памяти.

## Выполните проверки

Установите зависимости, включая инструменты разработки:

```bash
uv sync --frozen
```

Проверьте стиль и типы:

```bash
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy app
```

Для интеграционных тестов запустите отдельные сервисы профиля `test`:

```bash
docker compose --profile test up --detach --wait postgres-test redis-test
```

Тестовая база называется `crm_test`; её данные размещаются в `tmpfs` и
исчезают после остановки контейнера. Используйте эту базу только для тестов.
Основная база приложения остаётся в отдельном сервисе `postgres`.

Настройте подключения в текущем терминале:

```bash
export APP_ENV=test
export TEST_DATABASE_URL='postgresql+asyncpg://crm:LOCAL_PASSWORD@127.0.0.1:15432/crm_test'
export TEST_REDIS_URL='redis://127.0.0.1:16379/1'
export DATABASE_URL="$TEST_DATABASE_URL"
export REDIS_URL="$TEST_REDIS_URL"
```

Замените `LOCAL_PASSWORD` на локальный `POSTGRES_PASSWORD` из `.env`. Если меняли
имя пользователя или тестовые порты, обновите их в URL. Эти значения должны
указывать на выделенное тестовое окружение без рабочих данных.

Примените миграции, проверьте соответствие ORM и запустите тесты:

```bash
uv run --frozen alembic upgrade head
uv run --frozen alembic check
uv run --frozen pytest
```

Проверка `alembic check` должна завершиться без новых операций обновления
схемы. Полная проверка требует PostgreSQL и Redis: один запуск только
изолированных unit-тестов не заменяет интеграционные проверки.

GitHub Actions выполняет установку из lock-файла, Ruff, mypy, миграции,
интеграционные тесты с отдельными PostgreSQL/Redis, проверку Compose и сборку
Docker. Workflow имеет права `contents: read`; сборка образа не публикует его
и не выполняет деплой. Пароли в CI относятся только к одноразовым сервисам job.

## Диагностируйте запуск

Проверьте состояние и последние журналы:

```bash
docker compose ps
docker compose logs --tail 100 api
docker compose logs --tail 100 postgres redis
```

HTTP `503` от `/health/ready` означает, что приложение не готово обслуживать
запросы; проверьте сервисы, подключения и миграции. Healthcheck контейнера
проверяет `/health/live` и подтверждает доступность процесса.

Если Compose сообщает, что не задан `POSTGRES_PASSWORD`, заполните `.env`.
Если порт API занят, измените `API_PORT`. Изменение пароля в `.env` не меняет
пароль уже созданного пользователя PostgreSQL в существующем томе.

Для паузы локального окружения остановите его сервисы:

```bash
docker compose stop api postgres redis
docker compose --profile test stop postgres-test redis-test
```

Том основной PostgreSQL сохраняется. Перед передачей журналов удалите из них
секреты и пользовательские данные.

## Основания конфигурации

Версии образов сверены с официальными списками
[Python](https://github.com/docker-library/official-images/blob/master/library/python),
[PostgreSQL](https://github.com/docker-library/official-images/blob/master/library/postgres)
и [Redis](https://github.com/docker-library/official-images/blob/master/library/redis).
Установка uv в образ следует [руководству uv для Docker](https://docs.astral.sh/uv/guides/integration/docker/).
При обновлении версий меняйте Compose, Dockerfile и CI согласованно и повторяйте
проверки на чистой тестовой базе.
## Работа без веб-интерфейса

Актуальные сценарии бота и настройка справочника командами описаны в
[простом боте](simple-bot.md). Для ответов MAX дополнительно запускается
`uv run python -m app.bot.worker` или профиль Compose `bot`.
Без реального env этот процесс не запускайте; локальные тесты используют моки MAX.
