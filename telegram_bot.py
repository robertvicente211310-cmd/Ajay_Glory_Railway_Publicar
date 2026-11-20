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
        self.wizards: dict[int, dict] = {}
        self.pending_account_import: set[int] = set()

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
            [InlineKeyboardButton("📤 EXPORTAR CUENTAS", callback_data="data:export"), InlineKeyboardButton("📥 AGREGAR CUENTA", callback_data="accounts:add")],
            [InlineKeyboardButton("📂 IMPORTAR RESPALDO", callback_data="data:import")],
            [InlineKeyboardButton("🔑 RENOVAR TOKENS VENCIDOS", callback_data="token:refresh:all")],
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
        points_cfg = self._clan_points_config()
        clan_points = int(points_cfg.get("accounts", {}).get(uid, acc.get("clan_points", 0)) or 0)
        target = int(points_cfg.get("target", 4500) or 4500)
        remaining = max(0, target - clan_points)
        region = html.escape(str(acc.get("region") or "?"))
        return (f"• <b>{nick}</b> — UID {html.escape(uid)}\n"
                f"  Región: {region} | Estado: {status} | Nivel: {level} | EXP: {exp:,} (+{gained:,})\n"
                f"  🏰 Puntos clan: {clan_points:,}/{target:,} (faltan {remaining:,})")

    async def accounts_text(self, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
        accounts = sorted(bot_state.accounts.values(), key=lambda a: str(a.get("uid", "")))
        total_pages = max(1, (len(accounts) + self._page_size - 1) // self._page_size)
        page = max(0, min(page, total_pages - 1))
        subset = accounts[page * self._page_size:(page + 1) * self._page_size]
        text = f"👥 <b>CUENTAS</b> — página {page + 1}/{total_pages}\n\n"
        text += "\n\n".join(self.safe_account(a) for a in subset) if subset else "No hay cuentas cargadas."
        rows = []
        for acc in subset:
            uid = str(acc.get("uid", "")).strip()
            if uid:
                rows.append([InlineKeyboardButton(f"🔑 Renovar UID {uid[:18]}", callback_data=f"token:refresh:{uid}")])
        nav = []
        if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data=f"view:accounts:{page-1}"))
        nav.append(InlineKeyboardButton("🔄", callback_data=f"view:accounts:{page}"))
        if page + 1 < total_pages: nav.append(InlineKeyboardButton("▶️", callback_data=f"view:accounts:{page+1}"))
        nav.append(InlineKeyboardButton("⬅️ Panel", callback_data="panel"))
        rows.append([InlineKeyboardButton("➕ AGREGAR CUENTA", callback_data="accounts:add")])
        rows.append(nav)
        return text, InlineKeyboardMarkup(rows)

    async def refresh_account_token(self, uid: str) -> str:
        uid = str(uid).strip()
        ad = bot_state.account_credentials.get(uid)
        if not isinstance(ad, dict):
            return f"UID {uid}: no encuentro credenciales guardadas. Reimporta la cuenta con UID y contraseña."
        auth_uid = str(ad.get("auth_uid") or "").strip()
        auth_password = str(ad.get("auth_password") or "").strip()
        if not (auth_uid and auth_password):
            return f"UID {uid}: esta cuenta solo tiene un token. Si ya venció, no se puede regenerar desde ese mismo token; importa un token nuevo o las credenciales UID/contraseña."
        relogin = getattr(bot_state, "relogin_account", None)
        if not relogin:
            return f"UID {uid}: el servicio de reautenticación no está disponible."
        try:
            fresh = await relogin(uid, ad)
            if not fresh:
                return f"UID {uid}: Garena no entregó credenciales nuevas. Revisa el registro de actividad."
            worker = bot_state.account_workers.get(auth_uid)
            if worker and not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            cb = bot_state.refresh_callbacks.get("on_account_added")
            if bot_state.glory_started and cb:
                await cb({"uid": auth_uid, "password": auth_password})
            return f"UID {uid}: token renovado correctamente" + (" y worker reiniciado." if bot_state.glory_started else ". Pulsa INICIAR GLORY para reanudar.")
        except Exception as exc:
            return f"UID {uid}: error al renovar: {html.escape(str(exc)[:180])}"

    async def refresh_all_tokens(self) -> str:
        eligible = []
        seen = set()
        for ad in list(bot_state.account_credentials.values()):
            if not isinstance(ad, dict):
                continue
            uid = str(ad.get("account_id") or ad.get("auth_uid") or "").strip()
            if uid and uid not in seen and ad.get("auth_uid") and ad.get("auth_password"):
                eligible.append(uid); seen.add(uid)
        if not eligible:
            return "No hay cuentas con UID y contraseña guardados. Los tokens solos no se pueden renovar después de vencer."
        ok = failed = 0
        details = []
        for uid in eligible:
            result = await self.refresh_account_token(uid)
            if "token renovado correctamente" in result:
                ok += 1
            else:
                failed += 1
            details.append(result)
            await asyncio.sleep(0.8)
        return f"Renovación terminada: {ok} correcta(s), {failed} fallida(s), de {len(eligible)} cuenta(s).\n\n" + "\n".join(details[:25])

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
        uid = update.effective_user.id; state = self.wizards.get(uid)
        value = (update.effective_message.text or "").strip()
        if uid in self.pending_account_import:
            self.pending_account_import.discard(uid)
            await self._import_manual_account(update, value)
            return
        if not state or state.get("step") != "guild": return
        if not value.isdigit():
            await update.effective_message.reply_text("El ID del clan debe contener solo números.", reply_markup=ForceReply(selective=True)); return
        state.update({"guild_id": value, "step": "region"})
        await update.effective_message.reply_text("🌍 Selecciona la región del clan:", reply_markup=self.region_keyboard())

    async def wizard_region(self, query, region: str):
        state = self.wizards.setdefault(query.from_user.id, {})
        state.update({"region": region, "step": "count"})
        await query.edit_message_text(f"Región seleccionada: {region}\n\n¿Cuántas cuentas deseas crear?", reply_markup=self.count_keyboard())

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
        if data == "token:refresh:all":
            await query.edit_message_text("🔄 Renovando las cuentas que tienen UID y contraseña guardados. Esto puede tardar...", parse_mode=ParseMode.HTML)
            result = await self.refresh_all_tokens()
            return await query.message.reply_text(result[:3900], reply_markup=self.panel())
        if data.startswith("token:refresh:"):
            uid = data.split(":", 2)[2]
            await query.edit_message_text(f"🔄 Renovando credenciales de UID {html.escape(uid)}...")
            result = await self.refresh_account_token(uid)
            return await query.message.reply_text(result[:3900], reply_markup=self.panel())
        if data == "accounts:add":
            self.pending_account_import.add(query.from_user.id)
            return await query.edit_message_text(
                "➕ <b>AGREGAR CUENTA</b>\n\n"
                "Puedes adjuntar el archivo .dat/.json de la cuenta o escribir los datos en un mensaje.\n\n"
                "Formato manual: <code>UID:CONTRASEÑA</code>\n"
                "Para una cuenta con token: <code>TOKEN:tu_token</code>\n\n"
                "Al recibirla, el bot validará el acceso y mostrará nombre, UID, nivel y región.",
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Panel", callback_data="panel")]])
            )
        if data == "data:export":
            return await self.export_accounts(update)
        if data == "data:import":
            return await query.edit_message_text("📥 <b>IMPORTAR CUENTAS</b>\n\nAdjunta aquí el archivo JSON descargado con EXPORTAR CUENTAS. La importación fusionará los datos y reiniciará las cuentas si Glory está activo.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Panel", callback_data="panel")]]))
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

    def _clan_points_config(self) -> dict:
        data = self._read_data("clan_points.json", {"target": 4500, "accounts": {}})
        if not isinstance(data, dict):
            data = {"target": 4500, "accounts": {}}
        data.setdefault("target", 4500)
        data.setdefault("accounts", {})
        return data

    def _save_clan_points_config(self, data: dict) -> None:
        self._write_data_atomic("clan_points.json", data)

    async def command_clan_target(self, update, context):
        if not await self.reject_or_continue(update): return
        if not context.args or not context.args[0].isdigit():
            cfg = self._clan_points_config()
            await update.effective_message.reply_text(
                f"Meta actual de puntos del clan: {int(cfg.get('target', 4500)):,}.\n"
                "Para cambiarla: /meta_clan 4500")
            return
        target = int(context.args[0])
        if not 1 <= target <= 10_000_000:
            await update.effective_message.reply_text("La meta debe estar entre 1 y 10,000,000.")
            return
        cfg = self._clan_points_config(); cfg["target"] = target
        self._save_clan_points_config(cfg)
        await update.effective_message.reply_text(f"Meta individual guardada: {target:,} puntos de clan. La EXP/nivel sigue siendo independiente.")

    async def command_clan_points(self, update, context):
        if not await self.reject_or_continue(update): return
        cfg = self._clan_points_config(); target = int(cfg.get("target", 4500))
        if len(context.args) >= 2 and context.args[1].isdigit():
            uid = str(context.args[0]).strip(); points = int(context.args[1])
            if not uid.isdigit() or not 0 <= points <= 100_000_000:
                await update.effective_message.reply_text("Uso: /puntos UID CANTIDAD (puntos acumulados de clan).")
                return
            cfg.setdefault("accounts", {})[uid] = points
            self._save_clan_points_config(cfg)
            if uid in bot_state.accounts:
                bot_state.accounts[uid]["clan_points"] = points
            remaining = max(0, target - points)
            status = "Meta alcanzada" if points >= target else f"Faltan {remaining:,}"
            await update.effective_message.reply_text(f"UID {uid}: {points:,}/{target:,} puntos de clan. {status}.\nEste registro no modifica la EXP ni desconecta la cuenta.")
            return
        if context.args:
            await update.effective_message.reply_text("Uso: /puntos para ver el resumen o /puntos UID CANTIDAD para registrar puntos.")
            return
        rows = []
        for uid, acc in sorted(bot_state.accounts.items()):
            points = int(cfg.get("accounts", {}).get(uid, acc.get("clan_points", 0)) or 0)
            rows.append(f"• {html.escape(str(acc.get('nickname') or uid))} ({uid}): {points:,}/{target:,}")
        await update.effective_message.reply_text(
            f"🏰 PUNTOS DEL CLAN — META {target:,}\n\n" + ("\n".join(rows) if rows else "No hay cuentas cargadas.") +
            "\n\nEXP/nivel se controla por separado. Los puntos se registran manualmente.")

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
            "clan_points": self._clan_points_config(),
            # Estado visible para conservar nombres, niveles, EXP y orden de escuadra.
            "account_state": list(bot_state.accounts.values()),
        }
        payload = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
        name = f"ajay_glory_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
        await update.effective_message.reply_document(
            document=InputFile(io.BytesIO(payload), filename=name),
            caption="Respaldo completo de Ajay Glory. Contiene credenciales y tokens: guárdalo de forma privada."
        )

    def _extract_account_rows(self, data):
        """Acepta backups Ajay, guest_account_info .dat y credenciales simples."""
        if isinstance(data, dict) and isinstance(data.get("guest_account_info"), dict):
            info = data["guest_account_info"]
            uid = info.get("com.garena.msdk.guest_uid") or info.get("uid")
            password = info.get("com.garena.msdk.guest_password") or info.get("password")
            return ([{"uid": str(uid), "password": str(password)}] if uid and password else [])
        if isinstance(data, dict) and (data.get("uid") or data.get("token") or data.get("access_token")):
            row = dict(data)
            if row.get("access_token") and not row.get("token"): row["token"] = row["access_token"]
            return [row]
        if isinstance(data, list):
            return [dict(x) for x in data if isinstance(x, dict)]
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            return [dict(x) for x in data["accounts"] if isinstance(x, dict)]
        return []

    async def _validate_and_store_account(self, row):
        row = dict(row)
        uid = str(row.get("uid") or row.get("guest_uid") or "").strip()
        password = str(row.get("password") or row.get("guest_password") or "").strip()
        token = str(row.get("token") or row.get("access_token") or "").strip()
        callback = bot_state.refresh_callbacks.get("on_import_account")
        if not callback:
            raise RuntimeError("El motor de autenticación todavía no está listo; inténtalo de nuevo en unos segundos.")
        if uid and password:
            result = await callback({"uid": uid, "password": password})
            saved = {"uid": uid, "password": password}
        elif token:
            result = await callback({"token": token})
            saved = {"token": token}
        else:
            raise ValueError("No encontré UID+contraseña ni token en el archivo.")
        if not isinstance(result, dict) or not result.get("account_id"):
            raise ValueError("Garena no validó las credenciales. Revisa el archivo, la contraseña o el token.")
        account_id = str(result.get("account_id"))
        saved.update({"uid": account_id, "nickname": result.get("nickname") or f"Player_{account_id}",
                      "region": result.get("region") or "?", "level": int(result.get("level") or 1)})
        current = self._read_data("accounts.json", [])
        if not isinstance(current, list): current = []
        index = next((i for i, item in enumerate(current) if isinstance(item, dict) and str(item.get("uid", "")) == account_id), None)
        if index is None: current.append(saved); status = "nueva"
        else: current[index].update(saved); status = "actualizada"
        self._write_data_atomic("accounts.json", current)
        # Importar y verificar no inicia partidas ni envía cuentas a clanes automáticamente.
        return status, saved

    async def _import_manual_account(self, update: Update, value: str):
        try:
            if value.upper().startswith("TOKEN:"):
                row = {"token": value.split(":", 1)[1].strip()}
            elif ":" in value:
                uid, password = value.split(":", 1)
                row = {"uid": uid.strip(), "password": password.strip()}
            else:
                raise ValueError("Formato incorrecto. Usa UID:CONTRASEÑA o TOKEN:token.")
            status, acc = await self._validate_and_store_account(row)
            await update.effective_message.reply_text(
                f"<b>CUENTA {status.upper()}</b>\nNombre: {html.escape(str(acc['nickname']))}\n"
                f"ID: <code>{html.escape(str(acc['uid']))}</code>\nNivel: {acc['level']}\nRegión: {html.escape(str(acc['region']))}",
                parse_mode=ParseMode.HTML, reply_markup=self.panel())
        except Exception as exc:
            await update.effective_message.reply_text(f"No se pudo agregar la cuenta: {html.escape(str(exc))}", parse_mode=ParseMode.HTML, reply_markup=self.panel())

    async def import_accounts(self, update: Update):
        if not await self.reject_or_continue(update): return
        document = update.effective_message.document
        if not document:
            await update.effective_message.reply_text("Adjunta un archivo .dat/.json de cuenta o un respaldo JSON.")
            return
        if document.file_size and document.file_size > 15 * 1024 * 1024:
            await update.effective_message.reply_text("El archivo supera el límite de 15 MB."); return
        try:
            remote = await document.get_file()
            raw = bytes(await remote.download_as_bytearray())
            try:
                data = json.loads(raw.decode("utf-8-sig"))
            except Exception:
                # Algunos archivos .dat pueden traer JSON precedido por BOM/espacios; no se aceptan binarios opacos.
                raise ValueError("No pude leer el archivo. Envíame el .dat o .json original de la cuenta.")
            rows = self._extract_account_rows(data)
            if rows:
                # Backups completos conservan su ruta de restauración anterior.
                if isinstance(data, dict) and isinstance(data.get("accounts"), list) and data.get("format") == "ajay-glory-backup-v1":
                    await self._import_backup_bundle(update, data)
                    return
                results = []
                for row in rows:
                    try:
                        status, acc = await self._validate_and_store_account(row)
                        results.append(f"✅ {html.escape(str(acc['nickname']))} | ID {html.escape(str(acc['uid']))} | Nivel {acc['level']} | Región {html.escape(str(acc['region']))} ({status})")
                    except Exception as exc:
                        results.append(f"❌ {html.escape(str(exc))}")
                await update.effective_message.reply_text("<b>RESULTADO DE IMPORTACIÓN</b>\n\n" + "\n".join(results), parse_mode=ParseMode.HTML, reply_markup=self.panel())
                return
            raise ValueError("El archivo no contiene una cuenta reconocible. Se acepta guest_account_info, UID+contraseña o token.")
        except Exception as exc:
            await update.effective_message.reply_text(f"No se pudo importar: {html.escape(str(exc))}", parse_mode=ParseMode.HTML, reply_markup=self.panel())

    async def _import_backup_bundle(self, update: Update, bundle: dict):
        incoming = self._extract_account_rows(bundle)
        if not incoming: raise ValueError("El respaldo no contiene cuentas.")
        # Restaurar los datos auxiliares y validar cada credencial individualmente.
        imported = 0; failed = 0
        for row in incoming:
            try: await self._validate_and_store_account(row); imported += 1
            except Exception: failed += 1
        for name in ("token_cache", "devices", "guild_config", "banned_accounts"):
            if isinstance(bundle.get(name), dict):
                self._write_data_atomic(name + ".json", bundle[name])
        if isinstance(bundle.get("clan_points"), dict): self._save_clan_points_config(bundle["clan_points"])
        await update.effective_message.reply_text(f"Respaldo procesado. Cuentas validadas: {imported}; fallidas: {failed}.", reply_markup=self.panel())

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
        if await self.reject_or_continue(update): await update.effective_message.reply_text("/start /status /iniciar /detener /cuentas /stats /logs /generar CANTIDAD HILOS ID_CLAN REGION /exportar /importar /meta_clan PUNTOS /puntos [UID PUNTOS] /eliminar UID /gremio ID REGION /ayuda\n/id solo muestra tu ID y no concede acceso.")

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
        self.application.add_handler(CommandHandler("exportar", self.export_accounts))
        self.application.add_handler(CommandHandler("importar", self.import_accounts))
        self.application.add_handler(CommandHandler("meta_clan", self.command_clan_target))
        self.application.add_handler(CommandHandler("puntos", self.command_clan_points))
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
