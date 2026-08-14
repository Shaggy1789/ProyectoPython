#include <WiFi.h>
#include <DHT.h>
#include <WiFiUdp.h>

const char* ssid = "LAP_DARA 9861";
const char* password = "87654321";

const uint16_t serverPort = 9001;
const uint16_t udpPort = 5001;
WiFiUDP udp;

const int relayPins[] = {13, 15};
const int numRelays = sizeof(relayPins) / sizeof(relayPins[0]);

const int dhtPin = 5;
const uint8_t dhtType = DHT11;

DHT dht(dhtPin, dhtType);

bool relayState[numRelays];

WiFiServer server(serverPort);
#define MAX_CLIENTS 5
WiFiClient clients[MAX_CLIENTS];
String lines[MAX_CLIENTS];
unsigned long lastIpPrint = 0;
unsigned long lastDhtRead = 0;

float dhtTemp = -1;
float dhtHum = -1;

void broadcastState() {
  String estado = "STATE:R1:";
  estado += relayState[0] ? "1" : "0";
  estado += ",R2:";
  estado += relayState[1] ? "1" : "0";
  estado += "\r\n";
  for (int i = 0; i < MAX_CLIENTS; i++) {
    if (clients[i] && clients[i].connected()) {
      clients[i].print(estado);
      clients[i].flush();
    }
  }
}

void setRelay(int index, bool state) {
  if (index < 0 || index >= numRelays) return;
  if (relayState[index] == state) return;
  relayState[index] = state;
  digitalWrite(relayPins[index], state ? LOW : HIGH);
  Serial.print("Rele "); Serial.print(index + 1); Serial.print(" -> "); Serial.println(state ? "ON" : "OFF");
  broadcastState();
}

void readDHT() {
  unsigned long now = millis();
  if (now - lastDhtRead < 2000) return;
  lastDhtRead = now;
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  if (isnan(t) || isnan(h)) {
    dhtTemp = -1;
    dhtHum = -1;
  } else {
    dhtTemp = t;
    dhtHum = h;
  }
  Serial.print("DHT11: ");
  Serial.print(dhtTemp);
  Serial.print(" C, ");
  Serial.print(dhtHum);
  Serial.println(" %");
}

String handleCommand(String line) {
  line.trim();
  Serial.println("Recibido: " + line);

  if (line.startsWith("TOGGLE")) {
    int relay = line.substring(6).toInt() - 1;
    if (relay >= 0 && relay < numRelays) {
      setRelay(relay, !relayState[relay]);
      Serial.print("Rele "); Serial.print(relay + 1); Serial.print(" -> "); Serial.println(relayState[relay] ? "ON" : "OFF");
    }
    return "OK";
  } else if (line.startsWith("ON")) {
    int relay = line.substring(2).toInt() - 1;
    if (relay >= 0 && relay < numRelays) {
      setRelay(relay, true);
    }
    return "OK";
  } else if (line.startsWith("OFF")) {
    int relay = line.substring(3).toInt() - 1;
    if (relay >= 0 && relay < numRelays) {
      setRelay(relay, false);
    }
    return "OK";
  } else if (line.startsWith("DATA")) {
    readDHT();
    String resp = "T:";
    if (dhtTemp == -1) {
      resp += "-1";
    } else {
      resp += String(dhtTemp, 2);
    }
    resp += ",H:";
    if (dhtHum == -1) {
      resp += "-1";
    } else {
      resp += String(dhtHum, 2);
    }
    return resp;
  } else if (line.startsWith("ESTADO")) {
    String resp = "R1:";
    resp += relayState[0] ? "1" : "0";
    resp += ",R2:";
    resp += relayState[1] ? "1" : "0";
    return resp;
  } else if (line.startsWith("PING")) {
    return "PONG";
  }
  return "ERR";
}

String estadoWiFi(uint8_t s) {
  switch (s) {
    case WL_CONNECTED: return "CONECTADO";
    case WL_NO_SSID_AVAIL: return "RED NO ENCONTRADA";
    case WL_CONNECT_FAILED: return "CLAVE INCORRECTA";
    case WL_IDLE_STATUS: return "IDLE";
    default: return String("CODIGO ") + String(s);
  }
}

void conectarWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  Serial.print("Conectando a ");
  Serial.println(ssid);
  unsigned long inicio = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - inicio < 20000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    WiFi.setSleep(false);
    Serial.println("*** CONECTADO ***");
    Serial.print(">>> IP DEL ESP32: ");
    Serial.println(WiFi.localIP());
    Serial.println(">>> Copia esa IP en el sketch del Opla");
  } else {
    Serial.println("NO PUDE CONECTAR. Estado: " + estadoWiFi(WiFi.status()));
    Serial.println("Revisa: hotspot en banda 2.4GHz, nombre y clave exactos, cerca del ESP32.");
  }
  server.begin();
  Serial.println("Servidor listo en el puerto " + String(serverPort));
  udp.begin(udpPort);
  Serial.println("UDP de descubrimiento listo en el puerto " + String(udpPort));
}

void atenderDescubrimiento() {
  int sz = udp.parsePacket();
  if (sz <= 0) return;
  char buf[32];
  int n = udp.read(buf, sizeof(buf) - 1);
  if (n <= 0) return;
  buf[n] = '\0';
  String cmd(buf);
  cmd.trim();
  if (cmd == "FIND_ESP32") {
    String resp = "IP:";
    resp += WiFi.localIP().toString();
    udp.beginPacket(udp.remoteIP(), udp.remotePort());
    udp.print(resp);
    udp.endPacket();
    Serial.print("Descubrimiento -> ");
    Serial.println(resp);
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println();
  Serial.println("=== SERVIDOR RELES + DHT11 ===");

  for (int i = 0; i < numRelays; i++) {
    pinMode(relayPins[i], OUTPUT);
    setRelay(i, false);
  }

  dht.begin();

  conectarWiFi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Se perdio el WiFi, reconectando...");
    conectarWiFi();
    delay(1000);
    return;
  }

  if (millis() - lastIpPrint > 5000) {
    lastIpPrint = millis();
    Serial.print(">>> IP DEL ESP32: ");
    Serial.println(WiFi.localIP());
  }

  readDHT();

  atenderDescubrimiento();

  while (server.hasClient()) {
    WiFiClient nueva = server.available();
    if (!nueva) break;
    nueva.setNoDelay(true);
    bool slotLibre = false;
    for (int i = 0; i < MAX_CLIENTS; i++) {
      if (!clients[i] || !clients[i].connected()) {
        clients[i] = nueva;
        lines[i] = "";
        slotLibre = true;
        Serial.print("Cliente conectado (slot ");
        Serial.print(i);
        Serial.println(")");
        break;
      }
    }
    if (!slotLibre) {
      Serial.println("Cliente extra rechazado (slots ocupados)");
      nueva.stop();
    }
  }

  for (int i = 0; i < MAX_CLIENTS; i++) {
    if (clients[i] && clients[i].connected()) {
      while (clients[i].available()) {
        char c = clients[i].read();
        if (c == '\n' || c == '\r') {
          if (lines[i].length() > 0) {
            String resp = handleCommand(lines[i]);
            clients[i].print(resp);
            clients[i].print("\r\n");
            clients[i].flush();
            lines[i] = "";
          }
        } else {
          lines[i] += c;
        }
      }
    } else if (clients[i]) {
      clients[i].stop();
      clients[i] = WiFiClient();
      Serial.print("Cliente desconectado (slot ");
      Serial.print(i);
      Serial.println(")");
    }
  }

  delay(1);
}