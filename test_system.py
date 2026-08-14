#!/usr/bin/env python3
"""
test_system.py - Suite de pruebas automatizadas para el sistema de Jardín Inteligente

Prueba 5 componentes:
  1. Conexión a la base de datos PostgreSQL (Aiven)
  2. Comunicación TCP con el ESP32 (192.168.0.247:9001)
  3. Endpoints de la API Flask (localhost:5000)
  4. Integración completa (ESP32 -> BD -> ESP32)
  5. Rendimiento básico (10 peticiones concurrentes)

Uso:
  python test_system.py                  # Ejecutar todas las pruebas
  python test_system.py --skip-esp32     # Omitir pruebas con el ESP32
  python test_system.py --skip-api       # Omitir pruebas de la API
  python test_system.py --skip-perf      # Omitir prueba de carga

Requisitos: psycopg2, python-dotenv (ya instalados en el venv).
No requiere `requests`: usa urllib de la librería estándar.
"""

import os
import sys
import socket
import time
import json
import urllib.request
import urllib.error
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

try:
    import psycopg2
    from psycopg2 import sql as psql
except ImportError:
    print("ERROR: Falta psycopg2. Instálalo con: pip install psycopg2-binary")
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    print("ERROR: Falta python-dotenv. Instálalo con: pip install python-dotenv")
    sys.exit(1)

# Habilitar códigos de color ANSI en terminales de Windows modernas
os.system('')

# Forzar UTF-8 en la salida para que los emojis no rompan la consola
# (errors='replace' evita excepciones si la terminal no soporta el carácter)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

# ---------------------------------------------------------------------------
# Configuración / constantes
# ---------------------------------------------------------------------------

load_dotenv()

DB_URL = os.getenv("DATABASE_URL")
ESP32_IP = os.getenv("ESP32_IP", "192.168.0.247")
ESP32_PORT = int(os.getenv("ESP32_PORT", 9001))
ESP32_TIMEOUT = 3
FLASK_URL = os.getenv("FLASK_URL", "http://localhost:5000")
DB_TIMEOUT = 10

# Colores ANSI
OK = "\033[92m"
FAIL = "\033[91m"
WARN = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN, RED, YELLOW, BLUE, RES = OK, FAIL, WARN, CYAN, RESET

PASS_ICON = "✅"
FAIL_ICON = "❌"
WARN_ICON = "⚠️"

_results = []  # (nombre, pasó, mensaje) para el resumen final


def report(name, passed, message="", warn_only=False):
    """Registra y muestra el resultado de una prueba."""
    icon = PASS_ICON if passed else (WARN_ICON if warn_only else FAIL_ICON)
    color = GREEN if passed else (YELLOW if warn_only else RED)
    print(f"  {icon} [{color}{name}{RESET}] {message}")
    _results.append((name, passed or warn_only, message, warn_only))


# ---------------------------------------------------------------------------
# Utilidades de red
# ---------------------------------------------------------------------------

def esp32_send(command, timeout=ESP32_TIMEOUT):
    """
    Envía un comando al ESP32 y devuelve la respuesta (string) o lanza Exception.
    """
    with socket.create_connection((ESP32_IP, ESP32_PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(f"{command}\n".encode("utf-8"))
        response = sock.recv(1024).decode("utf-8").strip()
        return response


def parse_dht11(response):
    """
    Parsea una respuesta del ESP32 tipo "T:25.50,H:60.25".
    Devuelve (temperatura, humedad) o None si el formato no es válido.
    """
    if not response or not response.startswith("T:") or ",H:" not in response:
        return None
    try:
        temp_part, hum_part = response.split(",H:")
        temperatura = float(temp_part.replace("T:", "").strip())
        humedad = float(hum_part.strip())
        return (temperatura, humedad)
    except ValueError:
        return None


def http_get(url, timeout=5):
    """GET HTTP usando urllib. Devuelve (status_code, body_json_dict) o lanza excepción."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = resp.getcode()
        body = resp.read().decode("utf-8")
        return status, json.loads(body)


def server_is_up():
    """Comprueba si Flask responde en /health."""
    try:
        status, body = http_get(f"{FLASK_URL}/health", timeout=3)
        return status == 200 and body.get("status") == "healthy"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Utilidades de base de datos
# ---------------------------------------------------------------------------

def psycopg2_url():
    """
    Convierte la URL del .env a un formato compatible con psycopg2.
    El .env usa "postgresql+pg8000://..." (dialecto SQLAlchemy), pero
    psycopg2 necesita "postgresql://" o "postgres://".
    """
    if not DB_URL:
        return None
    url = DB_URL.replace("postgresql+pg8000://", "postgresql://", 1)
    url = url.replace("postgresql+psycopg2://", "postgresql://", 1)
    url = url.replace("postgres://", "postgresql://", 1)
    return url


def db_connect():
    """Abre una conexión psycopg2. Lanza excepción si falla."""
    url = psycopg2_url()
    if not url:
        raise Exception("DATABASE_URL no está definida en el .env")
    return psycopg2.connect(url, connect_timeout=DB_TIMEOUT)


CREATE_READINGS_SQL = """
CREATE TABLE IF NOT EXISTS dht11_readings (
    id          SERIAL PRIMARY KEY,
    timestamp   TIMESTAMP NOT NULL DEFAULT now(),
    temperatura FLOAT NOT NULL,
    humedad     FLOAT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dht11_readings_timestamp
    ON dht11_readings (timestamp);
"""

CREATE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS relay_events (
    id         SERIAL PRIMARY KEY,
    timestamp  TIMESTAMP NOT NULL DEFAULT now(),
    rele       INTEGER NOT NULL,
    estado     BOOLEAN NOT NULL,
    origen     VARCHAR(50) NOT NULL DEFAULT 'dashboard'
);
CREATE INDEX IF NOT EXISTS ix_relay_events_timestamp
    ON relay_events (timestamp);
CREATE INDEX IF NOT EXISTS ix_relay_events_rele
    ON relay_events (rele);
"""


# ---------------------------------------------------------------------------
# 1. PRUEBA DE BASE DE DATOS
# ---------------------------------------------------------------------------

def test_database():
    print(f"\n{BOLD}=== 1. BASE DE DATOS (PostgreSQL) ==={RESET}")

    if not DB_URL:
        report("BD: cadena de conexión", False,
               "DATABASE_URL no encontrada en el .env")
        return

    print(f"  DB_URL detectada (host: {psycopg2_url().split('@')[-1].split(':')[0]})")

    try:
        conn = db_connect()
    except Exception as e:
        report("BD: conexión", False,
               f"No se pudo conectar: {e} | Sugerencia: revisa que tu IP esté "
               f"en la whitelist de Aiven y que las credenciales sean correctas.")
        return

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            result = cur.fetchone()
            if result != (1,):
                raise Exception(f"SELECT 1 devolvió {result}")
        report("BD: conexión y SELECT 1", True, "Conexión establecida correctamente")
    except Exception as e:
        conn.close()
        report("BD: conexión y SELECT 1", False, f"Error en consulta: {e}")
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
            """)
            existing = {row[0] for row in cur.fetchall()}

        missing = []
        for table in ("dht11_readings", "relay_events"):
            if table not in existing:
                missing.append(table)

        if missing:
            with conn.cursor() as cur:
                cur.execute(CREATE_READINGS_SQL)
                cur.execute(CREATE_EVENTS_SQL)
            conn.commit()
            report("BD: tablas", True,
                   f"Tablas faltantes creadas automáticamente: {', '.join(missing)}")
        else:
            report("BD: tablas", True, "Tablas 'dht11_readings' y 'relay_events' existen")
    except Exception as e:
        conn.rollback()
        report("BD: tablas", False, f"No se pudieron verificar/crear tablas: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. PRUEBA DEL ESP32
# ---------------------------------------------------------------------------

def test_esp32():
    print(f"\n{BOLD}=== 2. COMUNICACIÓN CON EL ESP32 ({ESP32_IP}:{ESP32_PORT}) ==={RESET}")

    # 2a. Comando DATA
    try:
        response = esp32_send("DATA")
        parsed = parse_dht11(response)
        if parsed:
            temp, hum = parsed
            report("ESP32: comando DATA", True,
                   f"T={temp}°C, H={hum}% (respuesta: {response})")
        else:
            report("ESP32: comando DATA", False,
                   f"Respuesta inesperada: '{response}' | Esperado: 'T:xx.xx,H:xx.xx'")
    except Exception as e:
        report("ESP32: comando DATA", False,
               f"{e} | Sugerencia: ¿el ESP32 está encendido y en la misma red?")

    # 2b. Control de relé 1 (ON -> OFF)
    try:
        response_on = esp32_send("ON 1")
        if response_on == "OK":
            report("ESP32: ON 1", True, "Relé 1 encendido (respuesta OK)")
        else:
            report("ESP32: ON 1", False, f"Respuesta inesperada: '{response_on}'")
    except Exception as e:
        report("ESP32: ON 1", False, str(e))

    try:
        response_off = esp32_send("OFF 1")
        if response_off == "OK":
            report("ESP32: OFF 1", True, "Relé 1 apagado (respuesta OK)")
        else:
            report("ESP32: OFF 1", False, f"Respuesta inesperada: '{response_off}'")
    except Exception as e:
        report("ESP32: OFF 1", False, str(e))


# ---------------------------------------------------------------------------
# 3. PRUEBA DE LA API
# ---------------------------------------------------------------------------

def test_api():
    print(f"\n{BOLD}=== 3. ENDPOINTS DE LA API ({FLASK_URL}) ==={RESET}")

    if not server_is_up():
        print(f"  {WARN_ICON} [{YELLOW}API: servidor apagado{RESET}] Flask no responde en "
              f"{FLASK_URL}/health. Pruebas de API omitidas.")
        print(f"  Arranca la app con: venv\\Scripts\\python app.py")
        report("API: /api/status", False, "Servidor Flask no disponible", warn_only=True)
        report("API: /api/historical", False, "Servidor Flask no disponible", warn_only=True)
        report("API: /api/relay", False, "Servidor Flask no disponible", warn_only=True)
        return

    # 3a. /api/status
    try:
        status, data = http_get(f"{FLASK_URL}/api/status")
        required = {"esp32", "dht11", "relays", "timestamp"}
        ok_shape = status == 200 and required.issubset(data.keys())
        relays = data.get("relays", {})
        relay_detail = (f" | relés: 1={'ON' if relays.get(1) else 'OFF'}, "
                        f"2={'ON' if relays.get(2) else 'OFF'}")
        report("API: /api/status", ok_shape,
               f"HTTP {status}{relay_detail if ok_shape else ''}")
    except Exception as e:
        report("API: /api/status", False, f"Error: {e}")

    # 3b. /api/historical
    try:
        status, data = http_get(f"{FLASK_URL}/api/historical?hours=24")
        ok_shape = status == 200 and "readings" in data and "events" in data
        n_readings = len(data.get("readings", [])) if ok_shape else 0
        n_events = len(data.get("events", [])) if ok_shape else 0
        report("API: /api/historical", ok_shape,
               f"HTTP {status} | {n_readings} lecturas, {n_events} eventos (24h)")
    except Exception as e:
        report("API: /api/historical", False, f"Error: {e}")

    # 3c. Control de relé vía API (usar método GET, que es como lo hace el frontend)
    #     OJO: cambia físicamente el relé. Se enciende y apaga para dejar estado original.
    try:
        status, data = http_get(f"{FLASK_URL}/api/relay/1/on")
        ok_shape = status == 200 and isinstance(data.get("success"), bool)
        report("API: /api/relay/1/on", ok_shape,
               f"HTTP {status} | success={data.get('success')}, msg='{data.get('message')}'")
    except Exception as e:
        report("API: /api/relay/1/on", False, f"Error: {e}")

    try:
        status, data = http_get(f"{FLASK_URL}/api/relay/1/off")
        ok_shape = status == 200 and isinstance(data.get("success"), bool)
        report("API: /api/relay/1/off", ok_shape,
               f"HTTP {status} | success={data.get('success')}, msg='{data.get('message')}'")
    except Exception as e:
        report("API: /api/relay/1/off", False, f"Error: {e}")


# ---------------------------------------------------------------------------
# 4. PRUEBA DE INTEGRACIÓN COMPLETA
# ---------------------------------------------------------------------------

def test_integration():
    print(f"\n{BOLD}=== 4. INTEGRACIÓN COMPLETA (ESP32 -> BD -> ESP32) ==={RESET}")

    # 4a. Pedir DATA al ESP32
    try:
        response = esp32_send("DATA")
        parsed = parse_dht11(response)
        if not parsed:
            report("Integración: leer DATA", False,
                   f"Respuesta inválida del ESP32: '{response}'")
            return
        temp, hum = parsed
        report("Integración: leer DATA", True, f"T={temp}°C, H={hum}%")
    except Exception as e:
        report("Integración: leer DATA", False, str(e))
        return

    # 4b. Guardar lectura en BD
    try:
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dht11_readings (temperatura, humedad) VALUES (%s, %s) RETURNING id",
                (temp, hum)
            )
            new_id = cur.fetchone()[0]
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT temperatura, humedad FROM dht11_readings WHERE id = %s",
                (new_id,)
            )
            row = cur.fetchone()
        conn.close()

        if row and abs(float(row[0]) - temp) < 0.001 and abs(float(row[1]) - hum) < 0.001:
            report("Integración: guardar en BD", True,
                   f"Lectura id={new_id} escrita y verificada (T={row[0]}, H={row[1]})")
        else:
            report("Integración: guardar en BD", False, f"Valores no coinciden: {row}")
            return
    except Exception as e:
        if 'conn' in locals() and conn:
            conn.close()
        report("Integración: guardar en BD", False, f"Error BD: {e}")
        return

    # 4c. Enviar ON 1 al ESP32 y confirmar evento en BD
    try:
        response = esp32_send("ON 1")
        if response != "OK":
            report("Integración: ON 1", False, f"ESP32 respondió: '{response}'")
            return
        report("Integración: ON 1", True, "Relé 1 encendido (OK)")
    except Exception as e:
        report("Integración: ON 1", False, str(e))
        return

    try:
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rele, estado, origen FROM relay_events ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        conn.close()

        if row and int(row[0]) == 1 and row[1] is True:
            report("Integración: evento en BD", True,
                   f"Evento confirmado: rele={row[0]}, estado=ON, origen='{row[2]}'")
        else:
            report("Integración: evento en BD", False,
                   f"No se encontró el evento esperado. Último registro: {row}")
    except Exception as e:
        if 'conn' in locals() and conn:
            conn.close()
        report("Integración: evento en BD", False, f"Error BD: {e}")

    # 4d. Restaurar relé a OFF (higiene de la prueba)
    try:
        response = esp32_send("OFF 1")
        report("Integración: restaurar OFF 1", response == "OK",
               "Relé 1 apagado (estado original)" if response == "OK"
               else f"Respuesta inesperada: '{response}'")
    except Exception as e:
        report("Integración: restaurar OFF 1", False, str(e))


# ---------------------------------------------------------------------------
# 5. PRUEBA DE CARGA BÁSICA
# ---------------------------------------------------------------------------

def test_performance():
    print(f"\n{BOLD}=== 5. RENDIMIENTO (10 peticiones concurrentes a /api/status) ==={RESET}")

    if not server_is_up():
        print(f"  {WARN_ICON} [{YELLOW}Rendimiento: servidor apagado{RESET}] Prueba omitida.")
        report("Rendimiento", False, "Servidor Flask no disponible", warn_only=True)
        return

    def single_call():
        t0 = time.perf_counter()
        try:
            http_get(f"{FLASK_URL}/api/status", timeout=5)
            return time.perf_counter() - t0, True
        except Exception:
            return time.perf_counter() - t0, False

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(single_call) for _ in range(10)]
        results = [f.result() for f in futures]

    times = [t for t, ok in results]
    ok_count = sum(1 for _, ok in results if ok)

    if ok_count == 0:
        report("Rendimiento", False, "Las 10 peticiones fallaron")
        return

    avg = sum(times) / len(times)
    tmin = min(times)
    tmax = max(times)

    print(f"  ⏱️  Peticiones OK: {ok_count}/10")
    print(f"  ⏱️  Promedio: {avg * 1000:.1f} ms | Mínimo: {tmin * 1000:.1f} ms | "
          f"Máximo: {tmax * 1000:.1f} ms")

    report("Rendimiento", True,
           f"{ok_count}/10 OK, promedio {avg * 1000:.1f} ms, "
           f"mín {tmin * 1000:.1f} ms, máx {tmax * 1000:.1f} ms")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Pruebas automatizadas del sistema de Jardín Inteligente"
    )
    parser.add_argument("--skip-db", action="store_true", help="Omitir prueba de BD")
    parser.add_argument("--skip-esp32", action="store_true", help="Omitir prueba del ESP32")
    parser.add_argument("--skip-api", action="store_true", help="Omitir prueba de API")
    parser.add_argument("--skip-integration", action="store_true", help="Omitir integración")
    parser.add_argument("--skip-perf", action="store_true", help="Omitir prueba de rendimiento")
    args = parser.parse_args()

    print(f"{BOLD}{CYAN}🧪 INICIANDO PRUEBAS DEL SISTEMA - JARDÍN INTELIGENTE{RESET}")
    print(f"{CYAN}   Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{CYAN}   ESP32: {ESP32_IP}:{ESP32_PORT} | Flask: {FLASK_URL}{RESET}")
    print(f"{CYAN}   {'-' * 60}{RESET}")

    if not args.skip_db:
        test_database()
    if not args.skip_esp32:
        test_esp32()
    if not args.skip_api:
        test_api()
    if not args.skip_integration:
        test_integration()
    if not args.skip_perf:
        test_performance()

    # Resumen final
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}RESUMEN FINAL{RESET}")

    passed = sum(1 for _, p, _, w in _results if p and not w)
    warned = sum(1 for _, _, _, w in _results if w)
    failed = sum(1 for _, p, _, w in _results if not p and not w)
    total = len(_results)

    for name, p, msg, w in _results:
        icon = WARN_ICON if w else (PASS_ICON if p else FAIL_ICON)
        color = YELLOW if w else (GREEN if p else RED)
        print(f"  {icon} [{color}{name}{RESET}] {msg}")

    print(f"\n  Resultado: {GREEN}{passed} correctas{RESET} | "
          f"{YELLOW}{warned} omitidas/advertencias{RESET} | "
          f"{RED}{failed} fallidas{RESET} | total {total}")

    if failed > 0:
        print(f"  {FAIL_ICON} Hay fallos. Revisa los mensajes y los logs de app.py.")
        sys.exit(1)
    elif warned > 0:
        print(f"  {WARN_ICON} Con advertencias (componentes no probados o conexión perdida).")
        sys.exit(0)
    else:
        print(f"  {PASS_ICON} Todas las pruebas pasaron correctamente.")
        sys.exit(0)


if __name__ == "__main__":
    main()
