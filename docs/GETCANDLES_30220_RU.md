# GetCandles HTTP 400 / API 30220

Дата: 2026-11-11. База a0f75da, ветка gldrubf-v1-paper-api, PR #1.

## Значение ошибки и подтверждённая причина

Официальный каталог RussianInvestments/investAPI, коммит
3eaf23a25f598fe483c913184acdbd9132bc68d2, содержит точную запись:

> 30220 — INVALID_ARGUMENT — `candle_type` not supported when `limit` specified.
> Входной параметр `candle_type` не может быть задан, если передан `limit`.

[Каталог JSON](https://github.com/RussianInvestments/investAPI/blob/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/errors/api_errors.json)
и [таблица ошибок](https://github.com/RussianInvestments/investAPI/blob/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/errors.md).
Код уже правильно извлекался существующим числовым allowlist.

Это указание на несовместимость с заданным limit, не на глубину истории.
Клиент всегда добавлял limit=2400 к типизированному диапазонному запросу.
Однако публичный GetCandlesRequest не содержит поля candle_type/candleType:
есть interval и candleSourceType. Каталог не уточняет, какое из них соответствует
внутреннему термину. Не утверждаем без подтверждения, что причина именно в
одном конкретном enum; удаляем необязательный limit, сохраняя фильтры и даты.

## Сверка фактического конструктора запроса

[Protobuf](https://github.com/RussianInvestments/investAPI/blob/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/contracts/marketdata.proto)
и [OpenAPI v1.43](https://github.com/RussianInvestments/investAPI/blob/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/swagger-ui/openapi.yaml):

| Поле | До исправления | Оценка |
| --- | --- | --- |
| instrumentId | UID из FindInstrument, проверенный FutureBy | Допустимо; реальный UID Windows в сообщении не указан |
| from | RFC 3339 UTC, начало чанка | Допустимый формат |
| to | min(from + 90 дней, конец) для DAY | Ограниченный период |
| interval | CANDLE_INTERVAL_DAY, enum 5 | Допустимо |
| candleSourceType | CANDLE_SOURCE_EXCHANGE, enum 1 | Допустимо; фильтр сохранён |
| limit | 2400, int32 | Поле optional; совместное использование отклонено кодом 30220 |

Required в OpenAPI: from, to, interval. Проверка типов отдельных полей не
доказывает допустимость их сочетания на сервере; семантическое ограничение
30220 описано отдельно в каталоге ошибок.

## Исторический период GLDRUBF

[FAQ MarketData](https://github.com/RussianInvestments/investAPI/blob/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/faq_marketdata.md)
и CandleInterval указывают для DAY от одного дня до шести лет, максимум 2400
свечей. Для M1 — от минуты до одного дня. У DAY время суток игнорируется,
даты границ могут включаться в ответ; код сохраняет их проверку/дедупликацию.

Диапазон, упоминавшийся ранее (2024-11-05 — 2026-11-06), короче шести лет,
каждый наш DAY-запрос — максимум 90 дней. Нет оснований приписывать 30220
слишком длинному периоду или превышению числового максимума 2400.
Это не гарантирует наличие данных GLDRUBF на всём периоде: реальное начало
определяется first1dayCandleDate из FutureBy. Его авторизованное текущее значение
здесь неизвестно. Windows-архив сохраняет его как first_daily в instrument.json,
в том числе перед отказавшим GetCandles. Отсутствие свечей не доказывает отсутствие торгов.

## Исправление и диагностика

Из конструктора GetCandles удалён только optional limit. Для DAY/M1 сохранены
UID, диапазоны/чанки, interval, CANDLE_SOURCE_EXCHANGE, все проверки ответов.
Не добавлены limit=0, другой источник, ослабление фильтров или автоматический
повтор после 400/30220. Серверный максимум числа свечей не отменяется.
Старые snapshots не переписываются, новые manifests фиксируют запрос без limit.

На ошибке теперь видны instrumentId, from_utc, to_utc, interval,
candleSourceType и limit. UUID печатается только в корректном формате; произвольный
instrumentId и совпадение с токеном заменяются на redacted. Даты нормализуются,
enum выводится из фиксированного списка, числа ограничены; отсутствующее поле —
omitted, некорректное — invalid. Токен, Authorization, message, тело ответа и
произвольные значения не выводятся. На новом запросе ошибка покажет limit=omitted.

SMA20/50, PAPER-алгоритм/SQLite/расходы, LIVE, main и merge не менялись.
Изменён только запрос общего API-клиента и его безопасная диагностика.

## Тесты и проверка Windows

Новый regression до исправления реально завершился имитированным 400/30220;
лог GETCANDLES_30220_REGRESSION_BEFORE.txt. После исправления запрос проходит
без retry. Это имитация семантического отказа, не настоящий API-запуск.

Шесть новых методов проверяют отсутствие limit при сохранении других полей,
DAY/M1 и границ чанков, разбор 30220/закрытие ответа без retry, безопасный
вывод параметров, omitted limit, UTC-offset и защиту секрета, включая UUID-формат.

**140 тестов прошли на Linux/Python 3.12 и 140 на Python 3.13** с
-W error::ResourceWarning; compileall, CLI help, git diff --check PASS.
Логи: GETCANDLES_30220_TEST_RESULTS.txt. Источники и хеши:
GETCANDLES_30220_SPEC_REFERENCE.json.

Новый Windows-запуск с настоящим API здесь не выполнялся. Нужно повторить
download_candles_windows.cmd. Если 30220 останется, нужны только безопасная
полная строка параметров, версия/коммит и first_daily из instrument.json,
без токена или HTTP-дампа. limit=2400 указывает на старую конструкцию запроса;
при limit=omitted дальнейшую причину надо исследовать на реальном API,
не объявляя проблему устранённой и не меняя интервалы/источник наугад.
