import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- CONFIGURACIÓN DE TELEGRAM Y BÚSQUEDA ---
TOKEN_TELEGRAM = 'TU_TOKEN_AQUI'
CHAT_ID = 'TU_CHAT_ID_AQUI'
URL_TOYOTA = 'https://www.toyota.es/coches-segunda-mano?brands=38&model=CO,CR&price=cash:6900-27000&mileage=1-100000&year=2025-2026&seats=1-9&doors=4-4'

# Memoria para recordar qué enlaces ya hemos enviado
coches_vistos = set()

# --- CLASES CSS (Debes ajustarlas según el paso 3) ---
CSS_TARJETA_COCHE = "div.tarjeta-ejemplo" # Reemplazar por la clase real
CSS_TITULO = "h2.titulo-ejemplo"          # Reemplazar por la clase real
CSS_PRECIO = "span.precio-ejemplo"        # Reemplazar por la clase real

def enviar_mensaje_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/sendMessage"
    datos = {'chat_id': CHAT_ID, 'text': mensaje}
    requests.post(url, data=datos)

def configurar_navegador():
    chrome_options = Options()
    chrome_options.add_argument("--headless") # Ejecuta Chrome en modo invisible
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    
    # Inicializamos el navegador
    return webdriver.Chrome(options=chrome_options)

def buscar_coches_toyota():
    driver = configurar_navegador()
    print("Accediendo a la web de Toyota...")
    
    try:
        driver.get(URL_TOYOTA)
        
        # Esperamos hasta 15 segundos a que aparezca al menos un coche en pantalla
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, CSS_TARJETA_COCHE)) 
        )
        
        # Extraemos la lista de todas las "tarjetas" de coches
        anuncios = driver.find_elements(By.CSS_SELECTOR, CSS_TARJETA_COCHE)
        print(f"Se han cargado {len(anuncios)} coches en la página.")
        
        for anuncio in anuncios:
            try:
                # 1. Obtenemos el enlace
                enlace_elemento = anuncio.find_element(By.CSS_SELECTOR, "a")
                enlace = enlace_elemento.get_attribute('href')
                
                # Si el enlace es nuevo, extraemos el resto y avisamos
                if enlace and enlace not in coches_vistos:
                    # Usamos get_attribute('textContent') por si los textos están tapados por el banner de cookies
                    titulo = anuncio.find_element(By.CSS_SELECTOR, CSS_TITULO).get_attribute('textContent').strip()
                    precio = anuncio.find_element(By.CSS_SELECTOR, CSS_PRECIO).get_attribute('textContent').strip()
                    
                    # Lo guardamos en memoria
                    coches_vistos.add(enlace)
                    
                    # Preparamos la alerta de Telegram
                    mensaje = f"🚗 ¡Nuevo Toyota detectado!\n\n🔹 Modelo: {titulo}\n💰 Precio: {precio}\n🔗 Ver coche: {enlace}"
                    enviar_mensaje_telegram(mensaje)
                    print(f"Alerta enviada: {titulo}")
                    
            except Exception as e:
                # Si falla al leer un coche concreto, saltamos al siguiente
                continue

    except Exception as e:
        print(f"No se encontraron coches o hubo un error: {e}")
    finally:
        driver.quit() # Cierra el navegador invisible liberando memoria

if __name__ == "__main__":
    print("Iniciando rastreador de Toyota...")
    enviar_mensaje_telegram("🤖 Rastreador de Toyota iniciado con tus filtros.")
    
    while True:
        buscar_coches_toyota()
        print("Búsqueda completada. Esperando 15 minutos...")
        time.sleep(900) # Revisa cada 15 minutos (900 segundos)