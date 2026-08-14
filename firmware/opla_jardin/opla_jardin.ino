#include <Arduino_MKRIoTCarrier.h>
#include <WiFiNINA.h>
#include <WiFiUdp.h>
#include <math.h>

// ============================================================
// RED: el Oplà de jardín se conecta al mismo hotspot que el
// resto del sistema y sirve un servidor TCP en el puerto 9001
// para que el dashboard Flask lo vea en línea y consulte datos:
//   PING -> PONG | DATA -> S:..,L:..,T:..,H:..,P:..
// También responde a descubrimiento UDP (FIND_OPLA -> IP:x.x.x.x)
// en el puerto 5002 para que el dashboard lo localice solo.
// ============================================================
const char ssid[] = "LAP_DARA 9861";
const char pass[] = "87654321";
const uint16_t serverPort = 9001;
const uint16_t udpPort = 5002;

WiFiServer tcpServer(serverPort);
WiFiUDP udp;
bool wfOk = false;

IPAddress gatewayIP(192, 168, 137, 1);
unsigned long lastPingCheck = 0;
int pingFails = 0;

#define GRIS 0x7BEF
#define NARANJA 0xFD20
#define FONDO_CARD 0x0008
#define FONDO_BAND 0x000B
#define SOMBRA 0x2104

MKRIoTCarrier carrier;

// ============================================================
// CALIBRACION DEL SENSOR DE SUELO v1.2 (capacitivo, SEN0193)
// 1) Sensor al AIRE (seco): anota el raw -> SOIL_AIR_RAW (0%)
// 2) Sensor en AGUA (mojado): anota el raw -> SOIL_WATER_RAW (100%)
// NOTA: en este sensor seco = ADC ALTO, mojado = ADC BAJO.
// El orden se soporta de forma implicita gracias a map() descendente.
// SOIL_RAW_TOL: margen sobre la calibracion; si el raw cae fuera,
// se considera sensor desconectado o con fallo y se muestra "Sensando...".
// ============================================================
const int SOIL_AIR_RAW = 520;
const int SOIL_WATER_RAW = 260;
const int SOIL_RAW_TOL = 40;

// ============================================================
// UMBRALES DE LUZ (canal "clear" del APDS-9960, sin lux reales)
// Debajo de LIGHT_LOW = "poca luz"; en/sobre LIGHT_SAT = sol fuerte
// (el canal se satura ~65535 con sol directo). Ajustar con los
// valores que imprima Serial (luz interior vs. sol).
// ============================================================
const int LIGHT_LOW = 200;
const int LIGHT_SAT = 60000;

// ============================================================
// UMBRALES DE HUMEDAD DE SUELO (hortalizas generales)
// <20% = regar | 20-30% = seco | 30-50% = correcta | >50% = muy humeda
// SOIL_HYST: ancho de la zona muerta. Evita el parpadeo de color en
// los bordes (p. ej. 29<->30): al subir se usan 20/30/50, al bajar
// 15/25/45. El color solo cambia si el % recorre todo el ancho.
// ============================================================
const int SOIL_REGAR = 20;
const int SOIL_SECO = 30;
const int SOIL_MOJA = 50;
const int SOIL_HYST = 5;

// Intervalos de lectura (ms). El clima necesita 500 ms para que
// el BSEC (rev2) procese muestras a su cadencia LP (~300 ms).
const unsigned long SOIL_INTERVAL = 2000;
const unsigned long ENV_INTERVAL = 500;
const unsigned long LIGHT_INTERVAL = 1000;

const int numPages = 2;
int page = 0;
int gardenScroll = 0;

int moistPin;
int soilPct = -1;
int soilBand = -1;
bool soilSensorError = false;
bool shownSoilError = false;
int lightClear = -1;
int lastLedState = -1;
int shownSoil = -2;
int shownBand = -2;
int shownLight = -2;
float shownEnvTemp = -99;
float shownEnvHum = -1;
float shownEnvPres = -1;
float shownClimaTemp = -99;
float shownClimaHum = -1;
float shownClimaPres = -1;
String shownIp = "";

float envTemp = -1;
float envHum = -1;
float envPres = -1;

unsigned long lastSoilRead = 0;
unsigned long lastEnvRead = 0;
unsigned long lastLightRead = 0;
unsigned long lastLedUpdate = 0;

unsigned long touchStableAt[5];
unsigned long lastTouchClick[5];
bool touchWasOn[5];

void drawText(int x, int y, const char* s, int size, uint16_t color) {
  carrier.display.setCursor(x, y);
  carrier.display.setTextSize(size);
  carrier.display.setTextColor(color);
  carrier.display.print(s);
}

void centerText(int y, const char* s, int size, uint16_t color) {
  int w = strlen(s) * 6 * size;
  drawText((240 - w) / 2, y, s, size, color);
}

void centerTextShadow(int y, const char* s, int size, uint16_t color) {
  int w = strlen(s) * 6 * size;
  drawText((240 - w) / 2 + 1, y + 1, s, size, SOMBRA);
  drawText((240 - w) / 2, y, s, size, color);
}

// El core SAMD (newlib-nano) NO soporta %f en sprintf.
// Este formateador convierte float a string con enteros.
void formatFloat(char* out, float value, int decimals) {
  if (value < 0) {
    out[0] = '-';
    formatFloat(out + 1, -value, decimals);
    return;
  }
  long scale = 1;
  for (int i = 0; i < decimals; i++) scale *= 10;
  long whole = (long)value;
  long frac = (long)((value - whole) * scale + 0.5);
  if (frac >= scale) {
    whole++;
    frac = 0;
  }
  if (decimals == 0) {
    sprintf(out, "%ld", whole);
  } else {
    sprintf(out, "%ld.%0*ld", whole, decimals, frac);
  }
}

void drawSun(int cx, int cy, int r, uint16_t color) {
  carrier.display.fillCircle(cx, cy, r, color);
  int rays[8][2] = {{1, 0}, {0, 1}, {-1, 0}, {0, -1}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}};
  for (int i = 0; i < 8; i++) {
    carrier.display.drawLine(cx + rays[i][0] * (r + 1), cy + rays[i][1] * (r + 1),
                             cx + rays[i][0] * (r + 3), cy + rays[i][1] * (r + 3), color);
  }
}

// Pantalla circular: radio visible ~108 px centrado en (120,120).
// halfW(y) = medio ancho visible a la altura y, con margen.
int halfW(int y) {
  int dy = y - 120;
  long d = 108L * 108 - (long)dy * dy;
  if (d < 0) d = 0;
  return (int)sqrtf((float)d) - 12;
}

void drawCard(int y, int h, uint16_t border) {
  int top = halfW(y);
  int bot = halfW(y + h);
  int hw = top < bot ? top : bot;
  if (hw < 20) hw = 20;
  int x = 120 - hw;
  carrier.display.fillRoundRect(x, y, hw * 2, h, 10, FONDO_CARD);
  carrier.display.drawRoundRect(x, y, hw * 2, h, 10, border);
}

void drawTitle(const char* title) {
  carrier.display.fillRect(0, 0, 240, 52, FONDO_BAND);
  centerText(20, title, 2, ST77XX_CYAN);
  carrier.display.fillRect(0, 48, 240, 3, GRIS);
}

void drawPageDots() {
  int startX = 120 - (numPages - 1) * 4;
  for (int i = 0; i < numPages; i++) {
    carrier.display.fillCircle(startX + i * 8, 62, 3, i == page ? ST77XX_WHITE : GRIS);
  }
}

void drawScrollChevrons() {
  uint16_t upCol = gardenScroll > 0 ? ST77XX_WHITE : GRIS;
  uint16_t dnCol = gardenScroll < 1 ? ST77XX_WHITE : GRIS;
  carrier.display.fillTriangle(218, 104, 226, 104, 222, 98, upCol);
  carrier.display.fillTriangle(218, 132, 226, 132, 222, 138, dnCol);
}

const char* soilMessage(int band) {
  switch (band) {
    case 0: return "NECESITA REGAR!";
    case 1: return "Seco, regar pronto";
    case 2: return "Humedad correcta";
    default: return "Muy humeda";
  }
}

uint16_t soilColor(int band) {
  switch (band) {
    case 0: return ST77XX_RED;
    case 1: return NARANJA;
    case 2: return ST77XX_GREEN;
    default: return ST77XX_BLUE;
  }
}

const char* lightMessage(int c) {
  if (c >= LIGHT_SAT) return "PROTEGER CULTIVOS";
  if (c < LIGHT_LOW) return "Poca luz";
  return "Luz adecuada";
}

const char* lightStatus(int c) {
  if (c >= LIGHT_SAT) return "SOL INTENSO";
  if (c < LIGHT_LOW) return "POCA LUZ";
  return "LUZ OK";
}

uint16_t lightColor(int c) {
  if (c >= LIGHT_SAT) return ST77XX_RED;
  if (c < LIGHT_LOW) return ST77XX_BLUE;
  return ST77XX_GREEN;
}

void drawSoilBar(int pct, uint16_t color, int y, int segW, int gap) {
  int n = 10;
  int total = n * segW + (n - 1) * gap;
  int x0 = (240 - total) / 2;
  int h = 12;
  int filled = (pct + 5) / 10;
  for (int i = 0; i < n; i++) {
    int x = x0 + i * (segW + gap);
    carrier.display.fillRoundRect(x, y, segW, h, 3, i < filled ? color : 0x2104);
  }
}

// Fila de clima: etiqueta a la izquierda + numero grande + unidad.
void drawClimaRow(int y, const char* label, uint16_t lColor,
                  float value, int decimals, const char* unit, bool valid) {
  char num[8];
  int lw = strlen(label) * 6;
  int uw = strlen(unit) * 6;
  formatFloat(num, value, decimals);
  int vw = (valid ? strlen(num) : 2) * 18;
  int total = lw + 8 + vw + 4 + uw;
  int x0 = (240 - total) / 2;

  drawText(x0, y, label, 1, lColor);
  if (valid) {
    drawText(x0 + lw + 8, y - 4, num, 3, lColor);
  } else {
    drawText(x0 + lw + 8, y - 4, "--", 3, GRIS);
  }
  drawText(x0 + lw + 8 + vw + 4, y, unit, 1, GRIS);
}

void drawClimaRows(int y0) {
  drawClimaRow(y0, "TEMPERATURA", ST77XX_YELLOW, envTemp, 1, "C", envTemp >= 0);
  carrier.display.drawFastHLine(40, y0 + 30, 160, GRIS);
  drawClimaRow(y0 + 36, "HUMEDAD", ST77XX_CYAN, envHum, 0, "%", envHum >= 0);
  carrier.display.drawFastHLine(40, y0 + 66, 160, GRIS);
  drawClimaRow(y0 + 72, "PRESION", ST77XX_GREEN, envPres, 1, "kPa", envPres > 0);
}

void drawGardenPage() {
  if (gardenScroll == 0) {
    if (soilPct == shownSoil && soilSensorError == shownSoilError && soilBand == shownBand) return;
    drawTitle("JARDIN");
    drawPageDots();
    drawScrollChevrons();

    uint16_t sCol = soilPct >= 0 ? soilColor(soilBand) : GRIS;
    drawCard(70, 110, sCol);

    if (soilPct >= 0) {
      char buf[12];
      centerText(74, "HUMEDAD", 2, ST77XX_WHITE);
      centerText(90, "DEL SUELO", 1, GRIS);
      sprintf(buf, "%d%%", soilPct);
      centerTextShadow(100, buf, 5, sCol);
      drawSoilBar(soilPct, sCol, 146, 12, 2);
      centerText(164, soilMessage(soilBand), 1, sCol);
    } else {
      centerText(74, "HUMEDAD", 2, ST77XX_WHITE);
      centerText(90, "DEL SUELO", 1, GRIS);
      centerTextShadow(112, "--", 5, GRIS);
      centerText(164, soilSensorError ? "Revisar sensor" : "Sensando...", 1, GRIS);
    }
    shownSoil = soilPct;
    shownSoilError = soilSensorError;
    shownBand = soilBand;
  } else {
    if (lightClear == shownLight) return;
    drawTitle("JARDIN");
    drawPageDots();
    drawScrollChevrons();

    uint16_t lCol = lightClear >= 0 ? lightColor(lightClear) : GRIS;
    drawCard(70, 92, lCol);

    if (lightClear >= 0) {
      char buf[16];
      drawSun(120, 86, 9, lCol);
      centerText(104, lightStatus(lightClear), 2, lCol);
      sprintf(buf, "clear %d", lightClear);
      centerText(126, buf, 2, ST77XX_WHITE);
      centerText(146, lightMessage(lightClear), 1, lCol);
    } else {
      drawSun(120, 86, 9, GRIS);
      centerText(104, "--", 2, GRIS);
      centerText(146, "Sensando...", 1, GRIS);
    }
    shownLight = lightClear;
  }
}

void drawClimatePage() {
  String ip = wfOk ? WiFi.localIP().toString() : "SIN RED";
  if (envTemp == shownClimaTemp && envHum == shownClimaHum && envPres == shownClimaPres && ip == shownIp) return;

  drawTitle("CLIMA");
  drawPageDots();
  drawClimaRows(82);

  String ipLine = "IP " + ip;
  drawText(24, 200, ipLine.c_str(), 1, wfOk ? ST77XX_GREEN : ST77XX_RED);

  shownClimaTemp = envTemp;
  shownClimaHum = envHum;
  shownClimaPres = envPres;
  shownIp = ip;
}

void drawPage() {
  carrier.display.fillRect(0, 0, 240, 240, ST77XX_BLACK);
  if (page == 0) {
    drawGardenPage();
  } else {
    drawClimatePage();
  }
}

void updateLed() {
  int state;
  if (soilBand >= 0) {
    state = soilBand;
  } else if (lightClear >= LIGHT_SAT) {
    state = 4;
  } else if (lightClear >= 0 && lightClear < LIGHT_LOW) {
    state = 5;
  } else {
    state = 6;
  }

  if (state == lastLedState) return;
  lastLedState = state;

  uint32_t color = 0;
  switch (state) {
    case 0: color = carrier.leds.Color(255, 0, 0); break;
    case 1: color = carrier.leds.Color(255, 120, 0); break;
    case 2: color = carrier.leds.Color(0, 255, 0); break;
    case 3: color = carrier.leds.Color(0, 100, 255); break;
    case 4: color = carrier.leds.Color(255, 0, 0); break;
    case 5: color = carrier.leds.Color(0, 0, 255); break;
    case 6: color = carrier.leds.Color(20, 20, 20); break;
  }
  for (int i = 0; i < 5; i++) {
    carrier.leds.setPixelColor(i, color);
  }
  carrier.leds.show();
}

void readSoil() {
  long sum = 0;
  const int n = 8;
  for (int i = 0; i < n; i++) {
    sum += analogRead(moistPin);
    delay(5);
  }
  int raw = sum / n;

  if (raw > SOIL_AIR_RAW + SOIL_RAW_TOL || raw < SOIL_WATER_RAW - SOIL_RAW_TOL) {
    soilPct = -1;
    soilBand = -1;
    soilSensorError = true;
    Serial.print("Suelo raw=");
    Serial.print(raw);
    Serial.println(" FUERA DE RANGO: revisar conexion del sensor");
    if (page == 0) {
      drawGardenPage();
    }
    return;
  }

  soilSensorError = false;
  int pct = map(raw, SOIL_AIR_RAW, SOIL_WATER_RAW, 0, 100);
  pct = constrain(pct, 0, 100);
  soilPct = pct;

  // Banda con histeresis: los limites dependen de la direccion.
  int band = soilBand;
  if (band < 0) {
    if (pct < SOIL_REGAR) band = 0;
    else if (pct < SOIL_SECO) band = 1;
    else if (pct <= SOIL_MOJA) band = 2;
    else band = 3;
  } else if (band == 0) {
    if (pct >= SOIL_REGAR) band = 1;
  } else if (band == 1) {
    if (pct < SOIL_REGAR - SOIL_HYST) band = 0;
    else if (pct >= SOIL_SECO) band = 2;
  } else if (band == 2) {
    if (pct < SOIL_SECO - SOIL_HYST) band = 1;
    else if (pct > SOIL_MOJA) band = 3;
  } else {
    if (pct < SOIL_MOJA - SOIL_HYST) band = 2;
  }
  soilBand = band;

  Serial.print("Suelo raw=");
  Serial.print(raw);
  Serial.print(" (aire:");
  Serial.print(SOIL_AIR_RAW);
  Serial.print(" agua:");
  Serial.print(SOIL_WATER_RAW);
  Serial.print(") -> ");
  Serial.print(soilPct);
  Serial.println("%");

  if (page == 0) {
    drawGardenPage();
  }
}

void readLight() {
  if (millis() - lastLightRead < LIGHT_INTERVAL) return;
  lastLightRead = millis();

  if (!carrier.Light.colorAvailable()) return;

  int r, g, b, c;
  if (!carrier.Light.readColor(r, g, b, c)) {
    Serial.println("Luz: error de lectura, se conserva el ultimo valor valido");
    return;
  }
  if (c > 65000) c = 65000;

  bool redraw = false;
  if (lightClear < 0) {
    redraw = true;
  } else if ((c >= LIGHT_SAT) != (lightClear >= LIGHT_SAT)) {
    redraw = true;
  } else if ((c < LIGHT_LOW) != (lightClear < LIGHT_LOW)) {
    redraw = true;
  } else {
    int diff = c - lightClear;
    if (diff < 0) diff = -diff;
    if (diff > lightClear / 10) redraw = true;
  }

  lightClear = c;
  Serial.print("Luz clear=");
  Serial.println(lightClear);

  if (redraw && page == 0) {
    drawGardenPage();
  }
}

void readEnv() {
  float t = carrier.Env.readTemperature();
  float h = carrier.Env.readHumidity();
  float p = carrier.Pressure.readPressure();

  Serial.print("Clima RAW: T=");
  Serial.print(t, 2);
  Serial.print(" H=");
  Serial.print(h, 2);
  Serial.print(" P=");
  Serial.print(p, 2);
  Serial.println();

  // rev2 (BME68X+BSEC): durante el calentamiento devuelve 0.0
  float newT = envTemp;
  float newH = envHum;
  float newP = envPres;

  if (!isnan(t) && t != 0.0f && t > -40 && t < 80) newT = t;
  if (!isnan(h) && h != 0.0f && h >= 0 && h <= 100) newH = h;
  if (!isnan(p) && p != 0.0f && p > 60 && p < 110) newP = p;

  bool changed = false;
  if (newT != envTemp) { envTemp = newT; changed = true; }
  if (newH != envHum) { envHum = newH; changed = true; }
  if (newP != envPres) { envPres = newP; changed = true; }

  if (changed) {
    if (page == 0) {
      drawGardenPage();
    } else {
      drawClimatePage();
    }
  }
}

bool touchPressed(int pad) {
  unsigned long now = millis();
  bool on = carrier.Buttons.getTouch((touchButtons)pad);
  if (!on) {
    touchWasOn[pad] = false;
    touchStableAt[pad] = 0;
    return false;
  }
  if (!touchWasOn[pad]) {
    touchWasOn[pad] = true;
    touchStableAt[pad] = now;
    return false;
  }
  if (touchStableAt[pad] == 0) return false;
  if (now - touchStableAt[pad] < 60) return false;
  if (now - lastTouchClick[pad] < 300) return false;
  touchStableAt[pad] = 0;
  lastTouchClick[pad] = now;
  return true;
}

void forceRedraw() {
  shownSoil = -2;
  shownSoilError = false;
  shownBand = -2;
  shownLight = -2;
  shownEnvTemp = -99;
  shownEnvHum = -1;
  shownEnvPres = -1;
  shownClimaTemp = -99;
  shownClimaHum = -1;
  shownClimaPres = -1;
  drawPage();
}

void changePage(int delta) {
  page = (page + delta + numPages) % numPages;
  forceRedraw();
  Serial.print("Pagina: ");
  Serial.println(page);
}

void changeGardenScroll(int delta) {
  int old = gardenScroll;
  gardenScroll += delta;
  if (gardenScroll < 0) gardenScroll = 0;
  if (gardenScroll > 1) gardenScroll = 1;
  if (gardenScroll != old) {
    forceRedraw();
    Serial.print("Scroll JARDIN: ");
    Serial.println(gardenScroll);
  }
}

// ============================================================
// RED: conexión al hotspot (bloqueante con timeout)
// ============================================================
void connectWiFi() {
  Serial.print("Conectando WiFi (");
  Serial.print(ssid);
  Serial.print(")");
  WiFi.begin(ssid, pass);
  unsigned long inicio = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - inicio < 20000) {
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    WiFi.noLowPowerMode();
    wfOk = true;
    tcpServer.begin();
    udp.begin(udpPort);
    Serial.println(" OK");
    Serial.print(">>> IP DEL JARDIN: ");
    Serial.println(WiFi.localIP());
  } else {
    wfOk = false;
    Serial.println(" NO CONECTADO (revisar hotspot 2.4GHz)");
  }
}

// ============================================================
// RED: verifica conectividad real haciendo ping al gateway.
// WiFiNINA puede quedar "colgado" reportando WL_CONNECTED sin
// red real; si el ping falla 2 veces se fuerza reconexión
// completa con WiFi.end() + WiFi.begin().
// ============================================================
void verificarWiFi() {
  unsigned long now = millis();
  if (now - lastPingCheck < 15000) return;
  lastPingCheck = now;

  int r = WiFi.ping(gatewayIP);
  if (r > 0) {
    pingFails = 0;
    return;
  }
  pingFails++;
  Serial.print("Ping gateway falló ("); Serial.print(pingFails); Serial.println(")");
  if (pingFails >= 2) {
    Serial.println("WiFi muerto: reiniciando NINA...");
    wfOk = false;
    udp.stop();
    WiFi.end();
    delay(500);
    WiFi.begin(ssid, pass);
    unsigned long inicio = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - inicio < 15000) {
      delay(500);
    }
    if (WiFi.status() == WL_CONNECTED) {
      WiFi.noLowPowerMode();
      wfOk = true;
      pingFails = 0;
      tcpServer.begin();
      udp.begin(udpPort);
      forceRedraw();
      Serial.print(">>> IP DEL JARDIN: ");
      Serial.println(WiFi.localIP());
    } else {
      Serial.println("Sin WiFi tras reinicio");
    }
  }
}

// ============================================================
// RED: atiende descubrimiento UDP (dashboard Flask).
// FIND_OPLA -> IP:x.x.x.x
// ============================================================
void handleUdp() {
  if (!wfOk) return;
  int sz = udp.parsePacket();
  if (sz <= 0) return;
  char buf[32];
  int n = udp.read(buf, sizeof(buf) - 1);
  if (n <= 0) return;
  buf[n] = '\0';
  String cmd(buf);
  cmd.trim();
  if (cmd == "FIND_OPLA") {
    String resp = "IP:";
    resp += WiFi.localIP().toString();
    udp.beginPacket(udp.remoteIP(), udp.remotePort());
    udp.print(resp);
    udp.endPacket();
    Serial.print("Descubrimiento -> ");
    Serial.println(resp);
  }
}

// ============================================================
// RED: atiende una conexión TCP entrante (dashboard Flask).
// PING -> PONG | DATA -> S:..,L:..,T:..,H:..,P:..
// ============================================================
void handleTcp() {
  if (!wfOk) return;
  WiFiClient client = tcpServer.available();
  if (!client) return;
  Serial.println("Cliente TCP conectado");

  String line = "";
  unsigned long start = millis();
  while (client.connected() && millis() - start < 3000) {
    while (client.available()) {
      char c = client.read();
      if (c == '\n' || c == '\r') {
        if (line.length() == 0) continue;
        line.trim();
        Serial.print("Recibido: ");
        Serial.println(line);
        String resp;
        if (line == "PING") {
          resp = "PONG";
        } else if (line == "DATA") {
          char s[10], t[10], h[10], p[10];
          formatFloat(s, soilPct, 0);
          formatFloat(t, envTemp, 1);
          formatFloat(h, envHum, 0);
          formatFloat(p, envPres, 1);
          resp = "S:" + String(s) + ",L:" + String(lightClear) +
                 ",T:" + String(t) + ",H:" + String(h) + ",P:" + String(p);
        } else {
          resp = "ERR";
        }
        client.print(resp);
        client.print("\r\n");
        client.flush();
        line = "";
      } else {
        line += c;
      }
    }
  }
  client.stop();
  Serial.println("Cliente TCP desconectado");
}

void setup() {
  Serial.begin(9600);
  delay(500);

  carrier.withCase();
  carrier.begin();

  moistPin = (carrier.getBoardRevision() == 1) ? A5 : A0;
  pinMode(moistPin, INPUT);

  Serial.println("=== JARDIN OPLA ===");
  Serial.print("Revision del carrier: ");
  Serial.println(carrier.getBoardRevision());
  Serial.print("Pin de suelo: A");
  Serial.println(moistPin == A5 ? 5 : 0);
  Serial.println();
  Serial.println("CALIBRACION DEL SENSOR v1.2:");
  Serial.println(" 1) Sensor al AIRE: anota el raw -> SOIL_AIR_RAW (seco=0%)");
  Serial.println(" 2) Sensor en AGUA: anota el raw -> SOIL_WATER_RAW (mojado=100%)");
  Serial.println(" Edita las constantes en opla_jardin.ino y sube de nuevo.");
  Serial.println();

  carrier.display.fillScreen(ST77XX_BLACK);
  carrier.display.setRotation(0);
  carrier.display.setTextWrap(false);
  drawPage();
  updateLed();

  connectWiFi();
}

unsigned long lastWifiTry = 0;
bool wifiTrying = false;

void loop() {
  if (millis() - lastSoilRead >= SOIL_INTERVAL) {
    lastSoilRead = millis();
    readSoil();
  }

  if (millis() - lastEnvRead >= ENV_INTERVAL) {
    lastEnvRead = millis();
    readEnv();
  }

  if (millis() - lastLedUpdate >= 500) {
    lastLedUpdate = millis();
    updateLed();
  }

  readLight();
  carrier.Buttons.update();

  verificarWiFi();

  // Reconexión WiFi no bloqueante
  if (!wfOk && !wifiTrying && millis() - lastWifiTry > 10000) {
    wifiTrying = true;
    lastWifiTry = millis();
    Serial.println("Reintentando WiFi...");
    WiFi.end();
    delay(200);
    WiFi.begin(ssid, pass);
  } else if (wifiTrying && WiFi.status() == WL_CONNECTED) {
    wifiTrying = false;
    wfOk = true;
    WiFi.noLowPowerMode();
    tcpServer.begin();
    udp.begin(udpPort);
    forceRedraw();
    Serial.print(">>> IP DEL JARDIN: ");
    Serial.println(WiFi.localIP());
  } else if (wifiTrying && millis() - lastWifiTry > 30000) {
    wifiTrying = false;
  }
  if (wfOk && WiFi.status() != WL_CONNECTED) {
    wfOk = false;
    forceRedraw();
    Serial.println("WiFi perdido");
  }

  if (wfOk) {
    handleTcp();
    handleUdp();
  }

  if (touchPressed(1)) {
    changePage(-1);
  }
  if (touchPressed(3)) {
    changePage(1);
  }

  if (page == 0) {
    if (touchPressed(0)) {
      changeGardenScroll(-1);
    }
    if (touchPressed(4)) {
      changeGardenScroll(1);
    }
  }

  delay(10);
}