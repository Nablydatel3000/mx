# Исправление credential_access: план применения, не выполненное развёртывание

Ветка `fix/live-credential-maintenance`, база `11de6c6` (торговое ядро `0f52907`).
Команды ниже предназначены для пользователя после согласования обновления.
Они не запускают LIVE, не снимают halt/STOP и не отправляют/отменяют заявки.
Доступа к Jino у автора изменения нет.

## 1. Проверить метаданные, не читать токен

На Jino:

```bash
sudo systemctl is-active gldrubf-live.service
sudo stat -c '%U:%G %a %n' /etc/gldrubf-live /etc/gldrubf-live/trading-token /etc/gldrubf-live/config.json /var/lib/gldrubf-live
sudo -u gldrubf-live test -r /etc/gldrubf-live/config.json
sudo -u gldrubf-live test -r /var/lib/gldrubf-live/live.sqlite3
sudo -u gldrubf-live test -w /var/lib/gldrubf-live/live.sqlite3
```

LIVE-служба должна быть inactive/failed. Если активна — обслуживание не начинать;
не останавливать её автоматически. SQLite должна оставаться прежней.
Токен: root, обычный файл, 0600. Каталог состояния: gldrubf-live, 0700.
Для `/etc/gldrubf-live` подходит root:gldrubf-live 0750, для config.json —
root:gldrubf-live 0640. Если доступ к конфигурации сейчас отсутствует, допустимо
исправить только эти два объекта (не токен и не SQLite):

```bash
sudo chown root:gldrubf-live /etc/gldrubf-live /etc/gldrubf-live/config.json
sudo chmod 0750 /etc/gldrubf-live
sudo chmod 0640 /etc/gldrubf-live/config.json
```

Не использовать chown -R, chmod -R, cat токена или передачу токена аргументом.
Существующий install-token повторно не запускать: он создаёт новый файл
эксклюзивно, существующий токен сохраняется.

## 2. Установить только модуль обслуживания

Получить ветку в отдельный каталог; работающая копия и PAPER не переключаются:

```bash
git clone --single-branch --branch fix/live-credential-maintenance https://github.com/Nablydatel3000/mx.git ~/mx-credential-review
cd ~/mx-credential-review
git log -1 --oneline
```

Перед копированием убедиться, что десять fingerprint-файлов в установленной
копии действительно совпадают с этой веткой:

```bash
sudo python3 - <<'PY'
from pathlib import Path
names=('live.py','live_api.py','live_store.py','live_market.py','live_errors.py','strategy.py','api.py','autonomous.py','locking.py','history.py')
for name in names:
    assert (Path('src/gldrubf_bot')/name).read_bytes()==(Path('/opt/gldrubf-live/mx/src/gldrubf_bot')/name).read_bytes(), 'STOP: installed core differs'
print('Core identical; no fingerprint migration')
PY
```

При несовпадении остановиться, не переписывать хеши и не копировать ядро.
После успешной проверки:

```bash
sudo install -o root -g root -m 0644 src/gldrubf_bot/diagnose_live_sync.py /opt/gldrubf-live/mx/src/gldrubf_bot/diagnose_live_sync.py
sudo install -o root -g root -m 0644 src/gldrubf_bot/live_credential_entrypoint.py /opt/gldrubf-live/mx/src/gldrubf_bot/live_credential_entrypoint.py
sudo install -o root -g root -m 0755 deploy/live/manage.sh /usr/local/sbin/gldrubf-live-manage
```

Это добавляет два внешних модуля, не заменяет fingerprint-файлы. Если эти
модули уже содержат другие локальные изменения — сначала остановиться и
согласовать их. Не копировать systemd unit, не выполнять daemon-reload,
enable/start/restart, arm или resume в рамках этого восстановления.
Подготовленное изменение unit в репозитории относится к отдельному будущему
обновлению службы; сейчас используется только временная служба обслуживания.

## 3. Проверить доставку и сверить счёт

Выполнять последовательно, продолжать только при exit code 0:

```bash
sudo /usr/local/sbin/gldrubf-live-manage credential-check
sudo /usr/local/sbin/gldrubf-live-manage diagnose
sudo /usr/local/sbin/gldrubf-live-manage sync
```

Первый результат: `credential_access_ok`. Второй: `read_path_passed`.
Третий: `live_management_completed`, `halt_present: true`; ожидаемые по
предыдущему снимку позиция 0 и local_orders_count 0 должны быть подтверждены
заново, а не приняты как исходная истина.

PID1 читает root:0600 master через LoadCredential; дочерний Python работает
только от gldrubf-live. Никаких брокерских заявок эти команды не отправляют и
не отменяют. diagnose не пишет в реальную базу; sync делает обязательную
сверку фактических позиций/заявок и операций, записывает реальные снимки в
существующую SQLite. Успех diagnose не доказывает возможность записи:
это подтверждается только успешным sync на Jino.

Если halt неожиданно отсутствует, позиция/заявки отличаются от ожидаемых или
любая команда завершилась ошибкой — остановиться и передать безопасный JSON.
Не повторять sync вслепую; не удалять SQLite, sidecar, highwater, STOP или
LIVE_PERMISSION.json; не менять fingerprint.

## 4. Почему пока не resume

Существующий resume требует --allow-live, явный ACK, действующее разрешение с
совпадающими ledger_id/fingerprint и отсутствие неблагоприятных заявок.
Затем он действительно удаляет STOP и halt. Поэтому он непригоден для этой
задачи: разрешение LIVE ещё не выдавалось, снимать остановку запрещено.
Поведение resume намеренно не изменено. После успешного sync робот остаётся
остановленным; разрешение дальнейшего запуска — отдельное решение.

## Непроверенное

На Jino необходимо подтвердить фактическую доставку LoadCredential текущей
версией systemd, доступ к каталогам, запись снимков и сохранение halt/STOP.
Нет подтверждения реальной IOC-заявки, частичных исполнений, фактических
комиссий или восстановления после сбоя при передаче реальной заявки.
Эти действия данное исправление не выполняет и не разрешает.
