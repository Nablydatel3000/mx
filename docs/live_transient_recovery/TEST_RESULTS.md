# Фактически выполнено 25.11.2026

Локальный Linux, uid=1000. Все брокерские ответы/заявки в тестах — имитация.
Реальные API счёта, токен и Jino не использовались.

| Проверка | Результат |
|---|---|
| Python 3.12: `python3 -W error::ResourceWarning -m unittest discover -s tests` | 466 тестов, 465 успешно, 1 пропущен; 25.298 s |
| Python 3.13: `/usr/bin/python3.13 -W error::ResourceWarning -m unittest discover -s tests` | 466 тестов, 465 успешно, 1 пропущен; 25.511 s |
| `python3 -m compileall -q src tests` | успешно |
| `bash -n deploy/live/manage.sh` | успешно |
| `systemd-analyze verify deploy/live/gldrubf-live.service` | успешно, статическая проверка; службу не запускали |
| `git diff --check` | успешно |
| Frozen legacy bundle | Store/Strategy/API/PAPER/locking/history/calendar/error certificate побайтово совпадают с известным ядром; проверяет upgrade regression |
| Контроль утечки в итоговых test logs | Нет traceback, ResourceWarning или известного приватного текста технических исключений |

Добавлено 49 тестов: 34 для retry/recovery, 14 для offline upgrade/rollback,
1 для совместимости maintenance stdout JSON со stderr retry-журналами.
Пропущен прежний тест credential-доставки, требующий root/chown/setuid.
Реальный отказ файлового доступа под непривилегированным uid проверяется
существующим тестом. Root-эскалация не выполнялась.

Проверено: воспроизведение GetOrders/transport_or_request_error;
GetOperationsByCursor/HTTP 500/gRPC 13/API 70001 после 6h11; реальный parser
HTTP envelope на mock transport; 429/80002, 500/70002/70003, 502/503/504,
тайм-ауты до ответа и при чтении HTTP 200; bounded backoff/jitter;
непрерывное восстановление в одном процессе; clean reconciliation barrier;
запрет нового intent после retry финального статуса; новая проверка риска;
открытая позиция при outage/expiry; частичный fill и рестарт без повторного
PostOrder; UNKNOWN, STOP, mismatch и критический halt; неопределённая отмена
без повторной отправки; сохранность безопасной диагностики критической ошибки.

Проверено для обновления: dry-run без изменения байтов SQLite; старый
fingerprint; семантика/markers/highwater; отказ при pending, неизвестной
версии, corruption, параллельном runner и повреждённом backup; fsync failure
до изменения рабочей базы; сохранение ledger_id/halt/STOP/permission;
успешный откат без восстановления старой базы; запрет отката после новых
исполнений. Ревизии старых операций попадают в следующий полный снимок;
ошибка обновления не сохраняет частичный результат.

Windows, реальный LoadCredential/PID1, сеть/диск Jino и реальные заявки не
проверялись. В существующую GitHub Actions Ubuntu/Windows матрицу добавлен
push-триггер этой ветки; её фактический результат здесь не заявляется.

RELEASE_SHA256.json фиксирует новый bundle, OLD bundle зафиксирован
константой LEGACY_SOFTWARE и docs/live_credential_recovery/CORE_SHA256.json.
Инструкция: JINO_UPDATE_RU.md. Ни halt, ни STOP реального робота не снимались.
