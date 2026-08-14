# Jardín Inteligente - Panel de Control Flask

Sistema completo de monitoreo y control para jardín inteligente con Arduino Oplà (MKR WiFi 1010) y ESP32.

## Características

- **Dashboard web** en tiempo real con Bootstrap 5
- **Base de datos PostgreSQL** (Aiven) para persistencia
- **Comunicación TCP/IP** con ESP32 (puerto 9001)
- **Lectura automática** de sensor DHT11 cada 15 segundos
- **Control de relés** (2 canales) desde la web
- **Gráficos históricos** con Chart.js (24h, 48h, etc.)
- **API REST** para integración externa
- **Programador de tareas** en segundo plano (APScheduler)

## Estructura del Proyecto

```
jardin-inteligente/
├── app.py                 # Aplicación principal Flask
├── requirements.txt       # Dependencias Python
├── .env                   # Variables de entorno (NO commitear)
├── templates/
│   └── index.html        # Dashboard principal
├── static/
│   ├── css/
│   │   └── style.css     # Estilos personalizados
│   └── js/
│       └── dashboard.js  # Lógica frontend
└── README.md             # Este archivo
```

## Requisitos Previos

- Python 3.11+ (compatible con 3.14)
- PostgreSQL accesible (Aiven Cloud)
- ESP32 funcionando como servidor TCP en puerto 9001
- Red local accesible entre servidor Flask y ESP32

## Instalación

### 1. Clonar/Crear el proyecto

```bash
cd "C:\Users\chave\OneDrive\Escritorio\Proyecto Relee"
```

### 2. Crear entorno virtual

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux/Mac
python3 -m venv venv
source venv/bin/activate
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno

El archivo `.env` ya está creado con la configuración. Verifica que la cadena de conexión sea correcta:

```env
DATABASE_URL=postgres://avnadmin:TU_PASSWORD_AQUI@pg-1a687fbe-chavezrios2005-f4d8.d.aivencloud.com:28518/defaultdb?sslmode=require
ESP32_IP=192.168.0.247
ESP32_PORT=9001
FLASK_ENV=development
FLASK_DEBUG=1
SECRET_KEY=cambia-esta-clave-en-produccion
```

> **Importante**: Cambia `SECRET_KEY` por una clave segura en producción.

### 5. Verificar conectividad con ESP32

Antes de iniciar, asegúrate de que el ESP32 esté accesible:

```bash
# Probar conexión TCP (Windows PowerShell)
Test-NetConnection -ComputerName 192.168.0.247 -Port 9001

# O con netcat (Linux/Mac)
nc -zv 192.168.0.247 9001
```

### 6. Inicializar base de datos

Las tablas se crean automáticamente al iniciar la aplicación, pero puedes verificarlo:

```bash
python -c "from app import app, db; app.app_context().push(); db.create_all(); print('Tablas creadas')"
```

## Ejecución

### Desarrollo

```bash
python app.py
```

El servidor estará disponible en:
- **Dashboard**: http://localhost:5000
- **API Status**: http://localhost:5000/api/status
- **API Historical**: http://localhost:5000/api/historical
- **Health Check**: http://localhost:5000/health

### Producción (con Gunicorn)

```bash
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

### Como servicio systemd (Linux)

Crear `/etc/systemd/system/jardin-inteligente.service`:

```ini
[Unit]
Description=Jardín Inteligente Flask App
After=network.target

[Service]
Type=simple
User=tu_usuario
WorkingDirectory=/ruta/al/proyecto
Environment=PATH=/ruta/al/proyecto/venv/bin
ExecStart=/ruta/al/proyecto/venv/bin/gunicorn -w 4 -b 0.0.0.0:5000 app:app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable jardin-inteligente
sudo systemctl start jardin-inteligente
sudo systemctl status jardin-inteligente
```

## API Endpoints

### GET /api/status
Estado actual del sistema
```json
{
  "esp32": {
    "connected": true,
    "last_check": "2026-08-05T16:30:00",
    "ip": "192.168.0.247",
    "port": 9001,
    "last_error": null
  },
  "dht11": {
    "id": 123,
    "timestamp": "2026-08-05T16:30:00",
    "temperatura": 25.5,
    "humedad": 60.2
  },
  "relays": {
    "1": true,
    "2": false
  },
  "timestamp": "2026-08-05T16:30:00"
}
```

### GET /api/historical?hours=24
Datos históricos (por defecto 24h, máx 168h = 7 días)
```json
{
  "readings": [...],
  "events": [...]
}
```

### GET /api/relay/<id>/<action>
Controlar relé (id: 1 o 2, action: on/off)
```bash
curl http://localhost:5000/api/relay/1/on
curl http://localhost:5000/api/relay/2/off
```
Respuesta:
```json
{
  "success": true,
  "message": "Comando ejecutado correctamente",
  "relay": 1,
  "action": "on",
  "new_state": true
}
```

### GET /api/relay/state
Estado actual de ambos relés
```json
{ "1": true, "2": false }
```

## Protocolo TCP ESP32

El Flask se conecta al ESP32 como cliente TCP (conexiones breves):

| Comando | Descripción | Respuesta esperada |
|---------|-------------|-------------------|
| `DATA` | Solicitar lectura DHT11 | `T:25.50,H:60.25` |
| `ON 1` | Encender relé 1 | `OK` |
| `OFF 1` | Apagar relé 1 | `OK` |
| `ON 2` | Encender relé 2 | `OK` |
| `OFF 2` | Apagar relé 2 | `OK` |
| `PING` | Verificar conexión | `PONG` |

## Tareas Programadas (Background)

- **Cada 15s**: `poll_esp32_data()` - Lee DHT11 y guarda en BD
- **Cada 60s**: Verificación de conexión ESP32 (PING)

## Base de Datos

### Tabla: dht11_readings
```sql
id          SERIAL PRIMARY KEY
timestamp   TIMESTAMP DEFAULT NOW()
temperatura FLOAT NOT NULL
humedad     FLOAT NOT NULL
```

### Tabla: relay_events
```sql
id         SERIAL PRIMARY KEY
timestamp  TIMESTAMP DEFAULT NOW()
rele       INTEGER NOT NULL (1 o 2)
estado     BOOLEAN NOT NULL
origen     VARCHAR(50) DEFAULT 'dashboard'
```

## Solución de Problemas

### Error de conexión a PostgreSQL
- Verifica que la IP del servidor esté en la whitelist de Aiven
- Comprueba credenciales en `.env`
- Prueba conexión: `psql "postgres://avnadmin:PASS@host:port/db?sslmode=require"`

### ESP32 no responde
- Verifica IP en `.env` coincida con la del ESP32
- Comprueba que el ESP32 esté en la misma red
- Revisa firewall/antivirus bloqueando puerto 9001
- Verifica logs del ESP32 (Serial Monitor)

### Tablas no se crean
```bash
python -c "
from app import app, db
with app.app_context():
    db.create_all()
    print('Tablas creadas:', db.engine.table_names())
"
```

### Puerto 5000 ocupado
```bash
# Cambiar puerto en .env o línea de comandos
PORT=5001 python app.py
```

## Seguridad

Para producción:
1. Cambia `SECRET_KEY` por una cadena aleatoria larga
2. Configura `FLASK_ENV=production` y `FLASK_DEBUG=0`
3. Usa HTTPS (reverse proxy con Nginx + Certbot)
4. Restringe acceso por IP o añade autenticación
5. No commitees el archivo `.env` real

## Desarrollo

### Agregar nuevo sensor
1. Crear modelo en `app.py`
2. Añadir parsing en `poll_esp32_data()`
3. Actualizar `/api/status` y frontend

### Modificar intervalos
En `init_scheduler()`:
```python
# Cambiar 15 segundos a 30
scheduler.add_job(func=poll_esp32_data, trigger='interval', seconds=30, ...)
```

## Licencia

Proyecto personal - Sistema de Jardín Inteligente

## Soporte

Para dudas o problemas, revisa los logs de la consola donde se ejecuta `python app.py`.