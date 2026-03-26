#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import schedule
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

ATC_BASE_URL = "https://atcsports.io/results"
LOCAL_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@dataclass(frozen=True)
class ClubAvailability:
    date: str
    club: str
    address: str
    price_from: str | None
    matched_times: tuple[str, ...]

    @property
    def dedupe_key(self) -> str:
        raw = f"{self.date}|{self.club}|{self.address}|{','.join(self.matched_times)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def getenv_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Falta la variable de entorno requerida: {name}")
    return value


def get_state_path() -> Path:
    raw = os.getenv("STATE_PATH", "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).with_name("alert_state.json")


def local_today() -> dt.date:
    return dt.datetime.now(LOCAL_TZ).date()


def target_search_date(start_date: dt.date | None = None) -> dt.date:
    start = start_date or local_today()
    days_until_thursday = (3 - start.weekday()) % 7
    return start + dt.timedelta(days=days_until_thursday)


def build_url(date_: dt.date, place_id: str, location_name: str, sport_id: str, horario: str = "19:30") -> str:
    params = {
        "dia": date_.isoformat(),
        "horario": horario,
        "locationName": location_name,
        "placeId": place_id,
        "tipoDeporte": sport_id,
    }
    return f"{ATC_BASE_URL}?{urlencode(params)}"


def fetch_rendered_html(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = browser.new_context(
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(45000)

        # networkidle no conviene acá: Playwright lo desaconseja para páginas
        # con requests largos / persistentes porque puede no resolverse nunca.
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # Espera liviana para que hidraten los resultados.
        page.wait_for_timeout(5000)

        # Si el texto todavía no apareció, probamos una espera adicional
        # basada en contenido visible en vez de networkidle.
        body_text = page.locator("body").inner_text(timeout=10000)
        if "clubes encontrados" not in body_text.lower():
            page.wait_for_timeout(7000)

        html = page.content()
        browser.close()
        return html


def html_to_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    return [line.strip() for line in text.splitlines() if line.strip()]


def extract_clubs(lines: list[str], target_date: str, target_times: set[str]) -> list[ClubAvailability]:
    clubs: list[ClubAvailability] = []

    start_index = 0
    for i, line in enumerate(lines):
        if "clubes encontrados" in line.lower():
            start_index = i + 1
            break

    time_token_re = re.compile(r"(?:^|\s)((?:[01]\d|2[0-3]):[0-5]\d)(?=\s|$)")
    price_re = re.compile(r"^desde\$\s*([\d\.,]+)$", re.IGNORECASE)

    def is_noise(text: str) -> bool:
        lowered = text.lower().strip()
        return (
            not lowered
            or lowered in {"buscar", "ordenar", "superficie", "duracion", "mostrar el mapa"}
            or "clubes encontrados" in lowered
            or lowered.startswith("image:")
            or lowered.startswith("imagen de la cancha")
            or lowered.startswith("beelup disponible")
            or text.startswith("【")
        )

    i = start_index
    while i < len(lines):
        price_match = price_re.match(lines[i].strip())
        if not price_match:
            i += 1
            continue

        price = price_match.group(1)
        collected: list[str] = []
        j = i + 1

        while j < len(lines):
            candidate = lines[j].strip()
            if price_re.match(candidate):
                break
            if not is_noise(candidate):
                collected.append(candidate)
            j += 1

        if len(collected) >= 3:
            club = collected[0].replace("####", "").strip()
            address = collected[1].strip()

            availability_line = None
            for item in collected[2:]:
                lowered = item.lower()
                if "el complejo no cumple con los filtros seleccionados" in lowered:
                    availability_line = item
                    break
                if time_token_re.search(item):
                    availability_line = item
                    break

            if availability_line:
                found_times = tuple(m.group(1) for m in time_token_re.finditer(availability_line))
                matched = tuple(t for t in found_times if t in target_times)
                if matched:
                    clubs.append(
                        ClubAvailability(
                            date=target_date,
                            club=club,
                            address=address,
                            price_from=price,
                            matched_times=matched,
                        )
                    )

        i = max(j, i + 1)

    return clubs


def load_state() -> dict:
    state_path = get_state_path()
    if not state_path.exists():
        return {"sent_keys": [], "last_results": []}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {"sent_keys": [], "last_results": []}


def save_state(state: dict) -> None:
    state_path = get_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def filter_new_results(results: list[ClubAvailability], state: dict) -> list[ClubAvailability]:
    sent_keys = set(state.get("sent_keys", []))
    return [r for r in results if r.dedupe_key not in sent_keys]


def update_state(results: list[ClubAvailability], state: dict, mark_as_sent: bool) -> dict:
    sent_keys = set(state.get("sent_keys", []))
    if mark_as_sent:
        for result in results:
            sent_keys.add(result.dedupe_key)

    new_state = {
        "sent_keys": sorted(sent_keys),
        "last_results": [asdict(r) for r in results],
        "updated_at_utc": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }
    save_state(new_state)
    return new_state


def render_email(results: list[ClubAvailability], location_name: str) -> tuple[str, str]:
    grouped: dict[str, list[ClubAvailability]] = {}
    for result in results:
        grouped.setdefault(result.date, []).append(result)

    subject = f"🎾 Hay canchas de pádel para tu jueves en {location_name}"
    lines = [
        "Encontré disponibilidad en ATC Sports para los horarios que pediste.",
        "",
    ]

    for date_, items in sorted(grouped.items()):
        lines.append(f"Jueves {date_}:")
        for item in items:
            times = ", ".join(item.matched_times)
            price_text = f" | desde ${item.price_from}" if item.price_from else ""
            url = build_url(
                dt.date.fromisoformat(item.date),
                os.environ["ATC_PLACE_ID"],
                location_name,
                os.environ["ATC_SPORT_ID"],
                "19:30",
            )
            lines.append(f"- {item.club} | {item.address} | horarios: {times}{price_text}")
            lines.append(f"  {url}")
        lines.append("")

    body = "\n".join(lines).strip() + "\n"
    return subject, body


def send_email(subject: str, body: str) -> None:
    host = getenv_required("SMTP_HOST")
    port = int(getenv_required("SMTP_PORT"))
    username = getenv_required("SMTP_USERNAME")
    password = getenv_required("SMTP_PASSWORD")
    email_from = getenv_required("EMAIL_FROM")
    email_to = getenv_required("EMAIL_TO")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(msg)


def check_availability() -> list[ClubAvailability]:
    place_id = getenv_required("ATC_PLACE_ID")
    location_name = getenv_required("ATC_LOCATION_NAME")
    sport_id = getenv_required("ATC_SPORT_ID")
    target_times = {
        token.strip()
        for token in getenv_required("ATC_TARGET_TIMES").split(",")
        if token.strip()
    }

    date_ = target_search_date()
    url = build_url(date_, place_id, location_name, sport_id, "19:30")
    logging.info("Revisando %s", url)
    html = fetch_rendered_html(url)
    lines = html_to_lines(html)
    results = extract_clubs(lines, date_.isoformat(), target_times)

    logging.info("Resultados parseados: %s", len(results))
    for item in results[:10]:
        logging.info("Match: %s | %s | %s", item.club, item.address, ", ".join(item.matched_times))

    unique: dict[str, ClubAvailability] = {}
    for item in results:
        unique[item.dedupe_key] = item
    return sorted(unique.values(), key=lambda x: (x.date, x.club, x.address))


def run_once() -> int:
    location_name = getenv_required("ATC_LOCATION_NAME")
    state = load_state()
    try:
        results = check_availability()
    except Exception as exc:
        logging.exception("Error al consultar ATC: %s", exc)
        return 2

    if not results:
        update_state([], state, mark_as_sent=False)
        logging.info("No encontré disponibilidad en los horarios objetivo. No se envía email.")
        return 0

    new_results = filter_new_results(results, state)
    if not new_results:
        update_state(results, state, mark_as_sent=False)
        logging.info("Hay resultados, pero ya habían sido notificados. No se envía email.")
        return 0

    subject, body = render_email(new_results, location_name)
    send_email(subject, body)
    update_state(new_results, state, mark_as_sent=True)
    logging.info("Email enviado con %s resultados nuevos.", len(new_results))
    return 0


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Alerta de canchas de pádel en ATC Sports")
    parser.add_argument("--once", action="store_true", help="ejecuta una sola vez y sale")
    parser.add_argument(
        "--every-minutes",
        type=int,
        default=30,
        help="frecuencia de chequeo en minutos cuando queda corriendo (default: 30)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stdout,
    )

    if args.once:
        return run_once()

    logging.info("Monitor iniciado. Frecuencia: cada %s minutos.", args.every_minutes)
    schedule.every(args.every_minutes).minutes.do(run_once)
    run_once()

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
