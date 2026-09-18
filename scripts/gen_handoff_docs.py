"""Generate two hand-off .docx documents.

1. Manager quick-start (cover-safe: connection only, zero hidden functionality).
2. DevOps deploy notes (concise, assumes competence; .env specifics + sizing).

Run:
    .venv/Scripts/python.exe -m scripts.gen_handoff_docs            # both
    .venv/Scripts/python.exe -m scripts.gen_handoff_docs manager    # one of them
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
MANAGER_OUT = ROOT / "Partner Assistant — инструкция для менеджера.docx"
DEVOPS_OUT = ROOT / "Partner Assistant — деплой (DevOps).docx"

ACCENT = RGBColor(0x3B, 0x4C, 0xC0)   # indigo
WARN = RGBColor(0xB9, 0x1C, 0x1C)     # red
MUTED = RGBColor(0x70, 0x70, 0x70)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def title(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.size = Pt(20)
    run.font.bold = True
    run.font.color.rgb = ACCENT


def subtitle(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.size = Pt(10.5)
    run.font.color.rgb = MUTED


def h1(doc: Document, text: str) -> None:
    p = doc.add_heading(text, level=1)
    for run in p.runs:
        run.font.color.rgb = ACCENT


def h2(doc: Document, text: str) -> None:
    doc.add_heading(text, level=2)


def para(doc: Document, text: str = "") -> None:
    doc.add_paragraph(text)


def warn(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    run = p.add_run(f"❗ {text}")
    run.font.bold = True
    run.font.color.rgb = WARN


def bullets(doc: Document, items: list[str]) -> None:
    for it in items:
        doc.add_paragraph(it, style="List Bullet")


def numbered(doc: Document, items: list[str]) -> None:
    for it in items:
        doc.add_paragraph(it, style="List Number")


def mono(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.name = "Consolas"
    run.font.size = Pt(9.5)


def page_break(doc: Document) -> None:
    doc.add_page_break()


def table(doc: Document, headers: list[str], rows: list[list[str]]) -> None:
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Light Grid Accent 1"
    for i, hcell in enumerate(headers):
        run = t.rows[0].cells[i].paragraphs[0].add_run(hcell)
        run.font.bold = True
    for row in rows:
        cells = t.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = val
    doc.add_paragraph()


# --------------------------------------------------------------------------- #
# Document 1 — Manager quick start
# --------------------------------------------------------------------------- #
def build_manager() -> None:
    """Manager quick start (cover-safe) + a detachable last page for the head.

    Rewritten 2026-09-18 against the live handlers: ``/register`` OTP flow
    (registration.py), group auto-activation by a registered adder
    (chat_member.py), Telegram Business secretary mode (business.py) and the
    head's ``/dashboard`` sign-in link + release DM (dm_commands.py,
    generator._announce_to_readers). Sections 1–3 never mention monitoring,
    risks or any dashboard; section 4 mentions only the team dashboard.
    """
    doc = Document()
    title(doc, "Partner Assistant — подключение")
    subtitle(doc, "Инструкция для менеджера · обновлено 18.09.2026")
    para(doc)
    para(
        doc,
        "Partner Assistant — бот-помощник для работы с партнёрскими чатами. "
        "В Telegram он называется AffOps Helper (@affops_helper_bot). "
        "Подключение занимает пять минут и состоит из трёх "
        "шагов: регистрация, рабочие группы, личные диалоги с партнёрами.",
    )
    warn(doc, "Порядок важен: сначала регистрация, потом добавление бота в группы.")

    # ------------------------------------------------------------------ 1
    h1(doc, "1. Регистрация (один раз)")
    numbered(
        doc,
        [
            "Открой бота @affops_helper_bot в Telegram и нажми Start "
            "(или отправь /start).",
            "Отправь команду /register.",
            "Бот попросит твой Slack member ID. Где взять: в Slack нажми на свой "
            "аватар → View Profile → кнопка «More (…)» → Copy member ID. "
            "Выглядит как U01234ABCDE. Отправь его боту.",
            "В Slack тебе в личные сообщения от Partner Assistant придёт код из "
            "6 символов. Он действует 10 минут.",
            "Отправь этот код боту в Telegram. Ответ "
            "«✅ Your Slack account has been linked» — регистрация завершена.",
        ],
    )
    para(
        doc,
        "Ошибся или код не пришёл: отправь /cancel и начни заново с /register. "
        "Проверь, что скопировал именно member ID (начинается с U), "
        "а не имя пользователя.",
    )
    para(doc, "Сразу после регистрации укажи рабочие часы и часовой пояс:")
    mono(doc, "/set_hours 09:00-18:00 Europe/Kiev")
    para(doc, "Так бот знает, когда ты на смене.")

    # ------------------------------------------------------------------ 2
    h1(doc, "2. Добавь бота в рабочие группы")
    numbered(
        doc,
        [
            "Открой группу с партнёром → участники → «Добавить» → "
            "@affops_helper_bot. Обычным участником, права администратора не нужны.",
            "Проверь название группы: в нём должны быть ID партнёра и слово "
            "Beton.Win, разделённые «|».",
            "Готово — группа подключится автоматически, подтверждать ничего не нужно.",
        ],
    )
    para(doc, "Пример названия группы:")
    mono(doc, "78516 | Acme Media | Beton.Win")
    bullets(
        doc,
        [
            "Добавь бота во все свои текущие партнёрские группы. В новые группы — "
            "сразу при создании.",
            "Добавлять должен ты сам, со своего зарегистрированного аккаунта. "
            "Если бота добавит кто-то другой, группа автоматически не подключится.",
            "Сам бот в группах не пишет. Единственное исключение — служебные "
            "напоминания для партнёров: праздники в Аргентине и сбои платёжных "
            "провайдеров.",
        ],
    )

    # ------------------------------------------------------------------ 3
    h1(doc, "3. Личные диалоги с партнёрами — режим секретаря (Telegram Business)")
    para(
        doc,
        "Если ты общаешься с партнёрами не только в группах, но и в личке, "
        "подключи бота как чат-бота в Telegram Business. Нужен Telegram Premium.",
    )
    numbered(
        doc,
        [
            "Telegram → Настройки → Telegram для бизнеса → Чат-боты.",
            "В поле поиска введи @affops_helper_bot и выбери его.",
            "В разделе «Чаты» отметь партнёрские диалоги. Переписки с коллегами, "
            "друзьями и семьёй не включай.",
            "Разреши боту читать сообщения. Право отвечать от твоего имени не нужно — "
            "в личные диалоги бот никогда не пишет.",
            "Сохрани. Подключение активируется автоматически, новые партнёрские "
            "диалоги подхватываются сами.",
        ],
    )
    para(
        doc,
        "Режим секретаря работает, пока бот выбран в настройках Telegram Business. "
        "Если отключить Premium или убрать бота из чат-ботов, личные диалоги "
        "отключатся, группы продолжат работать.",
    )

    # ------------------------------------------------------------------ FAQ
    h1(doc, "Если что-то не работает")
    bullets(
        doc,
        [
            "Бот не отвечает на /register — проверь, что открыл именно "
            "@affops_helper_bot.",
            "Код не пришёл в Slack — проверь member ID, затем /cancel → /register.",
            "Бота добавили в группу до регистрации или его добавил другой человек — "
            "напиши администратору, он подключит группу вручную.",
            "Любые другие вопросы — администратору.",
        ],
    )

    # ------------------------------------------------------------------ 4 (head)
    page_break(doc)
    h1(doc, "4. Руководителю отдела")
    para(
        doc,
        "Регистрация, группы и режим секретаря — те же шаги 1–3. Дополнительно:",
    )
    bullets(
        doc,
        [
            "Твой Slack member ID заранее внесён администратором в настройки. Как "
            "только ты пройдёшь /register, бот автоматически откроет тебе доступ к "
            "дашборду команды и подтвердит это в ответе на регистрацию. Отдельно "
            "ничего запрашивать не нужно.",
            "Дашборд команды — сводка по работе всех менеджеров отдела с партнёрами: "
            "скорость ответов, активность в чатах, тон общения — за день, неделю, "
            "месяц.",
            "Как открыть: отправь боту /dashboard — он пришлёт персональную ссылку "
            "для входа. Ссылка одноразовая и действует 15 минут; после входа браузер "
            "остаётся авторизован 90 дней. С другого устройства — снова /dashboard.",
            "Рассылка: каждый понедельник (итоги недели) и 1-го числа (итоги месяца) "
            "бот присылает тебе в Telegram сообщение со ссылкой на дашборд. Та же "
            "ссылка публикуется в Slack-канале отчётов.",
            "Если ссылка открыла страницу входа — нажми на ней кнопку "
            "«Sign in via Telegram»: откроется бот и сразу пришлёт ссылку для входа. "
            "Это то же самое, что отправить ему /dashboard.",
            "Доступ персональный: страница открывается только под твоим аккаунтом, "
            "пересылать ссылку кому-либо бессмысленно.",
        ],
    )

    doc.save(str(MANAGER_OUT))
    print(f"wrote: {MANAGER_OUT.name}")


# --------------------------------------------------------------------------- #
# Document 2 — DevOps deploy notes
# --------------------------------------------------------------------------- #
def build_devops() -> None:
    doc = Document()
    title(doc, "Partner Assistant — заметки по деплою")
    subtitle(doc, "Для DevOps. Только особенности системы — без базовых вещей.")
    para(doc)

    h1(doc, "1. Доступ к репозиторию (приватный)")
    bullets(
        doc,
        [
            "Репозиторий: warden-afk/tg_ai_bot_bow (GitHub, private).",
            "Поделиться: GitHub → репо → Settings → Collaborators and teams → "
            "Invite — пригласить аккаунт DevOps (доступ Read достаточно).",
            "Для CI/сервера без личного аккаунта — Deploy key (read-only SSH) в "
            "Settings → Deploy keys.",
            "Текущий прод: Railway, подключён к этому репо — авто-деплой на push в main.",
        ],
    )

    h1(doc, "2. Стек и архитектура (как есть сейчас)")
    bullets(
        doc,
        [
            "Python 3.11, FastAPI + uvicorn — один процесс, порт 8080.",
            "Telegram — webhook (aiogram). На том же процессе: Slack-callbacks, "
            "веб-страницы отчётов, /health.",
            "БД — Supabase Postgres через asyncpg (пул 2–10). Ни Redis, ни брокера, "
            "ни Celery — очередь задач в самой БД.",
            "Фоновые воркеры — in-process asyncio (анализ, файлы, whisper-drain, "
            "summary-scheduler, ops-alerts, reapers). Стартуют в FastAPI lifespan, "
            "работают постоянно между запросами.",
            "LLM — OpenRouter (Haiku — анализ, Sonnet — саммари). Whisper (OpenAI) "
            "выключен флагом.",
            "Docker: python:3.11-slim + ffmpeg + curl; ставит из pyproject; non-root "
            "(uid 1000); HEALTHCHECK на /health.",
        ],
    )

    h1(doc, "3. Критично при деплое")
    warn(
        doc,
        "Никакого scale-to-zero / auto-stop на idle. Воркеры крутятся постоянно "
        "между HTTP-запросами; если платформа усыпляет инстанс — плановые рассылки "
        "(отчёты, ops-alerts) молча перестанут работать. На Fly: "
        "auto_stop_machines=false, min_machines_running=1.",
    )
    warn(
        doc,
        "Один инстанс. Воркеры in-process; горизонтальное масштабирование "
        "задублирует плановые рассылки (часть защищена dedup в БД, но не всё). "
        "Держать ровно одну реплику.",
    )
    bullets(
        doc,
        [
            "Webhook регистрируется при каждом старте, URL = SERVER_BASE_URL + "
            "/webhook. Нужен публичный HTTPS с валидным сертификатом (Railway/Fly "
            "дают из коробки; на голом сервере — reverse-proxy + TLS).",
            "Миграции НЕ применяются автоматически. supabase/migrations/0001–0016 "
            "прогнать вручную (Supabase SQL Editor или скриптом). Таблица "
            "schema_migrations не ведётся — ориентируйся по факту.",
            "/health отдаёт 503, если БД недоступна — годится для healthcheck/LB.",
        ],
    )

    h1(doc, "4. Особенности .env")
    bullets(
        doc,
        [
            "SERVER_BASE_URL — единственный URL, который надо задать. Webhook "
            "выводится из него автоматически; TELEGRAM_WEBHOOK_URL задавать НЕ нужно.",
            "SUPABASE_DB_URL — строка подключения asyncpg. Если в пароле есть "
            "спецсимволы (например +), задать SUPABASE_DB_PASSWORD отдельно — он "
            "перебивает пароль из URL.",
            "SUMMARY_ACCESS_TOKEN — bearer для ручного POST /summary/generate. К "
            "доступу к самим отчётам отношения не имеет (у отчётов свои share-токены "
            "+ пароль).",
            "Slack: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, SLACK_CHANNEL_ALERTS, "
            "SLACK_CHANNEL_REPORTS.",
            "DAILY_LLM_BUDGET_USD (по умолчанию 30) — дневной circuit breaker на LLM.",
            "Killswitches: WHISPER_ENABLED (false), FILE_ANALYSIS_ENABLED (true).",
            "Ops-alerts: OPS_ALERTS_ENABLED + OPS_FEED_URL. OPS_FEED_URL "
            "чувствительный — только в окружении деплоя, в коде/доках его нет.",
            "Секреты — только через env. Никаких .env в репозитории.",
        ],
    )
    para(doc, "Обязательные (без дефолтов — без них процесс не стартует):")
    mono(
        doc,
        "TELEGRAM_BOT_TOKEN  TELEGRAM_WEBHOOK_SECRET  SUPABASE_URL  "
        "SUPABASE_SERVICE_KEY\nSUPABASE_DB_URL  OPENROUTER_API_KEY  OPENAI_API_KEY  "
        "SLACK_BOT_TOKEN\nSLACK_SIGNING_SECRET  SLACK_CHANNEL_ALERTS  "
        "SLACK_CHANNEL_REPORTS",
    )
    para(
        doc,
        "Плюс на проде обязательно переопределить SERVER_BASE_URL и "
        "SUMMARY_ACCESS_TOKEN (у них есть дефолты-заглушки).",
    )

    h1(doc, "5. Требования к серверу (ориентир, без запаса «на всякий»)")
    table(
        doc,
        ["Ресурс", "Значение", "Почему"],
        [
            ["CPU", "1 vCPU (shared ОК), 2 — комфортно",
             "один процесс, нагрузка I/O-bound (HTTP к LLM/Telegram/БД)"],
            ["RAM", "512 MB рабочих, 1 GB рекомендовано",
             "Python + пул соединений + HTTP-буферы + извлечение текста из файлов "
             "(до ~40k симв) + загрузка файлов до 20 MB + ffmpeg; Whisper выключен"],
            ["Диск", "~5 GB",
             "образ + зависимости; постоянного состояния на диске нет — всё в Supabase"],
            ["Сеть", "исходящий + входящий HTTPS",
             "наружу: Telegram, Supabase, OpenRouter, OpenAI, Slack, ops-фид; "
             "внутрь: webhook с валидным TLS"],
            ["Runtime", "Python 3.11+, ffmpeg",
             "ffmpeg нужен для извлечения аудио из video_note"],
        ],
    )

    doc.save(str(DEVOPS_OUT))
    print(f"wrote: {DEVOPS_OUT.name}")


if __name__ == "__main__":
    import sys

    which = set(sys.argv[1:]) or {"manager", "devops"}
    if "manager" in which:
        build_manager()
    if "devops" in which:
        build_devops()
