import asyncio
import html
import json
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message,
                           WebAppInfo)
from aiohttp import web

import config
import db
from auth import check_init_data

log = logging.getLogger("cafe")
WEB_DIR = Path(__file__).parent / "webapp"

bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if config.BOT_TOKEN else None
dp = Dispatcher()
esc = html.escape


def money(n: float) -> str:
    s = str(int(n)) if float(n).is_integer() else f"{n:.2f}".replace(".", ",")
    return f"{s} {config.CURRENCY}"


def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


STATUS_LABEL = {
    "new": "🕐 Ожидает подтверждения",
    "accepted": "✅ Принят, готовится",
    "ready": "🍽 Готов / передан",
    "rejected": "❌ Отклонён",
}


# ======================= тексты и клавиатуры =======================
def order_text(o: dict) -> str:
    items = json.loads(o["items_json"])
    lines = [f"🧾 <b>Заказ №{o['id']}</b> — {STATUS_LABEL[o['status']]}", ""]
    for i in items:
        lines.append(f"• {esc(i['name'])} × {i['qty']} — {money(i['price'] * i['qty'])}")
    lines += ["", f"💰 <b>Итого: {money(o['total'])}</b>", ""]
    if o["type"] == "delivery":
        lines.append("🚚 <b>Доставка</b>: " + esc(o.get("address") or ""))
        if o.get("object_type"): lines.append("🏠 Тип объекта: " + esc(o["object_type"]))
        if o.get("unit_number"): lines.append("🔢 Квартира/помещение: " + esc(o["unit_number"]))
        if o.get("entrance"): lines.append("🚪 Подъезд: " + esc(o["entrance"]))
        if o.get("floor"): lines.append("🛗 Этаж: " + esc(o["floor"]))
        if o.get("intercom"): lines.append("🔐 Домофон: " + esc(o["intercom"]))
        if o.get("latitude") is not None and o.get("longitude") is not None:
            lines.append(f'📍 <a href="https://www.google.com/maps?q={o["latitude"]},{o["longitude"]}">Открыть точку на карте</a>')
    else:
        lines.append("🏃 <b>Самовывоз</b>")
    lines.append(f"👤 {esc(o['name'])}")
    lines.append(f"📞 {esc(o['phone'])}")
    if o["comment"]:
        lines.append(f"💬 {esc(o['comment'])}")
    return "\n".join(lines)


def order_kb(o: dict) -> InlineKeyboardMarkup:
    oid, st = o["id"], o["status"]
    rows = []
    if st == "new":
        rows.append([InlineKeyboardButton(text="✅ Принять", callback_data=f"o:{oid}:acc"),
                     InlineKeyboardButton(text="❌ Отклонить", callback_data=f"o:{oid}:rej")])
    elif st == "accepted":
        label = "🚗 Передан курьеру" if o["type"] == "delivery" else "🍽 Готов к выдаче"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"o:{oid}:rdy")])
        rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data=f"o:{oid}:rej")])
    rows.append([InlineKeyboardButton(text="💬 Написать клиенту", url=f"tg://user?id={o['user_id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def client_text(o: dict) -> str:
    n = o["id"]
    delivery = o["type"] == "delivery"
    return {
        "new": f"🕐 <b>Заказ №{n} оформлен!</b>\nАдминистратор получил его и скоро подтвердит. Мы пришлём сообщение.",
        "accepted": f"✅ <b>Заказ №{n} подтверждён!</b>\nУже готовим. "
                    + ("Привезём по адресу: " + esc(o["address"]) if delivery else "Ждём вас на самовывоз."),
        "ready": f"🍽 <b>Заказ №{n} готов!</b>\n" + ("Курьер уже выехал к вам 🚗" if delivery else "Можно забирать, приятного аппетита!"),
        "rejected": f"❌ <b>К сожалению, заказ №{n} не удалось принять.</b>\n"
                    f"Свяжитесь с администратором: напишите сюда или позвоните {esc(config.CAFE_PHONE)}.",
    }[o["status"]]


def main_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🍽 Открыть меню", web_app=WebAppInfo(url=config.WEBAPP_URL + "/"))],
            [InlineKeyboardButton(text="💬 Связаться с админом", callback_data="contact")]]
    if is_admin(uid):
        rows.append([InlineKeyboardButton(text="⚙️ Управление меню", web_app=WebAppInfo(url=config.WEBAPP_URL + "/?admin=1"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


CONTACT_TEXT = "💬 Напишите ваш вопрос одним сообщением — я сразу передам его администратору, ответ придёт сюда."


# ======================= логика заказов =======================
async def safe_send(chat_id, text, **kw):
    if not bot:
        log.info("[dev] -> %s: %s", chat_id, text)
        return None
    try:
        return await bot.send_message(chat_id, text, **kw)
    except Exception as e:  # пользователь мог заблокировать бота и т.п.
        log.warning("send to %s failed: %s", chat_id, e)
        return None


async def notify_admins_new_order(o: dict):
    msgs = []
    for admin in config.ADMIN_IDS:
        m = await safe_send(admin, order_text(o), reply_markup=order_kb(o))
        if m:
            msgs.append([admin, m.message_id])
    db.set_admin_msgs(o["id"], msgs)


async def apply_status(order_id: int, new: str):
    allowed = {"accepted": {"new"}, "rejected": {"new", "accepted"}, "ready": {"accepted"}}
    o = db.get_order(order_id)
    if not o or o["status"] not in allowed[new]:
        return None
    db.set_order_status(order_id, new)
    o = db.get_order(order_id)
    if bot:
        for chat, mid in json.loads(o["admin_msgs"]):
            try:
                await bot.edit_message_text(order_text(o), chat_id=chat, message_id=mid, reply_markup=order_kb(o))
            except Exception as e:
                log.warning("edit failed: %s", e)
    await safe_send(o["user_id"], client_text(o))
    return o


# ======================= хендлеры бота =======================
@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    if command.args == "contact":
        await m.answer(CONTACT_TEXT)
        return
    await m.answer(
        f"Добро пожаловать в <b>{esc(config.CAFE_NAME)}</b> 🌿\n"
        "Выберите блюда и оформите заказ прямо в этом сообщении.\n\n"
        "Если виджет не открылся, отправьте /start ещё раз.",
        reply_markup=main_kb(m.from_user.id))


@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("⚙️ Панель администратора: меняйте меню на день, ставьте блюда на стоп.",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                       text="⚙️ Управление меню", web_app=WebAppInfo(url=config.WEBAPP_URL + "/?admin=1"))]]))


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>")


@dp.callback_query(F.data == "contact")
async def cb_contact(c: CallbackQuery):
    await c.message.answer(CONTACT_TEXT)
    await c.answer()


@dp.callback_query(F.data.startswith("o:"))
async def cb_order(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    _, oid, act = c.data.split(":")
    new = {"acc": "accepted", "rej": "rejected", "rdy": "ready"}[act]
    o = await apply_status(int(oid), new)
    await c.answer("Готово ✓" if o else "Статус уже изменён")


# админ отвечает (reply) на сообщение клиента -> уходит клиенту
@dp.message(F.chat.type == "private", F.reply_to_message, F.func(lambda m: is_admin(m.from_user.id)))
async def admin_reply(m: Message):
    uid = db.get_relay(m.chat.id, m.reply_to_message.message_id)
    if not uid:
        return
    await bot.send_message(uid, "💬 <b>Ответ администратора:</b>")
    await bot.copy_message(uid, m.chat.id, m.message_id)
    await m.answer("✅ Отправлено клиенту")


# любое сообщение клиента -> всем админам
@dp.message(F.chat.type == "private")
async def client_message(m: Message):
    if is_admin(m.from_user.id):
        await m.answer("Чтобы ответить клиенту, ответьте (Reply) на его сообщение.")
        return
    u = m.from_user
    head = f"💬 <b>{esc(u.full_name)}</b>" + (f" (@{esc(u.username)})" if u.username else "") + \
           "\n<i>Ответьте на это сообщение (Reply), и ответ уйдёт клиенту.</i>"
    for admin in config.ADMIN_IDS:
        try:
            h = await bot.send_message(admin, head)
            c = await bot.copy_message(admin, m.chat.id, m.message_id)
            db.add_relay(admin, h.message_id, u.id)
            db.add_relay(admin, c.message_id, u.id)
        except Exception as e:
            log.warning("relay to %s failed: %s", admin, e)
    await m.answer("✅ Передали администратору. Ответ придёт сюда.")


# ======================= HTTP API для мини-приложения =======================
def json_err(msg, status=400):
    return web.json_response({"error": msg}, status=status)


def get_user(request: web.Request):
    init = request.headers.get("X-Init-Data", "")
    user = check_init_data(init, config.BOT_TOKEN)
    if user:
        return user
    if config.DEV_MODE and not init:
        return {"id": config.DEV_USER_ID, "first_name": "Тест", "username": "tester"}
    return None


def require_admin(request):
    u = get_user(request)
    return u if u and (is_admin(u["id"]) or (config.DEV_MODE and u["id"] == config.DEV_USER_ID)) else None


async def api_menu(request):
    items = db.list_items()
    return web.json_response({
        "categories": db.grouped(items),
        "today": db.today(),
        "orders_open": db.get_setting("orders_open", "1") == "1",
        "cafe": config.CAFE_NAME, "phone": config.CAFE_PHONE, "admin_username": config.ADMIN_USERNAME,
        "min_delivery": config.MIN_DELIVERY, "bot_username": request.app.get("bot_username", ""),
        "currency": config.CURRENCY,
    })


async def api_me(request):
    u = get_user(request)
    if not u:
        return json_err("auth", 401)
    return web.json_response({"id": u["id"], "name": u.get("first_name", ""), "is_admin": bool(require_admin(request))})


def order_public(o):
    return {"id": o["id"], "status": o["status"], "type": o["type"], "total": o["total"],
            "items": json.loads(o["items_json"]), "address": o["address"],
            "object_type": o.get("object_type", ""), "unit_number": o.get("unit_number", ""),
            "entrance": o.get("entrance", ""), "floor": o.get("floor", ""),
            "intercom": o.get("intercom", ""), "latitude": o.get("latitude"),
            "longitude": o.get("longitude"), "map_address": o.get("map_address", "")}


async def api_order_create(request):
    user = get_user(request)
    if not user:
        return json_err("Откройте приложение через Telegram", 401)
    if db.get_setting("orders_open", "1") != "1":
        return json_err("Сейчас приём заказов закрыт")
    d = await request.json()
    typ = d.get("type")
    name = str(d.get("name", "")).strip()[:60]
    phone = str(d.get("phone", "")).strip()[:30]
    address = str(d.get("address", "")).strip()[:250]
    if typ not in ("delivery", "pickup") or not name or len(phone) < 5:
        return json_err("Заполните имя и телефон")
    if typ == "delivery":
        if not address:
            return json_err("Укажите адрес доставки")
        if d.get("object_type") not in ("Квартира", "Помещение"):
            return json_err("Выберите тип объекта")
        if not str(d.get("unit_number", "")).strip():
            return json_err("Укажите номер квартиры или помещения")
        try:
            lat, lon = float(d["latitude"]), float(d["longitude"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180): raise ValueError
        except (KeyError, TypeError, ValueError):
            return json_err("Выберите точку доставки на карте")
    items, total = [], 0.0
    for it in d.get("items", [])[:60]:
        row = db.get_item(int(it.get("id", 0)))
        qty = int(it.get("qty", 0))
        if not row or not row["available"] or not 1 <= qty <= 50:
            return json_err("Некоторые блюда уже недоступны, обновите меню")
        items.append({"id": row["id"], "name": row["name"], "price": row["price"], "qty": qty})
        total += row["price"] * qty
    total = round(total, 2)
    if not items:
        return json_err("Корзина пуста")
    if typ == "delivery" and total < config.MIN_DELIVERY:
        return json_err(f"Минимальная сумма доставки — {money(config.MIN_DELIVERY)}")
    payload = {
        "type": typ, "name": name, "phone": phone, "address": address,
        "comment": str(d.get("comment", "")).strip()[:300],
        "object_type": str(d.get("object_type", ""))[:20],
        "unit_number": str(d.get("unit_number", "")).strip()[:30],
        "entrance": str(d.get("entrance", "")).strip()[:20],
        "floor": str(d.get("floor", "")).strip()[:20],
        "intercom": str(d.get("intercom", "")).strip()[:40],
        "latitude": d.get("latitude") if typ == "delivery" else None,
        "longitude": d.get("longitude") if typ == "delivery" else None,
        "map_address": str(d.get("map_address", "")).strip()[:250],
    }
    oid = db.create_order(user, payload, items, total)
    o = db.get_order(oid)
    await notify_admins_new_order(o)
    await safe_send(user["id"], client_text(o))
    return web.json_response(order_public(o))


async def api_order_get(request):
    user = get_user(request)
    o = db.get_order(int(request.match_info["id"]))
    if not user or not o or (o["user_id"] != user["id"] and not require_admin(request)):
        return json_err("not found", 404)
    return web.json_response(order_public(o))


# ---- админ ----
async def api_admin_menu(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    return web.json_response({
        "items": db.list_items(), "orders_open": db.get_setting("orders_open", "1") == "1",
        "updated": db.get_setting("menu_updated", ""), "today": db.today()})


async def api_admin_item(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    d = await request.json()
    try:
        d["category"], d["name"] = str(d["category"]).strip()[:60], str(d["name"]).strip()[:120]
        d["desc"] = str(d.get("desc", "")).strip()[:200]
        if not d["category"] or not d["name"] or float(d["price"]) < 0:
            raise ValueError
    except (KeyError, ValueError):
        return json_err("Проверьте поля")
    db.upsert_item(d)
    return web.json_response({"ok": True})


async def api_admin_toggle(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    d = await request.json()
    db.set_available(int(d["id"]), bool(d["available"]))
    return web.json_response({"ok": True})


async def api_admin_delete(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    db.delete_item(int((await request.json())["id"]))
    return web.json_response({"ok": True})


async def api_admin_import(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    menu = db.parse_menu_text(str((await request.json()).get("text", "")))
    if not menu:
        return json_err("Не удалось распознать меню. Формат: строка-категория, затем «Блюдо — цена».")
    db.replace_menu(menu)
    return web.json_response({"ok": True, "count": sum(len(v) for v in menu.values())})


async def api_admin_open(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    db.set_setting("orders_open", "1" if (await request.json())["open"] else "0")
    return web.json_response({"ok": True})


@web.middleware
async def nocache(request, handler):
    resp = await handler(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def build_app() -> web.Application:
    app = web.Application(middlewares=[nocache])
    app.router.add_get("/", lambda r: web.FileResponse(WEB_DIR / "index.html"))
    app.router.add_get("/api/menu", api_menu)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/order", api_order_create)
    app.router.add_get("/api/order/{id}", api_order_get)
    app.router.add_get("/api/admin/menu", api_admin_menu)
    app.router.add_post("/api/admin/item", api_admin_item)
    app.router.add_post("/api/admin/toggle", api_admin_toggle)
    app.router.add_post("/api/admin/delete", api_admin_delete)
    app.router.add_post("/api/admin/import", api_admin_import)
    app.router.add_post("/api/admin/open", api_admin_open)
    return app


async def main():
    logging.basicConfig(level=logging.INFO)
    db.init()
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    log.info("Web server on :%s", config.PORT)
    if bot:
        me = await bot.get_me()
        app["bot_username"] = me.username
        if config.WEBAPP_URL:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="Меню", web_app=WebAppInfo(url=config.WEBAPP_URL + "/")))
        await dp.start_polling(bot)
    else:
        log.warning("BOT_TOKEN не задан — работает только веб-часть (DEV_MODE).")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
