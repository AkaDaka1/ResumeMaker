# Changelog

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версии следуют [Semantic Versioning](https://semver.org/lang/ru/).

## [Unreleased]

## [0.1.0] — 2026-11-05

Первый публичный срез: фундамент Sprint 1. Внешние интеграции намеренно заменены fake-провайдерами.

### Added

- Доменные enum'ы, ошибки, конечные автоматы операций, платежей и job, цены в кредитах.
- PostgreSQL-схема (`users`, `ledger_entries`, `operations`, `consents`, `payments`,
  `payment_events`, `jobs`, `llm_usage`) и начальная Alembic-миграция.
- Ledger как неизменяемый журнал: баланс = сумма записей, идемпотентность по ключу,
  запрет овердрафта, блокировка пользователя при конкурентных списаниях.
- Атомарный старт платной операции (`debit + operation + job`), завершение по фактической
  стоимости с возвратом разницы, идемпотентные `fail`/`cancel`.
- Платёжный сервис: сессии, проверка HMAC-подписи, сверка суммы/валюты/назначения,
  дедупликация событий, начисление ровно один раз, возвраты.
- Fake payment provider и fake LLM provider; классификация ошибок AnyModel.
- Раздельные согласия на обработку данных и передачу внешнему LLM с версией и hash текста.
- Учёт usage провайдера отдельно от внутреннего ledger.
- FastAPI-приложение: `/healthz`, `/readyz`, локальный fake-webhook за флагом.
- 113 тестов против реального PostgreSQL; скрипт одноразового тестового кластера.

[Unreleased]: https://github.com/AkaDaka1/ResumeMaker/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AkaDaka1/ResumeMaker/releases/tag/v0.1.0
