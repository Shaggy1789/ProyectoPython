#include <WiFiNINA.h>
#include <Arduino_MKRIoTCarrier.h>
#include <math.h>
#include <WiFiUdp.h>

const char ssid[] = "LAP_DARA 9861";
const char pass[] = "87654321";

char esp32IP[16] = "192.168.137.148";  // IP de respaldo si no hay respuesta UDP
const uint16_t port = 9001;
const uint16_t searchPort = 5001;
WiFiUDP udp;
unsigned long lastSearch = 0;

IPAddress gatewayIP(192, 168, 137, 1);
unsigned long lastPingCheck = 0;
int pingFails = 0;

bool buscarESP32() {
  udp.begin(0);
  udp.beginPacket(IPAddress(255, 255, 255, 255), searchPort);
  udp.write("FIND_ESP32\n");
  udp.endPacket();
  unsigned long inicio = millis();
  while (millis() - inicio < 1000) {
    int sz = udp.parsePacket();
    if (sz > 0) {
      char buf[24];
      int n = udp.read(buf, sizeof(buf) - 1);
      if (n > 0) {
        buf[n] = '\0';
        String s(buf);
        s.trim();
        if (s.startsWith("IP:")) {
          String ip = s.substring(3);
          if (ip.length() >= 7 && ip.length() < 16) {
            strncpy(esp32IP, ip.c_str(), sizeof(esp32IP) - 1);
            esp32IP[sizeof(esp32IP) - 1] = '\0';
            udp.stop();
            return true;
          }
        }
      }
    }
    delay(10);
  }
  udp.stop();
  return false;
}

#define GRIS 0x7BEF
#define FONDO_CARD 0x0008
#define FONDO_BAND 0x000B
#define SOMBRA 0x2104

MKRIoTCarrier carrier;
WiFiClient client;

bool relay1 = false;
bool relay2 = false;
bool wfOk = false;
bool wfWasOk = false;
bool espOk = false;
bool espConnecting = false;

const int numPages = 3;
int page = 0;

float dhtTemp = -1;
float dhtHum = -1;

char lastMsg[42];
char lastEnv[26];
char ipBuf[16];

unsigned long lastReconnect = 0;
unsigned long lastDhtReq = 0;

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

void renderLEDs() {
  carrier.leds.setPixelColor(0, relay1 ? 0 : 255, relay1 ? 255 : 0, 0);
  carrier.leds.setPixelColor(4, relay2 ? 0 : 255, relay2 ? 255 : 0, 0);
  carrier.leds.setPixelColor(2, espOk ? 0 : 30, espOk ? 255 : 0, espOk ? 0 : 60);
  carrier.leds.show();
}

void drawPageDots() {
  int startX = 120 - (numPages - 1) * 4;
  for (int i = 0; i < numPages; i++) {
    carrier.display.fillCircle(startX + i * 8, 62, 3, i == page ? ST77XX_WHITE : GRIS);
  }
}

void drawStatusBar() {
  carrier.display.fillRect(0, 0, 240, 52, FONDO_BAND);
  centerText(17, wfOk ? "WiFi OK" : "SIN WIFI", 1, wfOk ? ST77XX_GREEN : ST77XX_RED);
  centerText(28, espOk ? "ESP32 OK" : (espConnecting ? "Buscando..." : "ESP32 OFF"), 1,
             espOk ? ST77XX_GREEN : (espConnecting ? ST77XX_YELLOW : ST77XX_RED));
  centerText(39, lastEnv, 1, ST77XX_CYAN);
  carrier.display.fillRect(0, 48, 240, 3, GRIS);
}

void drawRelayCard(int y, const char* label, bool on) {
  uint16_t accent = on ? ST77XX_GREEN : ST77XX_RED;
  uint16_t fill = on ? 0x0B15 : 0xB004;
  drawCard(y, 48, accent);

  int hw = min(halfW(y), halfW(y + 48));
  if (hw < 20) hw = 20;
  int x0 = 120 - hw + 14;

  carrier.display.fillRoundRect(120 - hw + 2, y + 2, hw * 2 - 4, 44, 8, fill);
  carrier.display.fillCircle(x0, y + 18, 7, accent);
  drawText(x0 + 16, y + 12, label, 2, ST77XX_WHITE);

  char st[5];
  strcpy(st, on ? "ON" : "OFF");
  drawText(120 + hw - 14 - strlen(st) * 12, y + 12, st, 2, accent);
}

void drawRelayPage() {
  carrier.display.fillRect(0, 56, 240, 184, ST77XX_BLACK);
  drawPageDots();
  drawRelayCard(76, "RELAY 1", relay1);
  drawRelayCard(130, "RELAY 2", relay2);
  centerText(190, "Pad 1/3: paginas", 1, GRIS);
  centerText(202, "Pad 0/4: reles", 1, GRIS);
}

void drawSensorsPage() {
  carrier.display.fillRect(0, 56, 240, 184, ST77XX_BLACK);
  drawPageDots();

  drawCard(78, 58, 0x4A29);
  centerText(94, "TEMPERATURA", 1, GRIS);
  char t[16];
  if (dhtTemp == -1) {
    strcpy(t, "--");
  } else {
    formatFloat(t, dhtTemp, 1);
  }
  centerTextShadow(110, t, 3, ST77XX_WHITE);
  drawText(120 + strlen(t) * 9 + 6, 118, " C", 1, ST77XX_CYAN);

  drawCard(146, 58, 0x4A29);
  centerText(162, "HUMEDAD", 1, GRIS);
  char h[16];
  if (dhtHum == -1) {
    strcpy(h, "--");
  } else {
    formatFloat(h, dhtHum, 0);
  }
  centerTextShadow(178, h, 3, ST77XX_WHITE);
  drawText(120 + strlen(h) * 9 + 6, 186, " %", 1, ST77XX_CYAN);
}

void drawInfoRow(int y, const char* label, const char* value, uint16_t vColor) {
  drawCard(y, 26, 0x4A29);
  int hw = min(halfW(y), halfW(y + 26));
  if (hw < 20) hw = 20;
  drawText(120 - hw + 12, y + 9, label, 1, GRIS);
  drawText(120 + hw - 12 - strlen(value) * 6, y + 9, value, 1, vColor);
}

void drawStatusPage() {
  carrier.display.fillRect(0, 56, 240, 184, ST77XX_BLACK);
  drawPageDots();

  drawInfoRow(76, "WiFi:", wfOk ? "OK" : "SIN", wfOk ? ST77XX_GREEN : ST77XX_RED);
  drawInfoRow(108, "ESP32:", espOk ? "CONECTADO" : "APAGADO", espOk ? ST77XX_GREEN : ST77XX_RED);
  drawInfoRow(140, "IP OPLA:", ipBuf, ST77XX_WHITE);
  drawInfoRow(172, "IP ESP:", esp32IP, ST77XX_CYAN);
  centerText(210, lastMsg, 1, ST77XX_WHITE);
}

void drawPage() {
  switch (page) {
    case 0: drawRelayPage(); break;
    case 1: drawSensorsPage(); break;
    case 2: drawStatusPage(); break;
  }
}

void updateEnvLine() {
  char t[8];
  char h[8];
  if (dhtTemp == -1) {
    strcpy(t, "--");
  } else {
    formatFloat(t, dhtTemp, 1);
  }
  if (dhtHum == -1) {
    strcpy(h, "--");
  } else {
    formatFloat(h, dhtHum, 0);
  }
  sprintf(lastEnv, "DHT11 %s C %s%%", t, h);
}

bool sendCommand(const char* cmd) {
  if (!client.connected()) {
    espConnecting = true;
    drawStatusBar();
    renderLEDs();
    client.stop();
    if (!client.connect(esp32IP, port)) {
      espOk = false;
      espConnecting = false;
      return false;
    }
    espOk = true;
    espConnecting = false;
  }
  client.print(cmd);
  client.print("\n");
  client.flush();
  return true;
}

bool doCommand(const char* cmd) {
  if (!sendCommand(cmd)) {
    sprintf(lastMsg, "ESP32 no responde");
    return false;
  }
  client.setTimeout(300);
  for (int attempt = 0; attempt < 3; attempt++) {
    String resp = client.readStringUntil('\n');
    resp.trim();
    if (resp == "OK") {
      sprintf(lastMsg, "Enviado: %s", cmd);
      return true;
    }
    if (resp.startsWith("T:")) {
      continue;
    }
  }
  espOk = false;
  client.stop();
  sprintf(lastMsg, "ESP32 no responde");
  return false;
}

void tryReconnect() {
  if (espOk) return;
  unsigned long now = millis();
  if (now - lastReconnect < 3000) return;
  lastReconnect = now;
  espConnecting = true;
  drawStatusBar();
  renderLEDs();
  client.stop();
  if (now - lastSearch >= 10000) {
    lastSearch = now;
    if (buscarESP32()) {
      sprintf(lastMsg, "ESP32 hallado: %s", esp32IP);
    } else {
      sprintf(lastMsg, "UDP: IP fija %s", esp32IP);
    }
    drawStatusBar();
    if (page == 2) {
      drawStatusPage();
    }
  }
  if (client.connect(esp32IP, port)) {
    espOk = true;
    espConnecting = false;
    sprintf(lastMsg, "Conectado a ESP32");
    client.setTimeout(300);
    client.print("ESTADO\n");
    client.flush();
    String resp = client.readStringUntil('\n');
    resp.trim();
    int r1pos = resp.indexOf("R1:");
    int r2pos = resp.indexOf(",R2:");
    if (r1pos >= 0 && r2pos > r1pos) {
      relay1 = resp.substring(r1pos + 3, r2pos).toInt() == 1;
      relay2 = resp.substring(r2pos + 4).toInt() == 1;
      Serial.print("Sincronizado al conectar: R1=");
      Serial.print(relay1);
      Serial.print(" R2=");
      Serial.println(relay2);
    } else {
      Serial.print("Respuesta ESTADO al conectar: <");
      Serial.print(resp);
      Serial.println(">");
    }
  } else {
    espOk = false;
    espConnecting = false;
    sprintf(lastMsg, "Sin ESP32 (IP?)");
  }
  drawStatusBar();
  renderLEDs();
}

void requestDHT() {
  if (!espOk) {
    dhtTemp = -1;
    dhtHum = -1;
    return;
  }
  client.print("DATA\n");
  client.flush();
  client.setTimeout(300);
  for (int attempt = 0; attempt < 6; attempt++) {
    String resp = client.readStringUntil('\n');
    resp.trim();
    if (resp.startsWith("STATE:")) {
      int r1p = resp.indexOf("R1:");
      int r2p = resp.indexOf(",R2:");
      if (r1p >= 0 && r2p > r1p) {
        relay1 = resp.substring(r1p + 3, r2p).toInt() == 1;
        relay2 = resp.substring(r2p + 4).toInt() == 1;
        if (page == 0) {
          drawRelayCard(76, "RELAY 1", relay1);
          drawRelayCard(130, "RELAY 2", relay2);
        }
        renderLEDs();
      }
      continue;
    }
    Serial.print("RAW[" + String(attempt) + "]: <");
    Serial.print(resp);
    Serial.println(">");
    int comma = resp.indexOf(',');
    int hPos = resp.indexOf("H:");
    if (resp.startsWith("T:") && comma > 2 && hPos > comma) {
    dhtTemp = resp.substring(2, comma).toFloat();
    dhtHum = resp.substring(hPos + 2).toFloat();
    if (dhtTemp < 0 || dhtTemp > 80) dhtTemp = -1;
    if (dhtHum <= 0 || dhtHum > 100) dhtHum = -1;
      Serial.print("Parseado: T=" + String(dhtTemp) + " H=" + String(dhtHum));
      Serial.println(" | mostrado: " + String(lastEnv));
      return;
    }
  }
  dhtTemp = -1;
  dhtHum = -1;
  espOk = false;
  client.stop();
  Serial.println("Sin respuesta del ESP32: reconectando...");
}

// Lee mensajes "STATE:R1:x,R2:y" que el ESP32 envía cuando cambia un relé.
// Así el Oplà actualiza la UI al instante cuando se enciende/apaga desde la web.
void procesarEstado() {
  if (!client.connected()) return;
  String resp;
  while (client.available()) {
    char c = client.read();
    if (c == '\n' || c == '\r') {
      if (resp.length() == 0) continue;
      resp.trim();
      Serial.print("PUSH: <");
      Serial.print(resp);
      Serial.println(">");
      if (resp.startsWith("STATE:")) {
        int r1pos = resp.indexOf("R1:");
        int r2pos = resp.indexOf(",R2:");
        if (r1pos >= 0 && r2pos > r1pos) {
          bool nuevo1 = resp.substring(r1pos + 3, r2pos).toInt() == 1;
          bool nuevo2 = resp.substring(r2pos + 4).toInt() == 1;
          if (nuevo1 != relay1 || nuevo2 != relay2) {
            relay1 = nuevo1;
            relay2 = nuevo2;
            sprintf(lastMsg, "Sinc: R1 %s R2 %s", relay1 ? "ON" : "OFF", relay2 ? "ON" : "OFF");
            Serial.print("Estado actualizado: R1=");
            Serial.print(relay1);
            Serial.print(" R2=");
            Serial.println(relay2);
            drawStatusBar();
            if (page == 0) {
              drawRelayCard(76, "RELAY 1", relay1);
              drawRelayCard(130, "RELAY 2", relay2);
            }
            renderLEDs();
          }
        }
      }
      resp = "";
    } else {
      resp += c;
    }
  }
}

// Verifica conectividad real haciendo ping al gateway. Si falla 2 veces,
// fuerza reconexión completa con WiFi.end() para limpiar el NINA.
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
    if (client.connected()) client.stop();
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
      sprintf(lastMsg, "WiFi reconectado");
      Serial.print(">>> IP OPLA: ");
      Serial.println(WiFi.localIP());
      drawStatusBar();
      renderLEDs();
    } else {
      sprintf(lastMsg, "Sin WiFi");
      drawStatusBar();
    }
  }
}

void setup() {
  Serial.begin(9600);
  delay(500);

  strcpy(ipBuf, "0.0.0.0");

  carrier.withCase();
  carrier.Buttons.updateConfig(80);
  carrier.Buttons.updateConfig(80, TOUCH3);
  carrier.begin();
  carrier.Light.setLEDBoost(0);
  carrier.display.fillScreen(ST77XX_BLACK);
  carrier.display.setRotation(0);
  carrier.display.setTextWrap(false);

  sprintf(lastMsg, "Listo para conectar");
  updateEnvLine();
  drawPage();
  renderLEDs();

  Serial.print("Conectando WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    WiFi.begin(ssid, pass);
    delay(1000);
    Serial.print(".");
  }
  Serial.println(" OK");
  WiFi.noLowPowerMode();
  wfOk = true;
  String localIp = WiFi.localIP().toString();
  strncpy(ipBuf, localIp.c_str(), sizeof(ipBuf) - 1);
  ipBuf[sizeof(ipBuf) - 1] = '\0';
  sprintf(lastMsg, "WiFi OK. Buscando ESP32...");
  drawStatusBar();
  renderLEDs();
  if (buscarESP32()) {
    sprintf(lastMsg, "ESP32 hallado: %s", esp32IP);
  } else {
    sprintf(lastMsg, "UDP: IP fija %s", esp32IP);
  }
  drawStatusBar();
  if (page == 2) {
    drawStatusPage();
  }
  renderLEDs();
  tryReconnect();
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
  if (now - touchStableAt[pad] < 150) return false;
  if (now - lastTouchClick[pad] < 400) return false;
  touchStableAt[pad] = 0;
  lastTouchClick[pad] = now;
  return true;
}

void changePage(int delta) {
  page = (page + delta + numPages) % numPages;
  drawPage();
}

void loop() {
  wfOk = (WiFi.status() == WL_CONNECTED);
  verificarWiFi();

  if (wfOk && !wfWasOk) {
    String localIp = WiFi.localIP().toString();
    strncpy(ipBuf, localIp.c_str(), sizeof(ipBuf) - 1);
    ipBuf[sizeof(ipBuf) - 1] = '\0';
    drawStatusBar();
    if (page == 2) {
      drawStatusPage();
    }
  }
  wfWasOk = wfOk;

  if (!wfOk) {
    WiFi.end();
    delay(200);
    WiFi.begin(ssid, pass);
    sprintf(lastMsg, "WiFi caido, reintentando...");
    drawStatusBar();
    delay(1000);
    return;
  }

  if (!client.connected()) {
    espOk = false;
    tryReconnect();
  } else {
    procesarEstado();
  }

  if (millis() - lastDhtReq >= 15000) {    lastDhtReq = millis();
    requestDHT();
    updateEnvLine();
    drawStatusBar();
    if (page == 1) {
      drawSensorsPage();
    }
  }

  carrier.Buttons.update();

  if (touchPressed(0)) {
    relay1 = !relay1;
    if (!doCommand(relay1 ? "ON 1" : "OFF 1")) {
      relay1 = !relay1;
    }
    drawStatusBar();
    if (page == 0) {
      drawRelayCard(76, "RELAY 1", relay1);
    }
    renderLEDs();
  }

  if (touchPressed(4)) {
    relay2 = !relay2;
    if (!doCommand(relay2 ? "ON 2" : "OFF 2")) {
      relay2 = !relay2;
    }
    drawStatusBar();
    if (page == 0) {
      drawRelayCard(130, "RELAY 2", relay2);
    }
    renderLEDs();
  }

  if (touchPressed(2)) {
    bool new1 = !relay1;
    bool new2 = !relay2;
    if (doCommand(new1 ? "ON 1" : "OFF 1") && doCommand(new2 ? "ON 2" : "OFF 2")) {
      relay1 = new1;
      relay2 = new2;
      sprintf(lastMsg, "Enviado: R1 %s R2 %s", relay1 ? "ON" : "OFF", relay2 ? "ON" : "OFF");
    } else {
      sprintf(lastMsg, "ESP32 no responde");
    }
    drawStatusBar();
    if (page == 0) {
      drawRelayCard(76, "RELAY 1", relay1);
      drawRelayCard(130, "RELAY 2", relay2);
    }
    renderLEDs();
  }

  if (touchPressed(1)) {
    changePage(-1);
  }

  if (touchPressed(3)) {
    changePage(1);
  }

  delay(10);
}