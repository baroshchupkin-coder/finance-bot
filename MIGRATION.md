# Миграция активных счетов на нового Telegram-бота

Скрипт: `migrate_active_invoices.py`

Он переносит только счета со статусом `Согласован`. Счета в статусах `Оплачено`, `Отклонен`, `Отменен` не трогает.

## Переменные Render

Перед запуском должны быть заданы:

```text
BOT_TOKEN=токен старого бота
NEW_BOT_TOKEN=токен нового бота
GOOGLE_CREDENTIALS=текущие Google credentials
```

Лог пишется в лист `logs` в таблице `Finance bot`.

## Проверка без отправки

```bash
python migrate_active_invoices.py --dry-run
```

По умолчанию режим тоже `dry-run`, поэтому можно запустить и так:

```bash
python migrate_active_invoices.py
```

## Проверка одного счета

```bash
python migrate_active_invoices.py --dry-run --request-id 123
```

## Боевой запуск одного счета

```bash
python migrate_active_invoices.py --run --request-id 123
```

## Боевой запуск всех активных счетов

```bash
python migrate_active_invoices.py --run
```

## Если старые сообщения пока не нужно удалять

```bash
python migrate_active_invoices.py --run --keep-old
```

Telegram может не дать удалить сообщения старше 48 часов. Такие ошибки будут записаны в `logs`, но новые сообщения от нового бота уже будут отправлены.
