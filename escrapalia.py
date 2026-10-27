#!/usr/bin/env python3
"""Robot de subastas de Escrapalia. Solo AVISA: nunca puja.
 
  - Avisa de lotes NUEVOS de las categorías que elijas, filtrados por provincias
    (o por radio desde Llodio, p. ej. Inmuebles).
  - Cada aviso lleva un botón ⭐ Seguir. Los lotes que sigues reciben avisos
    de cuenta atrás (24 h, 3 h, 1 h, 15 min) y salen en el resumen de la noche.
  - Comandos del bot: /favoritos, /quitar N, /resumen, /ayuda.
 
Uso:
    python escrapalia.py            # ejecución normal (avisa y guarda estado)
    python escrapalia.py --probar   # prueba: muestra lo que lee, sin avisar ni guardar
 
Comparte configuración (CONFIG_YAML), estado, Telegram y cálculo de distancias
con robot.py (hay que dejarlo en la misma carpeta).
"""
 
import hashlib
import html
import json
import os
import random
import re
import string
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
 
import requests
 
import robot
 
API = "https://api.escrapalia.com/api/web/batch"
WEB = "https://www.escrapalia.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
    "Origin": WEB,
    "Referer": WEB + "/",
}
TAM_PAGINA = 50
MAX_PAGINAS = 40                  # tope de seguridad: 2000 lotes por categoría
MAX_AVISOS_POR_EJECUCION = 30
PROVINCIAS_DEFECTO = ["Bizkaia", "Álava", "Gipuzkoa", "Navarra", "Cantabria",
                      "Burgos", "La Rioja"]
RECORDATORIOS_DEFECTO = [24, 3, 1, 0.25]       # horas antes del final
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MADRID = ZoneInfo("Europe/Madrid")
LIMITE_TELEGRAM = 3800
 
 
# --------------------------------------------------------------------------
# Lectura de la API
# --------------------------------------------------------------------------
 
def pedir_pagina(api, path, skip, tam):
    params = {
        "country": "España", "isFinished": "false",
        "language": "es", "pageIndex": skip // tam + 1, "pageSize": tam,
        "skip": skip, "rnd": "".join(random.choices(string.ascii_letters, k=16)),
    }
    if path:                       # Inmuebles (getEstate) no lleva categoryPath
        params["categoryPath"] = path
    r = requests.get(api, params=params, headers=HEADERS, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Escrapalia falló (HTTP {r.status_code})")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError("Escrapalia devolvió algo que no es JSON")
 
 
def leer_lotes(api, path):
    """Todos los lotes activos de una categoría. Devuelve (lotes, total, truncado)."""
    lotes, vistos, skip, total = [], set(), 0, 0
    for _ in range(MAX_PAGINAS):
        datos = pedir_pagina(api, path, skip, TAM_PAGINA)
        items = datos.get("data") or []
        total = datos.get("count") or total
        for it in items:
            if it.get("id") not in vistos:
                vistos.add(it.get("id"))
                lotes.append(it)
        skip += len(items)        # el servidor puede devolver menos de los pedidos
        if not items or skip >= total:
            return lotes, total, False
        time.sleep(random.uniform(0.5, 1.2))
    return lotes, total, True
 
 
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
 
 
def _fecha(txt):
    try:
        d = datetime.fromisoformat(txt)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
 
 
def normaliza(it):
    if not it.get("id"):
        return None
    loc = it.get("location") or {}
    cat_hash, hash_ = it.get("categoryHash"), it.get("hash")
    web = it.get("_web") or WEB
    if it.get("estate") and it.get("eventUrl"):      # Inmuebles: página del evento
        url = it["eventUrl"]
    else:
        url = (f"{web}/es/lote/{cat_hash}/{hash_}" if cat_hash and hash_
               else it.get("eventUrl") or web)
    return {
        "id": str(it["id"]),
        "titulo": (it.get("name") or "").strip() or "(sin título)",
        "puja": _num(it.get("lastBid") if it.get("lastBid") is not None
                     else it.get("startPrice", it.get("price"))),
        "siguiente": _num(it.get("nextBid")),
        "pujas": int(it.get("bidCount") or 0),
        "fin": _fecha(it.get("endDate")),
        "ciudad": (loc.get("city") or "").strip(),
        "region": (loc.get("province") or "").strip(),
        "url": url,
        "categoria": it.get("categoryName") or "",
        "cat_hash": cat_hash or "",
        "activa": it.get("isAuctionActive", True),
    }
 
 
def corto(ident):
    """Clave corta para los botones de Telegram (límite de 64 bytes)."""
    return hashlib.sha1(str(ident).encode()).hexdigest()[:12]
 
 
# --------------------------------------------------------------------------
# Zona: por provincias (lo normal) o por radio desde el centro (Inmuebles)
# --------------------------------------------------------------------------
 
def veredicto(cat, l, zona, ecfg, est, ctx, centro):
    """("ok", km|None) / ("dudoso", txt) / ("sinubicacion", txt) / ("fuera", txt).
    Lanza robot.ErrorGeo si hay que reintentar (solo en modo radio)."""
    if cat.get("radio_km"):
        z = {**zona, "radio_km": cat["radio_km"]}
        # "Piélagos (Somacueva, Liencres)" -> "Piélagos" para buscarlo en el mapa
        ciudad = re.split(r"\s*[(/,]", l["ciudad"])[0].strip()
        return robot.evaluar({**l, "ciudad": ciudad}, z, est, ctx, centro)
    provs = cat.get("provincias") or ecfg.get("provincias") or PROVINCIAS_DEFECTO
    if not l["region"]:
        return "fuera", "sin provincia"
    if robot.provincia_en(l["region"], provs):
        return "ok", None
    return "fuera", f"provincia {l['region']}"
 
 
# --------------------------------------------------------------------------
# Telegram (con botones y lectura de pulsaciones)
# --------------------------------------------------------------------------
 
def tg(metodo, **datos):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Falta el secreto TELEGRAM_TOKEN")
    for k, v in list(datos.items()):
        if isinstance(v, (dict, list)):
            datos[k] = json.dumps(v)
    r = requests.post(f"https://api.telegram.org/bot{token}/{metodo}",
                      data=datos, timeout=20)
    if r.status_code != 200:      # no se imprime la URL (lleva el token)
        raise RuntimeError(f"Telegram ({metodo}) respondió {r.status_code}: {r.text[:200]}")
    return r.json()
 
 
def enviar(texto, botones=None, previa=True):
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat:
        raise RuntimeError("Falta el secreto TELEGRAM_CHAT_ID")
    datos = {"chat_id": chat, "text": texto, "parse_mode": "HTML",
             "disable_web_page_preview": "false" if previa else "true"}
    if botones:
        datos["reply_markup"] = {"inline_keyboard": botones}
    tg("sendMessage", **datos)
 
 
def enviar_largo(lineas, botones=None):
    """Envía líneas de texto partiéndolas en mensajes de tamaño permitido."""
    trozo, largo, partes = [], 0, []
    for ln in lineas:
        if trozo and largo + len(ln) + 1 > LIMITE_TELEGRAM:
            partes.append("\n".join(trozo))
            trozo, largo = [], 0
        trozo.append(ln)
        largo += len(ln) + 1
    if trozo:
        partes.append("\n".join(trozo))
    for i, p in enumerate(partes):
        enviar(p, botones if i == len(partes) - 1 else None, previa=False)
 
 
def boton_lote(k, l_url, siguiendo):
    if siguiendo:
        sel = {"text": "✅ Siguiendo · quitar", "callback_data": f"u:{k}"}
    else:
        sel = {"text": "⭐ Seguir", "callback_data": f"f:{k}"}
    return [[sel, {"text": "🔗 Abrir", "url": l_url}]]
 
 
# --------------------------------------------------------------------------
# Textos
# --------------------------------------------------------------------------
 
def horas_restantes(fin, ahora):
    return (fin - ahora).total_seconds() / 3600 if fin else None
 
 
def en_tiempo(h):
    m = max(int(h * 60), 0)
    if m < 60:
        return f"{m} min"
    if m < 24 * 60:
        return f"{m // 60} h {m % 60:02d} min"
    return f"{m // 1440} d {(m % 1440) // 60} h"
 
 
def fecha_es(dt):
    d = dt.astimezone(MADRID)
    return f"{DIAS[d.weekday()]} {d:%d/%m} {d:%H:%M}"
 
 
def precio_txt(f):
    """Puja actual; si aún no hay pujas, el precio de salida."""
    if not f.get("pujas") and f.get("sig") is not None:
        return f"sale en {robot.formatea_precio(f['sig'])}"
    return robot.formatea_precio(f.get("puja"))
 
 
def euros(p):
    return robot.formatea_precio(p)
 
 
def txt_pujas(n):
    return "sin pujas" if not n else f"{n} {'puja' if n == 1 else 'pujas'}"
 
 
def cuerpo(cat_nombre, l, km, dudoso, ahora):
    lugar = ", ".join(x for x in (l["ciudad"], l["region"]) if x) or "ubicación no indicada"
    if km is not None:
        lugar += f" (a {km:.0f} km)"
    if dudoso:
        lugar += "\nℹ️ Ubicación dudosa"
    if not l["pujas"] and l["siguiente"] is not None:
        puja = f"Sin pujas · precio de salida {euros(l['siguiente'])}"
    else:
        puja = f"Puja actual {euros(l['puja'])} ({txt_pujas(l['pujas'])})"
        if l["siguiente"] is not None:
            puja += f" · siguiente {euros(l['siguiente'])}"
    h = horas_restantes(l["fin"], ahora)
    fin = f"\n⏳ Termina {fecha_es(l['fin'])} (en {en_tiempo(h)})" if h is not None else ""
    return (f"<b>[Escrapalia · {html.escape(cat_nombre)}]</b> {html.escape(l['titulo'])}\n"
            f"💶 {puja}\n📍 {html.escape(lugar)}{fin}\n{html.escape(l['url'])}")
 
 
def enlace(f):
    return f'<a href="{html.escape(f["url"])}">{html.escape(f["titulo"])}</a>'
 
 
def linea_lote(f, ahora, i=None):
    """Línea de resumen de un lote guardado (favorito o no)."""
    fin = _fecha(f.get("fin"))
    pre = f"{i}. " if i else "• "
    precio = precio_txt(f)
    p0 = f.get("p0")
    if f.get("estado") == "terminado":
        return (f"{pre}{enlace(f)}\n   🏁 Terminó con {precio} ({txt_pujas(f.get('pujas'))}; "
                "último precio visto)")
    cambio = ""
    if p0 is not None and f.get("puja") is not None and f["puja"] != p0:
        cambio = f" (al marcarlo {euros(p0)}, {f['puja'] - p0:+,.0f} €)".replace(",", ".")
    h = horas_restantes(fin, datetime.now(timezone.utc))
    cuando = (f" · ⏳ {fecha_es(fin)} (en {en_tiempo(h)})" if h is not None else "")
    return f"{pre}{enlace(f)}\n   💶 {precio}{cambio} · {txt_pujas(f.get('pujas'))}{cuando}"
 
 
def clave_fin(f):
    return _fecha(f.get("fin")) or datetime.max.replace(tzinfo=timezone.utc)
 
 
def texto_favoritos(est):
    favs = sorted(est["esc"]["fav"].items(), key=lambda kv: clave_fin(kv[1]))
    if not favs:
        return ["No sigues ningún lote. Pulsa ⭐ Seguir en un aviso para seguirlo."], None
    lineas = ["<b>⭐ Lotes que sigues</b>"]
    botones, fila = [], []
    for i, (k, f) in enumerate(favs, 1):
        lineas.append(linea_lote(f, None, i))
        fila.append({"text": f"❌ {i}", "callback_data": f"u:{k}"})
        if len(fila) == 4:
            botones.append(fila)
            fila = []
    if fila:
        botones.append(fila)
    lineas.append("\n<i>Pulsa ❌ con el número para dejar de seguir un lote.</i>")
    return lineas, botones
 
 
def texto_resumen(est, ahora):
    """Resumen de la noche: favoritos, finalizados hoy, lotes de mañana y avisos de hoy."""
    e = est["esc"]
    hoy = ahora.astimezone(MADRID).date()
    manana = hoy + timedelta(days=1)
    lineas = [f"🌙 <b>Resumen de subastas · {DIAS[hoy.weekday()]} {hoy:%d/%m}</b>"]
 
    favs = sorted(e["fav"].items(), key=lambda kv: clave_fin(kv[1]))
    activos = [f for _, f in favs if f.get("estado") != "terminado"]
    terminados = [f for _, f in favs if f.get("estado") == "terminado"]
    lineas.append("\n<b>⭐ Tus favoritos</b>")
    if activos:
        for i, f in enumerate(activos, 1):
            lineas.append(linea_lote(f, ahora, i))
    else:
        lineas.append("Ninguno activo.")
    if terminados:
        lineas.append("\n<b>🏁 Favoritos terminados</b>")
        for f in terminados:
            lineas.append(linea_lote(f, ahora))
 
    sig = [(k, f) for k, f in e["lotes"].items()
           if k not in e["fav"] and _fecha(f.get("fin"))
           and _fecha(f["fin"]).astimezone(MADRID).date() == manana
           and _fecha(f["fin"]) > ahora]
    sig.sort(key=lambda kv: clave_fin(kv[1]))
    resumen_botones, fila = [], []
    if sig:
        lineas.append(f"\n<b>⏰ Terminan mañana en tu zona ({len(sig)})</b>")
        for i, (k, f) in enumerate(sig[:10], 1):
            fin = _fecha(f["fin"]).astimezone(MADRID)
            lineas.append(f"{i}. {enlace(f)}\n   💶 {precio_txt(f)} · "
                          f"{txt_pujas(f.get('pujas'))} · {fin:%H:%M}")
            fila.append({"text": f"⭐ {i}", "callback_data": f"f:{k}"})
            if len(fila) == 5:
                resumen_botones.append(fila)
                fila = []
        if fila:
            resumen_botones.append(fila)
        if len(sig) > 10:
            lineas.append(f"… y {len(sig) - 10} más.")
 
    hoy_s = hoy.isoformat()
    n = (e.get("hoy") or {}).get("n", {}) if (e.get("hoy") or {}).get("fecha") == hoy_s else {}
    if n:
        lineas.append(f"\n<b>🆕 Avisos de hoy: {sum(n.values())}</b> · "
                      + ", ".join(f"{html.escape(c)}: {v}" for c, v in sorted(n.items())))
    return lineas, resumen_botones or None
 
 
# --------------------------------------------------------------------------
# Estado propio de Escrapalia (dentro del mismo state/vistos.json)
# --------------------------------------------------------------------------
 
def estado_esc(est):
    e = est.setdefault("esc", {})
    e.setdefault("offset", 0)       # siguiente update de Telegram a leer
    e.setdefault("lotes", {})       # clave corta -> datos del lote (en tu zona)
    e.setdefault("fav", {})         # clave corta -> lote que sigues
    e.setdefault("hoy", {})         # {"fecha": ..., "n": {categoría: nº avisos}}
    e.setdefault("palabras", [])    # palabras que te interesan (/palabras)
    e.setdefault("silencio", 0)     # hora (epoch) hasta la que no avisa de lotes nuevos
    e.setdefault("resumen", "")     # fecha del último resumen enviado
    return e
 
 
def instantanea(cat_nombre, l, km, dudoso):
    return {
        "id": l["id"], "titulo": l["titulo"], "url": l["url"],
        "fin": l["fin"].isoformat() if l["fin"] else None,
        "puja": l["puja"], "pujas": l["pujas"], "sig": l["siguiente"], "cat": cat_nombre,
        "km": km, "dudoso": dudoso, "vt": time.time(),
        "lugar": ", ".join(x for x in (l["ciudad"], l["region"]) if x),
    }
 
 
def refrescar(f, l):
    f["puja"], f["pujas"], f["sig"] = l["puja"], l["pujas"], l["siguiente"]
    if l["fin"]:
        f["fin"] = l["fin"].isoformat()
    f["vt"] = time.time()
 
 
def podar(est, ahora):
    e = est["esc"]
    limite = time.time() - 3 * 86400
    for k in [k for k, f in e["lotes"].items()
              if k not in e["fav"] and f.get("vt", 0) < limite]:
        del e["lotes"][k]
    for k in [k for k, f in e["fav"].items()
              if f.get("estado") == "terminado"
              and (_fecha(f.get("fin")) or ahora) < ahora - timedelta(days=3)]:
        del e["fav"][k]
 
 
# --------------------------------------------------------------------------
# Pulsaciones y comandos de Telegram
# --------------------------------------------------------------------------
 
AYUDA = ("<b>Robot de subastas</b>\n"
         "⭐ en un aviso: seguir ese lote (la confirmación puede tardar unos 10 min).\n\n"
         "/favoritos · lotes que sigues (con ❌ para quitarlos)\n"
         "/quitar N · dejar de seguir el lote N de /favoritos\n"
         "/terminan · lotes de tu zona que acaban en 24 h\n"
         "/sinpujas · lotes que acaban en 48 h y aún no tienen pujas\n"
         "/hoy · lotes de los que te he avisado hoy\n"
         "/buscar palabra · busca en los lotes de tu zona\n"
         "/palabras · tus palabras de interés (/palabras añadir X · /palabras quitar X)\n"
         "/silencio 8h · pausa los avisos de lotes nuevos (/silencio off para quitarla)\n"
         "/stats · cuántos lotes hay por categoría y cómo van tus favoritos\n"
         "/resumen · resumen de ahora mismo\n\n"
         "Los favoritos avisan a 24 h, 3 h, 1 h y 15 min del final y salen en el "
         "resumen de la noche; eso no se silencia.")
 
 
def cambiar_boton(msg, k, url, siguiendo):
    if not msg.get("message_id") or (msg.get("text") or "").startswith(("⭐ Lotes", "🌙", "📋")):
        return
    try:
        tg("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
           reply_markup={"inline_keyboard": boton_lote(k, url, siguiendo)})
    except Exception as ex:  # noqa: BLE001
        robot.log("No he podido cambiar el botón:", ex)
 
 
def seguir(est, k, ahora):
    e = est["esc"]
    lot = e["lotes"].get(k)
    if not lot:
        enviar("Ya no tengo datos de ese lote (puede haber terminado).")
        return None
    if k not in e["fav"]:
        e["fav"][k] = {**lot, "p0": lot.get("puja"), "av": [], "estado": "activo",
                       "alta": ahora.isoformat()}
    f = e["fav"][k]
    enviar(f"⭐ <b>Siguiendo:</b> {html.escape(f['titulo'])}\n"
           f"💶 {precio_txt(f)} · {txt_pujas(f.get('pujas'))}", previa=False)
    return f
 
 
def dejar(est, k):
    f = est["esc"]["fav"].pop(k, None)
    if f:
        enviar(f"❌ <b>Dejas de seguir:</b> {html.escape(f['titulo'])}", previa=False)
    return f
 
 
def listado(titulo, items, maximo=12):
    """Mensaje numerado con botones ⭐ para seguir cada lote. items = [(k, lote)]."""
    if not items:
        return [f"📋 <b>{html.escape(titulo)}</b>\nNo hay ninguno ahora mismo."], None
    lineas = [f"📋 <b>{html.escape(titulo)}</b> ({len(items)})"]
    botones, fila = [], []
    for i, (k, f) in enumerate(items[:maximo], 1):
        fin = _fecha(f.get("fin"))
        cuando = f" · ⏳ {fecha_es(fin)}" if fin else ""
        lugar = f" · 📍 {html.escape(f['lugar'])}" if f.get("lugar") else ""
        lineas.append(f"{i}. {enlace(f)}\n   💶 {precio_txt(f)} · "
                      f"{txt_pujas(f.get('pujas'))}{cuando}{lugar}")
        fila.append({"text": f"⭐ {i}", "callback_data": f"f:{k}"})
        if len(fila) == 5:
            botones.append(fila)
            fila = []
    if fila:
        botones.append(fila)
    if len(items) > maximo:
        lineas.append(f"… y {len(items) - maximo} más.")
    return lineas, botones
 
 
def lotes_activos(est, ahora, solo_no_fav=True):
    e = est["esc"]
    return [(k, f) for k, f in e["lotes"].items()
            if (not solo_no_fav or k not in e["fav"])
            and _fecha(f.get("fin")) and _fecha(f["fin"]) > ahora]
 
 
def duracion(txt):
    """'8h', '90m', '2d' -> segundos; None si no se entiende."""
    txt = (txt or "").strip().lower()
    try:
        n = float(txt[:-1])
    except ValueError:
        return None
    return n * {"m": 60, "h": 3600, "d": 86400}.get(txt[-1:], 0) or None
 
 
def coincide_palabra(est, titulo):
    t = robot.sin_acentos(titulo)
    for w in est["esc"]["palabras"]:
        if robot.sin_acentos(w) in t:
            return w
    return None
 
 
def texto_stats(est, ahora):
    e = est["esc"]
    act = lotes_activos(est, ahora, solo_no_fav=False)
    por = {}
    for _, f in act:
        c = por.setdefault(f.get("cat", "?"), [0, 0])
        c[0] += 1
        c[1] += not f.get("pujas")
    lineas = [f"📋 <b>Estadísticas</b>\nLotes activos en tu zona: {len(act)}"]
    for c, (n, s) in sorted(por.items()):
        lineas.append(f"• {html.escape(c)}: {n} (sin pujas: {s})")
    favs = list(e["fav"].values())
    sub = [(f["puja"] - f["p0"]) for f in favs
           if f.get("p0") is not None and f.get("puja") is not None]
    if favs:
        lineas.append(f"\n⭐ Favoritos: {len(favs)} "
                      f"({sum(f.get('estado') == 'terminado' for f in favs)} terminados)")
        if sub:
            lineas.append(f"Subida media de la puja desde que los marcaste: "
                          f"{euros(sum(sub) / len(sub))}")
    return lineas
 
 
def procesar_telegram(est, ahora):
    """Lee lo que has pulsado/escrito desde la última vez y lo aplica."""
    e = est["esc"]
    chat = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    try:
        r = tg("getUpdates", offset=e["offset"], timeout=0,
               allowed_updates=["message", "callback_query"])
    except Exception as ex:  # noqa: BLE001
        robot.log("No he podido leer los mensajes de Telegram:", ex)
        return
    for u in r.get("result", []):
        e["offset"] = max(e["offset"], u["update_id"] + 1)
        cb = u.get("callback_query")
        if cb:
            msg = cb.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != chat:
                continue
            accion, _, k = (cb.get("data") or "").partition(":")
            if accion == "f":
                f = seguir(est, k, ahora)
                if f:
                    cambiar_boton(msg, k, f["url"], True)
            elif accion == "u":
                f = dejar(est, k)
                if f:
                    cambiar_boton(msg, k, f["url"], False)
            continue
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != chat:
            continue
        partes = (m.get("text") or "").split()
        if not partes or not partes[0].startswith("/"):
            continue
        cmd = partes[0].split("@")[0].lower()
        if cmd in ("/start", "/ayuda", "/help"):
            enviar(AYUDA, previa=False)
        elif cmd == "/favoritos":
            lineas, botones = texto_favoritos(est)
            enviar_largo(lineas, botones)
        elif cmd == "/quitar":
            favs = sorted(e["fav"].items(), key=lambda kv: clave_fin(kv[1]))
            try:
                dejar(est, favs[int(partes[1]) - 1][0])
            except (IndexError, ValueError):
                enviar("Usa /quitar seguido del número que ves en /favoritos. "
                       "Ejemplo: /quitar 2", previa=False)
        elif cmd == "/terminan":
            items = sorted((kv for kv in lotes_activos(est, ahora)
                            if _fecha(kv[1]["fin"]) <= ahora + timedelta(hours=24)),
                           key=lambda kv: clave_fin(kv[1]))
            enviar_largo(*listado("Terminan en las próximas 24 h", items))
        elif cmd == "/sinpujas":
            items = sorted((kv for kv in lotes_activos(est, ahora)
                            if not kv[1].get("pujas")
                            and _fecha(kv[1]["fin"]) <= ahora + timedelta(hours=48)),
                           key=lambda kv: clave_fin(kv[1]))
            enviar_largo(*listado("Sin pujas y terminan en 48 h", items))
        elif cmd == "/hoy":
            ks = (e["hoy"].get("ks") or []) if e["hoy"].get("fecha") == \
                ahora.astimezone(MADRID).strftime("%Y-%m-%d") else []
            items = [(k, e["lotes"][k]) for k in ks if k in e["lotes"]]
            enviar_largo(*listado("Avisos de hoy", items))
        elif cmd == "/buscar":
            q = robot.sin_acentos(" ".join(partes[1:]))
            if not q:
                enviar("Escribe qué buscar. Ejemplo: /buscar audi", previa=False)
            else:
                items = sorted((kv for kv in lotes_activos(est, ahora, False)
                                if q in robot.sin_acentos(kv[1]["titulo"])),
                               key=lambda kv: clave_fin(kv[1]))
                enviar_largo(*listado(f"Resultados de «{q}»", items))
        elif cmd == "/palabras":
            sub = partes[1].lower() if len(partes) > 1 else ""
            w = " ".join(partes[2:]).strip().lower()
            if sub in ("añadir", "anadir", "add") and w:
                if w not in e["palabras"]:
                    e["palabras"].append(w)
                enviar(f"🔔 Palabra añadida: {html.escape(w)}", previa=False)
            elif sub in ("quitar", "borrar") and w:
                e["palabras"] = [x for x in e["palabras"] if x != w]
                enviar(f"Palabra quitada: {html.escape(w)}", previa=False)
            else:
                lista = ", ".join(html.escape(x) for x in e["palabras"]) or "ninguna"
                enviar(f"🔔 <b>Palabras de interés:</b> {lista}\n"
                       "Te aviso aunque estés en /silencio. Cambia con "
                       "/palabras añadir tractor · /palabras quitar tractor", previa=False)
        elif cmd == "/silencio":
            arg = partes[1].lower() if len(partes) > 1 else ""
            if arg in ("off", "no", "quitar"):
                e["silencio"] = 0
                enviar("🔔 Avisos reactivados.", previa=False)
            elif duracion(arg):
                e["silencio"] = time.time() + duracion(arg)
                hasta = datetime.fromtimestamp(e["silencio"], MADRID)
                enviar(f"🔕 Sin avisos de lotes nuevos hasta {hasta:%d/%m %H:%M}. Al acabar te "
                       "llegarán los que sigan activos. Favoritos y resumen siguen.",
                       previa=False)
            else:
                enviar("Ejemplos: /silencio 8h · /silencio 90m · /silencio off", previa=False)
        elif cmd == "/stats":
            enviar_largo(texto_stats(est, ahora))
        elif cmd == "/resumen":
            lineas, botones = texto_resumen(est, ahora)
            enviar_largo(lineas, botones)
 
 
# --------------------------------------------------------------------------
# Seguimiento de favoritos
# --------------------------------------------------------------------------
 
def seguir_favoritos(est, actual, ahora, umbrales):
    """Actualiza los favoritos y avisa de cuenta atrás y de lotes terminados."""
    e = est["esc"]
    for k, f in e["fav"].items():
        if f.get("estado") == "terminado":
            continue
        l = actual.get(f["id"])
        fin = _fecha(f.get("fin"))
        if l:
            refrescar(f, l)
            fin = l["fin"] or fin
        elif fin and fin <= ahora:
            f["estado"] = "terminado"
            enviar(f"🏁 <b>Terminó un lote que seguías</b>\n{enlace(f)}\n"
                   f"💶 Último precio visto: {euros(f.get('puja'))} "
                   f"({txt_pujas(f.get('pujas'))}). Mira en la web el resultado final.",
                   previa=False)
            continue
        h = horas_restantes(fin, ahora)
        if h is None or h <= 0:
            continue
        pendientes = [u for u in umbrales if h <= u and u not in f["av"]]
        if pendientes:
            f["av"].extend(pendientes)       # si se salta alguno, solo se avisa del último
            p0 = f.get("p0")
            cambio = ""
            if p0 is not None and f.get("puja") is not None and f["puja"] != p0:
                cambio = f" (al marcarlo {euros(p0)})"
            enviar(f"⏰ <b>⭐ Termina en {en_tiempo(h)}</b>\n{enlace(f)}\n"
                   f"💶 {precio_txt(f)}{cambio} · {txt_pujas(f.get('pujas'))}\n"
                   f"⏳ {fecha_es(fin)}", previa=False)
 
 
# --------------------------------------------------------------------------
# Programa principal
# --------------------------------------------------------------------------
 
def main():
    probar = "--probar" in sys.argv
    cfg = robot.cargar_config()
    ecfg = cfg.get("escrapalia") or {}
    if not ecfg or not ecfg.get("activa", True):
        robot.log("Escrapalia desactivada o sin configurar (clave 'escrapalia').")
        return
    cats = [c for c in ecfg.get("categorias", []) if (c.get("path") or c.get("api")) and c.get("activa", True)]
    if not cats:
        robot.log("Escrapalia: faltan las 'categorias' con su 'path'.")
        return
 
    zona = cfg.get("zona", {})
    centro = (zona.get("latitud", robot.LAT_DEFECTO), zona.get("longitud", robot.LON_DEFECTO))
    horas_fin = ecfg.get("termina_pronto_horas", 0) or 0
    avisar_nuevos = ecfg.get("avisar_nuevos", True)
    umbrales = sorted(ecfg.get("recordatorios_favoritos_horas", RECORDATORIOS_DEFECTO),
                      reverse=True)
    hora_resumen = (cfg.get("resumen") or {}).get("hora", 21)
    dudosos_ok = zona.get("avisar_ubicacion_dudosa", True)
    sin_ubic_ok = zona.get("avisar_sin_ubicacion", False)
 
    est = robot.cargar_estado()
    e = estado_esc(est)
    vistos = est["vistos"].setdefault("escrapalia", {})
    ctx = {"consultas": 0, "errores": 0}
    ahora = datetime.now(timezone.utc)
 
    def decide(v):   # ¿se avisa con este veredicto?
        return not (v == "fuera" or (v == "dudoso" and not dudosos_ok)
                    or (v == "sinubicacion" and not sin_ubic_ok))
 
    # ---- 1) Leer todas las categorías
    leidas, actual, fallos, leidas_crudas = [], {}, [], {}
    for cat in cats:
        path = cat.get("path") or ""
        nombre = cat.get("nombre") or path
        cat = {**cat, "nombre": nombre, "path": path}
        try:
            crudos, total, truncado = leer_lotes(cat.get("api") or API, path)
        except Exception as ex:  # noqa: BLE001
            robot.log(f"[{nombre}] error: {ex}")
            fallos.append(nombre)
            if not probar:
                robot.alertar(est, f"error|escrapalia|{nombre}",
                              f"No he podido leer '{nombre}' de Escrapalia: {ex}. "
                              "Lo sigo intentando en cada ejecución.")
            continue
        if truncado and not probar:
            robot.alertar(est, f"truncado|escrapalia|{nombre}",
                          f"'{nombre}' tiene más de {MAX_PAGINAS * TAM_PAGINA} lotes y solo "
                          "leo los primeros. Avísale a Claude para ajustarlo.", horas=48)
        excluir = set(cat.get("excluir_categorias") or [])
        lotes = []
        if cat.get("web"):
            for it in crudos:
                it["_web"] = cat["web"]
        leidas_crudas[nombre] = crudos[:1]
        for it in crudos:
            l = normaliza(it)
            if not l:
                continue
            actual[l["id"]] = l
            if (l["activa"] and l["cat_hash"] not in excluir
                    and (l["fin"] is None or l["fin"] > ahora)):
                lotes.append(l)
        leidas.append((cat, lotes, total))
 
    # ---- Modo prueba: solo informa
    if probar:
        for cat, lotes, total in leidas:
            robot.log(f"\n=== {cat['nombre']} ({cat['path'] or cat.get('api')}): {len(lotes)} lotes activos "
                      f"de {total} ===")
            if leidas_crudas.get(cat["nombre"]):
                r0 = leidas_crudas[cat["nombre"]][0]
                robot.log("Campos de cada lote: " + ", ".join(sorted(r0.keys())))
                robot.log("Ubicación del primer lote: " + json.dumps(
                    r0.get("location"), ensure_ascii=False))
            cuenta, provs = {}, {}
            for l in lotes:
                cuenta[l["cat_hash"]] = cuenta.get(l["cat_hash"], 0) + 1
                provs[l["region"] or "(sin provincia)"] = provs.get(
                    l["region"] or "(sin provincia)", 0) + 1
            robot.log("Subcategorías (categoryHash, para 'excluir_categorias'): "
                      + ", ".join(f"{k}: {n}" for k, n in sorted(cuenta.items())))
            robot.log("Provincias de los lotes: "
                      + ", ".join(f"{k}: {n}" for k, n in sorted(provs.items(),
                                                                 key=lambda x: -x[1])))
            dentro = []
            for l in lotes:
                try:
                    v, d = veredicto(cat, l, zona, ecfg, est, ctx, centro)
                except robot.ErrorGeo as ex:
                    v, d = "?", str(ex)
                if v == "ok" or (v == "dudoso" and dudosos_ok):
                    dentro.append((l, v, d))
                if len(dentro) >= 8:
                    break
            modo = (f"radio {cat['radio_km']} km" if cat.get("radio_km")
                    else "provincias " + ", ".join(
                        cat.get("provincias") or ecfg.get("provincias") or PROVINCIAS_DEFECTO))
            robot.log(f"Zona de esta categoría: {modo}. Primeros lotes que entrarían:")
            for l, v, d in dentro:
                h = horas_restantes(l["fin"], ahora)
                km = f", a {d:.0f} km" if isinstance(d, (int, float)) else ""
                robot.log(f"- {l['titulo'][:45]!r} | puja {euros(l['puja'])} "
                          f"({l['pujas']}) | termina en "
                          f"{en_tiempo(h) if h is not None else '?'} | "
                          f"{l['ciudad']}, {l['region']}{km} | {v}")
            if not dentro:
                robot.log("(ningún lote de esta categoría entra en la zona ahora mismo)")
        robot.log(f"\nModo prueba: no se ha avisado ni guardado nada "
                  f"({ctx['consultas']} consultas al mapa). Favoritos y botones no se "
                  "prueban aquí: se ven en Telegram.")
        return
 
    # ---- 2) Pulsaciones y comandos de Telegram
    try:
        procesar_telegram(est, ahora)
    except Exception as ex:  # noqa: BLE001
        robot.log("Error enviando a Telegram:", ex)
        sys.exit(1)
 
    # ---- 3) Lotes nuevos / terminan pronto
    avisos = []            # {"txt", "botones", "deshacer"}
    inicializadas, n_dentro, n_marcados = [], {}, 0
    for cat, lotes, _total in leidas:
        nombre, path = cat["nombre"], cat["path"]
        clave = f"escrapalia|{path or nombre}"
        primera = clave not in est["listos"]
        modo_radio = bool(cat.get("radio_km"))
        dentro = 0
        for l in lotes:
            k = corto(l["id"])
            reg = robot.registro(vistos, l["id"])
            nuevo = reg is None
            if nuevo:
                reg = vistos[l["id"]] = {"t": time.time(), "p": l["puja"], "d": None, "k": None}
                n_marcados += primera
            reg["t"] = time.time()
            if l["puja"] is not None:
                reg["p"] = l["puja"]
            try:
                if reg["d"] is None and not (nuevo and primera and modo_radio):
                    v, d = veredicto(cat, l, zona, ecfg, est, ctx, centro)
                    reg["d"] = 1 if decide(v) else 0
                    reg["k"] = d if v == "ok" and isinstance(d, (int, float)) else None
                    reg["v"] = "dudoso" if v in ("dudoso", "sinubicacion") else "ok"
            except robot.ErrorGeo as ex:
                robot.log(f"[{nombre}] '{l['titulo'][:30]}' se reintentará más tarde ({ex})")
                if nuevo:
                    del vistos[l["id"]]
                continue
            if reg["d"] != 1:
                continue
            dentro += 1
            dudoso = reg.get("v") == "dudoso"
            snap = e["lotes"].get(k)
            if snap:
                refrescar(snap, l)
            else:
                e["lotes"][k] = instantanea(nombre, l, reg["k"], dudoso)
            siguiendo = k in e["fav"]
            h = horas_restantes(l["fin"], ahora)
            botones = boton_lote(k, l["url"], siguiendo)
 
            if nuevo and not primera and avisar_nuevos:
                w = coincide_palabra(est, l["titulo"])
                avisos.append({
                    "txt": ("🔔 " if w else "🆕 ") + cuerpo(nombre, l, reg["k"], dudoso, ahora)
                           + (f"\n🔔 Coincide con «{html.escape(w)}»" if w else ""),
                    "botones": botones, "cat": nombre, "k": k, "prioridad": bool(w),
                    "deshacer": lambda i=l["id"]: vistos.pop(i, None)})
                if h is not None and 0 < h <= horas_fin:
                    reg["fin"] = 1          # ya avisado, no repetir
            elif (horas_fin and h is not None and 0 < h <= horas_fin
                  and not reg.get("fin") and not siguiendo and not nuevo):
                reg["fin"] = 1
                avisos.append({
                    "txt": (f"⏰ <b>Termina en {en_tiempo(h)}</b>\n"
                            + cuerpo(nombre, l, reg["k"], dudoso, ahora)),
                    "botones": botones, "cat": None, "prioridad": False,
                    "deshacer": lambda r=reg: r.pop("fin", None)})
        n_dentro[nombre] = dentro
        if primera:
            est["listos"].append(clave)
            inicializadas.append(nombre)
        robot.log(f"[{nombre}] {len(lotes)} lotes activos, {dentro} en tu zona")
 
    if ctx["errores"] >= robot.MAX_ERRORES_GEO:
        robot.alertar(est, "geo", "No consigo consultar el mapa (Nominatim) para calcular "
                      "distancias. Los lotes afectados se reintentarán en la próxima revisión.",
                      horas=12)
 
    # ---- 4) Enviar avisos, favoritos, resumen
    try:
        if inicializadas:
            detalle = ", ".join(f"{html.escape(n)} ({n_dentro.get(n, 0)})" for n in inicializadas)
            enviar("✅ <b>Escrapalia activado.</b> Categorías (lotes en tu zona ahora): "
                   f"{detalle}. Los lotes que ya había quedan como vistos; te aviso de los "
                   "nuevos. En cada aviso, ⭐ Seguir añade el lote a favoritos. "
                   "Escribe /ayuda para ver los comandos.", previa=False)
        if time.time() < e["silencio"]:      # /silencio: solo pasan las palabras de interés
            for a in [a for a in avisos if not a["prioridad"]]:
                a["deshacer"]()
            avisos = [a for a in avisos if a["prioridad"]]
        avisos.sort(key=lambda a: not a["prioridad"])
        enviados = avisos[:MAX_AVISOS_POR_EJECUCION]
        for a in avisos[MAX_AVISOS_POR_EJECUCION:]:
            a["deshacer"]()          # se avisará en la próxima revisión
        hoy = ahora.astimezone(MADRID).strftime("%Y-%m-%d")
        if e["hoy"].get("fecha") != hoy:
            e["hoy"] = {"fecha": hoy, "n": {}, "ks": []}
        for a in enviados:
            enviar(a["txt"], a["botones"])
            if a["cat"]:
                e["hoy"]["n"][a["cat"]] = e["hoy"]["n"].get(a["cat"], 0) + 1
                e["hoy"].setdefault("ks", []).append(a["k"])
            time.sleep(0.7)
        seguir_favoritos(est, actual, ahora, umbrales)
        if (ahora.astimezone(MADRID).hour >= hora_resumen and e["resumen"] != hoy
                and not fallos):
            lineas, botones = texto_resumen(est, ahora)
            enviar_largo(lineas, botones)
            e["resumen"] = hoy
    except Exception as ex:  # noqa: BLE001
        # Si Telegram falla no se guarda el estado: así se reintentará
        robot.log("Error enviando a Telegram:", ex)
        sys.exit(1)
 
    podar(est, ahora)
    robot.guardar_estado(est)
    robot.log(f"Hecho. Avisos de Escrapalia: {len(enviados)} "
              f"({ctx['consultas']} consultas al mapa, {len(e['fav'])} favoritos)")
 
 
if __name__ == "__main__":
    main()
 
