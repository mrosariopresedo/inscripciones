# Monitor de vacantes UADE

Revisa cada ~15 min si se libera cupo en materias puntuales del portal de
inscripciones de UADE y avisa por WhatsApp (CallMeBot). Corre solo en GitHub
Actions, sin necesidad de tener la computadora prendida.

## Cómo funciona

`vacantes.py` (headless Chrome + Selenium):
1. Entra a la app con **HTTP Basic Auth** (usuario/clave UADE en la cabecera).
2. Abre la pantalla de buscar clases usando una URL con `?param=...` **genuina**
   (identifica a la alumna; va firmada, no se puede fabricar).
3. Selecciona las materias objetivo, busca en los turnos configurados y lee las
   vacantes de la grilla.
4. Avisa por WhatsApp las clases con cupo > 0 (por defecto ignora los intensivos
   de Pinamar; ver `EXCLUIR_INTENSIVOS`).

El workflow `.github/workflows/vacantes.yml` lo dispara por cron cada 15 min (o a
mano desde la pestaña **Actions**).

## Configuración (GitHub Secrets)

En el repo: **Settings → Secrets and variables → Actions → New repository secret**.
Cargar estos 5:

| Secret | Qué es |
|---|---|
| `UADE_USER` | usuario UADE (mail) |
| `UADE_PASS` | contraseña UADE |
| `PARAM_URL` | URL completa con `?param=...` del período a vigilar (copiada de la barra de direcciones ya logueada) |
| `WHATSAPP_TELEFONO` | teléfono CallMeBot (ej. 549XXXXXXXXXX) |
| `WHATSAPP_APIKEY` | apikey de CallMeBot |

El `PARAM_URL` apunta a un año/cuatrimestre fijo. Si cambia el período o deja de
funcionar, hay que copiar uno nuevo y actualizar el secret.

## Correr local (para probar)

Copiar `run_local.ps1.ejemplo` a `run_local.ps1`, completar los datos y ejecutarlo.
`run_local.ps1` está en `.gitignore` para no subir secretos.

## Ajustes en `vacantes.py`

- `MATERIAS_OBJETIVO`: las materias a vigilar (por código).
- `TURNOS`: turnos a recorrer (NOCHE / ONLINE / etc.).
- `EXCLUIR_INTENSIVOS`: `True` ignora los intensivos de Pinamar.
