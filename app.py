"""
Aplicación Flask para Sistema de Jardín Inteligente / SIGMA-IOT
Panel de control, autenticación JWT por roles y servidor de base de datos
para Arduino Oplà y ESP32.
"""

import ipaddress
import json
import logging
import os
import socket
import time
from datetime import datetime, timedelta

from flask import Flask, redirect, render_template, jsonify, request, url_for, Response, stream_with_context
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from database import db, configure_database
from models import DHT11Reading, RelayEvent, SistemaConfig, DispositivoOpla
from auth import (
    auth_bp,
    autenticado,
    auditoria,
    permiso_requerido,
    seed_admin,
    seed_config,
    seed_roles_permisos,
)

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicializar Flask
app = Flask(__name__)

# Configuración de la base de datos
configure_database(app)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')

# CORS: orígenes permitidos desde variables de entorno
cors_origins = [
    o.strip() for o in os.getenv(
        'CORS_ORIGINS',
        'http://localhost:5000,http://127.0.0.1:5000'
    ).split(',') if o.strip()
]
CORS(app, resources={r"/api/*": {"origins": cors_origins}}, supports_credentials=False)

# Registro del blueprint de autenticación
app.register_blueprint(auth_bp)

# Configuración del ESP32
ESP32_IP = os.getenv('ESP32_IP', '192.168.0.247')
ESP32_PORT = int(os.getenv('ESP32_PORT', 9001))
ESP32_TIMEOUT = 5  # segundos

# Variables globales para estado de conexión
esp32_status = {
    'connected': False,
    'last_check': None,
    'ip': ESP32_IP,
    'port': ESP32_PORT,
    'last_error': None
}

relay_states = {1: False, 2: False}  # Estado actual de los relés (False=OFF, True=ON)


def actualizar_env_esp32_ip(nueva_ip):
    """Actualiza ESP32_IP en .env y recarga variables globales."""
    global ESP32_IP, ESP32_PORT, esp32_status, esp32_client
    ruta_env = os.path.join(os.path.dirname(__file__), '.env')
    lineas = []
    with open(ruta_env, 'r', encoding='utf-8') as f:
        for linea in f:
            if linea.strip().startswith('ESP32_IP='):
                lineas.append(f'ESP32_IP={nueva_ip}\n')
            else:
                lineas.append(linea)
    with open(ruta_env, 'w', encoding='utf-8') as f:
        f.writelines(lineas)
    # Recargar en memoria
    ESP32_IP = nueva_ip
    esp32_status['ip'] = nueva_ip
    logger.info(f'.env actualizado: ESP32_IP={nueva_ip}')


# ============================================
# DESCUBRIMIENTO DE DISPOSITIVOS (UDP BROADCAST)
# ============================================

ESP32_DISCOVERY_PORT = 5001
OPLA_DISCOVERY_PORT = 5002
DISCOVERY_TIMEOUT = 2.0


def descubrir_esp32(timeout=None):
    """Envía FIND_ESP32 por UDP broadcast y devuelve la IP del ESP32 o None."""
    return _descubrir_por_broadcast('FIND_ESP32', ESP32_DISCOVERY_PORT,
                                    'IP:', unica=True, timeout=timeout)


def descubrir_oplas(timeout=None):
    """Envía FIND_OPLA por UDP broadcast y devuelve lista de IPs de Oplà."""
    return _descubrir_por_broadcast('FIND_OPLA', OPLA_DISCOVERY_PORT,
                                    'IP:', unica=False, timeout=timeout)


def _descubrir_por_broadcast(cmd, puerto, prefijo, unica, timeout=None):
    """Envía un comando por UDP broadcast y recolecta respuestas 'prefijo:valor'.
    Si unica=True devuelve el primer valor válido; si no, devuelve una lista.
    Envía al broadcast global y, si detecta la subred del hotspot (192.168.137.x),
    también al broadcast de esa subred para asegurar que salga por la interfaz correcta."""
    t = timeout or DISCOVERY_TIMEOUT
    sock = None
    destinos = [('255.255.255.255', puerto)]
    for ip_local in _ips_locales():
        if ip_local.startswith('192.168.137.'):
            destinos.append(('192.168.137.255', puerto))
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)
        mensaje = f"{cmd}\n".encode('utf-8')
        for destino in destinos:
            try:
                sock.sendto(mensaje, destino)
            except OSError:
                continue
        encontrados = []
        inicio = time.time()
        while time.time() - inicio < t:
            try:
                data, addr = sock.recvfrom(64)
            except socket.timeout:
                continue
            texto = data.decode('utf-8', 'ignore').strip()
            if texto.startswith(prefijo):
                valor = texto[len(prefijo):].strip()
                if _ip_valida(valor) and addr[0] == valor:
                    if unica:
                        return valor
                    if valor not in encontrados:
                        encontrados.append(valor)
        return None if unica else encontrados
    except Exception as e:
        logger.error(f"Error en descubrimiento UDP ({cmd}): {e}")
        return None if unica else []
    finally:
        if sock:
            sock.close()


def _ips_locales():
    """Devuelve las direcciones IPv4 locales de esta máquina."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        import subprocess
        out = subprocess.run(['ipconfig'], capture_output=True, text=True).stdout
        import re
        for m in re.finditer(r'IPv4[^:]*:\s*(\d+\.\d+\.\d+\.\d+)', out):
            ips.add(m.group(1))
    except Exception:
        pass
    return list(ips)


def _ip_valida(ip):
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


# ============================================
# CLIENTE TCP PARA COMUNICACIÓN CON ESP32
# ============================================

class ESP32Client:
    """Cliente TCP para comunicarse con el ESP32"""

    def __init__(self, host, port, timeout=5):
        self.host = host
        self.port = port
        self.timeout = timeout

    def send_command(self, command):
        """
        Envía un comando al ESP32 y retorna la respuesta.
        Conexión breve: abrir, enviar, recibir, cerrar.
        """
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                message = f"{command}\n"
                sock.sendall(message.encode('utf-8'))
                response = sock.recv(1024).decode('utf-8').strip()
                return response
        except socket.timeout:
            raise Exception(f"Timeout conectando a ESP32 ({self.host}:{self.port})")
        except ConnectionRefusedError:
            raise Exception(f"Conexión rechazada por ESP32 ({self.host}:{self.port})")
        except Exception as e:
            raise Exception(f"Error de comunicación con ESP32: {str(e)}")

    def test_connection(self):
        """Prueba la conexión enviando un comando simple"""
        try:
            response = self.send_command("PING")
            return response == "PONG"
        except Exception:
            return False


esp32_client = ESP32Client(ESP32_IP, ESP32_PORT, ESP32_TIMEOUT)


# ============================================
# DISPOSITIVOS (OPLÀ) — REGISTRO Y ESTADO
# ============================================

_online_cache = {}
ONLINE_TTL = 15  # segundos entre verificaciones de un mismo dispositivo


def _check_online(ip, puerto):
    """Verifica si un Oplà responde en TCP (ip:puerto). Con caché corta.
    Devuelve True/False, o None si no hay IP registrada."""
    if not ip or not puerto:
        return None
    now = time.time()
    cache_key = f"{ip}:{puerto}"
    if cache_key in _online_cache and now - _online_cache[cache_key][0] < ONLINE_TTL:
        return _online_cache[cache_key][1]
    try:
        with socket.create_connection((ip, puerto), timeout=0.7):
            online = True
    except Exception:
        online = False
    _online_cache[cache_key] = (now, online)
    return online


def _parse_clave_valor(respuesta):
    """Parsea respuestas 'clave:valor,clave:valor' (formato ESP32 y Oplà)."""
    datos = {}
    if not respuesta:
        return datos
    for parte in respuesta.split(','):
        parte = parte.strip()
        if ':' in parte:
            clave, valor = parte.split(':', 1)
            datos[clave.strip()] = valor.strip()
    return datos


def _validar_dispositivo(datos, dispositivo=None, parcial=False):
    """Valida y devuelve (nombre, seccion, zona, ip, puerto) de un DispositivoOpla.
    Lanza ValueError con el motivo si los datos no son válidos."""
    nombre = (datos.get('nombre') or '').strip() if (datos.get('nombre') or parcial) else None
    seccion = (datos.get('seccion') or '').strip().lower()
    zona = (datos.get('zona') or '').strip() or None
    ip = (datos.get('ip') or '').strip() or None
    puerto = datos.get('puerto', 9001)

    if not parcial:
        if not nombre or len(nombre) > 100:
            raise ValueError('El nombre es obligatorio (máx. 100 caracteres).')
    if seccion and seccion not in ('jardin', 'laboratorio'):
        raise ValueError("La sección debe ser 'jardin' o 'laboratorio'.")
    if zona and len(zona) > 100:
        raise ValueError('La zona debe tener como máximo 100 caracteres.')
    if ip:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            raise ValueError('IP inválida.')
    try:
        puerto = int(puerto)
    except (TypeError, ValueError):
        puerto = 9001
    if not (1 <= puerto <= 65535):
        raise ValueError('Puerto inválido.')
    return {'nombre': nombre, 'seccion': seccion, 'zona': zona, 'ip': ip, 'puerto': puerto}


def _ip_duplicada(ip, excluir_id=None):
    if not ip:
        return False
    q = DispositivoOpla.query.filter_by(ip=ip)
    if excluir_id:
        q = q.filter(DispositivoOpla.id != excluir_id)
    return q.first() is not None


@app.route('/api/dispositivos')
@permiso_requerido('dispositivos.ver')
def api_listar_dispositivos(usuario_actual, sesion_actual):
    """Lista los Oplàs registrados agrupados por sección, con estado en línea."""
    dispositivos = DispositivoOpla.query.order_by(DispositivoOpla.fecha_registro.asc()).all()
    grupos = {'jardin': [], 'laboratorio': []}
    for d in dispositivos:
        item = d.to_dict()
        item['online'] = _check_online(d.ip, d.puerto)
        grupos.get(d.seccion, grupos['jardin']).append(item)
    return jsonify({'success': True, 'grupos': grupos})


@app.route('/api/dispositivos/discover', methods=['POST'])
@autenticado
def api_descubrir_dispositivos(usuario_actual, sesion_actual):
    """Descubre ESP32 y Oplà en la red por UDP broadcast.
    Devuelve las IPs encontradas sin modificar la BD."""
    esp32_ip = descubrir_esp32()
    oplas = descubrir_oplas()
    logger.info(f"Descubrimiento: ESP32={esp32_ip}, Oplàs={oplas}")
    return jsonify({
        'success': True,
        'esp32': esp32_ip,
        'oplas': oplas,
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route('/api/dispositivos', methods=['POST'])
@permiso_requerido('dispositivos.gestionar')
def api_crear_dispositivo(usuario_actual, sesion_actual):
    """Registra un nuevo Oplà (jardín o laboratorio)."""
    datos = request.get_json(silent=True) or {}
    try:
        campos = _validar_dispositivo(datos)
        if _ip_duplicada(campos['ip']):
            return jsonify({'success': False, 'error': 'Ya existe un dispositivo con esa IP.'}), 400
        disp = DispositivoOpla(nombre=campos['nombre'], seccion=campos['seccion'],
                               zona=campos['zona'], ip=campos['ip'], puerto=campos['puerto'])
        db.session.add(disp)
        auditoria(usuario_actual.id, 'DISPOSITIVO_CREADO', 'exito',
                  f"Oplà '{disp.nombre}' ({disp.seccion}) ip={disp.ip}")
        db.session.commit()
        return jsonify({'success': True, 'dispositivo': disp.to_dict()}), 201
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creando dispositivo: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/dispositivos/<int:disp_id>', methods=['PUT'])
@permiso_requerido('dispositivos.gestionar')
def api_editar_dispositivo(usuario_actual, sesion_actual, disp_id):
    """Edita un Oplà registrado."""
    disp = DispositivoOpla.query.get(disp_id)
    if disp is None:
        return jsonify({'success': False, 'error': 'Dispositivo no encontrado.'}), 404
    datos = request.get_json(silent=True) or {}
    try:
        campos = _validar_dispositivo(datos, disp, parcial=True)
        nombre = (datos.get('nombre') or disp.nombre).strip()
        seccion = campos['seccion'] or disp.seccion
        zona = campos['zona'] if 'zona' in datos else disp.zona
        ip = campos['ip'] if 'ip' in datos else disp.ip
        puerto = campos['puerto'] if 'puerto' in datos else disp.puerto
        if not nombre or len(nombre) > 100:
            raise ValueError('El nombre es obligatorio (máx. 100 caracteres).')
        if _ip_duplicada(ip, excluir_id=disp.id):
            return jsonify({'success': False, 'error': 'Ya existe un dispositivo con esa IP.'}), 400
        ip_anterior = disp.ip
        disp.nombre, disp.seccion, disp.zona, disp.ip, disp.puerto = nombre, seccion, zona, ip, puerto
        _online_cache.pop(f"{ip_anterior}:{disp.puerto}", None)
        _online_cache.pop(f"{disp.ip}:{disp.puerto}", None)
        
        # Si es el dispositivo ESP32 principal (sección laboratorio), actualizar .env y recargar cliente
        if disp.seccion == 'laboratorio' and ip_anterior != ip:
            actualizar_env_esp32_ip(ip)
            global esp32_client
            esp32_client = ESP32Client(ip, disp.puerto, ESP32_TIMEOUT)
            logger.info(f"ESP32 IP actualizada a {ip} y cliente recargado")
        
        auditoria(usuario_actual.id, 'DISPOSITIVO_EDITADO', 'exito',
                  f"Oplà '{disp.nombre}' ({disp.seccion}) ip={disp.ip}")
        db.session.commit()
        return jsonify({'success': True, 'dispositivo': disp.to_dict()})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error editando dispositivo: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/dispositivos/<int:disp_id>', methods=['DELETE'])
@permiso_requerido('dispositivos.gestionar')
def api_eliminar_dispositivo(usuario_actual, sesion_actual, disp_id):
    """Elimina un Oplà registrado."""
    disp = DispositivoOpla.query.get(disp_id)
    if disp is None:
        return jsonify({'success': False, 'error': 'Dispositivo no encontrado.'}), 404
    datos_previos = disp.to_dict()
    _online_cache.pop(f"{disp.ip}:{disp.puerto}", None)
    db.session.delete(disp)
    auditoria(usuario_actual.id, 'DISPOSITIVO_ELIMINADO', 'exito',
              f"Oplà '{datos_previos['nombre']}' ({datos_previos['seccion']})")
    db.session.commit()
    return jsonify({'success': True, 'message': 'Dispositivo eliminado.'})


@app.route('/api/dispositivos/<int:disp_id>/datos')
@permiso_requerido('dispositivos.ver')
def api_datos_dispositivo(usuario_actual, sesion_actual, disp_id):
    """Consulta en vivo los datos de un Oplà enviando el comando DATA."""
    disp = DispositivoOpla.query.get(disp_id)
    if disp is None:
        return jsonify({'success': False, 'error': 'Dispositivo no encontrado.'}), 404
    if not disp.ip:
        return jsonify({'success': False, 'online': None,
                        'error': 'El dispositivo no tiene IP registrada.'})
    try:
        client = ESP32Client(disp.ip, disp.puerto, timeout=3)
        respuesta = client.send_command('DATA')
        datos = _parse_clave_valor(respuesta)
        if not datos:
            return jsonify({'success': False, 'online': False,
                            'error': f'Respuesta inesperada: {respuesta[:60]!r}'}), 502
        return jsonify({'success': True, 'online': True, 'datos': datos,
                        'dispositivo': disp.to_dict()})
    except Exception as e:
        return jsonify({'success': False, 'online': False, 'error': str(e)}), 502


@app.route('/api/dispositivos/<int:disp_id>/estado')
@permiso_requerido('dispositivos.ver')
def api_estado_dispositivo(usuario_actual, sesion_actual, disp_id):
    """Consulta el estado de los relés de un Oplà enviando ESTADO.
    Los Oplàs de laboratorio (tipo ESP32) responden R1:/R2:; los de jardín
    no soportan el comando y se reporta sin estado de relés."""
    disp = DispositivoOpla.query.get(disp_id)
    if disp is None:
        return jsonify({'success': False, 'error': 'Dispositivo no encontrado.'}), 404
    if not disp.ip:
        return jsonify({'success': False, 'online': None,
                        'error': 'El dispositivo no tiene IP registrada.'})
    try:
        cliente = ESP32Client(disp.ip, disp.puerto, timeout=3)
        respuesta = cliente.send_command('ESTADO')
        rele = {}
        for parte in respuesta.split(','):
            parte = parte.strip()
            if parte.startswith('R1:') or parte.startswith('R2:'):
                try:
                    rele[int(parte[1])] = parte[3] == '1'
                except (ValueError, IndexError):
                    continue
        if not rele:
            return jsonify({'success': False, 'online': False,
                            'error': f'El dispositivo no soporta ESTADO: {respuesta[:60]!r}'})
        return jsonify({'success': True, 'online': True, 'rele': rele,
                        'dispositivo': disp.to_dict()})
    except Exception as e:
        return jsonify({'success': False, 'online': False, 'error': str(e)}), 502


# ============================================
# FUNCIONES DE LÓGICA DE NEGOCIO
# ============================================

def update_esp32_status(connected, error=None):
    """Actualiza el estado global de conexión del ESP32"""
    global esp32_status
    esp32_status['connected'] = connected
    esp32_status['last_check'] = datetime.utcnow()
    if error:
        esp32_status['last_error'] = str(error)
    else:
        esp32_status['last_error'] = None


def send_relay_command(relay_id, action, origen='dashboard'):
    """
    Envía comando ON/OFF al ESP32 para un relé específico.
    Registra el evento en BD, audita la acción y actualiza estado local.
    """
    global relay_states

    if relay_id not in [1, 2]:
        raise ValueError("ID de relé inválido (debe ser 1 o 2)")
    if action not in ['on', 'off']:
        raise ValueError("Acción inválida (debe ser 'on' u 'off')")

    command = f"{action.upper()} {relay_id}"

    try:
        logger.info(f"Enviando comando al ESP32: {command}")
        response = esp32_client.send_command(command)
        logger.info(f"Respuesta ESP32: {response}")

        if response == "OK":
            new_state = (action == 'on')
            relay_states[relay_id] = new_state

            with app.app_context():
                event = RelayEvent(rele=relay_id, estado=new_state, origen=origen)
                db.session.add(event)

                # Auditoría (ventilador = relé)
                if origen == 'automatico':
                    accion = 'VENTILADOR_AUTO_ON' if new_state else 'VENTILADOR_AUTO_OFF'
                    detalle = f'Relé {relay_id} activado/desactivado por modo automático'
                else:
                    accion = 'VENTILADOR_ENCENDIDO' if new_state else 'VENTILADOR_APAGADO'
                    detalle = f'Relé {relay_id} controlado manualmente ({origen})'
                auditoria(None, accion, 'exito', detalle)
                db.session.commit()

            update_esp32_status(True)
            return True, "Comando ejecutado correctamente"
        else:
            update_esp32_status(False, f"Respuesta inesperada: {response}")
            with app.app_context():
                accion = 'VENTILADOR_AUTO_ON' if (action == 'on' and origen == 'automatico') else 'VENTILADOR_ENCENDIDO'
                auditoria(None, accion, 'fallido', f'ESP32 no confirmó: {response} (relé {relay_id})')
                db.session.commit()
            return False, f"ESP32 respondió: {response}"

    except Exception as e:
        logger.error(f"Error enviando comando al ESP32: {e}")
        update_esp32_status(False, str(e))
        return False, str(e)


def aplicar_modo_automatico(temperatura, humedad):
    """
    Si el modo automático está activo, controla el ventilador (relé)
    según los umbrales de temperatura configurados.
    """
    with app.app_context():
        config = SistemaConfig.query.get(1)
        if config is None or not config.modo_automatico:
            return

        relay_id = config.relay_automatico
        if relay_id not in [1, 2]:
            return

        current = relay_states.get(relay_id, False)
        desired = None

        if temperatura > config.temperatura_max:
            desired = True
        elif temperatura < config.temperatura_min:
            desired = False
        # Dentro del rango: mantener el estado actual

        if desired is not None and desired != current:
            logger.info(f"Modo automático: {'encender' if desired else 'apagar'} relé {relay_id} "
                        f"(T={temperatura}°C, umbral {config.temperatura_min}-{config.temperatura_max}°C)")
            send_relay_command(relay_id, 'on' if desired else 'off', origen='automatico')


def sincronizar_reles(respuesta):
    """Parsea la respuesta ESTADO del ESP32 ('R1:1,R2:0') y actualiza el estado
    local + RelayEvent solo cuando el estado reportado difiere del conocido.
    Así los cambios hechos desde el Oplà quedan reflejados en el dashboard."""
    global relay_states
    if not respuesta or "R1:" not in respuesta:
        logger.warning(f"Sincronización de relés: respuesta inesperada: {respuesta}")
        return False

    reportado = {}
    for parte in respuesta.split(','):
        parte = parte.strip()
        if parte.startswith('R1:') or parte.startswith('R2:'):
            try:
                reportado[int(parte[1])] = parte[3] == '1'
            except (ValueError, IndexError):
                continue

    if len(reportado) != 2:
        logger.warning(f"Sincronización de relés: no se pudo parsear: {respuesta}")
        return False

    for rele in (1, 2):
        nuevo = reportado[rele]
        if relay_states.get(rele) != nuevo:
            relay_states[rele] = nuevo
            logger.info(f"Relé {rele} detectado como {'ON' if nuevo else 'OFF'} (desde ESP32/Oplà)")
            try:
                with app.app_context():
                    event = RelayEvent(rele=rele, estado=nuevo, origen='esp32')
                    db.session.add(event)
                    db.session.commit()
            except Exception as e:
                logger.error(f"Error guardando RelayEvent de sincronización: {e}")
                db.session.rollback()
    return True


def poll_esp32_data():
    """
    Tarea programada: Consulta al ESP32 el comando DATA cada 15 segundos.
    Parsea la respuesta (T:xx.xx,H:xx.xx), guarda en BD y aplica modo automático.
    Luego consulta ESTADO para sincronizar el estado real de los relés.
    """
    global esp32_client

    try:
        logger.info("Consultando datos al ESP32...")
        response = esp32_client.send_command("DATA")
        logger.info(f"Respuesta ESP32 DATA: {response}")

        if response.startswith("T:") and ",H:" in response:
            temp_part, hum_part = response.split(",H:")
            temperatura = float(temp_part.replace("T:", ""))
            humedad = float(hum_part)

            with app.app_context():
                reading = DHT11Reading(temperatura=temperatura, humedad=humedad)
                db.session.add(reading)
                db.session.commit()
                logger.info(f"Lectura guardada: T={temperatura}°C, H={humedad}%")

            update_esp32_status(True)

            # Modo automático
            aplicar_modo_automatico(temperatura, humedad)
        else:
            logger.warning(f"Formato de respuesta inesperado: {response}")
            update_esp32_status(False, "Formato de respuesta inválido")

        # Sincronizar estado real de los relés (cambios hechos desde el Oplà)
        try:
            estado = esp32_client.send_command("ESTADO")
            logger.info(f"Respuesta ESP32 ESTADO: {estado}")
            sincronizar_reles(estado)
        except Exception as e:
            logger.error(f"Error sincronizando relés: {e}")

    except Exception as e:
        logger.error(f"Error consultando ESP32: {e}")
        update_esp32_status(False, str(e))


def get_latest_status():
    """Obtiene el estado actual del sistema para el endpoint /api/status"""
    latest_reading = DHT11Reading.query.order_by(DHT11Reading.timestamp.desc()).first()

    return {
        'esp32': esp32_status.copy(),
        'dht11': latest_reading.to_dict() if latest_reading else None,
        'relays': {1: relay_states.get(1, False), 2: relay_states.get(2, False)},
        'timestamp': datetime.utcnow().isoformat()
    }


def get_historical_data(hours=24):
    """Obtiene datos históricos de las últimas N horas"""
    since = datetime.utcnow() - timedelta(hours=hours)

    readings = DHT11Reading.query.filter(
        DHT11Reading.timestamp >= since
    ).order_by(DHT11Reading.timestamp.asc()).all()

    events = RelayEvent.query.filter(
        RelayEvent.timestamp >= since
    ).order_by(RelayEvent.timestamp.asc()).all()

    return {
        'readings': [r.to_dict() for r in readings],
        'events': [e.to_dict() for e in events]
    }


def get_alertas(config=None):
    """Determina alertas según la última lectura frente a los umbrales."""
    with app.app_context():
        if config is None:
            config = SistemaConfig.query.get(1)
        lectura = DHT11Reading.query.order_by(DHT11Reading.timestamp.desc()).first()

        estado = {
            'config': config.to_dict() if config else None,
            'lectura': lectura.to_dict() if lectura else None,
            'alertas_activas': [],
            'sin_umbrales': config is None or lectura is None,
        }

        if config and lectura:
            if lectura.temperatura > config.temperatura_max:
                estado['alertas_activas'].append(
                    {'tipo': 'Temperatura alta', 'valor': lectura.temperatura,
                     'umbral': config.temperatura_max})
            if lectura.temperatura < config.temperatura_min:
                estado['alertas_activas'].append(
                    {'tipo': 'Temperatura baja', 'valor': lectura.temperatura,
                     'umbral': config.temperatura_min})
            if lectura.humedad > config.humedad_max:
                estado['alertas_activas'].append(
                    {'tipo': 'Humedad alta', 'valor': lectura.humedad,
                     'umbral': config.humedad_max})
            if lectura.humedad < config.humedad_min:
                estado['alertas_activas'].append(
                    {'tipo': 'Humedad baja', 'valor': lectura.humedad,
                     'umbral': config.humedad_min})

        return estado, lectura, config


# ============================================
# PÁGINAS (SERVIDOR)
# ============================================

@app.route('/')
def index():
    """Panel principal - requiere autenticación (cookie marcada por el frontend)."""
    if not request.cookies.get('sigma_auth'):
        return redirect(url_for('auth_login_page'))
    return render_template('index.html')


@app.route('/login')
def auth_login_page():
    return render_template('login.html')


@app.route('/register')
def auth_register_page():
    return render_template('register.html')


@app.route('/forgot')
def auth_forgot_page():
    return render_template('forgot.html')


@app.route('/reset')
def auth_reset_page():
    return render_template('reset.html')


# ============================================
# API
# ============================================

@app.route('/api/status')
@permiso_requerido('temperatura.consultar')
def api_status(usuario_actual, sesion_actual):
    """Endpoint para obtener estado actual del sistema"""
    try:
        status = get_latest_status()
        if status['esp32']['last_check']:
            status['esp32']['last_check'] = status['esp32']['last_check'].isoformat()
        status['usuario'] = {'username': usuario_actual.username, 'roles': [r.nombre for r in usuario_actual.roles]}
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error en /api/status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/historical')
@permiso_requerido('historicos.consultar')
def api_historical(usuario_actual, sesion_actual):
    """Endpoint para obtener datos históricos (últimas 24h por defecto)"""
    try:
        hours = request.args.get('hours', 24, type=int)
        hours = min(hours, 168)
        data = get_historical_data(hours)
        return jsonify(data)
    except Exception as e:
        logger.error(f"Error en /api/historical: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/relay/<int:relay_id>/<action>')
@permiso_requerido('ventilador.controlar')
def api_relay_control(usuario_actual, sesion_actual, relay_id, action):
    """Endpoint para controlar relés (ventilador): solo con permiso, validado en backend."""
    try:
        if relay_id not in [1, 2]:
            return jsonify({'success': False, 'error': 'ID de relé inválido (1 o 2)'}), 400
        if action not in ['on', 'off']:
            return jsonify({'success': False, 'error': 'Acción inválida (on u off)'}), 400

        origen = f"manual:{usuario_actual.username}"
        success, message = send_relay_command(relay_id, action, origen)

        return jsonify({
            'success': success,
            'message': message,
            'relay': relay_id,
            'action': action,
            'new_state': action == 'on'
        })
    except Exception as e:
        logger.error(f"Error en /api/relay: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/relay/state')
@autenticado
def api_relay_state(usuario_actual, sesion_actual):
    """Endpoint para consultar estado actual de los relés (sincronizado con el ESP32)"""
    return jsonify({1: relay_states.get(1, False), 2: relay_states.get(2, False)})


@app.route('/api/config', methods=['GET'])
@autenticado
def api_obtener_config(usuario_actual, sesion_actual):
    """Consulta la configuración actual (umbrales y modo automático)."""
    with app.app_context():
        config = SistemaConfig.query.get(1)
        if config is None:
            config = SistemaConfig(id=1)
            db.session.add(config)
            db.session.commit()
        return jsonify({'success': True, 'config': config.to_dict()})


@app.route('/api/config/umbrales', methods=['PUT'])
@permiso_requerido('umbrales.configurar')
def api_configurar_umbrales(usuario_actual, sesion_actual):
    """Configura los umbrales de temperatura y humedad (solo admin)."""
    datos = request.get_json(silent=True) or {}
    config = SistemaConfig.query.get(1)
    if config is None:
        config = SistemaConfig(id=1)
        db.session.add(config)

    try:
        config.temperatura_min = float(datos.get('temperatura_min', config.temperatura_min))
        config.temperatura_max = float(datos.get('temperatura_max', config.temperatura_max))
        config.humedad_min = float(datos.get('humedad_min', config.humedad_min))
        config.humedad_max = float(datos.get('humedad_max', config.humedad_max))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Los umbrales deben ser numéricos.'}), 400

    relay = datos.get('relay_automatico', config.relay_automatico)
    if relay not in (1, 2):
        return jsonify({'success': False, 'error': 'El relé automático debe ser 1 o 2.'}), 400
    config.relay_automatico = relay

    if config.temperatura_min >= config.temperatura_max:
        return jsonify({'success': False, 'error': 'La temperatura mínima debe ser menor que la máxima.'}), 400
    if config.humedad_min >= config.humedad_max:
        return jsonify({'success': False, 'error': 'La humedad mínima debe ser menor que la máxima.'}), 400

    config.actualizado_por = usuario_actual.id
    config.actualizado_en = datetime.utcnow()

    auditoria(usuario_actual.id, 'CONFIG_UMBRALES', 'exito',
              f'Tmin={config.temperatura_min} Tmax={config.temperatura_max} '
              f'Hmin={config.humedad_min} Hmax={config.humedad_max}')
    db.session.commit()

    return jsonify({'success': True, 'message': 'Umbrales actualizados.', 'config': config.to_dict()})


@app.route('/api/config/modo', methods=['PUT'])
@permiso_requerido('modo_automatico.usar')
def api_configurar_modo(usuario_actual, sesion_actual):
    """Activa/desactiva el modo automático del ventilador."""
    datos = request.get_json(silent=True) or {}
    config = SistemaConfig.query.get(1)
    if config is None:
        config = SistemaConfig(id=1)
        db.session.add(config)

    activar = bool(datos.get('activo', not config.modo_automatico))
    config.modo_automatico = activar
    config.actualizado_por = usuario_actual.id
    config.actualizado_en = datetime.utcnow()

    auditoria(usuario_actual.id,
              'MODO_AUTO_ACTIVADO' if activar else 'MODO_AUTO_DESACTIVADO',
              'exito', 'Cambio de modo automático del ventilador')
    db.session.commit()

    return jsonify({'success': True, 'message': 'Modo automático actualizado.',
                    'config': config.to_dict()})


@app.route('/api/alertas', methods=['GET'])
@permiso_requerido('alertas.consultar')
def api_alertas(usuario_actual, sesion_actual):
    """Consulta de alertas activas a partir de la última lectura y los umbrales."""
    estado, lectura, config = get_alertas()
    return jsonify({'success': True, **estado})


# ============================================
# STREAM EN TIEMPO REAL (SSE)
# ============================================

def _sse_evento(evento, datos):
    """Serializa un evento SSE (evento + data JSON)."""
    payload = json.dumps(datos, ensure_ascii=False)
    return f"event: {evento}\ndata: {payload}\n\n"


def _stream_status_payload():
    """Estado del sistema + configuración para emitir por SSE."""
    status = get_latest_status()
    if status['esp32']['last_check']:
        status['esp32']['last_check'] = status['esp32']['last_check'].isoformat()
    config = SistemaConfig.query.get(1)
    status['config'] = config.to_dict() if config else None
    return status


@app.route('/api/stream')
@autenticado
def api_stream(usuario_actual, sesion_actual):
    """
    Flujo Server-Sent Events con autenticación vía cabecera.

    Emite un evento 'status' cada 3 segundos con el estado del sistema
    (ESP32, DHT11, relés) y la configuración actual (modo automático y
    umbrales). El cliente usa fetch + ReadableStream para poder enviar
    el token JWT en la cabecera Authorization.
    """
    logger.info("SSE conectado para usuario %s", usuario_actual.username)

    def generar():
        try:
            yield _sse_evento('hola', {
                'usuario': usuario_actual.username,
                'roles': [r.nombre for r in usuario_actual.roles],
            })
            while True:
                try:
                    with app.app_context():
                        yield _sse_evento('status', _stream_status_payload())
                except GeneratorExit:
                    raise
                except Exception as e:
                    # Mantener la conexión viva aunque la BD no esté disponible
                    logger.error("SSE: error al generar estado: %s", e)
                    yield _sse_evento('error', {
                        'message': 'No se pudo obtener el estado del sistema',
                    })
                time.sleep(3)
        except GeneratorExit:
            logger.info("SSE desconectado para usuario %s", usuario_actual.username)

    return Response(
        stream_with_context(generar()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


@app.route('/health')
def health_check():
    """Endpoint de health check para monitoreo (público)"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'esp32_connected': esp32_status['connected']
    })


# ============================================
# ERRORS
# ============================================

@app.errorhandler(401)
def no_autorizado(error):
    return jsonify({'error': 'No autorizado'}), 401


@app.errorhandler(403)
def acceso_denegado(error):
    return jsonify({'error': 'Acceso denegado: permisos insuficientes'}), 403


# ============================================
# INICIALIZACIÓN Y TAREAS PROGRAMADAS
# ============================================

def init_database():
    """Inicializa la base de datos: tablas, roles, permisos, admin y configuración."""
    with app.app_context():
        db.create_all()
        seed_roles_permisos()
        seed_admin()
        seed_config()
        logger.info("Base de datos inicializada - tablas y datos base creados")


def sincronizar_reles_rapido():
    """Tarea programada: consulta ESTADO cada 5s para que el dashboard
    detecte al instante los cambios de relé hechos desde el Oplà."""
    global esp32_client
    try:
        if not esp32_client.test_connection():
            return
        estado = esp32_client.send_command("ESTADO")
        sincronizar_reles(estado)
    except Exception as e:
        logger.debug(f"Sync rápido relés: {e}")


def init_scheduler():
    """Inicializa el programador de tareas en segundo plano"""
    scheduler = BackgroundScheduler(daemon=True)

    scheduler.add_job(
        func=poll_esp32_data,
        trigger='interval',
        seconds=15,
        id='poll_esp32',
        name='Consultar datos ESP32 cada 15s',
        replace_existing=True
    )

    scheduler.add_job(
        func=sincronizar_reles_rapido,
        trigger='interval',
        seconds=5,
        id='sync_reles',
        name='Sincronizar relés cada 5s',
        replace_existing=True
    )

    scheduler.add_job(
        func=lambda: update_esp32_status(esp32_client.test_connection()),
        trigger='interval',
        seconds=60,
        id='check_esp32_connection',
        name='Verificar conexión ESP32 cada 60s',
        replace_existing=True
    )

    scheduler.start()
    logger.info("Programador de tareas iniciado")
    return scheduler


def create_app():
    """Comodín: permite `flask run` y producción con gunicorn (app:create_app)."""
    init_database()
    return app


# ============================================
# PUNTO DE ENTRADA PRINCIPAL
# ============================================

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        seed_roles_permisos()
        seed_admin()
        seed_config()

    scheduler = init_scheduler()

    try:
        poll_esp32_data()
    except Exception:
        pass  # Ignorar errores en inicio

    port = int(os.getenv('PORT', 5000))

    logger.info(f"Iniciando servidor Flask en puerto {port}")
    logger.info(f"Dashboard disponible en: http://localhost:{port}")
    logger.info(f"API Status: http://localhost:{port}/api/status")
    logger.info(f"API Historical: http://localhost:{port}/api/historical")

    try:
        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)
    except KeyboardInterrupt:
        logger.info("Deteniendo servidor...")
        scheduler.shutdown()