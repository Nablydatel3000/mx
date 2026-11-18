# Fix Issue #2: bootstrap и безопасное восстановление Forward PAPER, файлы Linux VPS

Первый запуск больше не исполняет последнее историческое пересечение SMA20/50:
он сохраняет границу прогрева и остаётся без позиции до нового подтверждённого
пересечения. Перезапуск сохраняет позицию и pending без повторных сделок.

Исполнение требует принадлежности котировки и текущего времени одному разрешённому
интервалу и свежей завершённой истории по явно заданному UTC-порогу. Восстановление
SQLite проверяет причинность сигналов/fills/quotes и согласованность состояния;
single-runner блокирует физический файл, включая альтернативные пути/symlink.
Hardlink базы запрещён. Старый pending без доказанного происхождения сохраняется,
но блокируется до нового пересечения; уже открытая позиция не стирается.

Добавлены systemd-файлы, локальный мастер credentials, постоянный state, health,
ротация/явный retention, durable outbox Telegram/HTTPS и проверяемые SQLite
backup/restore. Восстановление старой копии по умолчанию оставляет паузу для сверки.
Установщик не запускает службы. Формула стратегии и модель расходов не меняются;
LIVE, merge и deployment не выполнялись.

Проверка: 237 offline-тестов на Linux/Python 3.12 и 3.13, ResourceWarning=error;
включая настоящий subprocess crash и второй процесс с symlink, SQLite semantic
corruption, bootstrap rollback, повтор quotes после restart, API/notification mocks.
Compileall, CLI/VPS help, bash -n и systemd-analyze verify проходят.
Windows CI/реальный API/VPS smoke здесь не подтверждены.

Перед пользовательским запуском нужны явно выбранный возраст завершённой свечи,
прежние параметры расходов и локальные notification credentials. Торговая
прибыльность и соответствие DAY-метки точной сессии не утверждаются.

Отчёт: docs/ISSUE2_FIXES_AND_VPS_RU.md. Инструкция: deploy/linux/README_RU.md.
Результаты: docs/ISSUE2_FIX_TEST_RESULTS.txt. Исходный аудит: audits/issue_2/.
