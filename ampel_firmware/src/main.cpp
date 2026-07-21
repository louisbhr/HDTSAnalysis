// ============================================================================
//  HDTS-Ampel  -  ESP32 Firmware  (USB-Serial + WLAN, RGB-Status via LEDC-PWM)
// ============================================================================
//  15-LED-Anzeige (WS2812B) + RGB-Status-LED + Schalter (Master an/aus).
//
//  RGB-Status-LED (bei EINGESCHALTETEM System), Prioritaet von oben:
//     Analyse laeuft          -> gruen
//     UI verbunden            -> gelb
//     Geraet im ESP-WLAN      -> rot (dauerhaft)
//     nur an, sonst nichts    -> rot pulsierend
//     System aus (Schalter)   -> RGB aus + Anzeige aus
//
//  WICHTIG: Die RGB-LED wird ueber LEDC (echtes PWM) angesteuert - dadurch
//  funktionieren Dimmen/Pulsieren UND saubere Mischfarben (Gelb) zuverlaessig.
//
//  Protokoll (Serial 115200 / TCP-Zeilen mit '\n'):
//    PC -> ESP:  PING | SHOW EARLY <1..3> | SHOW LATE <1..3> | SHOW GOOD |
//                OFF | POWER ON | POWER OFF | STATUS? |
//                UI ON | UI OFF | ANALYSIS ON | ANALYSIS OFF
//    ESP -> PC:  READY | PONG | EVENT POWER ON|OFF | OK ... | ERR ...
// ============================================================================

#include <Arduino.h>
#include <math.h>
#include <WiFi.h>
#include <Adafruit_NeoPixel.h>

// ############################################################################
// ##  KONFIG-BLOCK  -  falls Farben/Polaritaet nicht stimmen, HIER anpassen ##
// ############################################################################

// Pin je Farbkanal. Wenn z.B. Gruen und Blau vertauscht sind, einfach die
// Pin-Nummern hier tauschen (nicht im restlichen Code).
constexpr uint8_t PIN_R = 2;    // roter Kanal
constexpr uint8_t PIN_G = 4;    // gruener Kanal
constexpr uint8_t PIN_B = 16;   // blauer Kanal

// Gemeinsame KATHODE (gemeinsamer Pin an GND) -> false
// Gemeinsame ANODE   (gemeinsamer Pin an 3V3) -> true
constexpr bool RGB_COMMON_ANODE = false;

// Farbwerte der Status-Zustaende (0..255 je Kanal). Gelb ggf. hier feinjustieren.
struct RGBColor { uint8_t r, g, b; };
constexpr RGBColor COLOR_GREEN  = {   0, 255,   0 };  // Analyse laeuft
constexpr RGBColor COLOR_YELLOW = { 255, 160,   0 };  // UI verbunden
constexpr RGBColor COLOR_RED    = { 255,   0,   0 };  // WLAN / Puls-Grundfarbe

// ############################################################################

// ----------------------------- weitere Pins ---------------------------------
constexpr uint8_t  PIN_LEDS   = 5;    // Daten-Pin des WS2812B-Streifens
constexpr uint8_t  PIN_BUTTON = 13;   // Schalter gegen GND (interner Pullup)

constexpr uint16_t NUM_LEDS   = 15;
constexpr uint8_t  BRIGHTNESS = 60;

// ----------------------------- LEDC-PWM-Einstellungen -----------------------
constexpr uint32_t PWM_FREQ = 5000;   // 5 kHz - flimmerfrei
constexpr uint8_t  PWM_RES  = 8;      // 8 Bit -> Werte 0..255
// (Fuer aeltere Cores: feste Kanalnummern.)
constexpr uint8_t  CH_R = 0, CH_G = 1, CH_B = 2;

// ----------------------------- WLAN -----------------------------------------
constexpr char     WIFI_SSID[] = "HDTS-Ampel";
constexpr char     WIFI_PASS[] = "trampolin123";   // mind. 8 Zeichen!
constexpr uint16_t TCP_PORT    = 3333;

WiFiServer server(TCP_PORT);
WiFiClient client;
String     wifiBuffer = "";
bool       tcpWasConnected = false;

// ----------------------------- LED-Streifen ---------------------------------
Adafruit_NeoPixel strip(NUM_LEDS, PIN_LEDS, NEO_GRB + NEO_KHZ800);

const uint8_t EARLY_PAIRS[3][2] = { {4, 5}, {2, 3}, {0, 1} };
const uint8_t LATE_PAIRS [3][2] = { {9, 10}, {11, 12}, {13, 14} };
const uint8_t GREEN_TRIO [3]    = { 6, 7, 8 };

uint32_t COL_YELLOW, COL_GREEN, COL_BLUE;

// ----------------------------- Zustand --------------------------------------
bool    ampelOn         = true;
bool    uiConnected     = false;
bool    analysisRunning = false;
char    lastDir   = 'O';
uint8_t lastLevel = 0;
String  rxBuffer  = "";

bool     lastBtnReading = HIGH;
bool     btnStable      = HIGH;
uint32_t lastBtnChange  = 0;
constexpr uint32_t DEBOUNCE_MS = 30;
constexpr uint32_t PULSE_PERIOD_MS = 1600;

// ----------------------------------------------------------------------------
//  LEDC initialisieren (versionsrobust: Core 3.x vs. 2.x)
// ----------------------------------------------------------------------------
void setupPWM() {
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
  ledcAttach(PIN_R, PWM_FREQ, PWM_RES);
  ledcAttach(PIN_G, PWM_FREQ, PWM_RES);
  ledcAttach(PIN_B, PWM_FREQ, PWM_RES);
#else
  ledcSetup(CH_R, PWM_FREQ, PWM_RES); ledcAttachPin(PIN_R, CH_R);
  ledcSetup(CH_G, PWM_FREQ, PWM_RES); ledcAttachPin(PIN_G, CH_G);
  ledcSetup(CH_B, PWM_FREQ, PWM_RES); ledcAttachPin(PIN_B, CH_B);
#endif
}

// ----------------------------------------------------------------------------
//  RGB-Status-LED setzen (0..255 je Kanal), beruecksichtigt Anode/Kathode.
// ----------------------------------------------------------------------------
void setRGB(uint8_t r, uint8_t g, uint8_t b) {
  if (RGB_COMMON_ANODE) { r = 255 - r; g = 255 - g; b = 255 - b; }
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
  ledcWrite(PIN_R, r);
  ledcWrite(PIN_G, g);
  ledcWrite(PIN_B, b);
#else
  ledcWrite(CH_R, r);
  ledcWrite(CH_G, g);
  ledcWrite(CH_B, b);
#endif
}

void setRGB(const RGBColor &c) { setRGB(c.r, c.g, c.b); }

// ----------------------------------------------------------------------------
//  Status-LED anhand des aktuellen Zustands aktualisieren (nicht-blockierend).
// ----------------------------------------------------------------------------
void updateStatusLed() {
  if (!ampelOn)                       { setRGB(0, 0, 0);       return; }
  if (analysisRunning)                { setRGB(COLOR_GREEN);   return; }  // gruen
  if (uiConnected)                    { setRGB(COLOR_YELLOW);  return; }  // gelb
  if (WiFi.softAPgetStationNum() > 0) { setRGB(COLOR_RED);     return; }  // rot dauerhaft

  // rot pulsierend (an, sonst nichts verbunden)
  float phase = (millis() % PULSE_PERIOD_MS) / (float)PULSE_PERIOD_MS;   // 0..1
  float wave  = (sinf(phase * 2.0f * PI - PI / 2.0f) + 1.0f) * 0.5f;     // 0..1
  float scale = 0.08f + wave * 0.92f;   // 8%..100% -> deutlich sichtbares Pulsieren
  setRGB((uint8_t)(COLOR_RED.r * scale),
         (uint8_t)(COLOR_RED.g * scale),
         (uint8_t)(COLOR_RED.b * scale));
}

// ----------------------------------------------------------------------------
void broadcast(const String &msg) {
  Serial.println(msg);
  if (client && client.connected()) client.println(msg);
}

// ----------------------------------------------------------------------------
void renderDisplay() {
  strip.clear();
  if (ampelOn) {
    switch (lastDir) {
      case 'E':
        if (lastLevel >= 1 && lastLevel <= 3) {
          strip.setPixelColor(EARLY_PAIRS[lastLevel - 1][0], COL_YELLOW);
          strip.setPixelColor(EARLY_PAIRS[lastLevel - 1][1], COL_YELLOW);
        }
        break;
      case 'L':
        if (lastLevel >= 1 && lastLevel <= 3) {
          strip.setPixelColor(LATE_PAIRS[lastLevel - 1][0], COL_BLUE);
          strip.setPixelColor(LATE_PAIRS[lastLevel - 1][1], COL_BLUE);
        }
        break;
      case 'G':
        for (uint8_t i = 0; i < 3; i++) strip.setPixelColor(GREEN_TRIO[i], COL_GREEN);
        break;
      default: break;
    }
  }
  strip.show();
}

// ----------------------------------------------------------------------------
void setPower(bool on, bool announce) {
  ampelOn = on;
  renderDisplay();
  if (announce) broadcast(ampelOn ? "EVENT POWER ON" : "EVENT POWER OFF");
}

// ----------------------------------------------------------------------------
void handleCommand(String line, Stream &out) {
  line.trim();
  if (line.length() == 0) return;

  String upper = line;
  upper.toUpperCase();

  if (upper == "PING")         { out.println("PONG"); return; }

  if (upper == "UI ON")        { uiConnected = true;  out.println("OK UI ON"); return; }
  if (upper == "UI OFF")       { uiConnected = false; analysisRunning = false; out.println("OK UI OFF"); return; }
  if (upper == "ANALYSIS ON")  { analysisRunning = true;  out.println("OK ANALYSIS ON"); return; }
  if (upper == "ANALYSIS OFF") { analysisRunning = false; out.println("OK ANALYSIS OFF"); return; }

  if (upper == "OFF") {
    lastDir = 'O'; lastLevel = 0;
    renderDisplay();
    out.println("OK OFF");
    return;
  }

  if (upper == "POWER ON")  { setPower(true, false);  out.println("OK POWER ON");  return; }
  if (upper == "POWER OFF") { setPower(false, false); out.println("OK POWER OFF"); return; }

  if (upper == "STATUS?") {
    out.print("STATUS power=");   out.print(ampelOn ? "ON" : "OFF");
    out.print(" ui=");            out.print(uiConnected ? "1" : "0");
    out.print(" analysis=");      out.print(analysisRunning ? "1" : "0");
    out.print(" stations=");      out.print(WiFi.softAPgetStationNum());
    out.print(" dir=");           out.print(lastDir);
    out.print(" level=");         out.println(lastLevel);
    return;
  }

  if (upper.startsWith("SHOW")) {
    if (upper.indexOf("GOOD") >= 0) {
      lastDir = 'G'; lastLevel = 0;
      renderDisplay();
      out.println("OK SHOW GOOD");
      return;
    }
    bool early = upper.indexOf("EARLY") >= 0;
    bool late  = upper.indexOf("LATE")  >= 0;
    if (!early && !late) { out.println("ERR SHOW: EARLY|LATE|GOOD erwartet"); return; }

    int level = -1;
    for (int i = 0; i < (int)line.length(); i++) if (isdigit(line[i])) level = line[i] - '0';
    if (level < 1 || level > 3) { out.println("ERR SHOW: Stufe 1..3 erwartet"); return; }

    lastDir = early ? 'E' : 'L';
    lastLevel = (uint8_t)level;
    renderDisplay();
    out.print("OK SHOW "); out.print(early ? "EARLY " : "LATE "); out.println(level);
    return;
  }

  out.print("ERR unbekanntes Kommando: ");
  out.println(line);
}

// ----------------------------------------------------------------------------
void pollButton() {
  bool reading = digitalRead(PIN_BUTTON);
  if (reading != lastBtnReading) { lastBtnChange = millis(); lastBtnReading = reading; }
  if ((millis() - lastBtnChange) > DEBOUNCE_MS && reading != btnStable) {
    btnStable = reading;
    if (btnStable == LOW) setPower(!ampelOn, true);
  }
}

// ----------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);

  pinMode(PIN_BUTTON, INPUT_PULLUP);
  setupPWM();
  setRGB(0, 0, 0);

  strip.begin();
  strip.setBrightness(BRIGHTNESS);
  strip.clear();
  strip.show();

  COL_YELLOW = strip.Color(255, 170, 0);
  COL_GREEN  = strip.Color(0, 255, 40);
  COL_BLUE   = strip.Color(90, 0, 220);

  setPower(true, false);

  // Selbsttest Streifen: einmal kurz weiss aufblinken.
  strip.clear();
  for (uint16_t i = 0; i < NUM_LEDS; i++) strip.setPixelColor(i, strip.Color(255, 255, 255));
  strip.show();
  delay(150);
  strip.clear();
  strip.show();

  // Kein Selbsttest der Status-LED mehr - sie geht direkt in den
  // normalen Zustand ueber (updateStatusLed() in loop() uebernimmt ab hier).

  // WLAN als Access Point.
  WiFi.mode(WIFI_AP);
  bool apOk = WiFi.softAP(WIFI_SSID, WIFI_PASS);
  IPAddress ip = WiFi.softAPIP();
  Serial.print("WiFi-AP '"); Serial.print(WIFI_SSID);
  Serial.print(apOk ? "' aktiv. IP: " : "' FEHLER beim Start. IP: ");
  Serial.println(ip);
  server.begin();
  server.setNoDelay(true);

  Serial.println("READY");
}

void loop() {
  // 1) Serielle Befehle
  while (Serial.available() > 0) {
    char ch = (char)Serial.read();
    if (ch == '\n') { handleCommand(rxBuffer, Serial); rxBuffer = ""; }
    else if (ch != '\r') { rxBuffer += ch; if (rxBuffer.length() > 64) rxBuffer = ""; }
  }

  // 2) TCP-Client annehmen
  if (!client || !client.connected()) {
    WiFiClient incoming = server.available();
    if (incoming) {
      client = incoming;
      client.setNoDelay(true);
      wifiBuffer = "";
      client.println("READY");
    }
  }

  // 3) TCP-Befehle
  if (client && client.connected()) {
    while (client.available() > 0) {
      char ch = (char)client.read();
      if (ch == '\n') { handleCommand(wifiBuffer, client); wifiBuffer = ""; }
      else if (ch != '\r') { wifiBuffer += ch; if (wifiBuffer.length() > 64) wifiBuffer = ""; }
    }
  }

  // 3b) TCP-Client weg? -> UI/Analyse zuruecksetzen (nur WLAN relevant).
  bool nowTcp = client && client.connected();
  if (tcpWasConnected && !nowTcp) { uiConnected = false; analysisRunning = false; }
  tcpWasConnected = nowTcp;

  // 4) Schalter
  pollButton();

  // 5) Status-LED
  updateStatusLed();
}
