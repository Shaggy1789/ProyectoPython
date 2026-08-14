#!/usr/bin/env python3
"""
tests/test_auth.py - Suite de pruebas de autenticación y gestión de usuarios.

Cubre: registro, login, JWT, control de acceso por roles/permisos, perfil,
cambio y recuperación de contraseña, sesiones, rate limiting y auditoría.

Usa SQLite local (archivo temporal) para no depender de la base de datos
remota. Si ya tienes PostgreSQL configurada y no quieres SQLite, borra la
variable DATABASE_URL antes de ejecutar (se usará la del .env).

Uso:
  python tests/test_auth.py
"""

import hashlib
import os
import re
import sys
import tempfile

# ---------------------------------------------------------------------------
# Usar SQLite aislado ANTES de importar la aplicación
# ---------------------------------------------------------------------------
_TMP_DB = os.path.join(tempfile.gettempdir(), 'sigma_test_auth.db')
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ['DATABASE_URL'] = f'sqlite:///{_TMP_DB}'
os.environ['TESTING'] = '1'

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from database import db  # noqa: E402
from models import Auditoria, Sesion, TokenRecuperacion, Usuario  # noqa: E402
from auth import seed_admin, seed_roles_permisos, seed_config  # noqa: E402

results = []


def check(nombre, ok, detalle=''):
    results.append((nombre, ok))
    print(('PASS ' if ok else 'FAIL ') + nombre + (' | ' + str(detalle) if detalle else ''))


def setup_db():
    with app.app_context():
        db.create_all()
        seed_roles_permisos()
        seed_admin()
        seed_config()


# ---------------------------------------------------------------------------
# Captura de correos simulando el proveedor SMTP
# ---------------------------------------------------------------------------
_enlaces = {}

_original_enviar = None


def _fake_enviar(destinatario, asunto, html):
    m = re.search(r'https?://[^\s"<]+', html)
    if m:
        _enlaces[destinatario] = m.group(0)
    return True


def main():
    global _original_enviar
    setup_db()

    # Interceptar envío de correo (recuperación)
    import auth as auth_mod
    _original_enviar = auth_mod.enviar_correo
    auth_mod.enviar_correo = _fake_enviar

    client = app.test_client()
    admin_headers = None

    # ============ REGISTRO ============
    r = client.post('/api/auth/registro', json={
        'nombre': 'Juan', 'apellido': 'Perez', 'email': 'juan@test.local',
        'username': 'juan', 'password': 'clave1234', 'confirmacion': 'clave1234'})
    check('registro operador', r.status_code == 201)

    r = client.post('/api/auth/registro', json={
        'nombre': 'Juan', 'apellido': 'Perez', 'email': 'juan@test.local',
        'username': 'juan2', 'password': 'clave1234', 'confirmacion': 'clave1234'})
    check('registro duplicado email rechazado', r.status_code == 400)

    r = client.post('/api/auth/registro', json={
        'nombre': 'Juan', 'apellido': 'Perez', 'email': 'juan2@test.local',
        'username': 'juan', 'password': 'clave1234', 'confirmacion': 'clave1234'})
    check('registro duplicado usuario rechazado', r.status_code == 400)

    r = client.post('/api/auth/registro', json={
        'nombre': 'A', 'apellido': 'B', 'email': 'bad-email',
        'username': 'userextralargo', 'password': 'corta1',
        'confirmacion': 'corta1'})
    check('registro invalido (correo/contraseña) rechazado', r.status_code == 400)

    r = client.post('/api/auth/registro', json={
        'nombre': 'Ana', 'apellido': 'Lopez', 'email': 'ana@test.local',
        'username': 'ana', 'password': 'clave1234', 'confirmacion': 'otracosa'})
    check('registro confirmación incorrecta rechazado', r.status_code == 400)

    r = client.post('/api/auth/registro', json={
        'nombre': 'Ana', 'apellido': 'Lopez', 'email': 'ana@test.local',
        'username': 'ana', 'password': 'clave1234', 'confirmacion': 'clave1234'})
    check('registro ana válido', r.status_code == 201)

    # Contraseña almacenada con hash (nunca en claro)
    with app.app_context():
        juan = Usuario.query.filter_by(username='juan').first()
        check('contraseña NO en texto plano', juan is not None
              and juan.password_hash != 'clave1234'
              and juan.password_hash.startswith('$2'))

    # ============ LOGIN ============
    r = client.post('/api/auth/login', json={'identificador': 'juan', 'password': 'clave1234'})
    check('login operador', r.status_code == 200 and r.get_json().get('token'))
    op_headers = {'Authorization': 'Bearer ' + r.get_json()['token']}

    r = client.post('/api/auth/login', json={'identificador': 'juan', 'password': 'incorrecta'})
    check('login fallido (mensaje genérico)', r.status_code == 401)

    r = client.post('/api/auth/login', json={'identificador': 'noexiste', 'password': 'incorrecta'})
    check('login inexistente (mismo mensaje)', r.status_code == 401
          and 'credenciales' in r.get_json()['error'].lower())

    with app.app_context():
        n_log_fallidos = Auditoria.query.filter_by(accion='LOGIN', resultado='fallido').count()
    check('login fallido auditado', n_log_fallidos >= 2)

    # ============ PROTECCIÓN JWT ============
    check('sin token -> 401', client.get('/api/status').status_code == 401)
    check('token inválido -> 401',
          client.get('/api/status', headers={'Authorization': 'Bearer abc'}).status_code == 401)
    check('operador puede ver status (permiso temperatura.consultar)',
          client.get('/api/status', headers=op_headers).status_code == 200)
    check('operador puede ver histórico',
          client.get('/api/historical?hours=24', headers=op_headers).status_code == 200)

    # ============ PERMISOS / ROLES ============
    check('operador NO gestiona usuarios -> 403',
          client.get('/api/auth/usuarios', headers=op_headers).status_code == 403)
    check('operador NO configura umbrales -> 403',
          client.put('/api/config/umbrales', headers=op_headers,
                     json={'temperatura_min': 10, 'temperatura_max': 40}).status_code == 403)
    check('operador NO ve logs -> 403',
          client.get('/api/auth/logs', headers=op_headers).status_code == 403)
    check('operador NO crea usuarios -> 403',
          client.post('/api/auth/usuarios', headers=op_headers,
                      json={'nombre': 'x', 'apellido': 'y', 'email': 'x@y.z',
                            'username': 'xyz', 'password': 'clave1234'}).status_code == 403)

    # ============ ADMIN ============
    r = client.post('/api/auth/login', json={'identificador': os.getenv('ADMIN_USER', 'admin'), 'password': os.getenv('ADMIN_PASSWORD', 'Admin2026!')})
    check('login admin', r.status_code == 200)
    admin_headers = {'Authorization': 'Bearer ' + r.get_json()['token']}

    r = client.get('/api/auth/usuarios', headers=admin_headers)
    check('admin lista usuarios', r.status_code == 200 and len(r.get_json()['usuarios']) >= 2)

    r = client.put('/api/config/umbrales', headers=admin_headers,
                   json={'temperatura_min': 15, 'temperatura_max': 35,
                         'humedad_min': 30, 'humedad_max': 80, 'relay_automatico': 1})
    check('admin configura umbrales', r.status_code == 200)

    # Gestión de usuarios: actualizar rol y estado
    with app.app_context():
        ana = Usuario.query.filter_by(username='ana').first()
        ana_id = ana.id if ana else None
    if ana_id:
        r = client.put(f'/api/auth/usuarios/{ana_id}', headers=admin_headers,
                       json={'nombre': 'Ana', 'apellido': 'Lopez',
                             'email': 'ana@test.local', 'roles': ['OPERADOR', 'ADMINISTRADOR'],
                             'activo': True})
        check('admin actualiza roles de usuario', r.status_code == 200)

    # Autoeliminación / autodesactivación prevenidas
    admin_id = None
    with app.app_context():
        admin_id = Usuario.query.filter_by(username=os.getenv('ADMIN_USER', 'admin')).first().id
    r = client.put(f'/api/auth/usuarios/{admin_id}', headers=admin_headers,
                   json={'nombre': 'Administrador', 'apellido': 'Sistema',
                         'email': os.getenv('ADMIN_EMAIL', 'admin@sigma-iot.local'),
                         'activo': False})
    check('admin no puede desactivarse', r.status_code == 400)
    r = client.delete(f'/api/auth/usuarios/{admin_id}', headers=admin_headers)
    check('admin no puede eliminarse', r.status_code == 400)

    # ============ RECUPERACIÓN DE CONTRASEÑA ============
    r = client.post('/api/auth/recuperar', json={'email': 'ana@test.local'})
    check('solicitud recuperación aceptada', r.status_code == 200)
    r = client.post('/api/auth/recuperar', json={'email': 'inexistente@test.local'})
    check('respuesta genérica (no revela correo)', r.status_code == 200
          and 'recibirás' in r.get_json()['message'])

    enlace = _enlaces.get('ana@test.local')
    check('correo simulado con enlace', bool(enlace))
    token = enlace.split('token=')[-1] if enlace else ''

    r = client.post('/api/auth/recuperar/confirmar', json={'token': 'tokeninvalido', 'password': 'NuevaClave99'})
    check('confirmar con token inválido', r.status_code == 400)

    r = client.post('/api/auth/recuperar/confirmar', json={'token': token, 'password': 'NuevaClave99'})
    check('confirmar token válido', r.status_code == 200)

    r = client.post('/api/auth/recuperar/confirmar', json={'token': token, 'password': 'OtraClave88'})
    check('token de un solo uso', r.status_code == 400)

    r = client.post('/api/auth/login', json={'identificador': 'ana', 'password': 'NuevaClave99'})
    check('login con nueva contraseña', r.status_code == 200)

    # ============ SESIONES Y LOGOUT ============
    r = client.get('/api/auth/sesiones', headers=admin_headers)
    check('listar sesiones propias', r.status_code == 200 and r.get_json()['sesiones'])

    r = client.post('/api/auth/logout', headers=admin_headers)
    check('logout', r.status_code == 200)
    check('token revocado tras logout',
          client.get('/api/auth/me', headers=admin_headers).status_code == 401)

    # ============ PERFIL ============
    tok2 = client.post('/api/auth/login', json={'identificador': 'ana', 'password': 'NuevaClave99'}).get_json()['token']
    h2 = {'Authorization': 'Bearer ' + tok2}
    r = client.put('/api/auth/me', headers=h2,
                   json={'nombre': 'Ana María', 'apellido': 'Lopez Rojas',
                         'username': 'ana', 'email': 'ana@test.local'})
    check('actualizar perfil', r.status_code == 200
          and r.get_json()['usuario']['nombre'] == 'Ana María')

    r = client.post('/api/auth/cambiar-contrasena', headers=h2,
                    json={'actual': 'incorrecta', 'nueva': 'ClaveNueva7', 'confirmacion': 'ClaveNueva7'})
    check('cambio contraseña con actual incorrecta', r.status_code == 400)
    r = client.post('/api/auth/cambiar-contrasena', headers=h2,
                    json={'actual': 'NuevaClave99', 'nueva': 'ClaveNueva7', 'confirmacion': 'ClaveNueva7'})
    check('cambio contraseña correcto', r.status_code == 200)

    # ============ RATE LIMITING ============
    import auth as auth_mod2
    auth_mod2._login_attempts.clear()
    bloqueado = False
    for _ in range(7):
        r = client.post('/api/auth/login', json={'identificador': 'juan', 'password': 'malacontrasena'})
        if r.status_code == 429:
            bloqueado = True
    check('rate limiting tras intentos fallidos', bloqueado)

    # ============ AUDITORÍA ============
    r = client.post('/api/auth/login', json={'identificador': os.getenv('ADMIN_USER', 'admin'), 'password': os.getenv('ADMIN_PASSWORD', 'Admin2026!')})
    admin_headers = {'Authorization': 'Bearer ' + r.get_json()['token']}
    r = client.get('/api/auth/logs', headers=admin_headers)
    acciones = {l['accion'] for l in r.get_json()['logs']}
    check('auditoría registra LOGIN y REGISTRO',
          r.status_code == 200 and 'LOGIN' in acciones and 'REGISTRO_USUARIO' in acciones)

    # ============ RESUMEN ============
    pass_count = sum(1 for _, ok in results if ok)
    fail_count = len(results) - pass_count
    print('\n' + '=' * 55)
    print(f'RESUMEN: {pass_count}/{len(results)} correctas, {fail_count} fallidas')
    if fail_count:
        for nombre, ok in results:
            if not ok:
                print('  FAIL:', nombre)
        sys.exit(1)
    print('TODAS LAS PRUEBAS PASARON')


if __name__ == '__main__':
    try:
        main()
    finally:
        if _original_enviar is not None:
            import auth as auth_final
            auth_final.enviar_correo = _original_enviar
        try:
            os.remove(_TMP_DB)
        except OSError:
            pass