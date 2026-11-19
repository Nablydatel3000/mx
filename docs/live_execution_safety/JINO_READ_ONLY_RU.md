# Jino: обновить только read-only приёмку

Это инструкция для пользователя, не выполненный deployment. Codex к Jino
не подключался. PAPER и торговый LIVE этой процедурой не обновляются.

## Установка диагностического overlay

Создайте отдельный клон, не используйте каталог работающего PAPER:

```bash
git clone --branch fix/live-current-execution-safety https://github.com/Nablydatel3000/mx.git mx-readonly-safety
cd mx-readonly-safety
sudo python3 deploy/live/update_readonly.py
```

Если папка уже существует, не удаляйте её и не перезаписывайте файлы наугад:
выберите другое имя для нового клона. Обновитель предназначен для известного
старого окружения /opt/gldrubf-live-acceptance/mx, включая базу 96cd8e9.
При неизвестном коде, symlink или занятом read_only.lock он остановится.
Не обходите такой отказ ручным копированием.

Для точной версии 96cd8e9 добавляется **live_market.py**, заменяются только
**market_diagnostics.py и live_acceptance.py**. Старые api.py, live.py,
live_api.py и live_store.py остаются на месте; их хеши и зависимости проверяются
до изменений. Сохраняется закрытый read-only список методов: торговые методы
недоступны даже с торговым токеном. При известной более старой версии диагностики
возможны только дополнительные файлы из прежнего ограниченного списка обновителя.

Копии заменяемого кода — в root-only /opt/gldrubf-live-acceptance/acceptance-code-*.
Не меняются конфиг, токен, SQLite, журналы, отчёты и systemd. Из runtime-состояния открывается только
существующий read_only.lock. **Новый торговый runtime не копируется.**

## Проверка

```bash
sudo gldrubf-live-check calendar-diagnostics
sudo gldrubf-live-check
sudo gldrubf-live-status --json
```

После первой команды сохранится частный calendar_diagnostic.json в
/var/lib/gldrubf-live-acceptance. В нём ожидаются:

- validation_scope: current_execution_only;
- selection.selection: absolute-bounds-msk-day-v1;
- period_from/period_to и excluded_record_indexes с доказанно посторонними днями;
- holiday 21 ноября может иметь parse_ok=false, но **не разрешена** для торговли.

Если неподтверждённая фаза относится к текущей области, границы неполны или
покрытие недостаточно — остановка правильная, её не обходить. При необходимости
передайте только безопасный JSON для частного анализа, без токена/конфига/SQLite.

Полный check обновляет report.json; вспомогательная команда его не затирает,
поэтому до полного check status помечает прежний отчёт как неактуальный для
последней попытки. Отдельно смотрите market.wait_reasons: книга, свежесть цены,
статус или запас времени могут сохранять tradable_now=false после исправления
календарной области. Ценовой предел двух шагов не увеличивается.

## Чего не делать

Не запускать arm, run с торговым разрешением, resume, cancel или LIVE-службу.
Не применять этот overlay к /opt/gldrubf-live или /opt/gldrubf и не заменять там
исходники. Не менять fingerprint в SQLite и не удалять state/STOP/highwater.

Существующая LIVE SQLite с прежним fingerprint **не принята новой версией**.
Переход торгового состояния остановлен до отдельной проверенной процедуры;
успешная read-only приёмка не снимает этот блокер.

Проверка инструкции разработчиком воспроизводится без сервера и API:
`python3 tests/verify_readonly_overlay.py` (Linux, нужен git-объект 96cd8e9).
Она создаёт временную точную копию старого src, применяет overlay, повторяет
обновление и проверяет mock-календарь и запрет торговых методов на старом ядре.
