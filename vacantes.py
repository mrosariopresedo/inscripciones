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

El cron del workflow repite el barrido cada ~15 min: aca NO hay while True.
"""
import os
import re
import sys
import base64
from time import sleep

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

# Los 3 intensivos de Pinamar YA tienen vacante y no te interesan (sacaste el
# turno INTENSIVO). Si True, los omite para no spamear. Poner False para verlos.
EXCLUIR_INTENSIVOS = True

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36')


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


def leer_grilla(html):
    """Devuelve dict num_clase -> (desc, vacantes, detalle) de materias objetivo
    con cupo > 0. La vacante esta en <td class="tdvacantes"> (lblVacantesLibresAI);
    el id trae grdClases_N que mapea a lblMateriaDescripcion_N."""
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
        if EXCLUIR_INTENSIVOS and ('INTENSIVO' in detalle.upper() or 'PINAMAR' in detalle.upper()):
            continue
        # numero de clase = primer numero de 3+ digitos del detalle (dedupe key)
        mnum = re.search(r'\b(\d{3,})\b', detalle)
        clave = mnum.group(1) if mnum else detalle[:20]
        hallazgos[clave] = (desc, vac, detalle)
    return hallazgos


def barrido(browser):
    if not PARAM_URL:
        raise RuntimeError("Falta PARAM_URL (la URL con ?param= del 2026).")
    browser.get(PARAM_URL)
    sleep(3)
    if 'Request Rejected' in browser.page_source:
        raise RuntimeError("El WAF rechazo la entrada: el param no es valido/vigente.")
    seleccionar_materias_objetivo(browser)
    hallazgos = {}
    for valor, nombre in TURNOS.items():
        try:
            esperar_overlay(browser)
            Select(browser.find_element(By.ID, 'ContentPlaceHolder1_cboTurno')).select_by_value(valor)
            sleep(1)
            browser.execute_script(
                "arguments[0].click()",
                browser.find_element(By.ID, 'ContentPlaceHolder1_btnBuscar'))
            sleep(5)
            hallazgos.update(leer_grilla(browser.page_source))  # dedupe por num clase
        except Exception as e:
            print(f"Turno {nombre}: sin resultados o error ({e})")
    return hallazgos


def validar_config():
    req = {'UADE_USER': USUARIO_UADE, 'UADE_PASS': PASSWORD_UADE, 'PARAM_URL': PARAM_URL,
           'WHATSAPP_TELEFONO': WHATSAPP_TELEFONO, 'WHATSAPP_APIKEY': WHATSAPP_APIKEY}
    faltan = [k for k, v in req.items() if not v]
    if faltan:
        print("Faltan variables/secrets: " + ", ".join(faltan))
        sys.exit(1)


def main():
    validar_config()
    browser = armar_browser()
    try:
        hallazgos = barrido(browser)
        if hallazgos:
            for desc, vac, detalle in hallazgos.values():
                msg = f"VACANTE en {desc}: {vac} cupos. {detalle}"
                print(msg)
                send_msg(msg)
        else:
            print("Barrido ok: sin vacantes nuevas en las materias objetivo.")
    except Exception as e:
        print(f"Error en el barrido: {e}")
        send_msg(f"El monitor de vacantes UADE tuvo un error: {e}")
        sys.exit(1)
    finally:
        browser.quit()


if __name__ == '__main__':
    main()
