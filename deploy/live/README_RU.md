# Отдельный LIVE GLDRUBF: файлы и будущий запуск

**На этой стадии LIVE не включать:** код проверен с mocks, но реальные trading
API-ответы, IOC и финансовые параметры ещё не приняты. Полный перечень блокеров:
[технический отчёт](../../docs/LIVE_PREPARATION_RU.md). Здесь приведены команды
для будущего согласованного размещения/запуска. Codex их на VPS не выполнял.
Работающий PAPER и `/var/lib/gldrubf` не изменять.

## Отдельные файлы

Нужны отдельный выделенный обычный брокерский счёт, без других инструментов,
ручной торговли и других ботов, и один VPS-исполнитель. API не гарантирует общий
предел ±2 при вмешательстве другого клиента. Начальный бюджет 15000 ₽ и цель
2 контракта жёстко закреплены; PAPER не является источником реальной позиции.

Будущие пути: `/opt/gldrubf-live/mx`, `/etc/gldrubf-live`, `/var/lib/gldrubf-live`.
Пользователь `gldrubf-live` отличается от PAPER-пользователя. Подготовка на Ubuntu
24.04/systemd, **только после закрытия блокеров**:

```bash
sudo useradd --system --user-group --home-dir /var/lib/gldrubf-live --shell /usr/sbin/nologin gldrubf-live
sudo install -d -m 0755 /opt/gldrubf-live
sudo git clone --branch feature/gldrubf-live-gated https://github.com/Nablydatel3000/mx.git /opt/gldrubf-live/mx
sudo install -d -m 0750 -o root -g gldrubf-live /etc/gldrubf-live
sudo install -d -m 0700 -o gldrubf-live -g gldrubf-live /var/lib/gldrubf-live /var/lib/gldrubf-live/locks
sudo install -m 0640 -o root -g gldrubf-live /opt/gldrubf-live/mx/deploy/live/config.example.json /etc/gldrubf-live/config.json
sudoedit /etc/gldrubf-live/config.json
```

Не удалять существующего пользователя/каталоги, если они уже созданы. В конфиге
надо локально указать точный account_id и осознанно выбрать все risk/freshness-
параметры. `null` намеренно недопустимы. Значения тестов не переносить в production.
Политики: возраст завершённой свечи в UTC-часах, свежесть котировки/стакана и
снимка счёта, коэффициент роста ГО ≥1, резерв RUB, предельная комиссия на одну
заявку, предел slippage в ticks, тайм-аут заявки, poll seconds и буфер конца
сессии ≥20 s. Прочие инструменты/объём/капитал автоматически не подбираются.
Счёт выбирается явно; неизвестный/неактивный/неподходящий тип блокируется.

## Токен — только скрытым локальным вводом

Не вставлять токен в команду, Git, конфиг JSON, shell history, чат или Codex.
На самом VPS в интерактивном SSH-терминале:

```bash
cd /opt/gldrubf-live/mx
sudo python3 -m src.gldrubf_bot.live install-token
```

Ввод скрыт. Создаётся **новый** `/etc/gldrubf-live/trading-token` с правами 0600;
существующий файл не перезаписывается. Его владелец root. Это только сохранение
credential: не происходит подключения к API, отправки заявки или запуска службы.
Не использовать PAPER `.env`, credential или пользователя. systemd впоследствии
передаст токен службе через `LoadCredential`, без token в argv/EnvironmentFile.
Ротация — отдельная остановленная процедура с новым credential, не через Git.

## Инициализация и read-only проверка

```bash
cd /opt/gldrubf-live/mx
sudo -u gldrubf-live python3 -m src.gldrubf_bot.live init
```

Это offline-инициализация, disarmed. Повторное использование чужой/PAPER базы
запрещено. Не копировать PAPER SQLite в LIVE и не менять в ней объём/капитал.

Для будущей **проверки без отправки заявок**:

```bash
sudo python3 -m src.gldrubf_bot.live preflight
```

Команда читает локальный root-only credential и текущие брокерские показатели,
выводит ГО обеих сторон, RUB/пункт и capital-gate для LONG/SHORT. Оценка комиссии
здесь — заданный резерв `max_order_fee_rub`, не фактический тариф; перед реальной
заявкой будет отдельный GetOrderPrice. Никаких резервирований/ордеров нет. Этот
preflight в Codex **не запускался** с настоящим API. Если ответ не подтверждён,
разобрать причину; нельзя обходить проверки или увеличивать допустимый объём.

## Отдельное разрешение и активация

Разрешение связано с конфигом, SHA256 кода, ledger UUID и выбранным сроком UTC. Изменение
конфига не переоценивает старую базу автоматически. Сначала получить согласованный
допуск, затем вручную выбрать конкретное будущее UTC-время окончания разрешения:

```bash
sudo -u gldrubf-live python3 -m src.gldrubf_bot.live arm --acknowledge LIVE_GLDRUBF_2_CONTRACTS_15000_RUB_EXCLUSIVE --expires-utc ВЫБРАННОЕ_БУДУЩЕЕ_ВРЕМЯ_UTC
```

Последний аргумент надо заменить ISO-временем `YYYY-MM-DDTHH:MM:SSZ`.
Команда не отправляет заявки и не снимает STOP/halt. Нужны одновременно это
разрешение, runtime-флаг `--allow-live`, действующий trading token и все gates.
Срок разрешения не продлевается автоматически.

Только после отдельной приёмки реального API/финансов/восстановления:

```bash
sudo install -m 0644 deploy/live/gldrubf-live.service /etc/systemd/system/gldrubf-live.service
sudo install -m 0755 deploy/live/status.sh /usr/local/bin/gldrubf-live-status
sudo systemctl daemon-reload
sudo systemctl enable --now gldrubf-live.service
```

Это будущие действия пользователя, **не выполненный deployment**. При первой
истории позиций/заявок быть не должно; bootstrap ждёт нового подтверждённого
пересечения, исторический SHORT/LONG не открывается. Нельзя создавать искусственный
сигнал или включать legacy history replay для проверки настоящей заявки.

## Просмотр и остановка

Короткая команда статистики при работающей службе, без токена:

```bash
sudo -u gldrubf-live gldrubf-live-status
sudo journalctl -u gldrubf-live.service -n 30 --no-pager
```

Статистика читает кэш брокера; смотрите `as_of`/`operations_as_of`. Вывод приватен:
есть идентификаторы собственных заявок/исполнений. `account` содержит баланс,
ликвидность, доступные RUB и брокерскую текущую переоценку; `broker_fills` — реальные
исполнения; `realized_settlement_vm_cash_rub` — брокерский settled VM, включая
удерживаемые позиции. Это не модельный P&L закрытых сделок. Отдельный closed P&L
может быть null; операции/комиссии не суммируются дважды. `errors` и `halt`
показывают остановку, а `commission_coverage` — полноту комиссионных полей.
Telegram/внешние сообщения не подключены.

Аварийная остановка новых отправок:

```bash
cd /opt/gldrubf-live/mx
sudo -u gldrubf-live python3 -m src.gldrubf_bot.live kill
sudo systemctl stop gldrubf-live.service
```

Живой цикл отменяет свои pending; SIGTERM тоже пытается отменить до освобождения
locks. При потере связи отмена не гарантирована — **проверьте реальные открытые
заявки в приложении брокера**. Позиция автоматически не ликвидируется и остаётся
рыночным риском. Аварийная отмена только своих UUID при остановленной службе:

```bash
sudo python3 -m src.gldrubf_bot.live cancel --allow-live
```

Это уже реальный запрос отмены, а не read-only команда; здесь он не выполнялся.
Просроченное permission/STOP не запрещают отмену собственных заявок, но запрещают
новый PostOrder. Чужие заявки не отменяются.

После обычного сбоя с подтверждёнными состояниями, при остановленной службе:

```bash
sudo python3 -m src.gldrubf_bot.live sync
sudo python3 -m src.gldrubf_bot.live resume --allow-live --acknowledge LIVE_GLDRUBF_2_CONTRACTS_15000_RUB_EXCLUSIVE
```

Сначала сверка по UUID/фактической позиции/операциям, затем явный resume с ещё
действующим разрешением. UNKNOWN/not-found, отклонённые/отменённые заявки и
terminal partial не обходятся этим resume. Нужен отдельный аудированный разбор;
не удалять строки/базу/highwater, не создавать новый UUID «проверить ещё раз».
Возврат службы выполняется пользователем только после устранения причины.

## Хранение и ограничения

База, highwater, initialized marker, permission, STOP, snapshots/история находятся
в `/var/lib/gldrubf-live`. Не заменять базу при работе и не восстанавливать старую
копию в одиночку. Для backup нужен SQLite online backup согласованно с отдельным
highwater; полной автоматизированной LIVE-backup/restore процедуры пока нет.
History/операционные snapshots растут, retention и внешний монитор пока требуют
настройки до постоянного LIVE. Старый PAPER-backup не подходит к новой схеме.
Обнаруженная потеря/несогласованный откат ledger блокирует новые заявки, не чинится
пустой базой. Если одновременно откатить DB, highwater и initialized marker,
локально невозможно доказать, что за пределами копии не было новых сделок при
совпадающей итоговой позиции. Нужна независимая сверка с брокерской историей;
автоматическое восстановление такой старой копии не поддерживается.
Использовать локальную файловую систему; сетевые/NFS каталоги и распределённые
запуски на нескольких VPS не поддерживаются. Обновление кода требует остановки
и отдельной согласованной миграции/проверки ledger; нельзя обходить новый hash
удалением базы или ручным исправлением config_hash.

Поддержка IOC на GLDRUBF, актуальная спецификация/ГО, свежесть/маржа, комиссии,
финансирование и реальное восстановление после неоднозначного HTTP должны пройти
отдельную приёмку. Unit-тесты не означают, что запуск реальной торговли безопасен.
