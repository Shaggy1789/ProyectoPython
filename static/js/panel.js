/**
 * SIGMA-IOT - Lógica de secciones del panel
 * Alertas, configuración (modo automático y umbrales), usuarios, logs,
 * perfil y gestión de sesiones. Todo validado también en el backend.
 */

function escapeHtml(texto) {
    return String(texto == null ? '' : texto)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function setMsg(id, mensaje, tipo) {
    const el = document.getElementById(id);
    if (!el) return;
    const clases = { exito: 'alert-success', error: 'alert-danger', info: 'alert-info' };
    el.innerHTML = '<div class="alert ' + (clases[tipo] || 'alert-info') + ' py-2">' + mensaje + '</div>';
    if (tipo !== 'info') setTimeout(function () { el.innerHTML = ''; }, 5000);
}

/* ============================================================
 * DISPATCHER DE SECCIONES
 * ============================================================ */

window.onSeccionMostrada = function (seccion) {
    detenerArea();
    if (seccion === 'dashboard') {
        iniciarDashboard();
    } else if (seccion === 'historico') {
        detenerDashboard();
        iniciarHistorico();
    } else if (seccion === 'ventilador') {
        detenerDashboard();
        iniciarVentilador();
        cargarModoAuto();
    } else if (seccion === 'alertas') {
        detenerDashboard();
        cargarAlertas();
    } else if (seccion === 'jardines') {
        detenerDashboard();
        cargarDispositivos('jardin');
        iniciarArea('jardin');
    } else if (seccion === 'laboratorios') {
        detenerDashboard();
        cargarDispositivos('laboratorio');
        iniciarArea('laboratorio');
    } else if (seccion === 'configuracion') {
        detenerDashboard();
        cargarModoAuto();
    } else if (seccion === 'usuarios') {
        detenerDashboard();
        cargarRoles();
        cargarUsuarios();
    } else if (seccion === 'logs') {
        detenerDashboard();
        cargarLogs();
    } else if (seccion === 'perfil') {
        detenerDashboard();
        cargarPerfil();
        cargarSesiones();
    }
};

/* ============================================================
 * DATOS EN VIVO POR ÁREA (Jardines / Laboratorios)
 * ============================================================ */

let areaTimer = null;

function detenerArea() {
    if (areaTimer) { clearInterval(areaTimer); areaTimer = null; }
}

function iniciarArea(seccion) {
    detenerArea();
    cargarDatosArea(seccion);
    areaTimer = setInterval(function () { cargarDatosArea(seccion); }, 15000);
}

async function cargarDatosArea(seccion) {
    const cont = document.getElementById('datos-area-' + seccion);
    if (!cont) return;
    const data = await SIGMA.api('/api/dispositivos');
    if (!data || !data.success) return;
    const lista = (data.grupos && data.grupos[seccion]) || [];
    if (lista.length === 0) {
        cont.innerHTML = '<div class="col-12"><div class="alert alert-info py-2 mb-0">' +
            'No hay dispositivos registrados en esta área. Usa "Registrar dispositivo".</div></div>';
        return;
    }
    const tarjetas = await Promise.all(lista.map(tarjetaDatosArea));
    cont.innerHTML = tarjetas.join('');
}

async function tarjetaDatosArea(d) {
    const etiquetas = {
        'T': 'Temperatura', 'H': 'Humedad', 'P': 'Presión',
        'S': 'Humedad de suelo', 'L': 'Luz', 'U': 'UV', 'V': 'Voltaje',
    };

    let online = false;
    let valores = null;
    let rele = null;
    try {
        const res = await SIGMA.api('/api/dispositivos/' + d.id + '/datos');
        if (res && res.success && res.online) {
            online = true;
            valores = res.datos || null;
        }
    } catch (e) { /* sin conexión */ }
    try {
        const est = await SIGMA.api('/api/dispositivos/' + d.id + '/estado');
        if (est && est.success && est.rele) rele = est.rele;
    } catch (e) { /* sin estado de relés */ }

    const badge = d.online === null
        ? '<span class="badge bg-secondary">Sin IP</span>'
        : (online
            ? '<span class="badge badge-relay-on">En línea</span>'
            : '<span class="badge badge-relay-off">Sin conexión</span>');

    let filas = '<tr><td colspan="2" class="text-center text-muted">Sin datos disponibles</td></tr>';
    if (valores && Object.keys(valores).length > 0) {
        filas = Object.keys(valores).map(function (k) {
            const nombre = etiquetas[k] || k;
            return '<tr><td>' + escapeHtml(nombre) + '</td><td class="text-end"><strong>' +
                escapeHtml(valores[k]) + '</strong></td></tr>';
        }).join('');
    }
    if (rele && (1 in rele)) {
        const cls = rele[1] ? 'badge-relay-on' : 'badge-relay-off';
        const txt = rele[1] ? 'ENCENDIDO' : 'APAGADO';
        filas += '<tr><td>Ventilador (Relé 1)</td><td class="text-end">' +
            '<span class="badge ' + cls + '">' + txt + '</span></td></tr>';
    }

    return '<div class="col-12 col-md-6 col-xl-4">' +
        '<div class="card h-100">' +
        '<div class="card-header d-flex justify-content-between align-items-center py-2">' +
        '<strong>' + escapeHtml(d.nombre) + '</strong>' + badge +
        '</div>' +
        '<div class="card-body p-0"><table class="table table-sm mb-0"><tbody>' + filas + '</tbody></table></div>' +
        (d.zona ? '<div class="card-footer py-1 text-muted small">Zona: ' + escapeHtml(d.zona) + '</div>' : '') +
        '</div></div>';
}

/* ============================================================
 * ALERTAS
 * ============================================================ */

async function cargarAlertas() {
    const body = document.getElementById('alertas-body');
    if (!body) return;
    body.innerHTML = '<p class="text-muted text-center mb-0">Cargando alertas...</p>';

    const data = await SIGMA.api('/api/alertas');
    if (!data) return;

    let html = '';
    if (data.sin_umbrales || (data.lectura === null)) {
        html = '<p class="text-muted text-center mb-0"><i class="bi bi-info-circle me-1"></i>' +
            'No hay lecturas del sensor disponibles o aún no se configuran umbrales.</p>';
    } else if (data.alertas_activas.length === 0) {
        html = '<div class="alert alert-success mb-0"><i class="bi bi-check-circle me-1"></i>' +
            'No hay alertas activas: la última lectura está dentro de los umbrales configurados.</div>';
    } else {
        html = '<ul class="list-group">' + data.alertas_activas.map(function (a) {
            return '<li class="list-group-item d-flex justify-content-between align-items-center">' +
                '<span><i class="bi bi-exclamation-triangle-fill text-warning me-2"></i><strong>' +
                escapeHtml(a.tipo) + '</strong></span>' +
                '<span class="badge bg-danger">Valor: ' + a.valor + (a.tipo.indexOf('emperatura') !== -1 ? ' °C' : ' %') +
                ' (límite ' + a.umbral + ')</span></li>';
        }).join('') + '</ul>';
        if (data.lectura) {
            html += '<p class="text-muted small mt-2 mb-0">Última lectura: ' +
                new Date(data.lectura.timestamp).toLocaleString('es-ES') + '</p>';
        }
    }
    body.innerHTML = html;
}

/* ============================================================
 * CONFIGURACIÓN: MODO AUTOMÁTICO Y UMBRALES
 * ============================================================ */

let modoAutoInit = false;

async function cargarModoAuto() {
    const data = await SIGMA.api('/api/config');
    if (!data || !data.config) return;
    const c = data.config;

    const s1 = document.getElementById('modoAutoSwitch');
    const s2 = document.getElementById('modoAutoSwitch2');
    if (s1) s1.checked = c.modo_automatico;
    if (s2) s2.checked = c.modo_automatico;

    const badge = document.getElementById('modo-auto-badge');
    if (badge) {
        badge.textContent = c.modo_automatico ? 'Activado' : 'Desactivado';
        badge.classList.toggle('badge-relay-on', c.modo_automatico);
        badge.classList.toggle('badge-relay-off', !c.modo_automatico);
    }

    const info = '<small class="text-muted">Ventilador automático → Relé ' + c.relay_automatico +
        ' · Umbrales: T ' + c.temperatura_min + '–' + c.temperatura_max + ' °C · ' +
        'H ' + c.humedad_min + '–' + c.humedad_max + ' %</small>';
    const i1 = document.getElementById('modo-auto-info');
    const i2 = document.getElementById('modo-auto-info2');
    if (i1) i1.innerHTML = info;
    if (i2) i2.innerHTML = info;

    const tm = document.getElementById('temp-min'), tx = document.getElementById('temp-max');
    const hm = document.getElementById('hum-min'), hx = document.getElementById('hum-max');
    const ra = document.getElementById('relayAutoSelect');
    if (tm) tm.value = c.temperatura_min;
    if (tx) tx.value = c.temperatura_max;
    if (hm) hm.value = c.humedad_min;
    if (hx) hx.value = c.humedad_max;
    if (ra) ra.value = c.relay_automatico;

    // Ocultar umbrales para operadores (validado también en backend)
    if (!SIGMA.esAdmin()) {
        const cardU = document.getElementById('card-umbrales');
        if (cardU) cardU.style.display = 'none';
    }

    if (!modoAutoInit) {
        modoAutoInit = true;
        [s1, s2].forEach(function (sw) {
            if (!sw) return;
            sw.addEventListener('change', function () {
                guardarModoAuto(this.checked, sw === s1 ? 'modo-auto-msg' : 'modo-auto-msg2');
            });
        });
    }
}

async function guardarModoAuto(activo, msgId) {
    const data = await SIGMA.api('/api/config/modo', {
        method: 'PUT',
        body: JSON.stringify({ activo: activo })
    });
    if (data && data.success) {
        cargarModoAuto();
        setMsg(msgId, 'Modo automático ' + (activo ? 'activado' : 'desactivado') + '.', 'exito');
    } else {
        setMsg(msgId, data && data.error ? data.error : 'No fue posible cambiar el modo automático.', 'error');
    }
}

async function guardarUmbrales() {
    const payload = {
        temperatura_min: parseFloat(document.getElementById('temp-min').value),
        temperatura_max: parseFloat(document.getElementById('temp-max').value),
        humedad_min: parseFloat(document.getElementById('hum-min').value),
        humedad_max: parseFloat(document.getElementById('hum-max').value),
        relay_automatico: parseInt(document.getElementById('relayAutoSelect').value, 10)
    };
    const data = await SIGMA.api('/api/config/umbrales', {
        method: 'PUT',
        body: JSON.stringify(payload)
    });
    if (data && data.success) {
        setMsg('umbrales-msg', 'Umbrales actualizados correctamente.', 'exito');
        cargarModoAuto();
    } else {
        setMsg('umbrales-msg', data && data.error ? data.error : 'No fue posible guardar los umbrales.', 'error');
    }
}

/* ============================================================
 * USUARIOS (administrador)
 * ============================================================ */

let rolesDisponibles = [];

async function cargarRoles() {
    const data = await SIGMA.api('/api/auth/roles');
    if (data && data.success) rolesDisponibles = data.roles;
}

async function cargarUsuarios() {
    const tbody = document.getElementById('usuarios-body');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">Cargando usuarios...</td></tr>';

    const data = await SIGMA.api('/api/auth/usuarios');
    if (!data || !data.success) return;
    const me = SIGMA.getUsuario();

    if (data.usuarios.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No hay usuarios.</td></tr>';
        return;
    }

    tbody.innerHTML = data.usuarios.map(function (u) {
        const rolesBadges = (u.roles || []).map(function (r) {
            const cls = r === 'ADMINISTRADOR' ? 'bg-dark' : 'bg-primary';
            return '<span class="badge ' + cls + ' me-1">' + escapeHtml(r) + '</span>';
        }).join('') || '<span class="badge bg-secondary">sin rol</span>';

        const estado = u.activo
            ? '<span class="badge badge-relay-on">Activo</span>'
            : '<span class="badge bg-secondary">Inactivo</span>';

        const esYo = me && me.id === u.id;
        const botones = esYo
            ? '<span class="badge bg-info">Tú</span>'
            : '<button class="btn btn-sm btn-outline-primary" onclick="abrirEditarUsuario(' + u.id + ')">' +
              '<i class="bi bi-pencil"></i></button> ' +
              '<button class="btn btn-sm btn-outline-danger" onclick="eliminarUsuario(' + u.id + ')">' +
              '<i class="bi bi-trash"></i></button>';

        return '<tr><td>' + u.id + '</td><td>' + escapeHtml(u.nombre + ' ' + u.apellido) +
            '</td><td>' + escapeHtml(u.username) + '</td><td>' + escapeHtml(u.email) +
            '</td><td>' + rolesBadges + '</td><td>' + estado + '</td><td>' + botones + '</td></tr>';
    }).join('');
}

function renderRolesCheckboxes(seleccionados) {
    const cont = document.getElementById('u-roles');
    if (!cont) return;
    seleccionados = seleccionados || [];
    cont.innerHTML = rolesDisponibles.map(function (r) {
        const checked = seleccionados.indexOf(r.nombre) !== -1 ? 'checked' : '';
        return '<div class="form-check form-check-inline">' +
            '<input class="form-check-input" type="checkbox" id="rol-' + r.nombre +
            '" value="' + r.nombre + '" ' + checked + '>' +
            '<label class="form-check-label" for="rol-' + r.nombre + '">' + r.nombre + '</label></div>';
    }).join('');
}

function abrirNuevoUsuario() {
    document.getElementById('modalUsuarioTitle').textContent = 'Nuevo usuario';
    document.getElementById('u-id').value = '';
    ['u-nombre', 'u-apellido', 'u-username', 'u-email', 'u-password'].forEach(function (id) {
        document.getElementById(id).value = '';
    });
    document.getElementById('u-activo').checked = true;
    document.getElementById('u-password-group').style.display = '';
    renderRolesCheckboxes(['OPERADOR']);
    return true; // soportar onclick con data-bs-toggle
}

function abrirEditarUsuario(id) {
    const usuarios = /* no-op */ null;
    cargarUsuarios().then(function () {
        // no re-render: usamos búsqueda desde el objeto de la fila es complejo; consultamos uno a uno
    });
    // Cargar datos del usuario y abrir modal
    SIGMA.api('/api/auth/usuarios').then(function (data) {
        if (!data || !data.success) return;
        const u = data.usuarios.find(function (x) { return x.id === id; });
        if (!u) return;
        document.getElementById('modalUsuarioTitle').textContent = 'Editar usuario: ' + u.username;
        document.getElementById('u-id').value = u.id;
        document.getElementById('u-nombre').value = u.nombre;
        document.getElementById('u-apellido').value = u.apellido;
        document.getElementById('u-username').value = u.username;
        document.getElementById('u-email').value = u.email;
        document.getElementById('u-password').value = '';
        document.getElementById('u-activo').checked = u.activo;
        document.getElementById('u-password-group').style.display = 'none';
        renderRolesCheckboxes(u.roles || []);
        new bootstrap.Modal(document.getElementById('modalUsuario')).show();
    });
}

async function guardarUsuario() {
    const id = document.getElementById('u-id').value;
    const roles = rolesDisponibles
        .map(function (r) { return r.nombre; })
        .filter(function (n) { return document.getElementById('rol-' + n).checked; });

    const payload = {
        nombre: document.getElementById('u-nombre').value.trim(),
        apellido: document.getElementById('u-apellido').value.trim(),
        username: document.getElementById('u-username').value.trim(),
        email: document.getElementById('u-email').value.trim(),
        roles: roles,
        activo: document.getElementById('u-activo').checked
    };
    if (!id) payload.password = document.getElementById('u-password').value;

    let data;
    if (id) {
        data = await SIGMA.api('/api/auth/usuarios/' + id, { method: 'PUT', body: JSON.stringify(payload) });
    } else {
        data = await SIGMA.api('/api/auth/usuarios', { method: 'POST', body: JSON.stringify(payload) });
    }

    if (data && data.success) {
        bootstrap.Modal.getInstance(document.getElementById('modalUsuario')).hide();
        setMsg('usuarios-msg', 'Usuario guardado correctamente.', 'exito');
        cargarUsuarios();
    } else if (data && data.errors) {
        setMsg('usuario-modal-msg', data.errors.join('<br>'), 'error');
    } else {
        setMsg('usuario-modal-msg', data && data.error ? data.error : 'Error al guardar el usuario.', 'error');
    }
}

async function eliminarUsuario(id) {
    if (!confirm('¿Eliminar este usuario de forma permanente?')) return;
    const data = await SIGMA.api('/api/auth/usuarios/' + id, { method: 'DELETE' });
    if (data && data.success) {
        setMsg('usuarios-msg', 'Usuario eliminado.', 'exito');
        cargarUsuarios();
    } else {
        setMsg('usuarios-msg', data && data.error ? data.error : 'Error al eliminar.', 'error');
    }
}

/* ============================================================
 * LOGS (administrador)
 * ============================================================ */

async function cargarLogs() {
    const tbody = document.getElementById('logs-body');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">Cargando logs...</td></tr>';

    const accion = document.getElementById('log-accion') ? document.getElementById('log-accion').value.trim() : '';
    const resultado = document.getElementById('log-resultado') ? document.getElementById('log-resultado').value : '';
    let url = '/api/auth/logs?';
    if (accion) url += 'accion=' + encodeURIComponent(accion) + '&';
    if (resultado) url += 'resultado=' + resultado + '&';
    url = url.replace(/[?&]$/, '');

    const data = await SIGMA.api(url);
    if (!data || !data.success) return;

    if (data.logs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No hay registros con esos filtros.</td></tr>';
        return;
    }

    tbody.innerHTML = data.logs.map(function (l) {
        const cls = l.resultado === 'exito'
            ? 'badge badge-relay-on'
            : (l.resultado === 'fallido' ? 'badge bg-danger' : 'badge bg-secondary');
        return '<tr><td>' + new Date(l.fecha).toLocaleString('es-ES') +
            '</td><td>' + escapeHtml(l.usuario || '—') +
            '</td><td><span class="badge bg-light text-dark border">' + escapeHtml(l.accion) + '</span></td>' +
            '<td><span class="' + cls + '">' + escapeHtml(l.resultado) + '</span></td>' +
            '<td>' + escapeHtml(l.ip || '') + '</td><td class="small text-muted">' + escapeHtml(l.detalle) + '</td></tr>';
    }).join('');
}

/* ============================================================
 * PERFIL
 * ============================================================ */

async function cargarPerfil() {
    const data = await SIGMA.api('/api/auth/me');
    if (!data || !data.success) return;
    const u = data.usuario;

    document.getElementById('p-nombre').value = u.nombre;
    document.getElementById('p-apellido').value = u.apellido;
    document.getElementById('p-username').value = u.username;
    document.getElementById('p-email').value = u.email;

    // Actualizar datos mostrados en navbar (si hay cambios de rol/nombre)
    SIGMA.guardarSesion(SIGMA.getToken(), u);
    SIGMA.construirNavbar();
}

async function guardarPerfil() {
    const data = await SIGMA.api('/api/auth/me', {
        method: 'PUT',
        body: JSON.stringify({
            nombre: document.getElementById('p-nombre').value.trim(),
            apellido: document.getElementById('p-apellido').value.trim(),
            username: document.getElementById('p-username').value.trim(),
            email: document.getElementById('p-email').value.trim()
        })
    });
    if (data && data.success) {
        setMsg('perfil-msg', 'Datos actualizados.', 'exito');
        SIGMA.guardarSesion(SIGMA.getToken(), data.usuario);
        SIGMA.construirNavbar();
        cargarSesiones();
    } else if (data && data.errors) {
        setMsg('perfil-msg', data.errors.join('<br>'), 'error');
    } else {
        setMsg('perfil-msg', data && data.error ? data.error : 'Error al actualizar el perfil.', 'error');
    }
}

async function cambiarPassword() {
    const password = document.getElementById('p-nueva').value;
    const confirmacion = document.getElementById('p-confirmar').value;

    if (password !== confirmacion) {
        setMsg('pass-msg', 'La confirmación de contraseña no coincide.', 'error');
        return;
    }

    const data = await SIGMA.api('/api/auth/cambiar-contrasena', {
        method: 'POST',
        body: JSON.stringify({
            actual: document.getElementById('p-actual').value,
            nueva: password,
            confirmacion: confirmacion
        })
    });

    if (data && data.success) {
        setMsg('pass-msg', 'Contraseña actualizada.', 'exito');
        ['p-actual', 'p-nueva', 'p-confirmar'].forEach(function (id) {
            document.getElementById(id).value = '';
        });
        cargarSesiones();
    } else {
        setMsg('pass-msg', data && data.error ? data.error : 'Error al cambiar la contraseña.', 'error');
    }
}

/* ============================================================
 * GESTIÓN DE SESIONES
 * ============================================================ */

async function cargarSesiones() {
    const tbody = document.getElementById('sesiones-body');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">Cargando sesiones...</td></tr>';

    const me = SIGMA.getUsuario();
    const data = await SIGMA.api('/api/auth/sesiones');
    if (!data || !data.success) return;

    if (data.sesiones.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No hay sesiones.</td></tr>';
        return;
    }

    tbody.innerHTML = data.sesiones.map(function (s) {
        const actual = me && s.usuario_id === me.id && s.jti &&
            document.cookie.indexOf('sigma_auth') !== -1 ? false : false; // marcamos por ip/ua aproximado
        const esActual = s.ip;
        const estado = s.revocada || s.fecha_expiracion < new Date().toISOString()
            ? '<span class="badge bg-secondary">Expirada/Revocada</span>'
            : '<span class="badge badge-relay-on">Activa</span>';
        const acciones = s.revocada ? ''
            : '<button class="btn btn-sm btn-outline-warning" onclick="revocarSesion(' + s.id + ', this)">' +
              '<i class="bi bi-x-circle me-1"></i>Revocar</button>';
        return '<tr><td>' + escapeHtml(s.user_agent || 'Desconocido') +
            '</td><td>' + escapeHtml(s.ip || '') +
            '</td><td>' + (s.fecha_creacion ? new Date(s.fecha_creacion).toLocaleString('es-ES') : '—') +
            '</td><td>' + (s.fecha_expiracion ? new Date(s.fecha_expiracion).toLocaleString('es-ES') : '—') +
            '</td><td>' + estado + '</td><td>' + acciones + '</td></tr>';
    }).join('');
}

async function revocarSesion(id, btn) {
    const data = await SIGMA.api('/api/auth/sesiones/' + id, { method: 'DELETE' });
    if (data && data.success) {
        if (btn) { btn.closest('tr').remove(); }
    } else {
        showToast(data && data.error ? data.error : 'No fue posible revocar la sesión.', 'danger');
    }
}

/* Limpiar mensajes del modal al abrirlo */
document.addEventListener('DOMContentLoaded', function () {
    const modal = document.getElementById('modalUsuario');
    if (modal) {
        modal.addEventListener('show.bs.modal', function () {
            const m = document.getElementById('usuario-modal-msg');
            if (m) m.innerHTML = '';
        });
    }
    const modalDisp = document.getElementById('modalDispositivo');
    if (modalDisp) {
        modalDisp.addEventListener('show.bs.modal', function () {
            const m = document.getElementById('dispositivo-modal-msg');
            if (m) m.innerHTML = '';
        });
    }
});

/* ============================================================
 * DISPOSITIVOS (OPLÀ)
 * ============================================================ */

function cargarDispositivos(seccion) {
    const tbody = document.getElementById('disp-' + seccion + '-body');
    const msg = document.getElementById('disp-' + seccion + '-msg');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">Cargando...</td></tr>';

    SIGMA.api('/api/dispositivos').then(function (data) {
        if (!data || !data.success) {
            tbody.innerHTML = '<tr><td colspan="7" class="text-center text-danger">Error al cargar dispositivos.</td></tr>';
            return;
        }
        const lista = (data.grupos && data.grupos[seccion]) || [];
        if (lista.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">' +
                'No hay dispositivos registrados en esta sección.</td></tr>';
            return;
        }
        tbody.innerHTML = lista.map(renderFilaDispositivo).join('');
        if (msg) msg.innerHTML = '';
    }).catch(function () {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-danger">Error de conexión.</td></tr>';
    });
}

function renderFilaDispositivo(d) {
    let badge;
    if (d.online === null) {
        badge = '<span class="badge bg-secondary">Sin IP</span>';
    } else if (d.online) {
        badge = '<span class="badge badge-relay-on">En línea</span>';
    } else {
        badge = '<span class="badge badge-relay-off">Sin conexión</span>';
    }
    const fecha = d.fecha_registro ? new Date(d.fecha_registro).toLocaleString('es-ES') : '—';
    const puedeGestionar = SIGMA.esAdmin() || SIGMA.hayPermiso('dispositivos.gestionar');
    let acciones = '<button class="btn btn-sm btn-outline-info" title="Ver datos" ' +
        'onclick="verDatosDispositivo(' + d.id + ')"><i class="bi bi-activity"></i></button>';
    if (puedeGestionar) {
        acciones += ' <button class="btn btn-sm btn-outline-primary" title="Editar" ' +
            'onclick="abrirEditarDispositivo(' + d.id + ')"><i class="bi bi-pencil"></i></button>' +
            ' <button class="btn btn-sm btn-outline-danger" title="Eliminar" ' +
            'onclick="eliminarDispositivo(' + d.id + ')"><i class="bi bi-trash"></i></button>';
    }
    return '<tr><td>' + escapeHtml(d.nombre) + '</td><td>' + escapeHtml(d.zona || '—') +
        '</td><td>' + escapeHtml(d.ip || '—') +
        '</td><td>' + d.puerto + '</td><td>' + badge + '</td><td>' + fecha + '</td><td>' + acciones + '</td></tr>';
}

function nuevoDispositivo(seccion) {
    document.getElementById('modalDispositivoTitle').textContent = 'Registrar dispositivo Oplà';
    document.getElementById('d-id').value = '';
    document.getElementById('d-nombre').value = '';
    document.getElementById('d-zona').value = '';
    document.getElementById('d-ip').value = '';
    document.getElementById('d-puerto').value = '9001';
    document.getElementById('d-seccion').value = (seccion === 'laboratorio') ? 'laboratorio' : 'jardin';
    return true; // soportar onclick con data-bs-toggle
}

function abrirEditarDispositivo(id) {
    SIGMA.api('/api/dispositivos').then(function (data) {
        if (!data || !data.success) return;
        const todos = [].concat(data.grupos.jardin || [], data.grupos.laboratorio || []);
        const d = todos.find(function (x) { return x.id === id; });
        if (!d) return;
        document.getElementById('modalDispositivoTitle').textContent = 'Editar: ' + d.nombre;
        document.getElementById('d-id').value = d.id;
        document.getElementById('d-nombre').value = d.nombre;
        document.getElementById('d-zona').value = d.zona || '';
        document.getElementById('d-ip').value = d.ip || '';
        document.getElementById('d-puerto').value = d.puerto;
        document.getElementById('d-seccion').value = d.seccion;
        const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('modalDispositivo'));
        modal.show();
    });
}

function guardarDispositivo() {
    const id = document.getElementById('d-id').value;
    const payload = {
        nombre: document.getElementById('d-nombre').value.trim(),
        zona: document.getElementById('d-zona').value.trim(),
        ip: document.getElementById('d-ip').value.trim(),
        puerto: parseInt(document.getElementById('d-puerto').value, 10) || 9001,
        seccion: document.getElementById('d-seccion').value,
    };
    const msg = document.getElementById('dispositivo-modal-msg');
    msg.innerHTML = '<div class="text-muted">Guardando...</div>';

    const peticion = id
        ? SIGMA.api('/api/dispositivos/' + id, { method: 'PUT', body: JSON.stringify(payload) })
        : SIGMA.api('/api/dispositivos', { method: 'POST', body: JSON.stringify(payload) });

    peticion.then(function (data) {
        if (!data || !data.success) {
            msg.innerHTML = '<div class="alert alert-danger py-2">' + escapeHtml(data.error || 'Error al guardar.') + '</div>';
            return;
        }
        const modal = bootstrap.Modal.getInstance(document.getElementById('modalDispositivo'));
        if (modal) modal.hide();
        cargarDispositivos('jardin');
        cargarDispositivos('laboratorio');
    }).catch(function () {
        msg.innerHTML = '<div class="alert alert-danger py-2">Error de conexión.</div>';
    });
}

function detectarDispositivos(seccion) {
    const boton = event && event.currentTarget ? event.currentTarget : null;
    const textoOriginal = boton ? boton.innerHTML : '';
    if (boton) boton.innerHTML = '<i class="bi bi-hourglass-split me-1"></i>Buscando...';

    SIGMA.api('/api/dispositivos/discover', { method: 'POST' }).then(function (data) {
        if (boton) boton.innerHTML = textoOriginal;
        if (!data || !data.success) {
            showToast((data && data.error) || 'No se pudo escanear la red.', 'danger');
            return;
        }
        return SIGMA.api('/api/dispositivos').then(function (reg) {
            const todos = [].concat(reg.grupos.jardin || [], reg.grupos.laboratorio || []);
            const esp32 = data.esp32;
            const oplas = data.oplas || [];

            if (seccion === 'laboratorio') {
                if (!esp32) {
                    showToast('No se detectó el ESP32 en la red.', 'warning');
                    return;
                }
                gestionarDetectado('laboratorio', esp32, todos);
            } else {
                if (oplas.length === 0) {
                    showToast('No se detectaron Oplàs de jardín en la red.', 'warning');
                    return;
                }
                if (oplas.length === 1) {
                    gestionarDetectado('jardin', oplas[0], todos);
                } else {
                    const opciones = oplas.map(function (ip) {
                        const reg = todos.find(function (x) { return x.ip === ip; });
                        return ip + (reg ? '  [REGISTRADO: ' + reg.nombre + ']' : '  [NUEVO]');
                    }).join('\n');
                    const elegida = prompt('Se detectaron varios Oplàs:\n' + opciones +
                        '\n\nEscribe la IP que quieres gestionar:', oplas[0]);
                    if (elegida && oplas.indexOf(elegida) !== -1) {
                        gestionarDetectado('jardin', elegida, todos);
                    }
                }
            }
        });
    }).catch(function () {
        if (boton) boton.innerHTML = textoOriginal;
        showToast('Error de conexión al escanear.', 'danger');
    });
}

function gestionarDetectado(seccion, ip, todos) {
    const registrado = todos.find(function (x) { return x.ip === ip; });

    if (registrado) {
        if (registrado.seccion !== seccion) {
            showToast(registrado.nombre + ' (' + ip + ') ya está registrado en ' +
                (registrado.seccion === 'jardin' ? 'Jardines' : 'Laboratorios') + '.', 'warning');
            return;
        }
        if (confirm('Oplà "' + registrado.nombre + '" ya está registrado en ' + ip + '.\n\n¿Actualizar sus datos?')) {
            abrirEditarDispositivo(registrado.id);
        }
        return;
    }

    if (confirm('Se detectó un dispositivo NUEVO en ' + ip + '\n(no está registrado en ' +
        (seccion === 'jardin' ? 'Jardines' : 'Laboratorios') + ').\n\n¿Registrarlo?')) {
        const nombreSugerido = seccion === 'jardin' ? 'Opla Jardin ' + ip.split('.').pop() : 'Lab-IOT ' + ip.split('.').pop();
        nuevoDispositivo(seccion);
        document.getElementById('d-ip').value = ip;
        document.getElementById('d-nombre').value = nombreSugerido;
        const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('modalDispositivo'));
        modal.show();
        showToast('Dispositivo nuevo detectado: ' + ip + '. Completa el nombre y guarda.', 'success');
    }
}

function aplicarIpDetectada(seccion, ip) {
    SIGMA.api('/api/dispositivos').then(function (data) {
        if (!data || !data.success) return;
        const lista = (data.grupos && data.grupos[seccion]) || [];
        const d = lista[0];
        if (!d) {
            showToast('No hay dispositivos registrados en ' + seccion + '. Crea uno primero.', 'warning');
            return;
        }
        SIGMA.api('/api/dispositivos/' + d.id, {
            method: 'PUT',
            body: JSON.stringify({ ip: ip })
        }).then(function (res) {
            if (res && res.success) {
                showToast(d.nombre + ' actualizado a ' + ip, 'success');
                cargarDispositivos('jardin');
                cargarDispositivos('laboratorio');
                if (seccion === 'laboratorio') {
                    setTimeout(function () {
                        if (typeof cargarDatosArea === 'function') cargarDatosArea('laboratorio');
                    }, 1000);
                }
            } else if (res && res.error) {
                showToast(res.error, 'danger');
            }
        });
    });
}

function eliminarDispositivo(id) {
    if (!confirm('¿Eliminar este dispositivo del registro?')) return;
    SIGMA.api('/api/dispositivos/' + id, { method: 'DELETE' }).then(function (data) {
        if (data && data.success) {
            cargarDispositivos('jardin');
            cargarDispositivos('laboratorio');
        } else if (data && data.error) {
            alert(data.error);
        }
    });
}

function verDatosDispositivo(id) {
    const body = document.getElementById('disp-datos-body');
    const msg = document.getElementById('disp-datos-msg');
    body.innerHTML = '<div class="text-muted">Consultando al dispositivo...</div>';
    if (msg) msg.innerHTML = '';
    const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('modalDispositivoDatos'));
    modal.show();

    SIGMA.api('/api/dispositivos/' + id + '/datos').then(function (data) {
        if (!data || !data.success) {
            body.innerHTML = '<div class="alert alert-danger py-2">' + escapeHtml(data.error || 'Error.') + '</div>';
            return;
        }
        document.getElementById('modalDatosTitle').textContent =
            'Datos de ' + (data.dispositivo ? data.dispositivo.nombre : 'dispositivo');
        const etiquetas = {
            'T': 'Temperatura', 'H': 'Humedad', 'P': 'Presión',
            'S': 'Humedad de suelo', 'L': 'Luz', 'U': 'UV', 'V': 'Voltaje',
        };
        const filas = Object.keys(data.datos).map(function (k) {
            const etiqueta = etiquetas[k] || k;
            return '<tr><td>' + escapeHtml(etiqueta) + '</td><td class="text-end"><strong>' +
                escapeHtml(data.datos[k]) + '</strong></td></tr>';
        }).join('');
        body.innerHTML = '<table class="table table-sm mb-0"><tbody>' + filas + '</tbody></table>';
    }).catch(function () {
        body.innerHTML = '<div class="alert alert-danger py-2">Error de conexión con el dispositivo.</div>';
    });
}