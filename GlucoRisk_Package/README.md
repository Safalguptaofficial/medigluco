# 🩺 GlucoRisk — TinyML on ESP32

Predicts hypoglycemia / glucose spike risk using a trained MLP neural network running **on the ESP32 chip**.

## Model Info
| Property | Value |
|---|---|
| Architecture | MLP 8 → 16 → 8 → 4 |
| Dataset size | 3,000 samples |
| Accuracy | **92%** |
| Classes | NORMAL, LOW_RISK, MODERATE_RISK, HIGH_RISK |
| Inference time | ~0.5ms on ESP32 |

## Input Features
| # | Feature | Range | Source |
|---|---|---|---|
| 1 | Blood Glucose | 30–400 mg/dL | CGM / manual |
| 2 | Heart Rate | 40–200 BPM | Manual |
| 3 | GSR (Skin Conductance) | 0–1023 | Manual |
| 4 | SpO₂ | 80–100 % | Manual |
| 5 | Stress Level | 1–10 | Manual |
| 6 | Age | 18–90 years | Manual |
| 7 | BMI | 15–50 | Manual |
| 8 | Activity Level | 0–3 | Manual |

## Setup

### ESP32 (Arduino IDE)
1. Open `GlucoRisk_ESP32/GlucoRisk_ESP32.ino`
2. Select board: **ESP32 Dev Module**
3. Upload — no libraries needed (pure math, no sensor libs)

### Python CLI App
```bash
pip install pyserial rich
python glucorisk_app.py           # auto-detect port
python glucorisk_app.py COM3      # specify port (Windows)
python glucorisk_app.py /dev/ttyUSB0  # Linux/Mac
```
Works in **offline mode** too (local Python inference, no ESP32 needed).

### Web Dashboard
A Flask-based web interface provides user registration/login and a dashboard
with historical charts of glucose spikes.

Dependencies:
```bash
pip install flask werkzeug
```

Run with:
```bash
python web_app.py
```
Then visit `http://127.0.0.1:5000` in your browser.  The application will
create a `glucorisk.db` SQLite database on first start.  Use the **Register**
page to create an account, then log in and enter patient measurements.  The
results and time-series plots are shown on the dashboard.

## Serial Protocol
```
PC → ESP32:  GLU:95.0|HR:72.0|GSR:500|SPO2:98.5|STRESS:3.0|AGE:35|BMI:25.0|ACT:0\n
ESP32 → PC:  {"risk":"NORMAL","score":99,"probs":[99,0,0,0],"advice":"..."}\n
```

## Retrain Model
```bash
python train_model.py
# → generates model_weights.h (paste into .ino) and model.json (for Python app)
```

## Clinical Overrides (always applied on top of ML)
- Glucose < 54 → always HIGH_RISK
- Glucose > 250 → always HIGH_RISK  
- Glucose < 70 → minimum MODERATE_RISK
- Glucose > 180 → minimum LOW_RISK
