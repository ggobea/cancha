# Monitor de pádel ATC 🎾

Esta versión parsea el JSON embebido en `__NEXT_DATA__`, que es mucho más estable que intentar leer el HTML visual renderizado.

## Qué revisa
- Solo revisa **un solo jueves** por corrida:
  - si hoy es jueves, revisa hoy
  - si hoy no es jueves, revisa el próximo jueves
- Busca horarios entre **18:00 y 19:30**
- Manda mail **solo si encuentra disponibilidad nueva**
- No manda nada si no encuentra
- Guarda estado en `.github/padel_state.json`

## Archivos
- `padel_alert.py`
- `.github/workflows/padel_alert.yml`
- `.github/padel_state.json`
- `requirements.txt`

## Cómo funciona
La página de ATC incluye un bloque JSON en:

```html
<script id="__NEXT_DATA__" type="application/json">...</script>
```

El script lo extrae y lee:

```text
props.pageProps.bookingsBySport[*].available_slots
```

Ahí están los horarios reales y los precios.

## GitHub Actions
El workflow corre cada 2 horas en un repo público y persiste el estado haciendo commit del JSON.

## Variables esperadas
```text
ATC_PLACE_ID=69y6bfcuh
ATC_LOCATION_NAME=Ituzaingó, Provincia de Buenos Aires, Argentina
ATC_SPORT_ID=7
ATC_TARGET_TIMES=18:00,18:30,19:00,19:30
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=tu_mail@gmail.com
SMTP_PASSWORD=tu_app_password
EMAIL_FROM=tu_mail@gmail.com
EMAIL_TO=destino@gmail.com
```
