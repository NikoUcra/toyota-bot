#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 Scraper-Bot Toyota Ocasión  ->  Alertas de Telegram
================================================================================

Monitoriza el catálogo de vehículos de ocasión de Toyota España y notifica por
Telegram cada coche NUEVO que aparezca según tus filtros.

Arquitectura (dos motores intercambiables + fallback automático):

  1) MOTOR "api"  (POR DEFECTO, recomendado)
     Llama directamente al endpoint JSON interno que usa la propia web de Toyota:
        POST https://usc-webcomponents.toyota-europe.com/v1/api/usedcars/results/es/es?brand=toyota
     Devuelve los coches ya estructurados (modelo, precio, km, concesionario...).
     Ventajas frente al scraping de DOM con CSS:
        - No hay selectores CSS que se rompan cuando Toyota cambie el diseño.
        - No abre ningún navegador  ->  cero fugas de memoria, corre en un VPS minúsculo.
        - Mucho más rápido y estable.

  2) MOTOR "selenium"  (red de seguridad / requisito de pliego)
     Si algún día Toyota protegiera la API ante peticiones directas (403/429),
     este motor abre Chrome headless (vía webdriver-manager), navega a toyota.es
     para conseguir una sesión de navegador legítima (cookies/origin correctos) y
     relanza EXACTAMENTE la misma llamada a la API desde dentro de la página.
     Sigue obteniendo JSON limpio (no DOM frágil) y SIEMPRE cierra el navegador.

  MODO "auto" (por defecto): intenta "api" y, si falla, cae a "selenium".

--------------------------------------------------------------------------------
 CONFIGURACIÓN  (todo por variables de entorno / archivo .env)
--------------------------------------------------------------------------------
Crea un archivo .env junto a este script (ver .env.example). Variables:

    TELEGRAM_TOKEN     ->  Token del bot (de @BotFather)
    TELEGRAM_CHAT_ID   ->  Chat/grupo donde recibir las alertas
    TOYOTA_SEARCH_URL  ->  *** AQUÍ CAMBIAS LOS FILTROS ***  Pega la URL de
                           https://www.toyota.es/coches-segunda-mano con los
                           filtros aplicados (marca, modelo, precio, km, año...).
                           El bot la traduce automáticamente a la consulta de la API.
    SCRAPER_ENGINE     ->  auto | api | selenium      (por defecto: auto)
    POLL_INTERVAL      ->  segundos entre revisiones    (por defecto: 900 = 15 min)
    STATE_FILE         ->  ruta del JSON de memoria      (por defecto: coches_vistos.json)
    NOTIFY_ON_START    ->  true/false: avisar al arrancar (por defecto: true)
    TELEGRAM_PARSE_MODE->  HTML | MarkdownV2            (por defecto: HTML)

--------------------------------------------------------------------------------
 EJECUCIÓN
--------------------------------------------------------------------------------
    pip install -r requirements.txt

    # Bucle continuo (proceso/daemon):
    python3 bot_toyota.py

    # Una sola pasada (ideal para cron, p.ej.  */15 * * * *):
    python3 bot_toyota.py --once
================================================================================
"""

import os
import re
import sys
import json
import time
import html
import signal
import logging
import argparse
import unicodedata
import urllib.parse as urlparse
from datetime import datetime, timezone

import requests

# python-dotenv es opcional: si está instalado, cargamos el .env automáticamente.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover
    pass


# ==============================================================================
#  LOGGING
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        # Descomenta para guardar también en archivo:
        # logging.FileHandler("bot_toyota.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("toyota-bot")


# ==============================================================================
#  CONFIGURACIÓN
# ==============================================================================
class Config:
    """Lee toda la configuración desde variables de entorno (.env)."""

    # --- Credenciales de Telegram ---------------------------------------------
    # >>> INSERTA TUS CREDENCIALES EN EL ARCHIVO .env (NO las pongas aquí) <<<
    TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    PARSE_MODE       = os.getenv("TELEGRAM_PARSE_MODE", "HTML").strip()

    # --- Búsqueda --------------------------------------------------------------
    # *** Cambia los filtros editando esta URL en tu .env (TOYOTA_SEARCH_URL) ***
    SEARCH_URL = os.getenv(
        "TOYOTA_SEARCH_URL",
        "https://www.toyota.es/coches-segunda-mano"
        "?brands=38&model=CO,CR&price=cash:6900-27000&mileage=1-100000"
        "&year=2025-2026&seats=1-9&doors=4-4",
    ).strip()

    # --- Motor y cadencia ------------------------------------------------------
    ENGINE        = os.getenv("SCRAPER_ENGINE", "auto").strip().lower()   # auto|api|selenium
    POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "900"))                # segundos
    STATE_FILE    = os.getenv("STATE_FILE", "coches_vistos.json").strip()
    NOTIFY_ON_START = os.getenv("NOTIFY_ON_START", "true").strip().lower() in ("1", "true", "yes", "si", "sí")

    # --- Constantes de la API interna de Toyota Europe -------------------------
    API_BASE = "https://usc-webcomponents.toyota-europe.com/v1/api/usedcars/results/es/es?brand=toyota"
    SITE_BASE = "https://www.toyota.es"
    PAGE_SIZE = 30                          # coches por página de la API
    REQUEST_TIMEOUT = 30                    # segundos
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    @classmethod
    def validate(cls):
        missing = []
        if not cls.TELEGRAM_TOKEN:
            missing.append("TELEGRAM_TOKEN")
        if not cls.TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise SystemExit(
                "[CONFIG] Faltan variables obligatorias: " + ", ".join(missing) +
                ".\nCréalas en el archivo .env (mira .env.example)."
            )


# ==============================================================================
#  UTILIDADES
# ==============================================================================
def slugify(text: str) -> str:
    """'Automático' -> 'automatico'. Quita acentos y deja minúsculas con guiones."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text


def fmt_price(value) -> str:
    """25900 -> '25.900 €'  (formato español)."""
    try:
        n = int(round(float(value)))
        return f"{n:,}".replace(",", ".") + " €"
    except (TypeError, ValueError):
        return "Precio a consultar"


def fmt_km(value) -> str:
    try:
        n = int(round(float(value)))
        return f"{n:,}".replace(",", ".") + " km"
    except (TypeError, ValueError):
        return "—"


# ==============================================================================
#  TRADUCTOR  URL de toyota.es  ->  payload de la API
# ==============================================================================
def search_url_to_payload(url: str, offset: int = 0, page_size: int = Config.PAGE_SIZE) -> dict:
    """
    Convierte la URL pública de búsqueda (la que copias del navegador) en el
    cuerpo JSON que espera la API interna. Así, para cambiar los filtros solo
    tienes que actualizar TOYOTA_SEARCH_URL en el .env.

    Parámetros soportados en la URL:
        brands=38                  -> usedCarBrand   (lista de IDs)
        model=CO,CR                -> usedCarModel   (lista de IDs)
        price=cash:6900-27000      -> filtro cash    (min-max)
        mileage=1-100000           -> usedCarMileage (min-max)
        year=2025-2026             -> usedCarYear    (min-max)
        seats=1-9                  -> usedCarSeats   (min-max)
        doors=4-4                  -> usedCarDoors   (min-max)
        power=0-200                -> usedCarPowerOutput (min-max)
        fueltype=5,1               -> usedCarFuelType (lista de IDs)
        transmission=AT            -> usedCarTransmission (lista de IDs)
    """
    qs = urlparse.parse_qs(urlparse.urlparse(url).query)

    # Mapa: parámetro de la URL -> (filterId de la API, tipo)
    RANGE_FILTERS = {
        "mileage": "usedCarMileage",
        "year":    "usedCarYear",
        "seats":   "usedCarSeats",
        "doors":   "usedCarDoors",
        "power":   "usedCarPowerOutput",
    }
    LIST_FILTERS = {
        "brands":       "usedCarBrand",
        "model":        "usedCarModel",
        "fueltype":     "usedCarFuelType",
        "transmission": "usedCarTransmission",
    }

    filters = []

    def parse_range(raw):
        """'1-100000' -> (1, 100000). Soporta extremos abiertos."""
        m = re.match(r"^\s*(\d+)?\s*-\s*(\d+)?\s*$", raw)
        if not m:
            return None, None
        lo = int(m.group(1)) if m.group(1) else None
        hi = int(m.group(2)) if m.group(2) else None
        return lo, hi

    for param, filter_id in RANGE_FILTERS.items():
        if param in qs:
            lo, hi = parse_range(qs[param][0])
            filters.append({"filterId": filter_id, "min": lo, "max": hi})

    for param, filter_id in LIST_FILTERS.items():
        if param in qs:
            ids = [v for v in qs[param][0].split(",") if v]
            if ids:
                filters.append({"filterId": filter_id, "valueIds": ids})

    # Precio: viene como price=cash:6900-27000  o  price=monthly:150-400
    if "price" in qs:
        raw = qs["price"][0]
        if ":" in raw:
            kind, rng = raw.split(":", 1)
        else:
            kind, rng = "cash", raw
        lo, hi = parse_range(rng)
        filters.append({"filterId": kind.strip() or "cash", "min": lo, "max": hi})

    return {
        "uscEnv": "production",
        "filters": filters,
        "filterContext": "used",
        "offset": offset,
        "resultCount": page_size,
        "sortOrder": "published",
        "includeActiveFilterAggregations": False,
        "enableBiasedSort": False,
        "disabledFiltersIds": [],
        "enableExperimentalTotalCountQuery": False,
        "enableVehicleAggregations": False,
        "vehicleAggregationsVersionCode": "",
        "hasContentBlock": False,
        "enableDirectStockBiasedSort": False,
    }


# ==============================================================================
#  NORMALIZACIÓN DE UN COCHE  (JSON crudo de la API -> dict limpio)
# ==============================================================================
def normalize_car(raw: dict) -> dict:
    """Extrae los campos que nos interesan de la estructura de la API."""
    product = raw.get("product", {}) or {}
    model = (product.get("model", {}) or {}).get("description", "Toyota")
    version = product.get("versionName", "") or ""
    body = product.get("bodyType", "") or ""
    model_year = product.get("modelYear", "") or ""

    # Año de matriculación (suele ir en history.registrationDate)
    reg_date = (raw.get("history", {}) or {}).get("registrationDate", "") or ""
    reg_year = reg_date[:4] if reg_date else model_year

    engine = product.get("engine", {}) or {}
    fuel = (engine.get("marketingFuelType", {}) or {}).get("description", "") or ""

    transmission = ((product.get("transmission", {}) or {})
                    .get("transmissionType", {}) or {}).get("description", "") or ""

    # Gama/acabado (p.ej. "Style Plus"), preferimos la traducción es-ES.
    grade = ""
    grade_key = (product.get("grade", {}) or {}).get("gradeKey", {}) or {}
    for tr in grade_key.get("translations", []) or []:
        if tr.get("language") == "es-ES" and tr.get("description"):
            grade = tr["description"]; break
    if not grade:
        for tr in grade_key.get("translations", []) or []:
            if tr.get("description"):
                grade = tr["description"]; break

    price_val = (raw.get("price", {}) or {}).get("sellingPriceInclVAT")
    mileage_val = (raw.get("mileage", {}) or {}).get("value")

    dealer = raw.get("dealer", {}) or {}
    dealer_name = dealer.get("name", "") or ""
    dealer_city = (dealer.get("address", {}) or {}).get("city", "") or dealer.get("city", "") or ""

    images = raw.get("images") or []
    image_url = ""
    if images:
        u = images[0].get("url", "")
        image_url = ("https:" + u) if u.startswith("//") else u

    car_id = raw.get("id", "")

    # URL de la ficha pública. El UUID final es lo que resuelve la página, así
    # que aunque el "slug" cambie, el enlace seguirá funcionando.
    slug_parts = ["toyota", slugify(model), str(reg_year),
                  slugify(body), slugify(transmission), slugify(fuel)]
    slug = "-".join(p for p in slug_parts if p)
    detail_url = f"{Config.SITE_BASE}/coches-segunda-mano/ficha.{slug}-{car_id}"

    # Título limpio: "Corolla Style Plus". Evita repetir el modelo (la
    # versionName ya suele empezar por él) y cae a la versión si no hay gama.
    if grade:
        title = f"{model} {grade}".strip()
    elif version:
        title = version if version.lower().startswith(model.lower()) else f"{model} {version}".strip()
    else:
        title = f"{model} {model_year}".strip()

    return {
        "id": car_id,
        "title": title,
        "model": model,
        "version": version,
        "year": reg_year or model_year,
        "fuel": fuel,
        "transmission": transmission,
        "price": price_val,
        "price_str": fmt_price(price_val),
        "mileage_str": fmt_km(mileage_val),
        "dealer": dealer_name,
        "city": dealer_city,
        "image": image_url,
        "url": detail_url,
    }


# ==============================================================================
#  MOTORES DE EXTRACCIÓN
# ==============================================================================
class ScraperError(Exception):
    pass


class ToyotaScraper:
    """Obtiene la lista de coches mediante el motor configurado (con paginación)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ---------- MOTOR 1: API directa (requests) -------------------------------
    def _fetch_api(self):
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.cfg.USER_AGENT,
            "Origin": self.cfg.SITE_BASE,
            "Referer": self.cfg.SITE_BASE + "/",
        })
        cars, offset, total = [], 0, None
        while True:
            payload = search_url_to_payload(self.cfg.SEARCH_URL, offset=offset,
                                            page_size=self.cfg.PAGE_SIZE)
            resp = session.post(self.cfg.API_BASE, json=payload,
                                timeout=self.cfg.REQUEST_TIMEOUT)
            if resp.status_code != 200:
                raise ScraperError(f"API devolvió HTTP {resp.status_code}")
            data = resp.json()
            page = data.get("results", []) or []
            cars.extend(page)
            if total is None:
                total = data.get("totalResultCount", len(page))
            offset += self.cfg.PAGE_SIZE
            if offset >= (total or 0) or not page:
                break
        return cars

    # ---------- MOTOR 2: Selenium (fallback / requisito de pliego) -------------
    def _fetch_selenium(self):
        """
        Abre Chrome headless, navega a toyota.es para tener una sesión legítima de
        navegador y relanza la MISMA llamada a la API desde el contexto de la
        página (cookies + origin correctos). Garantiza driver.quit() en finally.
        """
        # Imports locales para no exigir selenium si usas solo el modo "api".
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")                 # necesario en muchos VPS Linux
        opts.add_argument("--disable-dev-shm-usage")      # evita /dev/shm pequeño en contenedores
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--ignore-certificate-errors")
        opts.add_argument(f"user-agent={self.cfg.USER_AGENT}")

        driver = None
        try:
            driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()), options=opts
            )
            driver.set_page_load_timeout(60)
            log.info("[selenium] Cargando toyota.es para obtener sesión de navegador...")
            driver.get(self.cfg.SITE_BASE + "/coches-segunda-mano")

            # WebDriverWait: esperamos a que el <body> esté presente (página lista).
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # Pop-up de cookies (OneTrust). Se ignora si no aparece o no bloquea.
            try:
                WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.ID, "onetrust-accept-btn-handler"))
                ).click()
                log.info("[selenium] Banner de cookies aceptado.")
            except Exception:
                pass  # sin cookies o no bloquea la lectura -> seguimos

            # Relanzamos la API desde dentro de la página con paginación.
            cars, offset, total = [], 0, None
            while True:
                payload = search_url_to_payload(self.cfg.SEARCH_URL, offset=offset,
                                                page_size=self.cfg.PAGE_SIZE)
                script = """
                    const cb = arguments[arguments.length - 1];
                    const [url, body] = [arguments[0], arguments[1]];
                    fetch(url, {method:'POST',
                                headers:{'Content-Type':'application/json','Accept':'application/json'},
                                body: JSON.stringify(body)})
                      .then(r => r.json()).then(j => cb(j))
                      .catch(e => cb({__error: String(e)}));
                """
                driver.set_script_timeout(self.cfg.REQUEST_TIMEOUT + 10)
                data = driver.execute_async_script(script, self.cfg.API_BASE, payload)
                if not isinstance(data, dict) or data.get("__error"):
                    raise ScraperError(f"fetch en página falló: {data}")
                page = data.get("results", []) or []
                cars.extend(page)
                if total is None:
                    total = data.get("totalResultCount", len(page))
                offset += self.cfg.PAGE_SIZE
                if offset >= (total or 0) or not page:
                    break
            return cars
        finally:
            # CRÍTICO: cerrar SIEMPRE el navegador para evitar fugas de memoria.
            if driver is not None:
                try:
                    driver.quit()
                    log.info("[selenium] Navegador cerrado.")
                except Exception:
                    pass

    # ---------- Orquestador del motor -----------------------------------------
    def get_cars(self):
        engine = self.cfg.ENGINE
        if engine == "api":
            return [normalize_car(c) for c in self._fetch_api()]
        if engine == "selenium":
            return [normalize_car(c) for c in self._fetch_selenium()]

        # auto: API primero; si falla, Selenium.
        try:
            return [normalize_car(c) for c in self._fetch_api()]
        except Exception as e:
            log.warning("[auto] Motor API falló (%s). Cambiando a Selenium...", e)
            return [normalize_car(c) for c in self._fetch_selenium()]


# ==============================================================================
#  PERSISTENCIA  (memoria de coches ya notificados)
# ==============================================================================
class StateStore:
    """Guarda en un JSON los IDs de coches ya enviados, para no duplicar avisos."""

    def __init__(self, path: str):
        self.path = path
        self.seen = self._load()

    def _load(self) -> set:
        if not os.path.exists(self.path):
            return set()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("seen_ids", []))
        except Exception as e:
            log.warning("No se pudo leer %s (%s). Empiezo de cero.", self.path, e)
            return set()

    def is_new(self, car_id: str) -> bool:
        return bool(car_id) and car_id not in self.seen

    def add(self, car_id: str):
        """Añade y persiste INMEDIATAMENTE (evita duplicados si el bot se reinicia)."""
        if not car_id:
            return
        self.seen.add(car_id)
        self._save()

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"updated_at": datetime.now(timezone.utc).isoformat(),
                     "seen_ids": sorted(self.seen)},
                    f, ensure_ascii=False, indent=2,
                )
            os.replace(tmp, self.path)   # escritura atómica
        except Exception as e:
            log.error("No se pudo guardar el estado en %s: %s", self.path, e)


# ==============================================================================
#  NOTIFICADOR DE TELEGRAM
# ==============================================================================
class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, parse_mode: str = "HTML"):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.parse_mode = parse_mode

    def _post(self, text: str, disable_preview: bool = False) -> bool:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": self.parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        for intento in range(3):
            try:
                r = requests.post(self.url, data=payload, timeout=20)
                if r.status_code == 200:
                    return True
                if r.status_code == 429:  # Too Many Requests -> respetar retry_after
                    retry = r.json().get("parameters", {}).get("retry_after", 5)
                    log.warning("Telegram 429: esperando %ss...", retry)
                    time.sleep(retry + 1)
                    continue
                log.error("Telegram HTTP %s: %s", r.status_code, r.text[:200])
            except requests.RequestException as e:
                log.error("Error de red enviando a Telegram: %s", e)
            time.sleep(2 * (intento + 1))
        return False

    def send_text(self, text: str):
        return self._post(text, disable_preview=True)

    def send_car(self, car: dict) -> bool:
        """Mensaje formateado en HTML, con negritas y emojis."""
        e = html.escape  # escapamos los campos dinámicos
        lines = [
            "🚗 <b>¡Nuevo Toyota de ocasión!</b>",
            "",
            f"🔹 <b>{e(car['title'])}</b>",
            f"💰 Precio: <b>{e(car['price_str'])}</b>",
            f"📅 Año: {e(str(car['year']))}    📏 {e(car['mileage_str'])}",
        ]
        extra = " · ".join(p for p in [car.get("fuel"), car.get("transmission")] if p)
        if extra:
            lines.append(f"⚙️ {e(extra)}")
        if car.get("dealer"):
            ubic = f" ({e(car['city'])})" if car.get("city") else ""
            lines.append(f"📍 {e(car['dealer'])}{ubic}")
        lines.append("")
        lines.append(f'🔗 <a href="{e(car["url"])}">Ver ficha del coche</a>')
        text = "\n".join(lines)
        return self._post(text, disable_preview=False)


# ==============================================================================
#  LÓGICA PRINCIPAL
# ==============================================================================
def run_once(cfg: Config, scraper: ToyotaScraper, store: StateStore,
             notifier: TelegramNotifier) -> int:
    """Una pasada: busca, detecta nuevos, notifica. Devuelve nº de nuevos."""
    log.info("Consultando catálogo (motor=%s)...", cfg.ENGINE)
    cars = scraper.get_cars()
    log.info("Coches recibidos: %d", len(cars))

    nuevos = 0
    for car in cars:
        if store.is_new(car["id"]):
            if notifier.send_car(car):
                store.add(car["id"])   # guardamos solo si el aviso se envió
                nuevos += 1
                log.info("ALERTA enviada: %s · %s", car["title"], car["price_str"])
                time.sleep(1)          # cortesía anti rate-limit de Telegram
            else:
                log.error("No se pudo notificar %s; se reintentará en la próxima vuelta.",
                          car["id"])
    if nuevos == 0:
        log.info("Sin coches nuevos en esta revisión.")
    return nuevos


def main():
    parser = argparse.ArgumentParser(description="Scraper-Bot Toyota Ocasión -> Telegram")
    parser.add_argument("--once", action="store_true",
                        help="Ejecuta una sola pasada y termina (para cron).")
    args = parser.parse_args()

    cfg = Config()
    cfg.validate()

    scraper = ToyotaScraper(cfg)
    store = StateStore(cfg.STATE_FILE)
    notifier = TelegramNotifier(cfg.TELEGRAM_TOKEN, cfg.TELEGRAM_CHAT_ID, cfg.PARSE_MODE)

    log.info("Rastreador iniciado | motor=%s | intervalo=%ss | estado=%s | ya vistos=%d",
             cfg.ENGINE, cfg.POLL_INTERVAL, cfg.STATE_FILE, len(store.seen))

    # --- Modo cron: una pasada y salimos --------------------------------------
    if args.once:
        try:
            run_once(cfg, scraper, store, notifier)
        except Exception as e:
            log.exception("Fallo en la pasada única: %s", e)
            sys.exit(1)
        return

    # --- Modo bucle continuo --------------------------------------------------
    if cfg.NOTIFY_ON_START:
        notifier.send_text("🤖 Rastreador de Toyota Ocasión iniciado con tus filtros.")

    # Apagado limpio con Ctrl+C / SIGTERM
    detener = {"flag": False}
    def _stop(signum, frame):
        log.info("Señal recibida (%s). Cerrando tras la pasada actual...", signum)
        detener["flag"] = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while not detener["flag"]:
        try:
            run_once(cfg, scraper, store, notifier)
        except Exception as e:
            # Resiliencia: cualquier error (web caída, cambio de API, red...) se
            # registra y se reintenta en la siguiente iteración. El bot NO cae.
            log.exception("Error en la iteración (se reintentará): %s", e)

        if detener["flag"]:
            break
        log.info("Esperando %d s hasta la próxima revisión...", cfg.POLL_INTERVAL)
        # Dormimos en tramos cortos para responder rápido a la señal de parada.
        slept = 0
        while slept < cfg.POLL_INTERVAL and not detener["flag"]:
            time.sleep(min(5, cfg.POLL_INTERVAL - slept))
            slept += 5

    log.info("Rastreador detenido. Hasta la próxima.")


if __name__ == "__main__":
    main()
