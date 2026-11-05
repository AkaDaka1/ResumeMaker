# Спринт 1 — план и границы

## Цель

Фундамент, на котором можно безопасно строить интервью, нормализацию и рендеринг: доменные модели,
явные состояния операций, PostgreSQL-схема и ledger с идемпотентным списанием. Все внешние
интеграции заменены fake-провайдерами.

## Стек

- Python 3.11+ (проверено на 3.15), `src/`-layout, пакет `resumemaker`.
- FastAPI — тонкий application-слой (health/readiness, позже bot script).
- SQLAlchemy 2.0 (sync) + psycopg3 + Alembic.
- Pydantic v2 + pydantic-settings — конфиг и будущие контракты профиля.
- pytest; интеграционные тесты идут против настоящего PostgreSQL.

## Карта файлов

```text
src/resumemaker/
  config.py                  Settings; production/staging отклоняются, пока есть только fake
  domain/enums.py            операции, ledger, платежи, job, согласия, языки
  domain/errors.py           доменные ошибки
  domain/states.py           автоматы operation/payment/job
  domain/pricing.py          цены в кредитах и перевод provider-tokens -> кредиты
  db/base.py, models.py      DeclarativeBase, таблицы
  db/session.py              engine/session factory, unit_of_work
  services/ledger.py         журнал, идемпотентность, запрет овердрафта
  services/operations.py     атомарный платный старт, завершение, refund
  services/payments.py       сессии, webhook, дедупликация, начисление, refund
  services/jobs.py           очередь и ограниченные повторы
  services/consents.py       согласия с версией и hash текста
  services/usage.py          usage провайдера отдельно от ledger
  providers/payments/        протокол + fake с HMAC-подписью
  providers/llm/             протокол + fake + классификация ошибок AnyModel
  api/app.py                 /healthz, /readyz, локальный fake-webhook
migrations/                  Alembic, начальная схема 0001
tests/                       pytest против реального PostgreSQL
scripts/test.ps1             одноразовый кластер + прогон тестов
```

## Что входит в спринт

1. Доменные enum'ы и конечные автоматы операций, платежей и job.
2. Таблицы: `users`, `ledger_entries`, `operations`, `consents`, `payments`, `payment_events`,
   `jobs`, `llm_usage`.
3. Ledger как неизменяемый журнал: баланс = сумма подтверждённых записей, запрет овердрафта,
   идемпотентность по `idempotency_key`, блокировка пользователя на время записи.
4. Атомарный старт платной операции: `debit + operation + job` в одной транзакции.
5. Завершение операции по фактической стоимости с возвратом разницы; refund при сбое.
6. Fake payment provider с HMAC-подписью webhook, fake LLM provider со скриптами и usage.
7. Платёжный сервис: создание сессии, проверка подписи, сверка суммы/валюты/назначения,
   дедупликация событий, начисление кредитов ровно один раз, refund.
8. Согласия: два независимых вида (обработка данных и передача внешнему LLM) с версией текста.
9. Начальная Alembic-миграция и тесты (ledger, операции, платежи, LLM, автоматы, миграции, API).

## Что сознательно не входит

- Telegram Bot API, реальные платежи (Stars/СБП), реальные ключи AnyModel.
- Interview/Normalizer/Renderer — только контракты провайдеров и состояния.
- Схема профиля кандидата и PDF-шаблоны.
- Retention-воркеры и удаление данных сверх soft-delete поля.

## Инварианты, которые защищают тесты

| Инвариант | Где проверяется |
|---|---|
| Повторный дебет по тому же `operation_id` не списывает дважды | `tests/test_ledger.py` |
| Баланс не уходит в минус при конкурентных списаниях | `tests/test_ledger.py` (две сессии, реальные row locks) |
| Списание, operation и job создаются атомарно | `tests/test_operations.py` |
| Сбой операции возвращает кредит ровно один раз | `tests/test_operations.py` |
| Webhook без валидной подписи отклоняется | `tests/test_payments.py` |
| Повторный webhook не начисляет кредиты повторно | `tests/test_payments.py` |
| Несовпадение суммы/валюты/назначения отклоняется | `tests/test_payments.py` |
| Оба согласия обязательны и отзыв учитывается | `tests/test_consents.py` |
| Миграция применяется и откатывается на чистой БД | `tests/test_migrations.py` |

## Явные границы для следующего спринта

- `ConsentService.require_both` вызывает application/bot script перед стартом платной операции;
  сервис операций не проверяет согласия сам, чтобы не дублировать политику в двух местах.
- Реальный платёжный адаптер потребует transactional outbox: сейчас fake-провайдер вызывается
  внутри транзакции, что для сетевого вызова недопустимо.
- Восстановление «зависших» операций (job в `queued`, кредит зарезервирован) — отдельный worker
  в следующем спринте; ручной путь уже есть через идемпотентные `fail`/`cancel`.
