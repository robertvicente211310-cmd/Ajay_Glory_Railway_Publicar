# -*- coding: utf-8 -*-
"""Panel privado de Telegram para controlar el motor existente de Ajay Glory."""
from __future__ import annotations

import asyncio
import html
import json
import os
import time
import io
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile, ForceReply
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from dashboard_server import bot_state, _run_automation, REGION_SERVER_MAP, _canonical_guild_region


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
        self.pending_imports: set[int] = set()
        self.pending_manual_dat: dict[int, str] = {}
        self.wizards: dict[int, dict] = {}

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
            [InlineKeyboardButton("👁️ CUENTAS JUGANDO", callback_data="view:playing"), InlineKeyboardButton("🏆 EXP POR CUENTA", callback_data="view:exp")],
            [InlineKeyboardButton("🏰 CONFIGURACIÓN DEL GREMIO", callback_data="view:guild"), InlineKeyboardButton("🤝 AMISTADES AUTO", callback_data="view:friends")],
            [InlineKeyboardButton("🚀 GENERAR + CLAN + JUGAR", callback_data="wizard:start"), InlineKeyboardButton("🔄 PROGRESO", callback_data="view:automation")],
            [InlineKeyboardButton("🎟️ CUENTAS AGREGADAS → CLAN + JUGAR", callback_data="existing:start")],
            [InlineKeyboardButton("➕ AGREGAR DAT MANUAL", callback_data="dat:manual")],
            [InlineKeyboardButton("📤 EXPORTAR CUENTAS", callback_data="data:export"), InlineKeyboardButton("📥 IMPORTAR CUENTAS", callback_data="data:import")],
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
        nick = html.escape(str(acc.get("nickname") or "sin nombre"))
        status = html.escape(str(acc.get("status", "?")))
        level = html.escape(str(acc.get("level", "?")))
        exp = int(acc.get("current_exp", acc.get("exp", 0)) or 0)
        gained = int(acc.get("gained_exp", 0) or 0)
        return f"• <b>{nick}</b> — UID {html.escape(uid)}\n  Estado: {status} | Nivel: {level} | EXP: {exp:,} (+{gained:,})"

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

    def region_keyboard(self):
        flags = {"IND": "🇮🇳", "ME": "🌍", "VN": "🇻🇳", "BD": "🇧🇩", "PK": "🇵🇰", "SG": "🇸🇬", "BR": "🇧🇷", "NA": "🇺🇸", "ID": "🇮🇩", "RU": "🇷🇺", "TH": "🇹🇭", "SAC": "🌎", "TW": "🇹🇼", "CIS": "🌐"}
        regions = [(flags.get(code, "🌐"), code) for code in REGION_SERVER_MAP]
        return InlineKeyboardMarkup([[InlineKeyboardButton(f"{flag} {code}", callback_data=f"wiz:region:{code}") for flag, code in regions[i:i+2]] for i in range(0, len(regions), 2)])

    def count_keyboard(self):
        return InlineKeyboardMarkup([[InlineKeyboardButton(str(n), callback_data=f"wiz:count:{n}") for n in (1, 2, 4, 8)], [InlineKeyboardButton("16", callback_data="wiz:count:16"), InlineKeyboardButton("32", callback_data="wiz:count:32")], [InlineKeyboardButton("⬅️ Cancelar", callback_data="panel")]])

    def thread_keyboard(self):
        return InlineKeyboardMarkup([[InlineKeyboardButton(f"🧵 {n} hilo(s)", callback_data=f"wiz:threads:{n}") for n in (1, 2, 3)], [InlineKeyboardButton("🧵 5 hilos (recomendado)", callback_data="wiz:threads:5")], [InlineKeyboardButton("⬅️ Cancelar", callback_data="panel")]])

    async def wizard_start(self, update: Update):
        uid = update.effective_user.id
        self.wizards[uid] = {"step": "guild"}
        await update.callback_query.edit_message_text("🚀 <b>CREAR CUENTAS + CLAN + JUGAR</b>\n\nPulsa el botón para introducir el ID del clan.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 PONER ID DEL CLAN", callback_data="wiz:askguild")], [InlineKeyboardButton("⬅️ Panel", callback_data="panel")]]))

    async def wizard_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.reject_or_continue(update): return
        uid = update.effective_user.id
        manual_stage = self.pending_manual_dat.get(uid)
        value = (update.effective_message.text or "").strip()
        if manual_stage == "uid":
            if not value.isdigit():
                await update.effective_message.reply_text("El guest_uid debe contener solo números.", reply_markup=ForceReply(selective=True)); return
            self.pending_manual_dat[uid] = "password:" + value
            await update.effective_message.reply_text("Ahora responde con el guest_account_info del DAT:", reply_markup=ForceReply(selective=True)); return
        if manual_stage and manual_stage.startswith("password:"):
            self.pending_manual_dat.pop(uid, None)
            return await self.add_manual_dat(update, manual_stage.split(":", 1)[1], value)
        state = self.wizards.get(uid)
        if not state or state.get("step") not in {"guild", "guild_existing"}: return
        if not value.isdigit():
            await update.effective_message.reply_text("El ID del clan debe contener solo números.", reply_markup=ForceReply(selective=True)); return
        state.update({"guild_id": value, "step": "region", "mode": "existing" if state.get("step") == "guild_existing" else "generate"})
        await update.effective_message.reply_text("🌍 Selecciona la región del clan:", reply_markup=self.region_keyboard())

    async def wizard_region(self, query, region: str):
        state = self.wizards.setdefault(query.from_user.id, {})
        state.update({"region": region, "step": "count"})
        if state.get("mode") == "existing":
            state["step"] = "confirm_existing"
            await query.edit_message_text(
                f"✅ <b>CONFIRMAR CUENTAS EXISTENTES</b>\n\nClan: {html.escape(str(state.get('guild_id')))}\nRegión: {html.escape(region)}\n\nSe usarán únicamente cuentas guardadas con token oficial. No se generarán cuentas ni se recreará el token.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎟️ ENVIAR AL CLAN Y JUGAR", callback_data="existing:run"), InlineKeyboardButton("❌ Cancelar", callback_data="panel")]])
            )
            return
        await query.edit_message_text(f"Región seleccionada: {region}\n\n¿Cuántas cuentas deseas crear?", reply_markup=self.count_keyboard())

    async def existing_start(self, update: Update):
        uid = update.effective_user.id
        self.wizards[uid] = {"step": "guild_existing", "mode": "existing"}
        await update.callback_query.edit_message_text(
            "🎟️ <b>CUENTAS EXISTENTES + CLAN + JUGAR</b>\n\nEscribe el ID numérico del clan. Solo se usarán cuentas con token oficial guardado; no se recrearán tokens.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data="panel")]])
        )

    async def existing_run(self, query):
        state = self.wizards.get(query.from_user.id, {})
        if bot_state.automation.get("running"):
            await query.edit_message_text("Ya hay una automatización en curso.", reply_markup=self.panel()); return
        if not state.get("guild_id") or not state.get("region"):
            await query.edit_message_text("Falta configurar el clan.", reply_markup=self.panel()); return
        from dashboard_server import _run_existing_accounts_to_guild
        bot_state.automation_task = asyncio.create_task(_run_existing_accounts_to_guild(state["guild_id"], state["region"]))
        self.wizards.pop(query.from_user.id, None)
        await query.edit_message_text("🎟️ Proceso iniciado. Se usarán los tokens oficiales guardados. Usa PROGRESO para consultar.", reply_markup=self.panel())

    async def wizard_count(self, query, count: int):
        state = self.wizards.setdefault(query.from_user.id, {})
        state.update({"total": count, "step": "threads"})
        await query.edit_message_text(f"Cuentas a crear: {count}\n\nSelecciona los hilos de generación:", reply_markup=self.thread_keyboard())

    async def wizard_threads(self, query, threads: int):
        state = self.wizards.setdefault(query.from_user.id, {})
        state.update({"threads": threads, "step": "confirm"})
        region = _canonical_guild_region(str(state.get("guild_id")), str(state.get("region", "IND")))
        state["region"] = region
        await query.edit_message_text(f"✅ <b>CONFIRMAR</b>\n\nClan: {html.escape(str(state.get('guild_id')))}\nRegión: {html.escape(region)}\nCuentas: {state.get('total')}\nHilos: {threads}\n\nEl flujo generará, enviará al clan y arrancará Glory.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 INICIAR", callback_data="wiz:run"), InlineKeyboardButton("❌ Cancelar", callback_data="panel")]]))

    async def wizard_run(self, query):
        state = self.wizards.get(query.from_user.id, {})
        if bot_state.automation.get("running"):
            await query.edit_message_text("Ya hay una automatización en curso.", reply_markup=self.panel()); return
        if not state.get("guild_id") or not state.get("region"):
            await query.edit_message_text("Falta configurar el clan.", reply_markup=self.panel()); return
        bot_state.automation_task = asyncio.create_task(_run_automation(state["guild_id"], state["region"], int(state.get("total", 1)), int(state.get("threads", 1)), True))
        self.wizards.pop(query.from_user.id, None)
        await query.edit_message_text("🚀 Proceso iniciado. Usa PROGRESO para ver generación, unión al clan y arranque de Glory.", reply_markup=self.panel())

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
        elif kind == "automation":
            a = bot_state.automation
            text = ("🚀 <b>AUTOMATIZACIÓN</b>\n\n" f"Estado: {a.get('stage','idle')}\n" f"Mensaje: {a.get('message','')}\n" f"Generadas: {a.get('created',0)}/{a.get('target',0)}\n" f"Unidas al clan: {a.get('joined',0)}\n" f"Fallidas: {a.get('failed',0)}")
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Actualizar", callback_data="view:automation"), InlineKeyboardButton("⬅️ Panel", callback_data="panel")]])
        elif kind == "playing":
            playing = [a for a in bot_state.accounts.values() if int(a.get('active_matches', 0) or 0) > 0 or str(a.get('status', '')).upper().startswith('IN_MATCH') or a.get('spectating')]
            lines = [f"🎮 {html.escape(str(a.get('nickname','sin nombre')))} — UID {html.escape(str(a.get('uid','?')))} | {'👁️ ESPECTANDO' if a.get('spectating') else '🟢 EN PARTIDA'} | Escuadra {html.escape(str(a.get('squad','?')))}" for a in playing]
            text = "👁️ <b>CUENTAS JUGANDO</b>\n\n" + ("\n".join(lines) if lines else "Ninguna cuenta está reportada en partida ahora.")
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Actualizar", callback_data="view:playing"), InlineKeyboardButton("⬅️ Panel", callback_data="panel")]])
        elif kind == "friends":
            text = "🤝 <b>AMISTADES AUTOMÁTICAS</b>\n\nEl motor tiene habilitada la aceptación automática de solicitudes mediante ConfirmFriendRequest. Las cuentas aceptan solicitudes mientras están conectadas; revisa REGISTRO DE ACTIVIDAD para ver confirmaciones."
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Ver registro", callback_data="view:logs"), InlineKeyboardButton("⬅️ Panel", callback_data="panel")]])
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
        if data == "wizard:start":
            return await self.wizard_start(update)
        if data == "existing:start":
            return await self.existing_start(update)
        if data == "dat:manual":
            self.pending_manual_dat[query.from_user.id] = "uid"
            await query.edit_message_text("➕ <b>AGREGAR DAT MANUAL</b>\n\nResponde a este mensaje con el guest_uid numérico.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="panel")]]))
            return await query.message.reply_text("guest_uid:", reply_markup=ForceReply(selective=True))
        if data == "wiz:askguild":
            self.wizards[query.from_user.id] = {"step": "guild"}
            await query.edit_message_text("Escribe ahora el ID numérico del clan:")
            return await query.message.reply_text("ID del clan:", reply_markup=ForceReply(selective=True))
        if data.startswith("wiz:region:"):
            return await self.wizard_region(query, data.split(":", 2)[2])
        if data.startswith("wiz:count:"):
            return await self.wizard_count(query, int(data.split(":", 2)[2]))
        if data.startswith("wiz:threads:"):
            return await self.wizard_threads(query, int(data.split(":", 2)[2]))
        if data == "wiz:run":
            return await self.wizard_run(query)
        if data == "existing:run":
            return await self.existing_run(query)
        if data == "data:export":
            return await self.export_accounts(update)
        if data == "data:import":
            self.pending_imports.add(query.from_user.id)
            await query.edit_message_text("📥 <b>IMPORTAR CUENTAS</b>\n\nPreparado. Ahora responde a este mensaje adjuntando el archivo <b>.dat de Garena</b> o el respaldo JSON.\n\nEl DAT se guardará protegido: no se generará ni se renovará ningún token.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="panel")]]))
            return await query.message.reply_text("📎 Adjunta ahora el archivo y usa la opción <b>Responder</b> sobre este mensaje.\n\nCuando termine, usa 🎟️ CUENTAS AGREGADAS → CLAN + JUGAR.", parse_mode=ParseMode.HTML, reply_markup=ForceReply(selective=True))
        if data == "automation:help":
            return await query.edit_message_text("🚀 <b>GENERAR + CLAN + JUGAR</b>\n\nUsa el comando:\n<code>/generar CANTIDAD HILOS ID_CLAN REGION</code>\n\nEjemplo: <code>/generar 4 2 2068400598 NA</code>\n\nEl flujo usa el generador original, consulta el clan, envía las cuentas al clan y arranca los workers de Glory.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Panel", callback_data="panel")]]))
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
    async def command_generate(self, update, context):
        if not await self.reject_or_continue(update): return
        if bot_state.automation.get("running"):
            await update.effective_message.reply_text("Ya hay una automatización en curso. Usa el botón PROGRESO.")
            return
        if not context.args or not context.args[0].isdigit():
            await update.effective_message.reply_text("Uso: /generar CANTIDAD [HILOS] [ID_CLAN] [REGION]\nEjemplo: /generar 4 2 2068400598 NA\nTambién puedes guardar el clan con /gremio ID REGION y usar /generar 4 2.")
            return
        total = max(1, min(1000, int(context.args[0])))
        threads = max(1, min(30, int(context.args[1]))) if len(context.args) > 1 and context.args[1].isdigit() else min(5, total)
        guild = bot_state.guild_config or {}
        guild_id = str(context.args[2]).strip() if len(context.args) > 2 else str(guild.get("guild_id", "")).strip()
        region = str(context.args[3]).strip().upper() if len(context.args) > 3 else str(guild.get("region", "IND")).upper()
        if not guild_id:
            await update.effective_message.reply_text("Falta el ID del clan. Usa /gremio ID REGION o incluye ID_CLAN en /generar.")
            return
        bot_state.automation_task = asyncio.create_task(_run_automation(guild_id, region, total, threads, True))
        await update.effective_message.reply_text(f"Automatización iniciada: generar {total} cuenta(s), enviar al clan {guild_id} ({region}) y comenzar Glory. Usa PROGRESO para consultar.")

    async def command_add_dat(self, update, context):
        if not await self.reject_or_continue(update): return
        if len(context.args) != 2:
            await update.effective_message.reply_text("Uso: /agregardat guest_uid guest_account_info\nEjemplo: /agregardat 8428044256 C911B5640C816C0A1BA1AA0A418E76C3E86B2C8C8AA30D5EE6C38E7A157E1E01")
            return
        await self.add_manual_dat(update, str(context.args[0]).strip(), str(context.args[1]).strip())

    def _data_path(self, name: str) -> str:
        return os.path.join(os.getenv("DATA_DIR", "."), name)

    def _read_data(self, name: str, default):
        try:
            with open(self._data_path(name), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _write_data_atomic(self, name: str, value):
        path = self._data_path(name); tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    async def export_accounts(self, update: Update):
        if not await self.reject_or_continue(update): return
        bundle = {
            "format": "ajay-glory-backup-v1",
            "created_at": int(time.time()),
            "accounts": self._read_data("accounts.json", []),
            "token_cache": self._read_data("token_cache.json", {}),
            "devices": self._read_data("devices.json", {}),
            "guild_config": self._read_data("guild_config.json", bot_state.guild_config or {}),
            "banned_accounts": self._read_data("banned_accounts.json", {}),
            # Estado visible para conservar nombres, niveles, EXP y orden de escuadra.
            "account_state": list(bot_state.accounts.values()),
        }
        payload = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
        name = f"ajay_glory_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
        await update.effective_message.reply_document(
            document=InputFile(io.BytesIO(payload), filename=name),
            caption="Respaldo completo de Ajay Glory. Contiene credenciales y tokens: guárdalo de forma privada."
        )

    async def import_accounts(self, update: Update):
        if not await self.reject_or_continue(update): return
        user_id = update.effective_user.id if update.effective_user else 0
        document = update.effective_message.document
        if not document:
            self.pending_imports.add(user_id)
            await update.effective_message.reply_text("📥 IMPORTAR CUENTAS\n\nResponde a este mensaje adjuntando el archivo .dat de Garena o el respaldo JSON.", reply_markup=ForceReply(selective=True))
            return
        if user_id not in self.pending_imports:
            await update.effective_message.reply_text("Primero pulsa 📥 IMPORTAR CUENTAS y luego responde al mensaje de importación adjuntando el archivo.")
            return
        self.pending_imports.discard(user_id)
        filename = str(document.file_name or "cuenta.dat")
        # Confirmación inmediata: evita que el usuario quede sin saber si Telegram
        # entregó el documento al bot mientras se descarga/procesa.
        await update.effective_message.reply_text(f"📥 Recibí {filename}. Procesando la cuenta DAT…")
        if document.file_size and document.file_size > 15 * 1024 * 1024:
            await update.effective_message.reply_text("El archivo supera el límite de 15 MB.")
            return
        try:
            remote = await document.get_file()
            raw = bytes(await remote.download_as_bytearray())
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("guest_account_info"), dict):
                guest = data.get("guest_account_info") or {}
                uid = str(guest.get("com.garena.msdk.guest_uid") or "").strip()
                password = str(guest.get("com.garena.msdk.guest_password") or "").strip()
                if not uid or not password:
                    raise ValueError("El DAT no contiene guest_uid y guest_password válidos")
                data = [{"uid": uid, "password": password, "auth_type": "dat", "source_file": filename}]
            if isinstance(data, list):
                bundle = {"accounts": data}
            elif isinstance(data, dict) and isinstance(data.get("accounts"), list):
                bundle = data
            else:
                raise ValueError("Formato de respaldo no reconocido")
            incoming = []
            for row in bundle.get("accounts", []):
                if not isinstance(row, dict): continue
                row = dict(row)
                if row.get("auth_type") == "dat" and row.get("uid") and row.get("password"):
                    row["uid"] = str(row["uid"]).strip(); row["password"] = str(row["password"]); incoming.append(row)
                elif row.get("uid") and row.get("password"):
                    row["uid"] = str(row["uid"]).strip(); incoming.append(row)
                elif row.get("token"):
                    row["token"] = str(row["token"]).strip(); incoming.append(row)
            if not incoming:
                raise ValueError("El respaldo no contiene cuentas válidas")
            current = self._read_data("accounts.json", [])
            merged = []
            index = {}
            def key(row):
                return "uid:" + str(row.get("uid")) if row.get("uid") else "token:" + str(row.get("token"))
            for row in current:
                if isinstance(row, dict): index[key(row)] = len(merged); merged.append(row)
            updated = 0; added = 0
            for row in incoming:
                k = key(row)
                if k in index:
                    if row.get("auth_type") == "dat":
                        # Si el UID ya existía como guest normal, convertirlo a DAT.
                        old = dict(merged[index[k]])
                        old.update(row)
                        old.pop("token", None); old.pop("game_token", None)
                        old.pop("auth_token", None); old.pop("auth_uid", None)
                        old["auth_type"] = "dat"
                        merged[index[k]] = old
                        updated += 1
                    else:
                        merged[index[k]].update(row); updated += 1
                else:
                    index[k] = len(merged); merged.append(row); added += 1
            self._write_data_atomic("accounts.json", merged)
            for name in ("token_cache", "devices"):
                if isinstance(bundle.get(name), dict):
                    existing = self._read_data(name + ".json", {})
                    if not isinstance(existing, dict): existing = {}
                    existing.update(bundle[name]); self._write_data_atomic(name + ".json", existing)
            if isinstance(bundle.get("guild_config"), dict) and bundle["guild_config"].get("guild_id"):
                bot_state.guild_config = bundle["guild_config"]
                self._write_data_atomic("guild_config.json", bot_state.guild_config)
            if isinstance(bundle.get("banned_accounts"), dict):
                self._write_data_atomic("banned_accounts.json", bundle["banned_accounts"])
            # Si Glory está activo, reiniciar solo las cuentas importadas para que
            # tomen sus credenciales/cache y vuelvan a entrar en sus escuadras.
            restarted = 0
            if bot_state.glory_started:
                for row in incoming:
                    cancelled = []
                    for worker_key, worker in list(bot_state.account_workers.items()):
                        if (row.get("uid") and str(worker_key) == str(row["uid"])) or (row.get("token") and str(worker_key).startswith(str(row["token"])[:10])):
                            worker.cancel(); cancelled.append(worker); bot_state.account_workers.pop(worker_key, None)
                    if cancelled:
                        await asyncio.gather(*cancelled, return_exceptions=True)
                    cb = bot_state.refresh_callbacks.get("on_account_added")
                    if cb:
                        await cb(row); restarted += 1
            await update.effective_message.reply_text(f"Importación completada. Nuevas: {added}, actualizadas: {updated}, workers reiniciados: {restarted}. El orden importado se conserva para reconstruir las escuadras.")
        except Exception as exc:
            await update.effective_message.reply_text(f"No se pudo importar el respaldo: {exc}")

    async def add_manual_dat(self, update: Update, uid: str, password: str):
        """Guarda manualmente el par guest_uid/guest_account_info como DAT."""
        if not uid.isdigit() or not password:
            await update.effective_message.reply_text("Formato inválido. Usa UID numérico y guest_account_info no vacío.")
            return
        row = {"uid": uid, "password": password, "auth_type": "dat", "source_file": "manual"}
        path = self._data_path("accounts.json")
        rows = self._read_data("accounts.json", [])
        if not isinstance(rows, list): rows = []
        found = False
        for i, old in enumerate(rows):
            if isinstance(old, dict) and str(old.get("uid", "")).strip() == uid:
                rows[i] = row; found = True; break
        if not found: rows.append(row)
        self._write_data_atomic("accounts.json", rows)
        cb = bot_state.refresh_callbacks.get("on_account_added")
        if cb and bot_state.glory_started:
            await cb(row)
        await update.effective_message.reply_text(f"✅ Cuenta DAT {'reemplazada' if found else 'agregada'}: UID {uid}.\nNo se guardó ningún token nuevo. Ya puedes usar CUENTAS AGREGADAS → CLAN + JUGAR.")

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
        if await self.reject_or_continue(update): await update.effective_message.reply_text("/start /status /iniciar /detener /cuentas /stats /logs /generar CANTIDAD HILOS ID_CLAN REGION /agregardat UID guest_account_info /exportar /importar /eliminar UID /gremio ID REGION /ayuda\n/id solo muestra tu ID y no concede acceso.")

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
        self.application.add_handler(CommandHandler("generar", self.command_generate))
        self.application.add_handler(CommandHandler("agregardat", self.command_add_dat))
        self.application.add_handler(CommandHandler("exportar", self.export_accounts))
        self.application.add_handler(CommandHandler("importar", self.import_accounts))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.wizard_text))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.import_accounts))
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
