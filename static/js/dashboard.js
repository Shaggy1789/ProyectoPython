/**
 * Dashboard JavaScript - SIGMA-IOT (panel autenticado)
 * Actualizaciones en tiempo real (SSE), gráfico histórico y control de relés.
 * Todas las peticiones usan el token JWT mediante SIGMA.api().
 */

const CHART_UPDATE_INTERVAL = 60000; // 1 min gráfico (sección histórico)

let historyChart = null;
let currentTimeRange = 24;
let historicoTimer = null;

/* ---------- CONTROL DE SECCIONES / TIMERS ---------- */

function iniciarDashboard() {
    conectarSSE();
    cargarEventosRecientes();
    cargarDropdownsOplas();
    actualizarOplaSeleccionado();
    if (!oplaTimer) {
        oplaTimer = setInterval(function () {
            cargarDropdownsOplas();
            actualizarOplaSeleccionado();
        }, 15000);
    }
}

function detenerDashboard() {
    desconectarSSE();
    if (historicoTimer) { clearInterval(historicoTimer); historicoTimer = null; }
    if (oplaTimer) { clearInterval(oplaTimer); oplaTimer = null; }
}

function iniciarHistorico() {
    refreshChart();
    cargarEventosRecientes();
    if (!historicoTimer) {
        historicoTimer = setInterval(function () {
            loadHistoricalData(currentTimeRange);
        }, CHART_UPDATE_INTERVAL);
    }
}

/* ---------- STREAM SSE EN TIEMPO REAL ---------- */

let sseAbortCtrl = null;
let sseReconnectTimer = null;
let sseIntentos = 0;

function eventSourceActivo() {
    return sseAbortCtrl !== null;
}

function conectarSSE() {
    if (sseAbortCtrl) return; // ya conectado
    const token = SIGMA.getToken();
    if (!token) return;

    const ctrl = new AbortController();
    sseAbortCtrl = ctrl;

    fetch('/api/stream', {
        headers: { 'Authorization': 'Bearer ' + token },
        signal: ctrl.signal
    }).then(function (res) {
        if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
        sseIntentos = 0;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        function leer() {
            reader.read().then(function (resultado) {
                if (resultado.done) { desconectarSSE(); return; }
                buffer += decoder.decode(resultado.value, { stream: true });
                let idx;
                while ((idx = buffer.indexOf('\n\n')) !== -1) {
                    const bloque = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 2);
                    procesarBloqueSSE(bloque);
                }
                leer();
            }).catch(function () { desconectarSSE(); });
        }
        leer();
    }).catch(function () { desconectarSSE(); });
}

function desconectarSSE() {
    if (sseReconnectTimer) { clearTimeout(sseReconnectTimer); sseReconnectTimer = null; }
    if (sseAbortCtrl) {
        try { sseAbortCtrl.abort(); } catch (e) { /* ignorar */ }
        sseAbortCtrl = null;
    }
    if (!document.hidden) {
        sseIntentos += 1;
        const espera = Math.min(1000 * sseIntentos, 10000);
        sseReconnectTimer = setTimeout(conectarSSE, espera);
    }
}

function procesarBloqueSSE(bloque) {
    let evento = 'mensaje';
    let datosTexto = '';
    bloque.split('\n').forEach(function (linea) {
        if (linea.startsWith('event:')) evento = linea.slice(6).trim();
        else if (linea.startsWith('data:')) datosTexto += linea.slice(5).trim();
    });
    if (!datosTexto) return;
    let datos;
    try { datos = JSON.parse(datosTexto); } catch (e) { return; }
    manejarEventoSSE(evento, datos);
}

function manejarEventoSSE(evento, datos) {
    if (evento === 'status') {
        actualizarDashboard(datos);
    }
}

/* Carga puntual del estado (fallback / al entrar a la sección ventilar). */
async function cargarStatus() {
    const data = await SIGMA.api('/api/status');
    if (data) actualizarDashboard(data);
}

function actualizarDashboard(data) {
    const esp32 = data.esp32 || {};
    actualizarConexion(esp32.connected, esp32.last_error);

    if (data.dht11) actualizarSensores(data.dht11);
    if (data.relays) {
        updateRelayDisplay(1, data.relays[1]);
        updateRelayDisplay(2, data.relays[2]);
    }
}

function actualizarConexion(connected, error) {
    const badge = document.getElementById('connection-status');
    if (!badge) return;
    if (connected) {
        badge.className = 'badge bg-success';
        badge.innerHTML = '<i class="bi bi-wifi me-1"></i>Conectado';
    } else {
        badge.className = 'badge bg-danger';
        badge.innerHTML = '<i class="bi bi-wifi-off me-1"></i>Desconectado';
        if (error) badge.title = 'Error: ' + error;
    }
    const lu = document.getElementById('last-update');
    if (lu) lu.textContent = new Date().toLocaleTimeString('es-ES');
}

function actualizarSensores(reading) {
    const temp = document.getElementById('temp-value');
    const hum = document.getElementById('humidity-value');
    const tempTime = document.getElementById('temp-time');
    const humTime = document.getElementById('humidity-time');

    if (temp) temp.textContent = reading.temperatura.toFixed(1) + '°C';
    if (hum) hum.textContent = reading.humedad.toFixed(1) + '%';
    const t = reading.timestamp ? new Date(reading.timestamp).toLocaleTimeString('es-ES') : '--';
    if (tempTime) tempTime.textContent = 'Última actualización: ' + t;
    if (humTime) humTime.textContent = 'Última actualización: ' + t;
}

function updateRelayDisplay(relayId, state) {
    // Badge y botones de la sección ventilar
    const st = document.getElementById('relay' + relayId + '-state');
    const on = document.getElementById('relay' + relayId + '-on');
    const off = document.getElementById('relay' + relayId + '-off');

    if (st) {
        st.textContent = state ? 'ENCENDIDO' : 'APAGADO';
        st.className = 'badge ' + (state ? 'badge-relay-on' : 'badge-relay-off');
    }
    if (on) on.disabled = Boolean(state);
    if (off) off.disabled = !state;
}

async function controlRelay(relayId, action) {
    const on = document.getElementById('relay' + relayId + '-on');
    const off = document.getElementById('relay' + relayId + '-off');
    if (on) on.disabled = true;
    if (off) off.disabled = true;

    const data = await SIGMA.api('/api/relay/' + relayId + '/' + action);
    if (data && data.success) {
        updateRelayDisplay(relayId, data.new_state);
        showToast('Relé ' + relayId + (action === 'on' ? ' encendido' : ' apagado'), 'success');
        cargarEventosRecientes();
    } else if (data && data.error) {
        showToast(data.error, 'danger');
    } else if (data) {
        showToast((data.message || 'Error al controlar el relé'), 'danger');
    }
}

/* ---------- HISTÓRICO ---------- */

async function loadHistoricalData(hours) {
    currentTimeRange = hours;
    const data = await SIGMA.api('/api/historical?hours=' + hours);
    if (!data) return;
    updateChart(data.readings || []);
}

function updateChart(readings) {
    const canvas = document.getElementById('historyChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    const labels = readings.map(r => new Date(r.timestamp).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' }));
    const tempData = readings.map(r => r.temperatura);
    const humidityData = readings.map(r => r.humedad);

    if (historyChart) {
        historyChart.data.labels = labels;
        historyChart.data.datasets[0].data = tempData;
        historyChart.data.datasets[1].data = humidityData;
        historyChart.update('none');
    } else {
        historyChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    { label: 'Temperatura (°C)', data: tempData, borderColor: '#e53935',
                      backgroundColor: 'rgba(229,57,53,0.1)', borderWidth: 2, fill: true, tension: 0.3,
                      pointRadius: 3, yAxisID: 'y' },
                    { label: 'Humedad (%)', data: humidityData, borderColor: '#1e88e5',
                      backgroundColor: 'rgba(30,136,229,0.1)', borderWidth: 2, fill: true, tension: 0.3,
                      pointRadius: 3, yAxisID: 'y1' }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { position: 'top', labels: { usePointStyle: true, padding: 20 } } },
                scales: {
                    x: { grid: { display: false }, ticks: { maxTicksLimit: 12, maxRotation: 45 } },
                    y: { type: 'linear', position: 'left', title: { display: true, text: 'Temperatura (°C)' },
                         min: 0, suggestedMax: 40 },
                    y1: { type: 'linear', position: 'right', title: { display: true, text: 'Humedad (%)' },
                          min: 0, max: 100, grid: { drawOnChartArea: false } }
                },
                animation: { duration: 500 }
            }
        });
    }
}

function changeTimeRange(hours) {
    document.querySelectorAll('[data-hours]').forEach(btn => {
        btn.classList.toggle('active', parseInt(btn.dataset.hours) === hours);
    });
    loadHistoricalData(hours);
}

function refreshChart() {
    loadHistoricalData(currentTimeRange);
    showToast('Gráfico actualizado', 'info');
}

/* ---------- EVENTOS ---------- */

async function cargarEventosRecientes() {
    const data = await SIGMA.api('/api/historical?hours=24');
    if (!data) return;
    const events = (data.events || [])
        .sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp))
        .slice(0, 20);

    ['events-body', 'events-body-hist'].forEach(function (id) {
        const tbody = document.getElementById(id);
        if (!tbody) return;
        if (events.length === 0) {
            tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No hay eventos recientes</td></tr>';
            return;
        }
        tbody.innerHTML = events.map(function (ev) {
            const fecha = new Date(ev.timestamp).toLocaleString('es-ES');
            const cls = ev.estado ? 'badge-relay-on' : 'badge-relay-off';
            return '<tr><td>' + fecha + '</td><td><strong>Relé ' + ev.rele +
                '</strong></td><td><span class="badge ' + cls + '">' +
                (ev.estado ? 'ON' : 'OFF') + '</span></td>' +
                '<td><span class="badge bg-secondary">' + ev.origen + '</span></td></tr>';
        }).join('');
    });
}

/* ---------- OPLÀS EN EL DASHBOARD ---------- */

let oplaTimer = null;
let oplaSeleccion = null; // { seccion, id }

async function cargarDropdownsOplas() {
    const data = await SIGMA.api('/api/dispositivos');
    if (!data || !data.success) return;
    ['jardin', 'laboratorio'].forEach(function (seccion) {
        const sel = document.getElementById('sel-opla-' + seccion);
        if (!sel) return;
        const prev = sel.value;
        const lista = (data.grupos && data.grupos[seccion]) || [];
        sel.innerHTML = '<option value="">— Seleccionar —</option>' + lista.map(function (d) {
            const etiqueta = escapeHtml(d.nombre) + (d.ip ? '' : ' (sin IP)');
            return '<option value="' + d.id + '"' + (String(d.id) === prev ? ' selected' : '') + '>' +
                etiqueta + '</option>';
        }).join('');
        sel.onchange = function () { mostrarOplaSeleccionado(seccion, this.value); };
    });
}

function mostrarOplaSeleccionado(seccion, id) {
    oplaSeleccion = id ? { seccion: seccion, id: id } : null;
    actualizarOplaSeleccionado();
}

async function actualizarOplaSeleccionado() {
    const body = document.getElementById('opla-seleccionado-body');
    if (!body) return;
    if (!oplaSeleccion) {
        body.innerHTML = '<p class="text-muted mb-0">Selecciona un Oplà para ver sus datos en vivo.</p>';
        return;
    }
    const [res, est] = await Promise.all([
        SIGMA.api('/api/dispositivos/' + oplaSeleccion.id + '/datos'),
        SIGMA.api('/api/dispositivos/' + oplaSeleccion.id + '/estado')
    ]);

    if (!res || !res.success) {
        body.innerHTML = '<div class="alert alert-danger py-2 mb-0">' +
            (res && res.online === null
                ? 'El dispositivo no tiene IP registrada.'
                : ((res && res.error) ? escapeHtml(res.error) : 'Dispositivo sin conexión.')) +
            '</div>';
        return;
    }

    const online = res.online === true;
    if (!online) {
        body.innerHTML = '<div class="alert alert-danger py-2 mb-0">Dispositivo sin conexión.</div>';
        return;
    }

    const dispositivo = res.dispositivo || {};
    const valores = res.datos || null;
    const rele = (est && est.success && est.rele) ? est.rele : null;

    const etiquetas = {
        'T': 'Temperatura', 'H': 'Humedad', 'S': 'Humedad de suelo', 'L': 'Luz',
        'P': 'Presión', 'U': 'UV', 'V': 'Voltaje',
    };
    const iconos = {
        'T': ['bi-thermometer-half', 'text-danger'], 'H': ['bi-droplet-half', 'text-primary'],
        'S': ['bi-moisture', 'text-success'], 'L': ['bi-sun', 'text-warning'],
        'P': ['bi-speedometer2', 'text-info'], 'U': ['bi-sunrise', 'text-warning'],
        'V': ['bi-battery-half', 'text-secondary'],
    };
    const unidades = { 'T': '°C', 'H': '%' };

    const claves = valores ? Object.keys(valores) : [];

    function tarjetaBig(k) {
        const ic = iconos[k];
        return '<div class="col-12 col-md-6 col-lg-4 mb-3"><div class="card sensor-card h-100">' +
            '<div class="card-body text-center">' +
            '<div class="sensor-icon ' + ic[1] + ' mb-2"><i class="bi ' + ic[0] + ' fs-1"></i></div>' +
            '<h6 class="text-muted mb-1">' + etiquetas[k] + '</h6>' +
            '<h2 class="mb-0">' + escapeHtml(valores[k]) + (unidades[k] || '') + '</h2>' +
            '</div></div></div>';
    }

    let tarjetas = '';
    ['T', 'H'].forEach(function (k) {
        if (claves.indexOf(k) !== -1) tarjetas += tarjetaBig(k);
    });

    if (rele) {
        const estados = {
            1: ['Ventilador (Relé 1)', 'bi-fan', 'text-success'],
            2: ['Luz (Relé 2)', 'bi-lightbulb', 'text-warning'],
        };
        [1, 2].forEach(function (r) {
            const conf = estados[r];
            if (!conf || !(r in rele)) return;
            const cls = rele[r] ? 'badge-relay-on' : 'badge-relay-off';
            const texto = rele[r] ? 'ENCENDIDO' : 'APAGADO';
            tarjetas += '<div class="col-12 col-md-6 col-lg-4 mb-3"><div class="card sensor-card h-100">' +
                '<div class="card-body text-center">' +
                '<div class="sensor-icon ' + conf[2] + ' mb-2"><i class="bi ' + conf[1] + ' fs-1"></i></div>' +
                '<h6 class="text-muted mb-1">' + conf[0] + '</h6>' +
                '<h4 class="mb-0"><span class="badge ' + cls + '">' + texto + '</span></h4>' +
                '</div></div></div>';
        });
    }

    claves.forEach(function (k) {
        if (k === 'T' || k === 'H' || !iconos[k]) return;
        const ic = iconos[k];
        tarjetas += '<div class="col-6 col-md-3 mb-3"><div class="card sensor-card h-100">' +
            '<div class="card-body text-center">' +
            '<div class="sensor-icon ' + ic[1] + ' mb-2"><i class="bi ' + ic[0] + '"></i></div>' +
            '<h6 class="text-muted mb-1 small">' + etiquetas[k] + '</h6>' +
            '<h5 class="mb-0">' + escapeHtml(valores[k]) + '</h5>' +
            '</div></div></div>';
    });

    const zona = dispositivo.zona
        ? '<small class="text-muted">Zona: ' + escapeHtml(dispositivo.zona) + '</small>'
        : '';
    const cabecera = '<div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">' +
        '<div><h6 class="mb-1"><i class="bi bi-cpu me-1 text-primary"></i>' +
        escapeHtml(dispositivo.nombre || 'Oplà') + '</h6>' + zona + '</div>' +
        '<span class="badge badge-relay-on">En línea</span></div>';

    if (!tarjetas) {
        body.innerHTML = cabecera +
            '<div class="alert alert-info py-2 mb-0">Dispositivo en línea pero sin datos.</div>';
        return;
    }
    body.innerHTML = cabecera + '<div class="row g-0">' + tarjetas + '</div>';
}

/* ---------- TOASTS ---------- */

function showToast(message, type) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container position-fixed bottom-0 end-0 p-3';
        container.style.zIndex = '1055';
        document.body.appendChild(container);
    }
    const bg = { success: 'bg-success', danger: 'bg-danger', warning: 'bg-warning' }[type] || 'bg-info';
    const id = 'toast-' + Date.now();
    container.insertAdjacentHTML('beforeend',
        '<div id="' + id + '" class="toast ' + bg + ' text-white" role="alert" aria-live="assertive" aria-atomic="true">' +
        '<div class="toast-header ' + bg + ' text-white border-0"><strong class="me-auto">SIGMA-IOT</strong>' +
        '<button type="button" class="btn-close btn-close-white ms-2" data-bs-dismiss="toast"></button></div>' +
        '<div class="toast-body">' + message + '</div></div>');

    const el = document.getElementById(id);
    new bootstrap.Toast(el, { delay: 3000 }).show();
    el.addEventListener('hidden.bs.toast', () => el.remove());
}

/* ---------- VISIBILIDAD DE PESTAÑA ---------- */

document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
        detenerDashboard();
    } else {
        iniciarDashboard();
    }
});