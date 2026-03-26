#!/usr/bin/env python3
"""
Monitor de canchas de pádel en ATC Sports.

Qué hace:
- Revisa todos los próximos jueves dentro del horizonte configurado.
- Busca disponibilidad entre 18:00 y 19:30.
- Si encuentra alguna cancha/club con esos horarios, manda un email.
- Guarda un estado local para no repetir la misma alerta una y otra vez.

Uso rápido:
  1) Copiá .env.example a .env y completá tus datos.
  2) pip install -r requirements.txt
  3) python padel_alert.py --once
  4) Para dejarlo corriendo: python padel_alert.py

También puede usarse en GitHub Actions con un archivo de estado versionado:
  STATE_PATH=.github/padel_state.json

Variables esperadas en .env:
  ATC_PLACE_ID=69y6bfcuh
  ATC_LOCATION_NAME=Ituzaingó, Provincia de Buenos Aires, Argentina
  ATC_SPORT_ID=7
  ATC_TARGET_TIMES=18:00,18:30,19:00,19:30
  ATC_LOOKAHEAD_WEEKS=12

  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USERNAME=tu_mail@gmail.com
  SMTP_PASSWORD=tu_app_password
  EMAIL_FROM=tu_mail@gmail.com
  EMAIL_TO=destino@gmail.com
"""

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
from typing import Iterable

import requests
import schedule
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ATC_BASE_URL = "https://atcsports.io/results"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    )
}


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


def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def get_state_path() -> Path:
    raw = os.getenv("STATE_PATH", "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).with_name("alert_state.json")


def next_thursdays(count: int, start_date: dt.date | None = None) -> list[dt.date]:
    start = start_date or dt.date.today()
    days_until_thursday = (3 - start.weekday()) % 7
    first = start + dt.timedelta(days=days_until_thursday)
    return [first + dt.timedelta(weeks=i) for i in range(count)]


def build_url(date_: dt.date, place_id: str, location_name: str, sport_id: str, horario: str = "19:30") -> str:
    from urllib.parse import urlencode

    params = {
        "dia": date_.isoformat(),
        "horario": horario,
        "locationName": location_name,
        "placeId": place_id,
        "tipoDeporte": sport_id,
    }
    return f"{ATC_BASE_URL}?{urlencode(params)}"


def fetch_text(session: requests.Session, url: str) -> str:
    response = session.get(url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


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

    date_re = re.compile(r"^\d{2}/\d{2}$")
    time_token_re = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    price_re = re.compile(r"^desde\$\s*([\d\.\,]+)$", re.IGNORECASE)

    i = start_index
    while i < len(lines):
        line = lines[i]
        price_match = price_re.match(line)
        if not price_match:
            i += 1
            continue

        price = price_match.group(1)
        if i + 3 >= len(lines):
            break

        club = lines[i + 1]
        address = lines[i + 2]
        times_line = lines[i + 3]

        if date_re.match(club) or club.lower().startswith("desde$"):
            i += 1
            continue

        found_times = tuple(tok for tok in times_line.split() if time_token_re.match(tok))
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
        i += 4

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


def update_state_with_sent(results: list[ClubAvailability], state: dict) -> dict:
    sent_keys = set(state.get("sent_keys", []))
    for result in results:
        sent_keys.add(result.dedupe_key)
    new_state = {
        "sent_keys": sorted(sent_keys),
        "last_results": [asdict(r) for r in results],
        "updated_at_utc": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }
    save_state(new_state)
    return new_state


def update_state_without_alert(results: list[ClubAvailability], state: dict) -> dict:
    new_state = {
        "sent_keys": sorted(set(state.get("sent_keys", []))),
        "last_results": [asdict(r) for r in results],
        "updated_at_utc": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }
    save_state(new_state)
    return new_state


def render_email(results: list[ClubAvailability], location_name: str) -> tuple[str, str]:
    grouped: dict[str, list[ClubAvailability]] = {}
    for result in results:
        grouped.setdefault(result.date, []).append(result)

    subject = f"🎾 Hay canchas de pádel para tus jueves en {location_name}"
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
    lookahead_weeks = getenv_int("ATC_LOOKAHEAD_WEEKS", 12)

    session = requests.Session()
    all_results: list[ClubAvailability] = []

    for date_ in next_thursdays(lookahead_weeks):
        url = build_url(date_, place_id, location_name, sport_id, "19:30")
        logging.info("Revisando %s", url)
        html = fetch_text(session, url)
        lines = html_to_lines(html)
        results = extract_clubs(lines, date_.isoformat(), target_times)
        all_results.extend(results)

    unique: dict[str, ClubAvailability] = {}
    for item in all_results:
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
        update_state_without_alert([], state)
        logging.info("No encontré disponibilidad en los horarios objetivo. No se envía email.")
        return 0

    new_results = filter_new_results(results, state)
    if not new_results:
        update_state_without_alert(results, state)
        logging.info("Hay resultados, pero ya habían sido notificados. No se envía email.")
        return 0

    subject, body = render_email(new_results, location_name)
    send_email(subject, body)
    update_state_with_sent(new_results, state)
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
