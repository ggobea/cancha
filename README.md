# Monitor de pádel ATC 🎾

Este proyecto revisa **solo el jueves relevante** y manda un **mail solo si aparece disponibilidad nueva** entre **18:00 y 19:30**.

- si hoy es jueves, revisa **hoy**
- si hoy no es jueves, revisa **el próximo jueves**

## Lo más barato / gratis
La opción más simple para que sea **gratis** es usar **GitHub Actions en un repositorio público**. GitHub indica que Actions es gratis para repos públicos en runners estándar. En repos privados hay una cuota mensual de minutos. Además, los workflows programados se pueden correr con `schedule` usando cron.

## Cómo funciona el estado
El script guarda el histórico en:

```text
.github/padel_state.json
```

Ese archivo se actualiza en cada corrida. El workflow lo vuelve a commitear al repo para que el estado persista entre ejecuciones.

## Frecuencia recomendada
Te dejé el workflow cada **2 horas** para mantenerlo bien liviano. Si después querés, podés cambiarlo a:
- cada 1 hora: `17 * * * *`
- cada 30 min: `*/30 * * * *`

## Archivos
- `padel_alert.py`: script principal
- `.github/workflows/padel_alert.yml`: workflow programado
- `.github/padel_state.json`: estado persistente
- `.env.example`: ejemplo para correrlo local
- `requirements.txt`: dependencias

## Paso a paso en GitHub

### 1) Crear repo público
Creá un repo **público** y subí estos archivos.

### 2) Agregar secrets
En GitHub: **Settings → Secrets and variables → Actions → New repository secret**

Agregá:
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `EMAIL_FROM`
- `EMAIL_TO`

### 3) Activar workflow
El archivo ya queda en:

```text
.github/workflows/padel_alert.yml
```

GitHub lo toma automáticamente.

### 4) Estado inicial
Subí también este archivo vacío:

```json
{
  "sent_keys": [],
  "last_results": []
}
```

### 5) Probarlo manualmente
En la pestaña **Actions**, corré `Padel alert` con **Run workflow**.

## Gmail
Si usás Gmail con SMTP, Google requiere una **contraseña de aplicación** con **verificación en dos pasos** activada.

## Ejecutarlo localmente
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python padel_alert.py --once
```

## Importante
- **No manda nada** si no encuentra disponibilidad.
- **No repite** avisos de resultados ya notificados.
- Solo revisa **un solo jueves** por corrida.
- El parseo se apoya en la página pública actual de ATC y tolera filas con imágenes intercaladas.
