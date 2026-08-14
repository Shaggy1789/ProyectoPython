"""
Modelos de base de datos del sistema.

Incluye:
  * Tablas existentes del jardín inteligente (DHT11Reading, RelayEvent).
  * Tablas de autenticación y gestión de usuarios (Usuarios, Roles,
    Permisos, UsuarioRoles, RolePermisos, Sesiones, TokensRecuperacion,
    Auditoria).
  * Configuración del sistema (umbrales y modo automático).

Las contraseñas nunca se guardan en texto plano: solo el hash bcrypt.
"""

from datetime import datetime

from database import db


# ============================================
# TABLAS EXISTENTES (jardín inteligente)
# ============================================

class DHT11Reading(db.Model):
    """Modelo para almacenar lecturas del sensor DHT11"""
    __tablename__ = 'dht11_readings'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    temperatura = db.Column(db.Float, nullable=False)
    humedad = db.Column(db.Float, nullable=False)

    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat(),
            'temperatura': self.temperatura,
            'humedad': self.humedad
        }

    def __repr__(self):
        return f'<DHT11Reading {self.timestamp}: T={self.temperatura}°C, H={self.humedad}%>'


class RelayEvent(db.Model):
    """Modelo para almacenar eventos de cambio de estado de relés"""
    __tablename__ = 'relay_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    rele = db.Column(db.Integer, nullable=False)  # 1 o 2
    estado = db.Column(db.Boolean, nullable=False)  # True=ON, False=OFF
    origen = db.Column(db.String(50), nullable=False, default='dashboard')  # 'dashboard', 'pads', 'api', etc.

    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat(),
            'rele': self.rele,
            'estado': self.estado,
            'origen': self.origen
        }

    def __repr__(self):
        return f'<RelayEvent Rele{self.rele} estado={self.estado} origen={self.origen} at {self.timestamp}>'


class DispositivoOpla(db.Model):
    """Oplàs registrados del sistema, agrupados por sección (jardín o laboratorio).

    `ip` es opcional: los de jardín sin WiFi quedan como registro (sin red).
    El dashboard verifica conectividad TCP en `puerto` (por defecto 9001).
    """
    __tablename__ = 'dispositivos_opla'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(100), nullable=False)
    ip = db.Column(db.String(45), unique=True, nullable=True, index=True)
    seccion = db.Column(db.String(20), nullable=False, default='jardin', index=True)  # 'jardin' | 'laboratorio'
    zona = db.Column(db.String(100), nullable=True)  # zona dentro de la sección (ej. 'Aula B', 'Macetero 3')
    puerto = db.Column(db.Integer, nullable=False, default=9001)
    fecha_registro = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            'id': self.id,
            'nombre': self.nombre,
            'ip': self.ip,
            'seccion': self.seccion,
            'zona': self.zona,
            'puerto': self.puerto,
            'fecha_registro': self.fecha_registro.isoformat() if self.fecha_registro else None,
        }

    def __repr__(self):
        return f'<DispositivoOpla {self.nombre} ({self.seccion}) ip={self.ip}>'


# ============================================
# TABLAS DE AUTENTICACIÓN Y USUARIOS
# ============================================

class Usuario(db.Model):
    """Tabla de usuarios. Almacena únicamente información necesaria (sin secretos en claro)."""
    __tablename__ = 'usuarios'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(100), nullable=False)
    apellido = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(100), nullable=False)  # hash bcrypt, nunca en claro
    activo = db.Column(db.Boolean, nullable=False, default=True)
    fecha_registro = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ultimo_acceso = db.Column(db.DateTime, nullable=True)

    roles = db.relationship('Rol', secondary='usuario_roles', lazy='selectin')

    def set_password(self, password):
        import bcrypt as _bcrypt
        self.password_hash = _bcrypt.hashpw(
            password.encode('utf-8'), _bcrypt.gensalt()
        ).decode('utf-8')

    def check_password(self, password):
        import bcrypt as _bcrypt
        try:
            return _bcrypt.checkpw(
                password.encode('utf-8'), self.password_hash.encode('utf-8')
            )
        except ValueError:
            return False

    def has_rol(self, nombre):
        return any(r.nombre == nombre for r in self.roles)

    def permisos(self):
        permisos = set()
        for rol in self.roles:
            for rp in rol.permisos:
                permisos.add(rp.nombre)
        return permisos

    def to_dict(self):
        return {
            'id': self.id,
            'nombre': self.nombre,
            'apellido': self.apellido,
            'email': self.email,
            'username': self.username,
            'activo': self.activo,
            'fecha_registro': self.fecha_registro.isoformat() if self.fecha_registro else None,
            'ultimo_acceso': self.ultimo_acceso.isoformat() if self.ultimo_acceso else None,
            'roles': [r.nombre for r in self.roles],
            'permisos': sorted(self.permisos())
        }

    def __repr__(self):
        return f'<Usuario {self.username}>'


class Rol(db.Model):
    """Tabla de roles del sistema (ADMINISTRADOR, OPERADOR, ...)."""
    __tablename__ = 'roles'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(50), unique=True, nullable=False, index=True)
    descripcion = db.Column(db.String(255), default='')

    permisos = db.relationship(
        'Permiso', secondary='role_permisos', lazy='selectin', backref='roles'
    )

    def to_dict(self, include_permisos=True):
        data = {
            'id': self.id,
            'nombre': self.nombre,
            'descripcion': self.descripcion,
        }
        if include_permisos:
            data['permisos'] = [p.nombre for p in self.permisos]
        return data

    def __repr__(self):
        return f'<Rol {self.nombre}>'


class Permiso(db.Model):
    """Tabla de permisos granulares del sistema."""
    __tablename__ = 'permisos'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(100), unique=True, nullable=False, index=True)
    descripcion = db.Column(db.String(255), default='')

    def to_dict(self):
        return {'id': self.id, 'nombre': self.nombre, 'descripcion': self.descripcion}

    def __repr__(self):
        return f'<Permiso {self.nombre}>'


class UsuarioRol(db.Model):
    """Tabla intermedia Usuario-Rol."""
    __tablename__ = 'usuario_roles'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id', ondelete='CASCADE'), nullable=False)
    rol_id = db.Column(db.Integer, db.ForeignKey('roles.id', ondelete='CASCADE'), nullable=False)

    __table_args__ = (db.UniqueConstraint('usuario_id', 'rol_id', name='uq_usuario_rol'),)


class RolePermiso(db.Model):
    """Tabla intermedia Rol-Permiso."""
    __tablename__ = 'role_permisos'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    rol_id = db.Column(db.Integer, db.ForeignKey('roles.id', ondelete='CASCADE'), nullable=False)
    permiso_id = db.Column(db.Integer, db.ForeignKey('permisos.id', ondelete='CASCADE'), nullable=False)

    __table_args__ = (db.UniqueConstraint('rol_id', 'permiso_id', name='uq_rol_permiso'),)


class Sesion(db.Model):
    """Tabla de sesiones JWT activas. Permite revocar sesiones individualmente."""
    __tablename__ = 'sesiones'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    jti = db.Column(db.String(64), unique=True, nullable=False, index=True)  # identidad del JWT
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id', ondelete='CASCADE'), nullable=False)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    fecha_expiracion = db.Column(db.DateTime, nullable=False)
    ip = db.Column(db.String(64), default='')
    user_agent = db.Column(db.String(255), default='')
    revocada = db.Column(db.Boolean, default=False, nullable=False)
    fecha_revocacion = db.Column(db.DateTime, nullable=True)
    ultima_actividad = db.Column(db.DateTime, default=datetime.utcnow, nullable=True)

    usuario = db.relationship(
        'Usuario',
        backref=db.backref('sesiones', passive_deletes=True),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'jti': self.jti,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'fecha_expiracion': self.fecha_expiracion.isoformat() if self.fecha_expiracion else None,
            'ip': self.ip,
            'user_agent': self.user_agent,
            'revocada': self.revocada,
            'fecha_revocacion': self.fecha_revocacion.isoformat() if self.fecha_revocacion else None,
            'ultima_actividad': self.ultima_actividad.isoformat() if self.ultima_actividad else None,
        }

    def __repr__(self):
        return f'<Sesion {self.jti} usuario={self.usuario_id} revocada={self.revocada}>'


class TokenRecuperacion(db.Model):
    """Tabla de tokens de recuperación de contraseña.

    El token nunca se almacena en claro: se guarda su hash SHA-256.
    Tienen expiración, son de un solo uso y se invalidan tras usarse.
    """
    __tablename__ = 'tokens_recuperacion'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id', ondelete='CASCADE'), nullable=False)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    fecha_expiracion = db.Column(db.DateTime, nullable=False)
    usado = db.Column(db.Boolean, default=False, nullable=False)
    fecha_uso = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f'<TokenRecuperacion usuario={self.usuario_id} usado={self.usado}>'


class Auditoria(db.Model):
    """Auditoría de acciones importantes del sistema."""
    __tablename__ = 'auditoria'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id', ondelete='SET NULL'), nullable=True)
    accion = db.Column(db.String(100), nullable=False, index=True)
    detalle = db.Column(db.String(500), default='')
    resultado = db.Column(db.String(20), nullable=False, default='exito')  # 'exito' | 'fallido'
    ip = db.Column(db.String(64), default='')
    user_agent = db.Column(db.String(255), default='')
    fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    usuario = db.relationship(
        'Usuario',
        backref=db.backref('auditoria', passive_deletes=True),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'usuario': self.usuario.username if self.usuario else None,
            'usuario_id': self.usuario_id,
            'accion': self.accion,
            'detalle': self.detalle,
            'resultado': self.resultado,
            'ip': self.ip,
            'fecha': self.fecha.isoformat() if self.fecha else None,
        }

    def __repr__(self):
        return f'<Auditoria {self.accion} ({self.resultado}) at {self.fecha}>'


# ============================================
# CONFIGURACIÓN DEL SISTEMA
# ============================================

class SistemaConfig(db.Model):
    """Configuración global del sistema (fila única id=1).

    Almacena umbrales de temperatura/humedad y el modo automático
    del ventilador (relé 1).
    """
    __tablename__ = 'configuracion'

    id = db.Column(db.Integer, primary_key=True)
    temperatura_min = db.Column(db.Float, nullable=False, default=20.0)
    temperatura_max = db.Column(db.Float, nullable=False, default=30.0)
    humedad_min = db.Column(db.Float, nullable=False, default=40.0)
    humedad_max = db.Column(db.Float, nullable=False, default=70.0)
    relay_automatico = db.Column(db.Integer, nullable=False, default=1)
    modo_automatico = db.Column(db.Boolean, nullable=False, default=False)
    actualizado_por = db.Column(db.Integer, db.ForeignKey('usuarios.id', ondelete='SET NULL'), nullable=True)
    actualizado_en = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            'temperatura_min': self.temperatura_min,
            'temperatura_max': self.temperatura_max,
            'humedad_min': self.humedad_min,
            'humedad_max': self.humedad_max,
            'relay_automatico': self.relay_automatico,
            'modo_automatico': self.modo_automatico,
            'actualizado_en': self.actualizado_en.isoformat() if self.actualizado_en else None,
        }

    def __repr__(self):
        return f'<SistemaConfig modo_automatico={self.modo_automatico}>'