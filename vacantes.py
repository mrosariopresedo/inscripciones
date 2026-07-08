"""
Monitor de vacantes UADE — version para GitHub Actions (headless, sin PC prendida).

=== COMO FUNCIONA (descubierto y VALIDADO probando el portal real) ===
- La app (inscripcionespia.uade.edu.ar) esta detras de HTTP Basic Auth: se manda
  usuario/clave en la cabecera Authorization. NO hacen falta cookies ni el SSO de
  Microsoft. (WWW-Authenticate: Basic realm="uade.edu.ar")
- Hay un WAF (F5). Para abrir el popup de materias la app necesita saber QUE
  ALUMNA sos, y eso viaja en el 'param' de la URL (base64 firmado con tu
  paramAlumId). Sin un param valido, el WAF rechaza ("Request Rejected"). El param
  no se puede fabricar (esta firmado): hay que usar uno GENUINO copiado de tu
  sesion logueada, del anio/cuatrimestre que quieras vigilar.
- Al buscar, un overlay "Cargando" (divModal) tapa la pagina: hay que esperar a
  que se oculte y clickear Buscar por JS, si no el clic se intercepta.

Validado el 2026-07-06: detecta bien las vacantes (los 3 intensivos daban 20/16/18).

Modo SOLO AVISO: detecta cupo y manda WhatsApp. No toca el carrito ni inscribe.

El script corre su PROPIO loop interno (un barrido cada ~60s por ~5,5 h) reusando
UN solo Chrome. Antes se lanzaba un Chrome por barrido y, cada minuto por horas,
se acumulaban procesos zombie hasta colgar el runner (timeouts en cadena). Con un
navegador unico eso se evita; solo se recrea si se rompe. El workflow lo lanza y
lo relanza cuando termina.
"""
import os
import re
import sys
import base64
from time import sleep, monotonic

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ------------------------------------------------------------------
# CONFIGURACION (de secrets en Actions; con fallback para probar local)
# ------------------------------------------------------------------
# TODO lo sensible viene de variables de entorno / GitHub Secrets. NO hardcodear
# credenciales ni el param aca: este archivo se sube a GitHub.
USUARIO_UADE  = os.environ.get('UADE_USER', '')
PASSWORD_UADE = os.environ.get('UADE_PASS', '')

WHATSAPP_TELEFONO = os.environ.get('WHATSAPP_TELEFONO', '')
WHATSAPP_APIKEY   = os.environ.get('WHATSAPP_APIKEY', '')

# URL COMPLETA con ?param=... GENUINA del periodo a vigilar (2do cuat 2026).
# Se copia de la barra de direcciones estando logueada en la pantalla de buscar
# clases. Es durable pero apunta a un anio/cuatrimestre fijo.
PARAM_URL = os.environ.get('PARAM_URL', '')

# Loop interno: cuanto dura la corrida y cada cuanto barre. ~2 min es el punto
# justo: rapido para agarrar un cupo, gentil con el portal y el runner.
LOOP_SECONDS = int(os.environ.get('LOOP_SECONDS', '19800'))   # ~5,5 h
INTERVALO = int(os.environ.get('INTERVALO_SEG', '120'))       # segundos entre barridos

# Turnos a recorrer (valores del combo cboTurno).
TURNOS = {
    '10154': 'NOCHE',
    '10156': 'ONLINE',
}

# Las 6 materias que te quedan (por codigo).
MATERIAS_OBJETIVO = {
    '3.4.221': 'Negocios Tecnológicos',
    '3.1.056': 'Estadística Avanzada',
    '1.1.093': 'Liderazgo y Negociación',
    '1.4.076': 'Fundamentos de Economía',
    '3.4.141': 'Seguridad de Procesos y Aplicaciones',
    '3.4.217': 'Ciencia de Datos',
}

# Excluir clases que contengan estas palabras en su detalle (sede/modalidad).
# Los intensivos de Pinamar/Costa Argentina no te interesan.
EXCLUIR_INTENSIVOS = True
EXCLUIR_KEYWORDS = ('INTENSIVO', 'PINAMAR', 'COSTA ARGENTINA', 'NAMAR')

# El monitor SOLO avisa: detecta el cupo y manda WhatsApp. No toca el carrito ni
# se inscribe (eso lo hacas vos a mano en el portal).

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36')


class Bloqueo(Exception):
    """El portal rechazo el acceso (WAF / 401 / 403 / param vencido)."""


# Senales de que el portal nos bloqueo o rechazo (case-insensitive).
BLOQUEO_SIGNALS = (
    'request rejected', 'requested url was rejected', 'access denied',
    '403 - forbidden', '403 forbidden', '401 - unauthorized', '401 unauthorized',
)


def es_bloqueo(html):
    h = html.lower()
    return any(s in h for s in BLOQUEO_SIGNALS)


def send_msg(message):
    """Manda un WhatsApp via CallMeBot. requests encodea el texto solo."""
    try:
        requests.get(
            'https://api.callmebot.com/whatsapp.php',
            params={'phone': WHATSAPP_TELEFONO, 'text': message, 'apikey': WHATSAPP_APIKEY},
            timeout=30,
        )
    except Exception as e:
        print(f"No se pudo mandar el WhatsApp: {e}")


def armar_browser():
    o = webdriver.ChromeOptions()
    for a in ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
              '--window-size=1920,1080', f'--user-agent={UA}',
              '--disable-blink-features=AutomationControlled']:
        o.add_argument(a)
    o.add_experimental_option('excludeSwitches', ['enable-automation'])
    browser = webdriver.Chrome(options=o)
    b64 = base64.b64encode(f"{USUARIO_UADE}:{PASSWORD_UADE}".encode()).decode()
    browser.execute_cdp_cmd('Network.enable', {})
    browser.execute_cdp_cmd('Network.setExtraHTTPHeaders',
                            {'headers': {'Authorization': f'Basic {b64}'}})
    return browser


def esperar_overlay(browser):
    """Espera a que el overlay 'Cargando' (divModal) se oculte."""
    try:
        WebDriverWait(browser, 20).until(
            EC.invisibility_of_element_located((By.ID, 'divModal')))
    except Exception:
        pass


def seleccionar_materias_objetivo(browser):
    """Abre el popup y tilda los checkboxes de las 6 materias objetivo."""
    wait = WebDriverWait(browser, 20)
    wait.until(EC.element_to_be_clickable(
        (By.ID, 'ContentPlaceHolder1_btnSeleccionarMaterias'))).click()
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'table.gridViewMaterias')))
    sleep(2)
    seleccionadas = 0
    for row in browser.find_elements(By.CSS_SELECTOR, 'table.gridViewMaterias tr'):
        try:
            cod = row.find_element(By.CSS_SELECTOR, 'td.colCodigo').text.strip()
        except Exception:
            continue
        if cod not in MATERIAS_OBJETIVO:
            continue
        try:
            chk = row.find_element(By.CSS_SELECTOR, "td.colAcciones input[type=checkbox]")
            if not chk.is_selected():
                chk.click()
                seleccionadas += 1
        except Exception:
            pass  # sin checkbox -> materia sin clases en el periodo
    print(f"Materias objetivo seleccionadas: {seleccionadas}")
    # Confirmar el popup (boton Aceptar/Confirmar).
    for x in browser.find_elements(By.XPATH, "//*[contains(@class,'ui-button')]"):
        if x.text.strip().lower() in ('aceptar', 'confirmar', 'seleccionar', 'ok'):
            x.click()
            break
    sleep(2)
    esperar_overlay(browser)
    return seleccionadas


# Dia de cursada: cada fila trae inputs ocultos hiddenLU/MA/MI/JU/VI/SA; el del
# dia de la clase tiene value="True".
DIAS = {'hiddenLU': 'Lunes', 'hiddenMA': 'Martes', 'hiddenMI': 'Miércoles',
        'hiddenJU': 'Jueves', 'hiddenVI': 'Viernes', 'hiddenSA': 'Sábado'}


def dia_de_fila(fila):
    if not fila:
        return ''
    dias = []
    for key, nombre in DIAS.items():
        inp = fila.find('input', id=re.compile(key))
        if inp and (inp.get('value') or '').strip().lower() == 'true':
            dias.append(nombre)
    return ' y '.join(dias)


def leer_grilla(html):
    """Devuelve dict num_clase -> (desc, vacantes, dia, sede, detalle) de materias
    objetivo con cupo > 0. La vacante esta en <td class="tdvacantes">
    (lblVacantesLibresAI); el id trae grdClases_N -> lblMateriaDescripcion_N.
    dia = dia(s) de cursada segun los inputs hiddenXX; sede = lblSede de la fila."""
    soup = BeautifulSoup(html, 'html.parser')
    hallazgos = {}
    for td in soup.select('td.tdvacantes'):
        span = td.find('span', id=re.compile(r'lblVacantesLibresAI'))
        if not span:
            continue
        m = re.search(r'grdClases_(\d+)_', span.get('id', ''))
        if not m:
            continue
        try:
            vac = int(span.get_text(strip=True))
        except ValueError:
            continue
        if vac <= 0:
            continue
        desc_el = soup.find(
            id=f'ContentPlaceHolder1_rptMateriaClases_lblMateriaDescripcion_{m.group(1)}')
        desc = desc_el.get_text(strip=True) if desc_el else '(materia?)'
        if not any(cod in desc for cod in MATERIAS_OBJETIVO):
            continue
        fila = td.find_parent('tr')
        detalle = ' '.join(fila.get_text(' ', strip=True).split()) if fila else ''
        if EXCLUIR_INTENSIVOS and any(k in detalle.upper() for k in EXCLUIR_KEYWORDS):
            continue
        dia = dia_de_fila(fila)
        sede_el = fila.find(id=re.compile(r'lblSede')) if fila else None
        sede = sede_el.get_text(strip=True) if sede_el else ''
        # numero de clase = primer numero de 3+ digitos del detalle (dedupe key)
        mnum = re.search(r'\b(\d{3,})\b', detalle)
        clave = mnum.group(1) if mnum else detalle[:20]
        hallazgos[clave] = (desc, vac, dia, sede, detalle)
    return hallazgos


def entrar_y_seleccionar(browser):
    """Navega al param y selecciona las 6 materias. Se hace cada barrido para
    asegurar el estado (mismo navegador, sin relanzar Chrome)."""
    browser.get(PARAM_URL)
    sleep(3)
    if es_bloqueo(browser.page_source):
        raise Bloqueo("el portal rechazo el acceso (WAF / 401 / 403).")
    if seleccionar_materias_objetivo(browser) == 0:
        raise Bloqueo("no pude entrar ni seleccionar materias (param vencido o bloqueo?).")


def buscar_turnos(browser):
    """Recorre los turnos y devuelve dict clave -> (desc, vac, detalle) con cupo.
    Si un turno falla y no hubo NINGUN resultado, lanza error para reintentar
    (no reportar 'sin vacantes' en falso)."""
    hallazgos = {}
    errores = 0
    for valor, nombre in TURNOS.items():
        try:
            esperar_overlay(browser)
            Select(browser.find_element(By.ID, 'ContentPlaceHolder1_cboTurno')).select_by_value(valor)
            sleep(1)
            browser.execute_script(
                "arguments[0].click()",
                browser.find_element(By.ID, 'ContentPlaceHolder1_btnBuscar'))
            sleep(5)
            if es_bloqueo(browser.page_source):
                raise Bloqueo("el portal rechazo la busqueda (WAF).")
            hallazgos.update(leer_grilla(browser.page_source))
        except Bloqueo:
            raise
        except Exception as e:
            errores += 1
            print(f"Turno {nombre}: error ({e})")
    if errores and not hallazgos:
        raise RuntimeError(f"{errores} turno(s) fallaron sin resultados; reintentar.")
    return hallazgos


def validar_config():
    req = {'UADE_USER': USUARIO_UADE, 'UADE_PASS': PASSWORD_UADE, 'PARAM_URL': PARAM_URL,
           'WHATSAPP_TELEFONO': WHATSAPP_TELEFONO, 'WHATSAPP_APIKEY': WHATSAPP_APIKEY}
    faltan = [k for k, v in req.items() if not v]
    if faltan:
        print("Faltan variables/secrets: " + ", ".join(faltan))
        sys.exit(1)


def cerrar(browser):
    try:
        if browser is not None:
            browser.quit()
    except Exception:
        pass


def main():
    validar_config()
    deadline = monotonic() + LOOP_SECONDS
    browser = None
    ya_avisadas = set()   # dedupe: no re-avisar la misma clase dentro de la corrida
    fallos = 0
    print(f"Monitor iniciado: barrido cada {INTERVALO}s por ~{LOOP_SECONDS // 60} min.")
    while monotonic() < deadline:
        try:
            if browser is None:
                browser = armar_browser()
            entrar_y_seleccionar(browser)
            hallazgos = buscar_turnos(browser)
            nuevas = [(k, v) for k, v in hallazgos.items() if k not in ya_avisadas]
            if nuevas:
                for i, (clave, (desc, vac, dia, sede, det)) in enumerate(nuevas):
                    ya_avisadas.add(clave)
                    dia_txt = dia if dia else "ver detalle"
                    sede_txt = sede if sede else "ver detalle"
                    msg = (f"Se libero cupo en {desc} — {sede_txt}, dia {dia_txt}: "
                           f"{vac} cupos. Anda a inscribirte. [{det}]")
                    print(msg)
                    send_msg(msg)
                    if i < len(nuevas) - 1:
                        sleep(5)   # separar WhatsApp (rate limit de CallMeBot)
            else:
                print("Ciclo ok: sin vacantes nuevas.")
            fallos = 0
        except Bloqueo as e:
            aviso = (f"ALERTA monitor UADE: posible BLOQUEO del portal ({e}) "
                     "El monitor se frena. Revisa tu acceso y reintenta mas tarde.")
            print(aviso)
            send_msg(aviso)
            cerrar(browser)
            sys.exit(2)          # exit 2 = bloqueo -> el workflow corta
        except Exception as e:
            fallos += 1
            print(f"Error transitorio (fallos seguidos={fallos}): {e}")
            cerrar(browser)
            browser = None       # recrear Chrome limpio en el proximo ciclo
            if fallos >= 5:
                send_msg(f"Monitor UADE fallando: {fallos} ciclos seguidos con error. "
                         f"Se detiene. Ultimo: {e}")
                sys.exit(1)
        sleep(INTERVALO)
    cerrar(browser)
    print("Loop completado (~5,5 h).")


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error fatal: {e}")
        try:
            send_msg(f"Monitor UADE: error fatal, se detuvo. {e}")
        except Exception:
            pass
        sys.exit(1)
