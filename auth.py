"""
Autenticación y gestión de usuarios.

Implementa:
  * Registro, login, logout y perfil de usuario.
  * Cambio y recuperación de contraseña (tokens de un solo uso con expiración).
  * Sesiones JWT validables/revocables individualmente.
  * Control de acceso por roles y permisos (validado en el backend).
  * Rate limiting para login (anti fuerza bruta).
  * Auditoría de acciones importantes.
  * Envío de correo preparado para proveedor externo vía variables de entorno.

Seguridad:
  * Hashing de contraseñas con bcrypt (nunca en claro).
  * El JWT NO contiene información sensible (solo sub + jti + tiempos).
  * Secretos únicamente en variables de entorno.
"""

import hashlib
import logging
import os
import re
import secrets
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

import jwt
from flask import Blueprint, jsonify, request

from database import db
from models import (
    Auditoria,
    Permiso,
    Rol,
    RolePermiso,
    Sesion,
    SistemaConfig,
    TokenRecuperacion,
    Usuario,
    UsuarioRol,
)

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')

# ============================================
# CONFIGURACIÓN DESDE VARIABLES DE ENTORNO
# ============================================

def _env(key, default=None):
    return os.getenv(key, default)


JWT_SECRET = _env('JWT_SECRET_KEY', _env('SECRET_KEY', 'dev-secret-key'))
JWT_EXPIRACION_MINUTOS = int(_env('JWT_EXPIRACION_MINUTOS', '480'))  # 8 horas
BASE_URL = _env('BASE_URL', 'http://localhost:5000')


# ============================================
# VALIDACIONES DE ENTRADA
# ============================================

REGEX_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def validar_email(email):
    return bool(REGEX_EMAIL.match(email or ''))


def validar_password(password):
    """Contraseña segura: mínimo 8 caracteres, al menos una letra y un número."""
    if not password or len(password) < 8:
        return False
    if not re.search(r'[A-Za-z]', password):
        return False
    if not re.search(r'\d', password):
        return False
    return True


def sanitizar(texto, max_len=255):
    """Elimina espacios en blanco iniciales/finales y limita longitud."""
    if texto is None:
        return ''
    return str(texto).strip()[:max_len]


def obtener_ip():
    return sanitizar(request.remote_addr or '', 64)


def obtener_user_agent():
    return sanitizar(request.headers.get('User-Agent', ''), 255)


# ============================================
# AUDITORÍA
# ============================================

def auditoria(usuario_id, accion, resultado='exito', detalle='',
              ip=None, user_agent=None):
    """Añade una fila de auditoría (el commit lo realiza la ruta que la invoca)."""
    db.session.add(Auditoria(
        usuario_id=usuario_id,
        accion=accion,
        detalle=sanitizar(detalle, 500),
        resultado=resultado,
        ip=ip if ip is not None else obtener_ip(),
        user_agent=user_agent if user_agent is not None else obtener_user_agent(),
    ))


# ============================================
# RATE LIMITING (anti fuerza bruta)
# ============================================

_login_attempts = {}  # clave -> [timestamps]
_MAX_ATTEMPTS = 5
_WINDOW = 900  # segundos (15 min)


def limpiar_rate_limit():
    """Elimina entradas antiguas para no crecer indefinidamente."""
    ahora = time.time()
    expiradas = [k for k, v in _login_attempts.items() if v and (ahora - v[-1]) > _WINDOW * 2]
    for k in expiradas:
        _login_attempts.pop(k, None)


def rate_limit_permitido(clave, max_attempts=_MAX_ATTEMPTS, window=_WINDOW):
    """Verifica y registra un intento. Devuelve (permitido, segundos_restantes)."""
    ahora = time.time()
    timestamps = _login_attempts.setdefault(clave, [])
    timestamps[:] = [t for t in timestamps if (ahora - t) < window]
    if len(timestamps) >= max_attempts:
        return False, int(window - (ahora - timestamps[0]) + 1)
    timestamps.append(ahora)
    return True, 0


def _limpiar_y_filtrar(clave):
    """Limpia entradas caducadas de una clave y devuelve las vigentes."""
    ahora = time.time()
    timestamps = _login_attempts.setdefault(clave, [])
    timestamps[:] = [t for t in timestamps if (ahora - t) < _WINDOW]
    return timestamps


def login_rate_limit_permitido(identificador):
    """Comprueba (sin registrar) el límite por IP e identificador.

    Solo cuentan intentos FALLIDOS: los registra login_rate_limit_registrar.
    """
    ip = obtener_ip()
    for clave in (f'ip:{ip}', f'id:{identificador.lower()}'):
        timestamps = _limpiar_y_filtrar(clave)
        if len(timestamps) >= _MAX_ATTEMPTS:
            restante = int(_WINDOW - (time.time() - timestamps[0]) + 1)
            return False, restante
    return True, 0


def login_rate_limit_registrar(identificador):
    """Registra un intento FALLIDO en los contadores por IP e identificador."""
    ip = obtener_ip()
    for clave in (f'ip:{ip}', f'id:{identificador.lower()}'):
        _login_attempts.setdefault(clave, []).append(time.time())


# ============================================
# JWT
# ============================================

def crear_token(usuario, sesion):
    """Crea un JWT con reclamaciones mínimas (sin datos sensibles)."""
    ahora = datetime.utcnow()
    expiracion = ahora + timedelta(minutes=JWT_EXPIRACION_MINUTOS)
    payload = {
        'sub': str(usuario.id),
        'jti': sesion.jti,
        'iat': ahora,
        'nbf': ahora,
        'exp': expiracion,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm='HS256')
    return token


def decodificar_token(token):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except jwt.PyJWTError:
        return None


def _actual_usuario():
    """Devuelve (usuario, sesion) si el token + sesión son válidos, si no None."""
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        return None

    payload = decodificar_token(header[7:])
    if not payload:
        return None

    jti, sub = payload.get('jti'), payload.get('sub')
    if not jti or not sub:
        return None

    sesion = Sesion.query.filter_by(jti=jti, revocada=False).first()
    if sesion is None:
        return None

    if sesion.fecha_expiracion < datetime.utcnow():
        sesion.revocada = True
        sesion.fecha_revocacion = datetime.utcnow()
        db.session.commit()
        return None

    try:
        usuario_id = int(sub)
    except (TypeError, ValueError):
        return None

    usuario = db.session.get(Usuario, usuario_id)
    if usuario is None or not usuario.activo:
        return None

    return usuario, sesion


def autenticado(f):
    """Requiere un token JWT válido. Inyecta usuario_actual y sesion_actual."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        res = _actual_usuario()
        if res is None:
            return jsonify({'error': 'No autorizado'}), 401
        usuario, sesion = res
        kwargs.setdefault('usuario_actual', usuario)
        kwargs.setdefault('sesion_actual', sesion)
        return f(*args, **kwargs)
    return wrapper


def permiso_requerido(nombre_permiso):
    """Requiere autenticación y el permiso indicado (validado en el backend)."""
    def decorator(f):
        @wraps(f)
        @autenticado
        def wrapper(*args, **kwargs):
            usuario = kwargs.get('usuario_actual')
            if nombre_permiso not in usuario.permisos():
                return jsonify({'error': 'Acceso denegado: permisos insuficientes'}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ============================================
# CORREO ELECTRÓNICO (proveedor vía entorno)
# ============================================

def configuracion_correo():
    return {
        'host': _env('SMTP_HOST'),
        'port': int(_env('SMTP_PORT', '465')),
        'user': _env('SMTP_USER'),
        'password': _env('SMTP_PASSWORD'),
        'from': _env('MAIL_FROM', _env('SMTP_USER', 'noreply@sigma-iot.local')),
        'nombre': _env('MAIL_FROM_NAME', 'SIGMA-IOT'),
        'ssl': _env('SMTP_SSL', '1') == '1',
    }


def enviar_correo(destinatario, asunto, html):
    """Envía un correo SMTP. Si no hay proveedor configurado, lo registra en logs."""
    cfg = configuracion_correo()
    if not cfg['host'] or not cfg['user']:
        logger.warning(
            "[SMTP no configurado] Se simula envío a %s. Asunto: %s. Para activar, "
            "configura SMTP_HOST/SMTP_USER/SMTP_PASSWORD en .env. Detalle: %s",
            destinatario, asunto, html,
        )
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = asunto
    msg['From'] = f"{cfg['nombre']} <{cfg['from']}>"
    msg['To'] = destinatario
    msg.attach(MIMEText(html, 'html'))

    try:
        if cfg['ssl']:
            server = smtplib.SMTP_SSL(cfg['host'], cfg['port'], timeout=15)
        else:
            server = smtplib.SMTP(cfg['host'], cfg['port'], timeout=15)
            server.starttls()
        server.login(cfg['user'], cfg['password'])
        server.sendmail(cfg['from'], [destinatario], msg.as_string())
        server.quit()
        logger.info("Correo enviado a %s", destinatario)
        return True
    except Exception as e:
        logger.error("Error enviando correo a %s: %s", destinatario, e)
        return False


# ============================================
# RECUPERACIÓN DE CONTRASEÑA
# ============================================

def hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def generar_token_recuperacion(usuario):
    """Genera un token seguro de un solo uso, almacenado con hash."""
    # Invalidar tokens previos no usados del usuario
    TokenRecuperacion.query.filter_by(
        usuario_id=usuario.id, usado=False
    ).update({'usado': True, 'fecha_uso': datetime.utcnow()})

    token = secrets.token_urlsafe(32)
    db.session.add(TokenRecuperacion(
        usuario_id=usuario.id,
        token_hash=hash_token(token),
        fecha_creacion=datetime.utcnow(),
        fecha_expiracion=datetime.utcnow() + timedelta(minutes=15),
    ))
    return token


# ============================================
# SIEMBRA DE ROLES, PERMISOS Y ADMINISTRADOR INICIAL
# ============================================

# Definición central de permisos y su asignación a roles.
PERMISOS = {
    'usuarios.gestionar': 'Crear, editar y eliminar usuarios',
    'dispositivos.gestionar': 'Gestionar dispositivos (relés y Oplàs)',
    'dispositivos.ver': 'Consultar los Oplàs registrados (jardín y laboratorios)',
    'umbrales.configurar': 'Configurar umbrales de temperatura y humedad',
    'ventilador.controlar': 'Encender y apagar el ventilador',
    'modo_automatico.usar': 'Activar y desactivar el modo automático',
    'temperatura.consultar': 'Consultar temperatura',
    'humedad.consultar': 'Consultar humedad',
    'historicos.consultar': 'Consultar históricos',
    'alertas.consultar': 'Consultar alertas',
    'logs.consultar': 'Consultar logs y auditoría',
}

ROLES = {
    'ADMINISTRADOR': {
        'descripcion': 'Acceso total al sistema: usuarios, dispositivos, '
                       'umbrales, control del ventilador, históricos, alertas y logs.',
        'permisos': list(PERMISOS.keys()),
    },
    'OPERADOR': {
        'descripcion': 'Opera el sistema: consulta sensores, históricos, alertas, '
                       'controla el ventilador y usa el modo automático.',
        'permisos': [
            'ventilador.controlar',
            'modo_automatico.usar',
            'temperatura.consultar',
            'humedad.consultar',
            'historicos.consultar',
            'alertas.consultar',
            'dispositivos.ver',
        ],
    },
}


def seed_roles_permisos():
    """Crea los permisos y roles base. Idempotente."""
    permisos_db = {}
    for nombre, descripcion in PERMISOS.items():
        permiso = Permiso.query.filter_by(nombre=nombre).first()
        if permiso is None:
            permiso = Permiso(nombre=nombre, descripcion=descripcion)
            db.session.add(permiso)
        permisos_db[nombre] = permiso

    db.session.flush()

    for nombre, info in ROLES.items():
        rol = Rol.query.filter_by(nombre=nombre).first()
        if rol is None:
            rol = Rol(nombre=nombre, descripcion=info['descripcion'])
            db.session.add(rol)
            db.session.flush()
        # Sincronizar el set de permisos del rol (creado o existente)
        rol.permisos = [permisos_db[p] for p in info['permisos']]

    db.session.commit()


def seed_admin():
    """Crea el usuario administrador inicial desde variables de entorno."""
    username = _env('ADMIN_USER', 'admin')
    if Usuario.query.filter_by(username=username).first():
        return

    email = _env('ADMIN_EMAIL', 'admin@sigma-iot.local')
    password = _env('ADMIN_PASSWORD', None)
    if not password:
        password = secrets.token_urlsafe(12)
        logger.warning("ADMIN_PASSWORD no configurado. Admin inicial con contraseña temporal: %s", password)

    usuario = Usuario(
        nombre=sanitizar(_env('ADMIN_NOMBRE', 'Administrador'), 100),
        apellido=sanitizar(_env('ADMIN_APELLIDO', 'Sistema'), 100),
        email=email,
        username=username,
        activo=True,
    )
    usuario.set_password(password)
    db.session.add(usuario)
    db.session.flush()

    rol_admin = Rol.query.filter_by(nombre='ADMINISTRADOR').first()
    if rol_admin:
        db.session.add(UsuarioRol(usuario_id=usuario.id, rol_id=rol_admin.id))

    db.session.commit()
    logger.info("Usuario administrador '%s' creado.", username)


def seed_config():
    """Garantiza la fila única de configuración del sistema."""
    config = SistemaConfig.query.get(1)
    if config is None:
        db.session.add(SistemaConfig(id=1))
        db.session.commit()


# ============================================
# RUTAS PÚBLICAS
# ============================================

@auth_bp.route('/registro', methods=['POST'])
def registrar():
    """Registro de nuevos usuarios (rol OPERADOR por defecto)."""
    limpiar_rate_limit()
    permitido, _ = rate_limit_permitido(f'registro:{obtener_ip()}', max_attempts=20, window=3600)
    if not permitido:
        return jsonify({'error': 'Demasiados intentos. Intente más tarde.'}), 429

    datos = request.get_json(silent=True) or {}
    nombre = sanitizar(datos.get('nombre'), 100)
    apellido = sanitizar(datos.get('apellido'), 100)
    email = sanitizar(datos.get('email'), 255)
    username = sanitizar(datos.get('username'), 50)
    password = datos.get('password') or ''
    confirmacion = datos.get('confirmacion') or ''

    errores = []
    if not nombre:
        errores.append('El nombre es obligatorio.')
    if not apellido:
        errores.append('El apellido es obligatorio.')
    if not username or len(username) < 3:
        errores.append('El nombre de usuario debe tener al menos 3 caracteres.')
    if not validar_email(email):
        errores.append('El formato del correo electrónico no es válido.')
    if not validar_password(password):
        errores.append('La contraseña debe tener al menos 8 caracteres, una letra y un número.')
    if password != confirmacion:
        errores.append('La confirmación de contraseña no coincide.')

    if not errores:
        if Usuario.query.filter_by(email=email).first():
            errores.append('El correo electrónico ya está registrado.')
        if Usuario.query.filter_by(username=username).first():
            errores.append('El nombre de usuario ya está en uso.')

    if errores:
        return jsonify({'success': False, 'errors': errores}), 400

    usuario = Usuario(nombre=nombre, apellido=apellido,
                      email=email, username=username, activo=True)
    usuario.set_password(password)
    db.session.add(usuario)
    db.session.flush()

    rol_operador = Rol.query.filter_by(nombre='OPERADOR').first()
    if rol_operador:
        db.session.add(UsuarioRol(usuario_id=usuario.id, rol_id=rol_operador.id))

    auditoria(usuario.id, 'REGISTRO_USUARIO', 'exito', 'Registro de cuenta')
    db.session.commit()

    return jsonify({'success': True, 'message': 'Cuenta creada correctamente. Ya puedes iniciar sesión.'}), 201


@auth_bp.route('/login', methods=['POST'])
def login():
    """Inicio de sesión. Emite JWT y registra la sesión en BD."""
    limpiar_rate_limit()
    datos = request.get_json(silent=True) or {}
    identificador = sanitizar(datos.get('identificador'))
    password = datos.get('password') or ''

    if not identificador or not password:
        auditoria(None, 'LOGIN', 'fallido', 'Credenciales ausentes')
        db.session.commit()
        return jsonify({'success': False, 'error': 'Credenciales incorrectas'}), 401

    permitido, restante = login_rate_limit_permitido(identificador)
    if not permitido:
        auditoria(None, 'LOGIN', 'fallido', f'Bloqueado por límite de intentos para {identificador}')
        db.session.commit()
        return jsonify({'success': False, 'error': f'Demasiados intentos fallidos. Intente en {restante} segundos.'}), 429

    # Buscar por correo o nombre de usuario
    usuario = Usuario.query.filter(
        (Usuario.email == identificador) | (Usuario.username == identificador)
    ).first()

    # Respuesta genérica: no revelar si el usuario existe
    if usuario is None or not usuario.check_password(password):
        login_rate_limit_registrar(identificador)
        auditoria(usuario.id if usuario else None, 'LOGIN', 'fallido',
                  'Credenciales incorrectas', ip=obtener_ip())
        db.session.commit()
        return jsonify({'success': False, 'error': 'Credenciales incorrectas'}), 401

    if not usuario.activo:
        auditoria(usuario.id, 'LOGIN', 'fallido', 'Cuenta desactivada')
        db.session.commit()
        return jsonify({'success': False, 'error': 'Credenciales incorrectas'}), 401

    # Crear sesión + JWT
    jti = secrets.token_hex(16)
    expiracion = datetime.utcnow() + timedelta(minutes=JWT_EXPIRACION_MINUTOS)
    sesion = Sesion(
        jti=jti,
        usuario_id=usuario.id,
        fecha_expiracion=expiracion,
        ip=obtener_ip(),
        user_agent=obtener_user_agent(),
    )
    db.session.add(sesion)
    usuario.ultimo_acceso = datetime.utcnow()

    auditoria(usuario.id, 'LOGIN', 'exito', 'Inicio de sesión')
    db.session.commit()

    token = crear_token(usuario, sesion)
    return jsonify({
        'success': True,
        'token': token,
        'expires_in': JWT_EXPIRACION_MINUTOS * 60,
        'usuario': usuario.to_dict(),
    })


@auth_bp.route('/recuperar', methods=['POST'])
def solicitar_recuperacion():
    """Solicita recuperación de contraseña por correo. No revela si el correo existe."""
    limpiar_rate_limit()
    datos = request.get_json(silent=True) or {}
    email = sanitizar(datos.get('email'), 255)

    if not validar_email(email):
        return jsonify({'success': False, 'error': 'Formato de correo no válido.'}), 400

    permitido, _ = rate_limit_permitido(f'recuperar:{obtener_ip()}', max_attempts=5, window=900)
    if not permitido:
        return jsonify({'success': False, 'error': 'Demasiadas solicitudes. Intente más tarde.'}), 429

    usuario = Usuario.query.filter_by(email=email).first()
    if usuario is not None and usuario.activo:
        token = generar_token_recuperacion(usuario)
        db.session.flush()
        db.session.commit()

        enlace = f"{BASE_URL}/reset?token={token}"
        cuerpo = (
            f"<p>Hola <b>{usuario.nombre}</b>,</p>"
            f"<p>Recibimos una solicitud para restablecer tu contraseña.</p>"
            f"<p>Este enlace es de <b>un solo uso</b> y expira en <b>15 minutos</b>:</p>"
            f"<p><a href=\"{enlace}\">{enlace}</a></p>"
            f"<p>Si no solicitaste este cambio, ignora este correo.</p>"
        )
        enviar_correo(usuario.email, 'Recuperación de contraseña - SIGMA-IOT', cuerpo)
        auditoria(usuario.id, 'RECUPERACION_CONTRASENA', 'exito', 'Token de recuperación generado')
        db.session.commit()

    # Respuesta genérica en ambos casos
    return jsonify({'success': True,
                    'message': 'Si el correo está registrado, recibirás un enlace para restablecer tu contraseña.'})


@auth_bp.route('/recuperar/confirmar', methods=['POST'])
def confirmar_recuperacion():
    """Confirma la recuperación con el token y establece la nueva contraseña."""
    limpiar_rate_limit()
    datos = request.get_json(silent=True) or {}
    token = sanitizar(datos.get('token'))
    password = datos.get('password') or ''

    if not token:
        return jsonify({'success': False, 'error': 'Token requerido.'}), 400
    if not validar_password(password):
        return jsonify({'success': False, 'error': 'La contraseña debe tener al menos 8 caracteres, una letra y un número.'}), 400

    registro = TokenRecuperacion.query.filter_by(token_hash=hash_token(token)).first()

    if registro is None or registro.usado or registro.fecha_expiracion < datetime.utcnow():
        return jsonify({'success': False, 'error': 'El enlace no es válido o ya fue utilizado.'}), 400

    usuario = db.session.get(Usuario, registro.usuario_id)
    if usuario is None or not usuario.activo:
        return jsonify({'success': False, 'error': 'El enlace no es válido o ya fue utilizado.'}), 400

    # Marcar token como usado (un solo uso)
    registro.usado = True
    registro.fecha_uso = datetime.utcnow()

    usuario.set_password(password)

    # Invalidar todas las sesiones tras el restablecimiento
    Sesion.query.filter_by(usuario_id=usuario.id).update(
        {'revocada': True, 'fecha_revocacion': datetime.utcnow()}
    )

    auditoria(usuario.id, 'RECUPERACION_CONTRASENA', 'exito', 'Contraseña restablecida')
    db.session.commit()

    return jsonify({'success': True, 'message': 'Contraseña actualizada. Ya puedes iniciar sesión.'})


# ============================================
# RUTAS AUTENTICADAS (usuario)
# ============================================

@auth_bp.route('/me', methods=['GET'])
@autenticado
def perfil(usuario_actual, sesion_actual):
    """Consulta el perfil del usuario autenticado."""
    return jsonify({'success': True, 'usuario': usuario_actual.to_dict()})


@auth_bp.route('/me', methods=['PUT'])
@autenticado
def actualizar_perfil(usuario_actual, sesion_actual):
    """Modifica nombre, apellido y correo de su propio perfil."""
    datos = request.get_json(silent=True) or {}
    nombre = sanitizar(datos.get('nombre'), 100)
    apellido = sanitizar(datos.get('apellido'), 100)
    email = sanitizar(datos.get('email'), 255)
    username = sanitizar(datos.get('username'), 50)

    errores = []
    if not nombre:
        errores.append('El nombre es obligatorio.')
    if not apellido:
        errores.append('El apellido es obligatorio.')
    if not validar_email(email):
        errores.append('Formato de correo no válido.')
    if not username or len(username) < 3:
        errores.append('El nombre de usuario debe tener al menos 3 caracteres.')

    actual = Usuario.query.filter(Usuario.email == email, Usuario.id != usuario_actual.id).first()
    if actual:
        errores.append('El correo electrónico ya está registrado por otro usuario.')
    otro = Usuario.query.filter(Usuario.username == username, Usuario.id != usuario_actual.id).first()
    if otro:
        errores.append('El nombre de usuario ya está en uso.')

    if errores:
        return jsonify({'success': False, 'errors': errores}), 400

    usuario_actual.nombre = nombre
    usuario_actual.apellido = apellido
    usuario_actual.email = email
    usuario_actual.username = username

    auditoria(usuario_actual.id, 'PERFIL_ACTUALIZADO', 'exito', 'Datos de perfil modificados')
    db.session.commit()

    return jsonify({'success': True, 'usuario': usuario_actual.to_dict()})


@auth_bp.route('/cambiar-contrasena', methods=['POST'])
@autenticado
def cambiar_contrasena(usuario_actual, sesion_actual):
    """Cambio de contraseña verificando la contraseña actual."""
    datos = request.get_json(silent=True) or {}
    actual = datos.get('actual') or ''
    nueva = datos.get('nueva') or ''
    confirmacion = datos.get('confirmacion') or ''

    if not usuario_actual.check_password(actual):
        auditoria(usuario_actual.id, 'CAMBIO_CONTRASENA', 'fallido', 'Contraseña actual incorrecta')
        db.session.commit()
        return jsonify({'success': False, 'error': 'La contraseña actual no es correcta.'}), 400

    if not validar_password(nueva):
        return jsonify({'success': False,
                        'error': 'La nueva contraseña debe tener al menos 8 caracteres, una letra y un número.'}), 400

    if nueva != confirmacion:
        return jsonify({'success': False, 'error': 'La confirmación de contraseña no coincide.'}), 400

    usuario_actual.set_password(nueva)

    # Revocar el resto de sesiones (mantiene la actual)
    Sesion.query.filter(
        Sesion.usuario_id == usuario_actual.id,
        Sesion.id != sesion_actual.id,
        Sesion.revocada == False,  # noqa: E712
    ).update({'revocada': True, 'fecha_revocacion': datetime.utcnow()})

    auditoria(usuario_actual.id, 'CAMBIO_CONTRASENA', 'exito', 'Contraseña actualizada')
    db.session.commit()

    return jsonify({'success': True, 'message': 'Contraseña actualizada correctamente.'})


@auth_bp.route('/logout', methods=['POST'])
@autenticado
def logout(usuario_actual, sesion_actual):
    """Cierre de sesión: revoca la sesión actual."""
    sesion_actual.revocada = True
    sesion_actual.fecha_revocacion = datetime.utcnow()

    auditoria(usuario_actual.id, 'LOGOUT', 'exito', 'Cierre de sesión')
    db.session.commit()

    return jsonify({'success': True, 'message': 'Sesión cerrada.'})


@auth_bp.route('/sesiones', methods=['GET'])
@autenticado
def listar_sesiones(usuario_actual, sesion_actual):
    """Lista las sesiones activas del usuario autenticado (gestión de sesiones)."""
    sesiones = Sesion.query.filter_by(usuario_id=usuario_actual.id).order_by(
        Sesion.fecha_creacion.desc()
    ).all()
    return jsonify({'success': True, 'sesiones': [s.to_dict() for s in sesiones]})


@auth_bp.route('/sesiones/<int:sesion_id>', methods=['DELETE'])
@autenticado
def revocar_sesion(usuario_actual, sesion_actual, sesion_id):
    """Revoca una sesión propia (no permite revocar la sesión actual)."""
    sesion = db.session.get(Sesion, sesion_id)
    if sesion is None or sesion.usuario_id != usuario_actual.id:
        return jsonify({'success': False, 'error': 'Sesión no encontrada.'}), 404
    if sesion.id == sesion_actual.id:
        return jsonify({'success': False, 'error': 'No puedes revocar la sesión actual.'}), 400

    sesion.revocada = True
    sesion.fecha_revocacion = datetime.utcnow()
    auditoria(usuario_actual.id, 'SESION_REVOCADA', 'exito', f'Sesión {sesion_id} revocada')
    db.session.commit()

    return jsonify({'success': True, 'message': 'Sesión revocada.'})


# ============================================
# RUTAS ADMINISTRATIVAS
# ============================================

@auth_bp.route('/permisos', methods=['GET'])
@permiso_requerido('usuarios.gestionar')
def listar_permisos(usuario_actual, sesion_actual):
    """Lista todos los permisos del sistema (para el editor de roles)."""
    permisos = Permiso.query.order_by(Permiso.nombre).all()
    return jsonify({'success': True, 'permisos': [p.to_dict() for p in permisos]})


@auth_bp.route('/roles', methods=['GET'])
@permiso_requerido('usuarios.gestionar')
def listar_roles(usuario_actual, sesion_actual):
    """Lista los roles con sus permisos."""
    roles = Rol.query.order_by(Rol.nombre).all()
    return jsonify({'success': True, 'roles': [r.to_dict() for r in roles]})


@auth_bp.route('/usuarios', methods=['GET'])
@permiso_requerido('usuarios.gestionar')
def listar_usuarios(usuario_actual, sesion_actual):
    """Lista los usuarios del sistema."""
    usuarios = Usuario.query.order_by(Usuario.fecha_registro.desc()).all()
    return jsonify({'success': True, 'usuarios': [u.to_dict() for u in usuarios]})


@auth_bp.route('/usuarios', methods=['POST'])
@permiso_requerido('usuarios.gestionar')
def crear_usuario(usuario_actual, sesion_actual):
    """Crea un usuario desde el panel de administración."""
    datos = request.get_json(silent=True) or {}
    nombre = sanitizar(datos.get('nombre'), 100)
    apellido = sanitizar(datos.get('apellido'), 100)
    email = sanitizar(datos.get('email'), 255)
    username = sanitizar(datos.get('username'), 50)
    password = datos.get('password') or ''
    roles = datos.get('roles') or ['OPERADOR']

    errores = []
    if not nombre or not apellido:
        errores.append('Nombre y apellido son obligatorios.')
    if not validar_email(email):
        errores.append('Formato de correo no válido.')
    if not username or len(username) < 3:
        errores.append('El nombre de usuario debe tener al menos 3 caracteres.')
    if not validar_password(password):
        errores.append('La contraseña debe tener al menos 8 caracteres, una letra y un número.')
    if Usuario.query.filter_by(email=email).first():
        errores.append('El correo ya está registrado.')
    if Usuario.query.filter_by(username=username).first():
        errores.append('El nombre de usuario ya está en uso.')

    if errores:
        return jsonify({'success': False, 'errors': errores}), 400

    usuario = Usuario(nombre=nombre, apellido=apellido, email=email,
                      username=username, activo=True)
    usuario.set_password(password)
    db.session.add(usuario)
    db.session.flush()

    for nombre_rol in roles:
        rol = Rol.query.filter_by(nombre=nombre_rol).first()
        if rol:
            db.session.add(UsuarioRol(usuario_id=usuario.id, rol_id=rol.id))

    auditoria(usuario_actual.id, 'USUARIO_CREADO', 'exito', f'Se creó el usuario {username}')
    db.session.commit()

    return jsonify({'success': True, 'usuario': usuario.to_dict()}), 201


@auth_bp.route('/usuarios/<int:usuario_id>', methods=['PUT'])
@permiso_requerido('usuarios.gestionar')
def actualizar_usuario(usuario_actual, sesion_actual, usuario_id):
    """Actualiza datos, roles y estado de un usuario."""
    usuario = db.session.get(Usuario, usuario_id)
    if usuario is None:
        return jsonify({'success': False, 'error': 'Usuario no encontrado.'}), 404

    datos = request.get_json(silent=True) or {}
    nombre = sanitizar(datos.get('nombre'), 100)
    apellido = sanitizar(datos.get('apellido'), 100)
    email = sanitizar(datos.get('email'), 255)
    activo = datos.get('activo', usuario.activo)

    errores = []
    if not nombre or not apellido:
        errores.append('Nombre y apellido son obligatorios.')
    if not validar_email(email):
        errores.append('Formato de correo no válido.')
    if Usuario.query.filter(Usuario.email == email, Usuario.id != usuario_id).first():
        errores.append('El correo ya está registrado por otro usuario.')

    if errores:
        return jsonify({'success': False, 'errors': errores}), 400

    usuario.nombre = nombre
    usuario.apellido = apellido
    usuario.email = email
    usuario.activo = bool(activo)

    # Prevenir auto-desactivación / auto-baja del último administrador
    if usuario.id == usuario_actual.id and not usuario.activo:
        return jsonify({'success': False, 'error': 'No puedes desactivar tu propia cuenta.'}), 400

    # Actualizar roles
    if 'roles' in datos and isinstance(datos['roles'], list):
        if admin_loses_last(usuario, usuario_actual, datos['roles']):
            return jsonify({'success': False,
                            'error': 'No puedes quitar ADMINISTRADOR al último administrador activo.'}), 400
        usuario.roles = [Rol.query.filter_by(nombre=n).first() for n in datos['roles']]
        usuario.roles = [r for r in usuario.roles if r is not None]

    auditoria(usuario_actual.id, 'USUARIO_ACTUALIZADO', 'exito',
              f'Se actualizó el usuario {usuario.username}')
    db.session.commit()

    return jsonify({'success': True, 'usuario': usuario.to_dict()})


@auth_bp.route('/usuarios/<int:usuario_id>', methods=['DELETE'])
@permiso_requerido('usuarios.gestionar')
def eliminar_usuario(usuario_actual, sesion_actual, usuario_id):
    """Elimina un usuario (no permite eliminarse a sí mismo)."""
    usuario = db.session.get(Usuario, usuario_id)
    if usuario is None:
        return jsonify({'success': False, 'error': 'Usuario no encontrado.'}), 404

    if usuario.id == usuario_actual.id:
        return jsonify({'success': False, 'error': 'No puedes eliminar tu propia cuenta.'}), 400

    auditoria(usuario_actual.id, 'USUARIO_ELIMINADO', 'exito',
              f'Se eliminó el usuario {usuario.username}')
    db.session.delete(usuario)
    db.session.commit()

    return jsonify({'success': True, 'message': 'Usuario eliminado.'})


@auth_bp.route('/logs', methods=['GET'])
@permiso_requerido('logs.consultar')
def listar_logs(usuario_actual, sesion_actual):
    """Consulta de auditoría con filtros (usuario admin)."""
    accion = request.args.get('accion')
    resultado = request.args.get('resultado')
    limite = min(request.args.get('limit', 50, type=int), 500)

    query = Auditoria.query.order_by(Auditoria.fecha.desc())
    if accion:
        query = query.filter(Auditoria.accion.like(f'%{accion}%'))
    if resultado in ('exito', 'fallido'):
        query = query.filter(Auditoria.resultado == resultado)

    logs = query.limit(limite).all()
    return jsonify({'success': True, 'logs': [l.to_dict() for l in logs]})


def admin_loses_last(usuario, usuario_actual, nuevos_roles):
    """True si 'usuario' perdería el rol ADMINISTRADOR siendo el último admin activo."""
    if 'ADMINISTRADOR' in [r.nombre for r in usuario.roles] and \
       'ADMINISTRADOR' not in nuevos_roles:
        admin_activos = Usuario.query.join(UsuarioRol).join(Rol).filter(
            Rol.nombre == 'ADMINISTRADOR', Usuario.activo == True  # noqa: E712
        ).count()
        if admin_activos <= 1:
            return True
    return False