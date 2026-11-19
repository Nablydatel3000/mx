# План диагностики на Jino — для отдельного согласования

Ниже подготовленные команды, **Codex их на Jino не выполнял**. Согласование
deployment/применения этим отчётом не подразумевается. Старый sync повторять
вслепую, запускать LIVE или снимать halt/STOP не нужно.

1. Согласовать запуск read-only диагностики. Уточнить Linux-пользователя и те же
   config/state/credential пути, которые использовались при сбойном sync.
   Смена пользователя может скрыть проблему прав; chmod/chown не предлагать
   как автоматическое исправление. Токен никому не пересылать.
2. После согласования создать **отдельный** клон вне /opt/gldrubf-live и PAPER:

```bash
git clone --branch fix/live-sync-safe-diagnostics https://github.com/Nablydatel3000/mx.git mx-sync-diagnostics
cd mx-sync-diagnostics
```

3. Выполнить из этого клона (пример для исходного sync через sudo и штатных путей):

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 python3 -m src.gldrubf_bot.diagnose_live_sync \
  --config /etc/gldrubf-live/config.json \
  --state /var/lib/gldrubf-live \
  --credential-file /etc/gldrubf-live/trading-token
```

Не копировать новые live.py/live_api.py/live_store.py в работающий каталог.
Все fingerprint-файлы в клоне совпадают с 0f52907, новый диагностический модуль
в fingerprint не входит. Несовпадение состояния всё равно остановит проверку,
никакого принятия базы или миграции нет.

Команда делает только чтения API и обновляет SQLite-копию в памяти. База,
маркеры, токен, служба и halt на VPS не изменяются. Файловый lock берётся только
на время чтения и освобождается своим процессом; торговая остановка не снимается.
Если runner занят, файлов нет или есть WAL/SHM/journal — не удалять ничего,
передать фиксированную причину для дальнейшего разбора.

4. Передать безопасный JSON вывода: result, failure, checks, broker_calls и
   operations_range. В нём нет содержимого токена, номера счёта или произвольного
   API body. Не прикладывать config, credential, SQLite, raw-ответы или traceback.

Пример диагностического отказа (пример из mock-теста):

```json
{"stage":"broker_operations","category":"validation_error","reason":"operation_account_echo_mismatch"}
```

Иные исходы:

- account_runner_lock/database_runner_lock: путь/права/занятый runner;
- ledger_*: fingerprint, семантика, markers или sidecars; ничего не пересоздавать;
- broker_permissions: доступ выбранного счёта;
- broker_reconciliation: заявки/позиция/схема счёта;
- broker_operations: поля операций/диапазон/пагинация;
- statistics_in_memory: построение статистики на копии;
- read_path_passed: текущий read-path прошёл, но старый sync не исправлен и записи
  на диск не проверены. Это основание отдельно разбирать прошлый transient-сбой
  или локальный write-path, а не команда resume/arm.

После установления причины потребуется отдельное точечное решение. Любое
будущее изменение fingerprint-файлов должно учитывать существующую LIVE SQLite;
данная диагностика специально не создаёт этого нового блокера.
