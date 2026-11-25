# Jino: ручное обновление и откат

Это подготовленные команды, а не выполненное развёртывание. Реальную SQLite
не передавать в GitHub/Codex/чат. Токен не читать, не копировать и не менять.
PAPER, его службу и каталоги не трогать.

## 1. Только при остановленной LIVE-службе

```bash
sudo systemctl is-active gldrubf-live.service
```

Продолжать только для inactive/failed. Если active — обновление на ходу
запрещено; нужна отдельная операторская остановка после актуальной проверки
заявок. Эта инструкция её автоматически не выполняет: существующий shutdown
может отменять собственную pending-заявку. Предыдущие сведения «позиция 0,
заявок 0» не заменяют актуальную сверку.

## 2. Получить код и сохранить прежние файлы

```bash
git clone --single-branch --branch fix/live-transient-api-recovery https://github.com/Nablydatel3000/mx.git ~/mx-api-recovery-review
cd ~/mx-api-recovery-review
git log -1 --oneline
```

Сверить SHA с итоговым отчётом. В этих командах предполагаются прежние пути
`/opt/gldrubf-live/mx`, `/etc/gldrubf-live/config.json`,
`/var/lib/gldrubf-live/live.sqlite3`; при других путях остановиться и уточнить.

```bash
LIVE_RECOVERY_CODE_BACKUP="/opt/gldrubf-live/code-before-api-recovery-$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -o root -g root -m 0750 "$LIVE_RECOVERY_CODE_BACKUP"
sudo cp -a /opt/gldrubf-live/mx/src/gldrubf_bot/live.py /opt/gldrubf-live/mx/src/gldrubf_bot/live_api.py "$LIVE_RECOVERY_CODE_BACKUP/"
printf '%s\n' "$LIVE_RECOVERY_CODE_BACKUP"
```

Сохранить напечатанный путь для отката. Нельзя перезаписывать старые резервные
копии. Для этого варианта требуется уже установленный helper из 7210db7:

```bash
sudo test -x /usr/local/sbin/gldrubf-live-manage
```

Если отсутствует — сначала применить отдельно согласованное исправление
LoadCredential из docs/live_credential_recovery/JINO_PLAN_RU.md.
Не пытаться решить это chmod 0644/0640 master-токена.

## 3. Установить четыре файла, выполнить офлайн-проверку

Служба по-прежнему остановлена. Ни systemd unit, ни config не заменяются:

```bash
sudo install -o root -g root -m 0644 src/gldrubf_bot/live.py /opt/gldrubf-live/mx/src/gldrubf_bot/live.py
sudo install -o root -g root -m 0644 src/gldrubf_bot/live_api.py /opt/gldrubf-live/mx/src/gldrubf_bot/live_api.py
sudo install -o root -g root -m 0644 src/gldrubf_bot/live_recovery.py /opt/gldrubf-live/mx/src/gldrubf_bot/live_recovery.py
sudo install -o root -g root -m 0644 src/gldrubf_bot/live_recovery_upgrade.py /opt/gldrubf-live/mx/src/gldrubf_bot/live_recovery_upgrade.py
cd /opt/gldrubf-live/mx
sudo -u gldrubf-live env PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m src.gldrubf_bot.live_recovery_upgrade
```

Ожидается `offline_upgrade_compatible`. Это ещё ничего не меняет в базе.
При отказе остановиться, передать только фиксированные category/reason;
не исправлять хеши вручную, не удалять базу, sidecars, highwater или STOP.
Для возвращения прежнего кода до миграции достаточно восстановить два файла
из шага 2. Не запускать службу с несовпадающей версией.

## 4. Явно подтвердить офлайн-обновление версии

```bash
sudo -u gldrubf-live env PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m src.gldrubf_bot.live_recovery_upgrade --apply --acknowledge OFFLINE_LIVE_READ_RECOVERY_UPGRADE
```

Ожидается `offline_upgrade_completed`; сохранить поле `backup`, например
`recovery-upgrades/20261125T...Z`. Команда сама создаёт и проверяет приватную
резервную копию SQLite и markers. Она не создаёт новую рабочую базу и не
изменяет торговый счёт. Halt остаётся прежним, STOP сохраняется/создаётся,
LIVE_PERMISSION не обновляется. Поэтому запуск после обновления сам по себе
не разрешит торговлю. При неизвестной исходной версии или pending/UNKNOWN
обновление откажет вместо принятия сомнительного состояния.

## 5. Проверить LoadCredential и сверить реальные данные без заявок

```bash
sudo /usr/local/sbin/gldrubf-live-manage credential-check
sudo /usr/local/sbin/gldrubf-live-manage diagnose
sudo /usr/local/sbin/gldrubf-live-manage sync
```

Продолжать только при exit code 0. Ожидаются credential_access_ok,
read_path_passed, live_management_completed. Последний результат подтверждает
запись снимков в существующую базу; halt_present должен остаться true, если
он был установлен. Фактические позиции/заявки должны совпасть с журналом;
расхождения не принимать автоматически. Sync не отправляет и не отменяет
заявки, не снимает halt/STOP. Retry-сообщения идут отдельно в stderr.

**Здесь процедура заканчивается.** Старый общий halt нельзя автоматически
считать безопасным только по его названию: он мог означать и критическую
ошибку. Его снятие, выдача разрешения для нового fingerprint и запуск службы
требуют отдельного явного решения оператора после сверки. Команды arm/resume/run
в этой инструкции намеренно не выполняются. Для будущих однозначно временных
сбоев уже работающий новый цикл не потребует этих ручных действий.

После отдельно разрешённого запуска наблюдать без изменения торговли:

```bash
sudo journalctl -u gldrubf-live.service -n 100 --no-pager
sudo -u gldrubf-live cat /var/lib/gldrubf-live/health.json
```

WAIT_API_RECOVERY означает запрет новых заявок. Только
API_CONNECTION_RECONCILED подтверждает завершённую повторную сверку; дальнейшее
исполнение всё равно проходит обычные рыночные/финансовые гейты.
Не перезапускать службу вслепую и не удалять STOP при длительном outage.

## Откат без подмены SQLite

Только при inactive/failed, до новых заявок/исполнений после обновления.
Указать фактический путь backup из шага 4; пример ниже нельзя копировать
буквально с многоточием:

```bash
cd /opt/gldrubf-live/mx
sudo -u gldrubf-live env PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m src.gldrubf_bot.live_recovery_upgrade --rollback-backup /var/lib/gldrubf-live/recovery-upgrades/ACTUAL_BACKUP_DIRECTORY
sudo -u gldrubf-live env PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m src.gldrubf_bot.live_recovery_upgrade --rollback-backup /var/lib/gldrubf-live/recovery-upgrades/ACTUAL_BACKUP_DIRECTORY --apply --acknowledge OFFLINE_LIVE_READ_RECOVERY_UPGRADE
```

После успеха восстановить прежние два файла из сохранённого пути шага 2:

```bash
sudo cp -a "$LIVE_RECOVERY_CODE_BACKUP/live.py" "$LIVE_RECOVERY_CODE_BACKUP/live_api.py" /opt/gldrubf-live/mx/src/gldrubf_bot/
```

В новой shell переменная LIVE_RECOVERY_CODE_BACKUP не сохранится — сначала
задать её точным ранее записанным путём. Восстановление версии выполняется
**до** возврата старого кода. Текущая SQLite не заменяется backup, её новые
журналы сохраняются. STOP/halt и необходимость отдельного допуска остаются.
При execution_history_changed откат запрещён: сохранить всё и провести
проверку состояния; нельзя обходить отказ копированием старой базы.
