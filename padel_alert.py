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

import requests
import schedule
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ATC_BASE_URL = "https://atcsports.io/results"
LOCAL_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@dataclass(frozen=True)
class ClubAvailability:
    date: str
    club: str
    address: str
    price_from: float | None
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
    return Path(raw) if raw else Path(__file__).with_name("alert_state.json")


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


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    response = requests.get(url, headers=headers, timeout=45)
    response.raise_for_status()
    return response.text


def extract_next_data_json(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        raise RuntimeError("No encontré el script __NEXT_DATA__ en la página de ATC.")
    raw_json = script.string or script.get_text(strip=True)
    if not raw_json:
        raise RuntimeError("El script __NEXT_DATA__ está vacío.")
    return json.loads(raw_json)


def parse_atc_next_data(data: dict, target_times: set[str]) -> list[ClubAvailability]:
    bookings = (
        data.get("props", {})
        .get("pageProps", {})
        .get("bookingsBySport", [])
    )

    results: list[ClubAvailability] = []
    seen_keys: set[str] = set()

    for club in bookings:
        club_name = (club.get("name") or "").strip()
        address = (
            club.get("location", {}).get("name")
            or club.get("address")
            or ""
        ).strip()
        slots = club.get("available_slots") or []
        date_value = None
        matched_times: set[str] = set()
        min_price: float | None = None

        for slot in slots:
            start = slot.get("start") or ""
            match = re.search(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", start)
            if not match:
                continue

            slot_date, hhmm = match.groups()
            if hhmm not in target_times:
                continue

            date_value = slot_date
            matched_times.add(hhmm)

            cents = ((slot.get("price") or {}).get("cents"))
            if isinstance(cents, int):
                ars = cents / 100.0
                if min_price is None or ars < min_price:
                    min_price = ars

        if matched_times:
            result = ClubAvailability(
                date=date_value or "",
                club=club_name,
                address=address,
                price_from=min_price,
                matched_times=tuple(sorted(matched_times)),
            )
            if result.dedupe_key not in seen_keys:
                seen_keys.add(result.dedupe_key)
                results.append(result)

    return sorted(results, key=lambda x: (x.date, x.club, x.address))


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


def update_state(results: list[ClubAvailability], state: dict, mark_as_sent: bool) -> None:
    sent_keys = set(state.get("sent_keys", []))
    if mark_as_sent:
        for result in results:
            sent_keys.add(result.dedupe_key)

    save_state(
        {
            "sent_keys": sorted(sent_keys),
            "last_results": [asdict(r) for r in results],
            "updated_at_utc": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        }
    )


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
            price_text = f" | desde ${item.price_from:,.0f}".replace(",", ".") if item.price_from is not None else ""
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

    html = fetch_html(url)
    data = extract_next_data_json(html)
    results = parse_atc_next_data(data, target_times)

    logging.info("Resultados parseados: %s", len(results))
    for item in results[:10]:
        logging.info(
            "Match: %s | %s | %s",
            item.club,
            item.address,
            ", ".join(item.matched_times),
        )

    return results


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
