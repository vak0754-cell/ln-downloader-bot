from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Settings
from core.drive_uploader import DriveUploader
from core.epub_builder import build_epub, build_txt
from core.parsers import (
    PoliteHttpClient,
    SourcePlan,
    extract_clean_chapter_html,
    inspect_url,
    parse_range_spec,
    sanitize_filename,
)
from core.storage import DownloadStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ln_downloader_bot")

settings = Settings()
settings.ensure_dirs()

http = PoliteHttpClient(settings.user_agent, settings.request_timeout, settings.rate_limit_delay)
store = DownloadStore(settings.data_dir / "cache" / "downloads.sqlite3")
drive = DriveUploader(settings.drive_root_folder_id, settings.google_service_account_file)

router = Router()

# Per-user pending plans & cancellation events
PLANS: dict[str, SourcePlan] = {}
WAITING_RANGE: dict[int, tuple[str, str]] = {}  # user_id -> (plan_key, fmt)
ACTIVE_CANCELS: dict[int, asyncio.Event] = {}

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _allowed(user_id: int) -> bool:
    ids = settings.allowed_user_ids
    return not ids or user_id in ids


def _register_plan(plan: SourcePlan) -> str:
    key = str(abs(hash(plan.source_url)))[:12]
    PLANS[key] = plan
    return key


def _book_keyboard(plan_key: str, total: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=f"📘 Усі {total} розд. (EPUB)", callback_data=f"dl:{plan_key}:epub:all"),
            InlineKeyboardButton(text=f"📄 Усі {total} розд. (TXT)", callback_data=f"dl:{plan_key}:txt:all"),
        ],
    ]
    if total > 20:
        rows.append([
            InlineKeyboardButton(text="⚡ Останні 20 (EPUB)", callback_data=f"dl:{plan_key}:epub:last20"),
            InlineKeyboardButton(text="🔢 Свій діапазон (EPUB)", callback_data=f"range:{plan_key}:epub"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="🔢 Свій діапазон (EPUB)", callback_data=f"range:{plan_key}:epub"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _dir_keyboard(plan_key: str, files: list[dict[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    epubs = [i for i, f in enumerate(files) if f["ext"] == ".epub"]
    if epubs:
        rows.append([InlineKeyboardButton(text=f"📦 Скачати всі EPUB ({len(epubs)})", callback_data=f"dirall:{plan_key}:epub")])
    rows.append([InlineKeyboardButton(text=f"📦 Скачати всі файли ({len(files)})", callback_data=f"dirall:{plan_key}:all")])
    for idx, f in enumerate(files[:12]):
        short = f["name"][:45] + ("…" if len(f["name"]) > 45 else "")
        rows.append([InlineKeyboardButton(text=f"⬇️ {short}", callback_data=f"dirone:{plan_key}:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    if not message.from_user or not _allowed(message.from_user.id):
        await message.answer("⛔ Доступ обмежено.")
        return
    await message.answer(
        "<b>LN Downloader Bot</b> — швидкий завантажувач ранобе та книг за посиланням.\n\n"
        "Просто надішли будь-який URL:\n"
        "• <b>Веб-книга / сторінка зі змістом</b> → інспекція розділів, вибір діапазону, збірка в <code>EPUB 3</code> або <code>TXT</code> із кешем розділів (повторний запуск докачує лише нові)\n"
        "• <b>Відкритий каталог файлів</b> → вибір одного або пакетне завантаження всіх <code>.epub</code>\n"
        "• <b>Пряме посилання</b> (<code>.epub</code>, <code>.pdf</code>, <code>.txt</code>, <code>.fb2</code>, <code>.zip</code>) → миттєве скачування в чат\n\n"
        "Команди: <code>/history</code> — останні завантаження | <code>/cancel</code> — зупинити поточний процес.",
        parse_mode="HTML",
    )


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    if not message.from_user or not _allowed(message.from_user.id):
        return
    rows = store.recent_history(message.from_user.id, limit=10)
    if not rows:
        await message.answer("Історія завантажень порожня.")
        return
    lines = ["<b>Останні завантаження:</b>"]
    for r in rows:
        lines.append(f"• <b>{r['book_title']}</b> ({r['chapters_count']} розд.) — <code>{Path(r['output_file']).name}</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    if not message.from_user or not _allowed(message.from_user.id):
        return
    ev = ACTIVE_CANCELS.get(message.from_user.id)
    if ev:
        ev.set()
        await message.answer("🛑 Завантаження зупиняється. Уже скачані розділи збережено в кеші.")
    else:
        await message.answer("Немає активного завантаження.")


@router.message(F.text)
async def handle_text(message: Message) -> None:
    if not message.from_user or not _allowed(message.from_user.id) or not message.text:
        return
    uid = message.from_user.id
    text = message.text.strip()

    # Check if user is entering a custom chapter range
    if uid in WAITING_RANGE and not URL_RE.search(text):
        plan_key, fmt = WAITING_RANGE.pop(uid)
        plan = PLANS.get(plan_key)
        if not plan:
            await message.answer("Сесія застаріла. Надішли посилання ще раз.")
            return
        indices = parse_range_spec(text, len(plan.chapters))
        if not indices:
            await message.answer("Некоректний діапазон. Приклад: <code>1-50</code> або <code>1,3,5-10</code>.", parse_mode="HTML")
            return
        asyncio.create_task(_download_web_book(message, uid, plan, fmt, indices))
        return

    m = URL_RE.search(text)
    if not m:
        await message.answer("Надішли посилання (<code>https://...</code>) на книгу, розділ або каталог.", parse_mode="HTML")
        return

    url = m.group(0)
    status = await message.answer("🔎 Аналізую посилання...")
    try:
        plan = await inspect_url(http, url)
    except Exception as exc:
        await status.edit_text(f"❌ {exc}")
        return

    if plan.mode == "direct_file":
        await status.edit_text(f"⬇️ Завантажую прямий файл <code>{plan.title}</code>...", parse_mode="HTML")
        try:
            async with httpx.AsyncClient() as client:
                saved = await http.download_binary(client, plan.source_url, settings.data_dir / "downloads", settings.max_file_mb)
            await status.delete()
            await _deliver_file(message, saved, plan.title)
            store.add_history(uid, plan.source_url, plan.title, 1, str(saved))
        except Exception as exc:
            await status.edit_text(f"❌ Помилка скачування: {exc}")
        return

    pkey = _register_plan(plan)
    if plan.mode == "directory":
        await status.edit_text(
            f"📂 <b>Відкритий каталог:</b> {plan.title}\nЗнайдено файлів: <b>{len(plan.files)}</b>. Вибери, що завантажити:",
            parse_mode="HTML",
            reply_markup=_dir_keyboard(pkey, plan.files),
        )
        return

    await status.edit_text(
        f"📘 <b>{plan.title}</b>\n"
        f"• Автор: {plan.author}\n"
        f"• Знайдено розділів: <b>{len(plan.chapters)}</b>\n"
        f"Вибери формат і діапазон:",
        parse_mode="HTML",
        reply_markup=_book_keyboard(pkey, len(plan.chapters)),
    )


@router.callback_query(F.data.startswith("range:"))
async def cb_range(call: CallbackQuery) -> None:
    if not call.from_user or not _allowed(call.from_user.id):
        return
    _, pkey, fmt = call.data.split(":", 2)
    WAITING_RANGE[call.from_user.id] = (pkey, fmt)
    await call.message.answer("Введи діапазон розділів (наприклад: <code>1-30</code> або <code>10,15,20-40</code>):", parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("dl:"))
async def cb_download_book(call: CallbackQuery) -> None:
    if not call.from_user or not _allowed(call.from_user.id):
        return
    _, pkey, fmt, spec = call.data.split(":", 3)
    plan = PLANS.get(pkey)
    if not plan:
        await call.answer("Посилання застаріло, надішли його знову.", show_alert=True)
        return
    total = len(plan.chapters)
    if spec == "last20":
        indices = list(range(max(1, total - 19), total + 1))
    else:
        indices = list(range(1, total + 1))
    await call.answer()
    asyncio.create_task(_download_web_book(call.message, call.from_user.id, plan, fmt, indices))


@router.callback_query(F.data.startswith("dirone:"))
async def cb_dir_one(call: CallbackQuery) -> None:
    if not call.from_user or not _allowed(call.from_user.id):
        return
    _, pkey, idx_str = call.data.split(":", 2)
    plan = PLANS.get(pkey)
    if not plan:
        await call.answer("Каталог застарів.", show_alert=True)
        return
    finfo = plan.files[int(idx_str)]
    await call.answer()
    msg = await call.message.answer(f"⬇️ Завантажую <code>{finfo['name']}</code>...", parse_mode="HTML")
    try:
        async with httpx.AsyncClient() as client:
            saved = await http.download_binary(client, finfo["url"], settings.data_dir / "downloads", settings.max_file_mb)
        await msg.delete()
        await _deliver_file(call.message, saved, plan.title)
        store.add_history(call.from_user.id, finfo["url"], finfo["name"], 1, str(saved))
    except Exception as exc:
        await msg.edit_text(f"❌ Помилка: {exc}")


@router.callback_query(F.data.startswith("dirall:"))
async def cb_dir_all(call: CallbackQuery) -> None:
    if not call.from_user or not _allowed(call.from_user.id):
        return
    _, pkey, kind = call.data.split(":", 2)
    plan = PLANS.get(pkey)
    if not plan:
        await call.answer("Каталог застарів.", show_alert=True)
        return
    targets = [f for f in plan.files if kind == "all" or f["ext"] == ".epub"]
    await call.answer()
    msg = await call.message.answer(f"📦 Пакетне завантаження: 0/{len(targets)}...")
    async with httpx.AsyncClient() as client:
        for i, finfo in enumerate(targets, start=1):
            try:
                saved = await http.download_binary(client, finfo["url"], settings.data_dir / "downloads", settings.max_file_mb)
                await _deliver_file(call.message, saved, plan.title)
                await msg.edit_text(f"📦 Пакетне завантаження: {i}/{len(targets)}...")
            except Exception as exc:
                await call.message.answer(f"⚠️ Пропущено <code>{finfo['name']}</code>: {exc}", parse_mode="HTML")
            await asyncio.sleep(settings.rate_limit_delay)
    await msg.edit_text(f"✅ Готово! Оброблено {len(targets)} файлів.")


async def _download_web_book(
    message: Message, user_id: int, plan: SourcePlan, fmt: str, indices: list[int]
) -> None:
    cancel_ev = asyncio.Event()
    ACTIVE_CANCELS[user_id] = cancel_ev
    selected = [plan.chapters[i - 1] for i in indices]
    total = len(selected)
    status = await message.answer(f"⬇️ Завантажую <b>{plan.title}</b>: 0/{total} розділів...", parse_mode="HTML")

    sem = asyncio.Semaphore(settings.concurrency)
    results: dict[int, dict[str, str]] = {}
    done = 0
    cached_hits = 0
    lock = asyncio.Lock()

    async with httpx.AsyncClient() as client:
        # Optional cover fetch
        cover_bytes: bytes | None = None
        cover_ext = ".jpg"
        if plan.cover_url and fmt == "epub":
            try:
                cresp = await client.get(plan.cover_url, timeout=15, follow_redirects=True)
                if cresp.status_code == 200 and len(cresp.content) < 5 * 1024 * 1024:
                    cover_bytes = cresp.content
                    if "png" in cresp.headers.get("content-type", "").lower():
                        cover_ext = ".png"
            except Exception:
                pass

        async def fetch_one(pos: int, ch_url: str, fallback_title: str) -> None:
            nonlocal done, cached_hits
            if cancel_ev.is_set():
                return
            cached = store.get_cached_chapter(ch_url)
            if cached:
                results[pos] = cached
                async with lock:
                    cached_hits += 1
                    done += 1
            else:
                async with sem:
                    if cancel_ev.is_set():
                        return
                    try:
                        raw_html, _ = await http.get_text(client, ch_url)
                        parsed_title, body_html = extract_clean_chapter_html(raw_html)
                        item = {"title": parsed_title or fallback_title, "body_html": body_html}
                        store.save_cached_chapter(ch_url, item["title"], item["body_html"])
                        results[pos] = item
                    except Exception as exc:
                        results[pos] = {"title": fallback_title, "body_html": f"<p>[Помилка завантаження: {exc}]</p>"}
                    await asyncio.sleep(settings.rate_limit_delay)
                async with lock:
                    done += 1

            if done == total or done % max(1, total // 10) == 0:
                pct = int(done * 100 / max(total, 1))
                try:
                    await status.edit_text(
                        f"⬇️ <b>{plan.title}</b>: <b>{done}/{total}</b> ({pct}%) | із кешу: {cached_hits}\n"
                        f"<i>Зупинити: /cancel</i>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        await asyncio.gather(*(fetch_one(i, ch.url, ch.title) for i, ch in enumerate(selected)))

    ACTIVE_CANCELS.pop(user_id, None)
    if cancel_ev.is_set():
        await status.edit_text(f"⏸️ Зупинено на {done}/{total}. Усі завантажені розділи збережено в кеші.")
        return

    ordered = [results[i] for i in range(total) if i in results]
    fname = sanitize_filename(f"{plan.title}_{indices[0]}-{indices[-1]}")
    books_dir = settings.data_dir / "books"

    await status.edit_text("📦 Збираю файл книги...")
    if fmt == "txt":
        out_path = await asyncio.to_thread(build_txt, title=plan.title, author=plan.author, chapters=ordered, out_path=books_dir / f"{fname}.txt")
    else:
        out_path = await asyncio.to_thread(
            build_epub,
            title=plan.title,
            author=plan.author,
            chapters=ordered,
            out_path=books_dir / f"{fname}.epub",
            cover_bytes=cover_bytes,
            cover_ext=cover_ext,
        )

    store.add_history(user_id, plan.source_url, plan.title, len(ordered), str(out_path))
    await status.delete()
    await _deliver_file(message, out_path, plan.title)


async def _deliver_file(message: Message, path: Path, title: str) -> None:
    size_mb = round(path.stat().st_size / (1024 * 1024), 2)
    if size_mb <= 48.0:
        await message.answer_document(
            FSInputFile(path),
            caption=f"✅ <code>{path.name}</code> ({size_mb} МБ)",
            parse_mode="HTML",
        )
    elif drive.enabled:
        slug = sanitize_filename(title).replace(" ", "_")
        up = await asyncio.to_thread(drive.upload, path, slug)
        await message.answer(
            f"☁️ Файл перевищує ліміт Telegram ({size_mb} МБ), завантажено на Google Drive:\n{up['webViewLink']}"
        )
    else:
        await message.answer(
            f"⚠️ Файл <code>{path.name}</code> завеликий для Telegram ({size_mb} МБ). "
            f"Збережено локально або підключи Google Drive у .env.",
            parse_mode="HTML",
        )


async def _run_polling() -> None:
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("LN Downloader Bot started (polling).")
    await dp.start_polling(bot)


async def _run_webhook(base_url: str) -> None:
    import hashlib
    import os

    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    secret = os.environ.get("WEBHOOK_SECRET") or None
    # Path derived from the token hash so the URL is not guessable and never exposes the token.
    path = "/webhook/" + hashlib.sha256(settings.bot_token.encode()).hexdigest()[:24]
    port = int(os.environ.get("PORT", "10000"))

    async def on_startup(bot: Bot) -> None:
        await bot.set_webhook(
            base_url.rstrip("/") + path,
            secret_token=secret,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        logger.info("Webhook set.")

    dp.startup.register(on_startup)

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("LN Downloader Bot started (webhook) on port %s.", port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    import os

    base_url = os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if base_url:
        await _run_webhook(base_url)
    else:
        await _run_polling()


if __name__ == "__main__":
    asyncio.run(main())
