# -*- coding: utf-8 -*-
"""Panel privado de Telegram para controlar el motor existente de Ajay Glory."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

from dashboard_server import bot_state


def _admin_ids() -> set[int]:
    raw = os.getenv("TELEGRAM_ADMIN_IDS", "")
    result = set()
    for value in raw.replace(";", ",").split(","):
        value = value.strip()
        if value.lstrip("-").isdigit():
            result.add(int(value))
    return result


def _authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in _admin_ids())


class TelegramControl:
    def __init__(self):
        self.application: Optional[Application] = None
        self.last_notice: dict[str, float] = {}
        self._page_size = 8
        self.pending_deletes: set[tuple[int, str]] = set()

    async def reject_or_continue(self, update: Update) -> bool:
        if _authorized(update):
            return True
        target = update.effective_message or (update.callback_query and update.callback_query.message)
        if target:
            await target.reply_text("Acceso denegado.")
        return False

    def panel(self) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton("▶️ INICIAR GLORY", callback_data="glory:start"), InlineKeyboardButton("⏹ DETENER GLORY", callback_data="glory:stop")],
            [InlineKeyboardButton("📊 ESTADÍSTICAS", callback_data="view:stats"), InlineKeyboardButton("👥 CUENTAS", callback_data="view:accounts:0")],
            [InlineKeyboardButton("🏆 EXPERIENCIA EXP", callback_data="view:exp"), InlineKeyboardButton("🏰 CONFIGURACIÓN DEL GREMIO", callback_data="view:guild")],
            [InlineKeyboardButton("📋 REGISTRO DE ACTIVIDAD", callback_data="view:logs"), InlineKeyboardButton("🔄 ACTUALIZAR", callback_data="panel")],
            [InlineKeyboardButton("⚙️ CONFIGURACIÓN", callback_data="view:config")],
        ]
        return InlineKeyboardMarkup(rows)

    async def panel_text(self) -> str:
        running = sum(1 for task in bot_state.account_workers.values() if not task.done())
        return ("🔥 <b>AJAY GLORY</b>\n\n"
                f"Estado: <b>{'ACTIVO' if bot_state.glory_started else 'DETENIDO'}</b>\n"
                f"Workers activos: <b>{running}</b>\n"
                f"Cuentas registradas: <b>{len(bot_state.accounts)}</b>")

    async def send_panel(self, update: Update, edit: bool = False):
        text = await self.panel_text()
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=self.panel())
        else:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=self.panel())

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.reject_or_continue(update): return
        await self.send_panel(update)

    async def id_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user:
            await update.effective_message.reply_text(f"Tu ID de Telegram es: {user.id}\nEste comando no concede permisos.")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.reject_or_continue(update): return
        await update.effective_message.reply_text(await self.panel_text(), parse_mode=ParseMode.HTML)

    async def start_glory(self, update: Update):
        if bot_state.glory_started:
            return "Glory ya estaba iniciado; no se duplicaron workers."
        callback = bot_state.refresh_callbacks.get("on_glory_start")
        if not callback:
            return "El callback de inicio todavía no está listo."
        await callback()
        bot_state.glory_started = True
        active = sum(1 for t in bot_state.account_workers.values() if not t.done())
        bot_state.log(f"Telegram: Glory iniciado ({active} worker(s))", "success")
        return f"Glory iniciado. Workers activos: {active}."

    async def stop_glory(self, update: Update | None = None):
        stopped = 0
        for key, task in list(bot_state.account_workers.items()):
            if not task.done():
                task.cancel(); stopped += 1
        if bot_state.account_workers:
            await asyncio.gather(*bot_state.account_workers.values(), return_exceptions=True)
        for uid in list(bot_state.active_writers):
            bot_state.close_writers_for_account(uid)
        bot_state.account_workers.clear()
        bot_state.glory_started = False
        for uid in list(bot_state.accounts):
            if bot_state.accounts[uid].get("status") not in {"ERROR", "BANNED"}:
                bot_state.update_status(uid, "OFFLINE", 0)
        bot_state.log(f"Telegram: Glory detenido ({stopped} worker(s))", "warning")
        return f"Glory detenido. Workers cancelados: {stopped}."

    async def command_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.reject_or_continue(update):
            await update.effective_message.reply_text(await self.start_glory(update))

    async def command_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.reject_or_continue(update):
            await update.effective_message.reply_text(await self.stop_glory(update))

    def safe_account(self, acc: dict) -> str:
        uid = str(acc.get("uid", "?"))
        nick = acc.get("nickname") or "sin nombre"
        status = acc.get("status", "?")
        level = acc.get("level", "?")
        exp = acc.get("current_exp", acc.get("exp", 0))
        return f"• <b>{nick}</b> — UID {uid}\n  Estado: {status} | Nivel: {level} | EXP: {exp:,}"

    async def accounts_text(self, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
        accounts = sorted(bot_state.accounts.values(), key=lambda a: str(a.get("uid", "")))
        total_pages = max(1, (len(accounts) + self._page_size - 1) // self._page_size)
        page = max(0, min(page, total_pages - 1))
        subset = accounts[page * self._page_size:(page + 1) * self._page_size]
        text = f"👥 <b>CUENTAS</b> — página {page + 1}/{total_pages}\n\n"
        text += "\n\n".join(self.safe_account(a) for a in subset) if subset else "No hay cuentas cargadas."
        nav = []
        if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data=f"view:accounts:{page-1}"))
        nav.append(InlineKeyboardButton("🔄", callback_data=f"view:accounts:{page}"))
        if page + 1 < total_pages: nav.append(InlineKeyboardButton("▶️", callback_data=f"view:accounts:{page+1}"))
        nav.append(InlineKeyboardButton("⬅️ Panel", callback_data="panel"))
        return text, InlineKeyboardMarkup([nav])

    async def stats_text(self) -> str:
        accounts = list(bot_state.accounts.values())
        connected = sum(1 for a in accounts if str(a.get("status", "")).upper() in {"ONLINE", "IN_MATCH", "CONNECTING"})
        errors = sum(1 for a in accounts if str(a.get("status", "")).upper() == "ERROR")
        active = sum(int(a.get("active_matches", 0) or 0) for a in accounts)
        uptime = max(0, int(time.time() - bot_state.start_time))
        return ("📊 <b>ESTADÍSTICAS</b>\n\n"
                f"Cuentas: {len(accounts)} | Conectadas: {connected} | Desconectadas: {len(accounts)-connected-errors} | Errores: {errors}\n"
                f"Partidas realizadas: {bot_state.total_matches}\nPartidas activas: {active}\n"
                f"EXP obtenida: {bot_state.total_gained_exp:,}\nTiempo activo: {uptime//3600}h {(uptime%3600)//60}m")

    async def render_view(self, query, kind: str, page: int = 0):
        if kind == "stats":
            text, markup = await self.stats_text(), InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Panel", callback_data="panel")]])
        elif kind == "accounts":
            text, markup = await self.accounts_text(page)
        elif kind == "exp":
            rows = [f"• {a.get('nickname','sin nombre')}: {int(a.get('gained_exp',0) or 0):,} EXP | Nivel {a.get('level','?')} | {a.get('progress_pct',0)}%" for a in bot_state.accounts.values()]
            text = "🏆 <b>EXPERIENCIA EXP</b>\n\n" + ("\n".join(rows) if rows else "No hay datos disponibles.")
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Panel", callback_data="panel")]])
        elif kind == "guild":
            guild = bot_state.guild_config or {}
            text = "🏰 <b>CONFIGURACIÓN DEL GREMIO</b>\n\n" + ("\n".join(f"{k}: {v}" for k, v in guild.items() if k not in {"token", "password"}) or "No configurado.")
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Panel", callback_data="panel")]])
        elif kind == "logs":
            lines = [f"[{e.get('time','')}] {e.get('message','')}" for e in bot_state.logs[-30:]]
            text, markup = "📋 <b>REGISTRO DE ACTIVIDAD</b>\n\n" + ("\n".join(lines) if lines else "Sin actividad."), InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Actualizar", callback_data="view:logs"), InlineKeyboardButton("⬅️ Panel", callback_data="panel")]])
        else:
            text = "⚙️ <b>CONFIGURACIÓN</b>\n\nVariables: TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_IDS, DATA_DIR.\nUsa /id para consultar tu ID."
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Panel", callback_data="panel")]])
        await query.edit_message_text(text[:3900], parse_mode=ParseMode.HTML, reply_markup=markup)

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not await self.reject_or_continue(update):
            await query.answer("No autorizado", show_alert=True); return
        await query.answer()
        data = query.data or ""
        if data == "panel": return await self.send_panel(update, edit=True)
        if data == "glory:start": return await query.edit_message_text(await self.start_glory(update), reply_markup=self.panel())
        if data.startswith("delete:yes:"):
            uid = data.split(":", 2)[2]
            if (update.effective_user.id, uid) not in self.pending_deletes:
                return await query.edit_message_text("Solicitud de eliminación no confirmada o expirada.", reply_markup=self.panel())
            self.pending_deletes.discard((update.effective_user.id, uid))
            await self._delete_account(uid)
            return await query.edit_message_text(f"Cuenta {uid} eliminada.", reply_markup=self.panel())
        if data == "glory:stop": return await query.edit_message_text(await self.stop_glory(update), reply_markup=self.panel())
        parts = data.split(":")
        if len(parts) >= 2 and parts[0] == "view":
            page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            return await self.render_view(query, parts[1], page)

    async def command_accounts(self, update, context):
        if await self.reject_or_continue(update):
            text, markup = await self.accounts_text(0); await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    async def command_stats(self, update, context):
        if await self.reject_or_continue(update): await update.effective_message.reply_text(await self.stats_text(), parse_mode=ParseMode.HTML)
    async def command_delete(self, update, context):
        if not await self.reject_or_continue(update): return
        if not context.args:
            await update.effective_message.reply_text("Uso: /eliminar UID")
            return
        uid = str(context.args[0]).strip()
        self.pending_deletes.add((update.effective_user.id, uid))
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirmar eliminación", callback_data=f"delete:yes:{uid}"), InlineKeyboardButton("Cancelar", callback_data="panel")]])
        await update.effective_message.reply_text(f"¿Eliminar la cuenta {uid}? Esta acción detendrá su worker y quitará su registro persistente.", reply_markup=markup)

    async def _delete_account(self, uid: str):
        for key, task in list(bot_state.account_workers.items()):
            if str(key) == uid or uid in str(key):
                task.cancel(); bot_state.account_workers.pop(key, None)
        await asyncio.gather(*bot_state.account_workers.values(), return_exceptions=True)
        bot_state.close_writers_for_account(uid)
        bot_state.accounts.pop(uid, None)
        bot_state.account_credentials.pop(uid, None)
        path = os.path.join(os.getenv("DATA_DIR", "."), "accounts.json")
        try:
            with open(path, "r", encoding="utf-8") as f: rows = json.load(f)
            rows = [row for row in rows if str(row.get("uid", "")) != uid]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: json.dump(rows, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass
        bot_state.recalc_totals()

    async def command_logs(self, update, context):
        if await self.reject_or_continue(update):
            lines = [f"[{e.get('time','')}] {e.get('message','')}" for e in bot_state.logs[-40:]]
            await update.effective_message.reply_text("📋 REGISTRO\n\n" + ("\n".join(lines) if lines else "Sin actividad."))
    async def command_guild(self, update, context):
        if not await self.reject_or_continue(update): return
        if not context.args:
            guild = bot_state.guild_config or {}
            safe = "\n".join(f"{k}: {v}" for k, v in guild.items() if k not in {"token", "password"}) or "No configurado."
            await update.effective_message.reply_text("Configuración actual del gremio:\n" + safe)
            return
        guild_id = str(context.args[0]).strip()
        region = str(context.args[1]).strip().upper() if len(context.args) > 1 else str((bot_state.guild_config or {}).get("region", "IND")).upper()
        bot_state.guild_config.update({"guild_id": guild_id, "region": region})
        path = os.path.join(os.getenv("DATA_DIR", "."), "guild_config.json")
        try:
            with open(path, "w", encoding="utf-8") as f: json.dump(bot_state.guild_config, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            await update.effective_message.reply_text(f"No se pudo guardar: {exc}"); return
        bot_state.log(f"Telegram: configuración de gremio actualizada ({guild_id}, {region})", "success")
        await update.effective_message.reply_text(f"Gremio guardado: {guild_id} ({region}).")

    async def help_command(self, update, context):
        if await self.reject_or_continue(update): await update.effective_message.reply_text("/start /status /iniciar /detener /cuentas /stats /logs /eliminar UID /gremio ID REGION /ayuda\n/id solo muestra tu ID y no concede acceso.")

    async def notify(self, message: str, level: str = "info"):
        if not self.application or not _admin_ids(): return
        key = message[:100]
        now = time.time()
        if now - self.last_notice.get(key, 0) < 60: return
        self.last_notice[key] = now
        prefix = "⚠️" if level in {"warning", "error"} else "ℹ️"
        for admin_id in _admin_ids():
            try: await self.application.bot.send_message(admin_id, f"{prefix} {message[:3500]}")
            except Exception: pass

    async def start(self):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            return False
        self.application = Application.builder().token(token).build()
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("id", self.id_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("iniciar", self.command_start))
        self.application.add_handler(CommandHandler("detener", self.command_stop))
        self.application.add_handler(CommandHandler("cuentas", self.command_accounts))
        self.application.add_handler(CommandHandler("stats", self.command_stats))
        self.application.add_handler(CommandHandler("logs", self.command_logs))
        self.application.add_handler(CommandHandler("eliminar", self.command_delete))
        self.application.add_handler(CommandHandler("gremio", self.command_guild))
        self.application.add_handler(CommandHandler("ayuda", self.help_command))
        self.application.add_handler(CallbackQueryHandler(self.callback))
        bot_state.notify_callback = self.notify
        await self.application.initialize(); await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=True)
        return True

    async def stop(self):
        if self.application:
            await self.application.updater.stop(); await self.application.stop(); await self.application.shutdown()
            self.application = None


def create_telegram_control() -> TelegramControl:
    return TelegramControl()
