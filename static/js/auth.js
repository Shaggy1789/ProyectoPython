/* ============================================================
 * SIGMA-IOT - Núcleo de autenticación (frontend)
 * Gestión del token JWT, estado de sesión, guards y navegación.
 * El control de acceso REAL se valida siempre en el backend.
 * ============================================================ */

const SIGMA = (function () {

    const TOKEN_KEY = 'sigma_token';
    const USER_KEY = 'sigma_usuario';

    function getToken() { return localStorage.getItem(TOKEN_KEY); }
    function getUsuario() {
        try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null'); }
        catch (e) { return null; }
    }
    function esAdmin() {
        const u = getUsuario();
        return !!u && Array.isArray(u.roles) && u.roles.indexOf('ADMINISTRADOR') !== -1;
    }
    function tieneRol(rol) {
        const u = getUsuario();
        return !!u && Array.isArray(u.roles) && u.roles.indexOf(rol) !== -1;
    }
    function hayPermiso(permiso) {
        const u = getUsuario();
        return !!u && Array.isArray(u.permisos) && u.permisos.indexOf(permiso) !== -1;
    }

    function setAuthCookie() {
        // Marca para el redirect del servidor en "/" (no es el token)
        document.cookie = 'sigma_auth=1; path=/';
    }
    function clearAuthCookie() {
        document.cookie = 'sigma_auth=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT';
    }

    function guardarSesion(token, usuario) {
        localStorage.setItem(TOKEN_KEY, token);
        localStorage.setItem(USER_KEY, JSON.stringify(usuario));
        setAuthCookie();
    }

    function limpiarSesion() {
        localStorage.removeItem(TOKEN_KEY);
        localStorage.removeItem(USER_KEY);
        clearAuthCookie();
    }

    function redirigirLogin() {
        limpiarSesion();
        window.location.href = '/login';
    }

    /* Llamada a la API con token y manejo de 401 (token inválido/expirado). */
    async function api(path, options) {
        options = options || {};
        const headers = Object.assign({}, options.headers || {});
        headers['Content-Type'] = 'application/json';
        const token = getToken();
        if (token) headers['Authorization'] = 'Bearer ' + token;

        let res;
        try {
            res = await fetch(path, Object.assign({}, options, { headers: headers }));
        } catch (e) {
            return { success: false, error: 'No fue posible conectar con el servidor.' };
        }

        let data = null;
        try { data = await res.json(); } catch (e) { data = null; }

        if (res.status === 401 && path.indexOf('/api/auth/login') === -1) {
            redirigirLogin();
            return null;
        }
        return data;
    }

    /* Overflow de mensajes (usa el contenedor #alertMsg de las páginas). */
    function mostrarError(mensaje, contenedor) {
        const el = document.getElementById(contenedor || 'alertMsg');
        if (el) {
            el.innerHTML = '<div class="alert alert-danger alert-dismissible fade show" role="alert">' +
                mensaje +
                '<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>';
        }
    }
    function mostrarExito(mensaje, contenedor) {
        const el = document.getElementById(contenedor || 'alertMsg');
        if (el) {
            el.innerHTML = '<div class="alert alert-success alert-dismissible fade show" role="alert">' +
                mensaje +
                '<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>';
        }
    }
    function mostrarInfo(mensaje, contenedor) {
        const el = document.getElementById(contenedor || 'alertMsg');
        if (el) {
            el.innerHTML = '<div class="alert alert-info alert-dismissible fade show" role="alert">' +
                mensaje +
                '<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>';
        }
    }

    /* ---- NAVEGACIÓN / GUARDS ---- */

    function construirNavbar() {
        const contenedor = document.getElementById('navLinks');
        const userMenu = document.getElementById('userMenu');
        const loginLink = document.getElementById('loginLink');
        const userName = document.getElementById('userName');
        const u = getUsuario();

        if (!u) {
            if (contenedor) contenedor.innerHTML = '';
            if (userMenu) userMenu.style.display = 'none';
            if (loginLink) loginLink.style.display = '';
            return;
        }

        if (userName) {
            userName.textContent = u.nombre ? (u.nombre + ' ' + (u.apellido || '')) : u.username;
        }
        if (loginLink) loginLink.style.display = 'none';
        if (userMenu) userMenu.style.display = '';

        const secciones = [
            ['dashboard', 'Dashboard', 'bi-speedometer2'],
        ];
        if (esAdmin() || tieneRol('OPERADOR')) {
            secciones.push(['ventilador', 'Ventilador', 'bi-fan']);
            secciones.push(['historico', 'Histórico', 'bi-graph-up']);
            secciones.push(['alertas', 'Alertas', 'bi-exclamation-triangle']);
            secciones.push(['jardines', 'Jardines', 'bi-flower1']);
            secciones.push(['laboratorios', 'Laboratorios', 'bi-buildings']);
            secciones.push(['configuracion', 'Configuración', 'bi-sliders']);
        }
        if (esAdmin()) {
            secciones.push(['usuarios', 'Usuarios', 'bi-people']);
            secciones.push(['logs', 'Logs', 'bi-journal-code']);
        }

        if (contenedor) {
            contenedor.innerHTML = '';
            secciones.forEach(function (s) {
                const li = document.createElement('li');
                li.className = 'nav-item';
                li.innerHTML = '<a class="nav-link" href="#" data-seccion="' + s[0] +
                    '" onclick="SIGMA.mostrarSeccion(\'' + s[0] + '\'); return false;">' +
                    '<i class="bi ' + s[2] + ' me-1"></i>' + s[1] + '</a>';
                contenedor.appendChild(li);
            });
        }
    }

    function guardSesion() {
        if (!getToken()) { redirigirLogin(); return false; }
        return true;
    }
    function guardAdmin() {
        if (!guardSesion()) return false;
        if (!esAdmin()) {
            window.location.href = '/';
            return false;
        }
        return true;
    }

    /* Muestra una sección del panel (solo visible en la página index). */
    function mostrarSeccion(nombre) {
        const seccion = document.getElementById('seccion-' + nombre);
        if (!seccion) return;
        document.querySelectorAll('.panel-seccion').forEach(function (s) { s.classList.add('d-none'); });
        seccion.classList.remove('d-none');
        document.querySelectorAll('#navLinks a[data-seccion]').forEach(function (a) {
            a.classList.toggle('active', a.getAttribute('data-seccion') === nombre);
        });
        sessionStorage.setItem('sigma_seccion', nombre);
        if (typeof onSeccionMostrada === 'function') onSeccionMostrada(nombre);
    }

    function irPerfil() {
        if (window.location.href.indexOf('/login') !== -1) redirigirLogin();
        sessionStorage.setItem('sigma_seccion', 'perfil');
        window.location.href = '/';
    }

    async function cerrarSesion() {
        try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { /* ignorar */ }
        limpiarSesion();
        window.location.href = '/login';
    }

    async function alreadyLoggedRedirect() {
        // Solo redirige a "/" si el token es realmente válido.
        // Evita el bucle: "/" exige cookie sigma_auth, y un token viejo
        // sin cookie haría rebotar login<->/ indefinidamente.
        if (!getToken()) return;
        try {
            const res = await fetch('/api/auth/me', {
                headers: { 'Authorization': 'Bearer ' + getToken() }
            });
            if (res.ok) {
                window.location.href = '/';
            } else {
                limpiarSesion();
            }
        } catch (e) {
            limpiarSesion();
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        construirNavbar();
    });

    return {
        api: api,
        getToken: getToken,
        getUsuario: getUsuario,
        esAdmin: esAdmin,
        tieneRol: tieneRol,
        hayPermiso: hayPermiso,
        guardarSesion: guardarSesion,
        limpiarSesion: limpiarSesion,
        redirigirLogin: redirigirLogin,
        guardSesion: guardSesion,
        guardAdmin: guardAdmin,
        mostrarSeccion: mostrarSeccion,
        irPerfil: irPerfil,
        cerrarSesion: cerrarSesion,
        alreadyLoggedRedirect: alreadyLoggedRedirect,
        construirNavbar: construirNavbar,
        mostrarError: mostrarError,
        mostrarExito: mostrarExito,
        mostrarInfo: mostrarInfo
    };
})();