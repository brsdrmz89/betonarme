# app.py — PART 1/3
# -*- coding: utf-8 -*-
from __future__ import annotations

import os, io, json, math, uuid, hashlib, requests
import numpy as np
import streamlit as st
import pandas as pd
from datetime import date, timedelta
from pandas import ExcelWriter
import matplotlib.pyplot as plt

# =============== 0) SABİTLER ===============
# NDFL: Net'ten brüt'e çevrimde kullanılıyor; işveren primleri "brüt"e uygulanır (brüt+NDFL DEĞİL)
NDFL_RUS = 0.130
NDFL_SNG = 0.130
NDFL_TUR = 0.130  # VKS için sabit oran modu başlangıcı; progressive modda 13/15/18/20/22 kademeleri kullanılır

# İşveren primleri (resmi BRÜT bazında)
OPS = 0.220   # emeklilik
OSS = 0.029  # sosyal
OMS = 0.051  # sağlık
NSIPZ_RISK_RUS_SNG = 0.009   # iş kazası/meslek hast. (RUS+SNG için tipik risk katsayısı)
NSIPZ_RISK_TUR_VKS  = 0.018  # VKS (TR) için iş kazası riski

# Patent + resmi tabanlar (sade model)
SNG_PATENT_MONTH = 7000      # sabit aylık patent ödemesi (örn. Moskova ~8900 güncel olabilir)
SNG_TAXED_BASE   = 33916     # resmi brüt tavan (aylık, sade model)
TUR_TAXED_BASE   = 167000    # VKS için resmi brüt tavan (aylık, sade model)

# Cash (elden) ödeme komisyonu — banka/çekim/kur riski gibi
CASH_COMMISSION_RATE = 0.235

# Varsayılan oranlar
OVERHEAD_RATE_DEFAULT = 15.0  # Yüzde olarak (15.0%)
OVERHEAD_RATE_MAX     = 25.0  # Yüzde olarak (25.0%)
CONSUMABLES_RATE_DEFAULT = 5.0  # Yüzde olarak (5.0%)

# =============== Yardımcı Fonksiyonlar ===============
def ratio_to_pct(value):
    try:
        return float(value) * 100.0
    except Exception:
        return 0.0


def pct_to_ratio(value):
    try:
        return float(value) / 100.0
    except Exception:
        return 0.0


def eff(key: str, default):
    """CONST_OVERRIDES içinden etkin değeri getirir, yoksa varsayılanı döner."""
    overrides = st.session_state.get("CONST_OVERRIDES", {})
    return overrides.get(key, default)

# --- Gruplu Sarf ve Genel Gider preset'leri ---
CONSUMABLES_PRESET = [
    ("Bağ teli / tel sarf", 1.2),
    ("Kesme / taşlama diskleri", 1.0),
    ("Gaz / oksijen (kaynak)", 0.8),
    ("Matkap/ucu/perçin sarfı", 0.6),
    ("Kalıp yağı / kimyasal", 0.7),
    ("Vibratör şişe/bakım sarf", 0.5),
    ("Çivi / dübel / ankraj sarf", 0.6),
    ("Eldiven / küçük el aleti sarf", 0.4),
]

OVERHEAD_GROUPS_PRESET = [
    ("Şantiye genel idare", 7.0),
    ("Ekipman/amortisman (küçük alet)", 5.0),
    ("Lojistik/koordinasyon", 3.0),
    ("Güvenlik & İSG idari", 2.0),
    ("Ofis/GSM/evrak/izin", 1.5),
]

# Indirect (şantiye hizmet/altyapı) preset — overhead ile çakışmayı önlemek için ayrıştırıldı
INDIRECT_PRESET_DEFAULTS = {
    "Şantiye enerji-su (Энергия/вода на площадке)": 2.0,
    "Geçici yollar/erişim (Временные дороги/подъезды)": 1.0,
    "Aydınlatma/jeneratör (Освещение/генератор)": 1.0,
    "Geçici ofis/soy. odaları (Врем. офис/раздевалки)": 0.8,
    "Depolama/çit/kapı güvenliği (Склад/ограждение/охрана)": 1.2,
    "Temizlik/çöp/saha bakım (Уборка/вывоз/обслуживание)": 1.0,
}

# Adam-saat normları
SCENARIO_NORMS = {
    "İdeal":     {"Grobeton": 8.0,  "Rostverk": 12.0, "Temel": 14.0, "Döşeme": 15.0, "Perde": 18.0, "Merdiven": 22.0},
    "Gerçekçi":  {"Grobeton": 10.0, "Rostverk": 14.0, "Temel": 16.0, "Döşeme": 18.0, "Perde": 21.0, "Merdiven": 26.0},
    "Kötü":      {"Grobeton": 12.0, "Rostverk": 16.0, "Temel": 19.0, "Döşeme": 22.0, "Perde": 26.0, "Merdiven": 32.0},
}

ELEMENT_ORDER = ["grobeton","rostverk","temel","doseme","perde","merdiven"]
LABELS = {
    "grobeton": "Grobeton (Подбетонка)",
    "rostverk": "Rostverk (Ростверк)",
    "temel":    "Temel (Фундамент)",
    "doseme":   "Döşeme (Плита перекрытия)",
    "perde":    "Perde (Стена/диафрагма)",
    "merdiven": "Merdiven (Лестница)",
}

# =============== Basit Versiyon Kontrol ===============
VERSION_FILE = os.path.join(os.path.dirname(__file__), "version.json")

def _file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _load_version() -> dict:
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"version": "0.1.0", "code_hash": ""}

def _save_version(v: dict) -> None:
    try:
        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            json.dump(v, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _bump_patch(ver: str) -> str:
    try:
        major, minor, patch = ver.split(".")
        return f"{major}.{minor}.{int(patch)+1}"
    except Exception:
        return "0.1.0"

def get_app_version(auto_bump: bool = True) -> str:
    state = _load_version()
    current_hash = _file_md5(__file__)
    if auto_bump and state.get("code_hash") != current_hash:
        state["version"] = _bump_patch(state.get("version", "0.1.0"))
        state["code_hash"] = current_hash
        _save_version(state)
    return state.get("version", "0.1.0")

# ---- Element key canon helpers (TR/RU/etiket -> kanonik anahtar) ----
CANON_KEYS = ("grobeton","rostverk","temel","doseme","perde","merdiven")

def get_tr_name_from_label(full_label: str) -> str:
    """'Grobeton (Подбетонка)' -> 'Grobeton'"""
    return str(full_label).split(" (")[0].strip()

def canon_key(x) -> str | None:
    """Kullanıcıdan/State'ten gelen değeri kanonik anahtara çevirir."""
    if x is None: return None
    s = str(x).strip()
    s_low = s.lower()

    # 1) Zaten kanonik mi?
    if s_low in CANON_KEYS:
        return s_low

    # 2) LABELS tam eşleşme (full label veya TR kısmı)
    try:
        for k, full in LABELS.items():
            if s == full or s_low == full.lower():
                return k
            if s_low == get_tr_name_from_label(full).lower():
                return k
    except Exception:
        pass

    # 3) Çıkmazsa None
    return None

def safe_label_from_key(k: str) -> str:
    """Kanonik anahtar -> ekranda görülen label. Yoksa anahtarı döndürür."""
    try:
        return LABELS[k]
    except Exception:
        return str(k)

# Oran doğrulaması için fallback kaynak
AUTO_RATE_SOURCES = [
    "https://raw.githubusercontent.com/bsrd-labs/ru-labor-rates/main/rates_latest.json",
    "https://raw.githubusercontent.com/bsrd-labs/ru-labor-rates/main/2024.json",
]

# OpenAI mevcut mu?
try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except Exception:
    _OPENAI_AVAILABLE = False

# =============== 1) SAYFA KONFİGÜRASYONU ===============
st.set_page_config(
    page_title="🏗️ Betonarme Hesaplama Modülü",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# =============== 2) MODERN STİL ===============
def inject_style():
    st.markdown("""
    <style>
        /* Ana tema renkleri */
        :root {
            --primary-color: #1f77b4;
            --secondary-color: #ff7f0e;
            --success-color: #2ca02c;
            --warning-color: #d62728;
            --info-color: #9467bd;
            --light-bg: #f8f9fa;
            --dark-bg: #343a40;
            --border-color: #dee2e6;
            --gradient-primary: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --gradient-secondary: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            
            /* Font ailesi ve boyutları */
            --font-primary: 'Segoe UI', 'Roboto', 'Helvetica Neue', Arial, sans-serif;
            --font-secondary: 'SF Pro Display', 'Inter', 'Segoe UI', sans-serif;
            --font-mono: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
            
            /* Font boyutları */
            --font-size-xs: 0.75rem;
            --font-size-sm: 0.875rem;
            --font-size-base: 1rem;
            --font-size-lg: 1.125rem;
            --font-size-xl: 1.25rem;
            --font-size-2xl: 1.5rem;
            --font-size-3xl: 1.875rem;
            --font-size-4xl: 2.25rem;
            
            /* Font ağırlıkları */
            --font-light: 300;
            --font-normal: 400;
            --font-medium: 500;
            --font-semibold: 600;
            --font-bold: 700;
            --font-extrabold: 800;
        }
        
        /* Sayfa başlığı */
        .main-header {
            background: var(--gradient-primary);
            color: white;
            padding: 2.5rem;
            border-radius: 20px;
            margin-bottom: 2rem;
            text-align: center;
            box-shadow: 0 15px 35px rgba(102, 126, 234, 0.3);
            position: relative;
            overflow: hidden;
        }
        
        .main-header::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><pattern id="grain" width="100" height="100" patternUnits="userSpaceOnUse"><circle cx="25" cy="25" r="1" fill="rgba(255,255,255,0.1)"/><circle cx="75" cy="75" r="1" fill="rgba(255,255,255,0.1)"/><circle cx="50" cy="10" r="0.5" fill="rgba(255,255,255,0.1)"/></pattern></defs><rect width="100" height="100" fill="url(%23grain)"/></svg>');
            opacity: 0.3;
        }
        
        .main-header h1 {
            font-family: var(--font-secondary);
            font-size: var(--font-size-4xl);
            font-weight: var(--font-extrabold);
            margin: 0;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
            position: relative;
            z-index: 1;
            letter-spacing: -0.025em;
        }
        
        .main-header p {
            font-family: var(--font-primary);
            font-size: var(--font-size-lg);
            font-weight: var(--font-medium);
            margin: 0.5rem 0 0 0;
            opacity: 0.95;
            position: relative;
            z-index: 1;
            letter-spacing: 0.025em;
        }
        
        /* Sekme stilleri */
        .stTabs [data-baseweb="tab-list"] {
            gap: 12px;
            background: var(--light-bg);
            border-radius: 15px;
            padding: 12px;
            border: 1px solid var(--border-color);
        }
        
        .stTabs [data-baseweb="tab"] {
            font-family: var(--font-primary);
            font-weight: 600;
            letter-spacing: 0.01em;
            border-radius: 12px;
            background: white;
            border: 2px solid var(--border-color);
            padding: 8px 12px;
            min-width: 48px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.2rem; /* emoji boyutu */
            transition: all 0.3s ease;
            position: relative;
            overflow: hidden;
        }
        
        .stTabs [data-baseweb="tab"]::before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: var(--gradient-primary);
            transition: left 0.4s ease;
            z-index: -1;
        }
        
        .stTabs [data-baseweb="tab"]:hover::before {
            left: 0;
        }
        
        .stTabs [data-baseweb="tab"]:hover {
            color: white;
            transform: translateY(-3px);
            box-shadow: 0 8px 25px rgba(31, 119, 180, 0.4);
            border-color: transparent;
        }
        
        .stTabs [aria-selected="true"] {
            background: var(--gradient-primary) !important;
            color: white !important;
            box-shadow: 0 8px 25px rgba(31, 119, 180, 0.5);
            border-color: transparent;
            transform: translateY(-2px);
        }
        
        /* Kart stilleri */
        .metric-card {
            background: white;
            border-radius: 20px;
            padding: 2rem;
            border: 2px solid var(--border-color);
            box-shadow: 0 8px 25px rgba(0,0,0,0.08);
            transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
        }
        
        .metric-card h3 {
            font-family: var(--font-secondary);
            font-size: var(--font-size-lg);
            font-weight: var(--font-semibold);
            color: #34495e;
            margin: 0 0 0.5rem 0;
            letter-spacing: 0.025em;
        }
        
        .metric-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 4px;
            height: 100%;
            background: var(--gradient-primary);
            transform: scaleY(0);
            transition: transform 0.3s ease;
        }
        
        .metric-card:hover::before {
            transform: scaleY(1);
        }
        
        .metric-card:hover {
            transform: translateY(-8px);
            box-shadow: 0 20px 40px rgba(0,0,0,0.15);
            border-color: var(--primary-color);
        }
        
        /* Buton stilleri */
        .stButton > button {
            border-radius: 30px;
            font-weight: 700;
            padding: 1rem 2.5rem;
            transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            border: none;
            box-shadow: 0 6px 20px rgba(0,0,0,0.15);
            font-size: 1.1rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .stButton > button:hover {
            transform: translateY(-4px);
            box-shadow: 0 12px 30px rgba(0,0,0,0.25);
        }
        
        /* Hesapla butonu özel stil */
        .hesapla-button {
            background: var(--gradient-primary);
            color: white;
            font-size: 1.4rem;
            padding: 1.5rem 4rem;
            border-radius: 40px;
            border: none;
            box-shadow: 0 12px 40px rgba(102, 126, 234, 0.5);
            transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
        }
        
        .hesapla-button::before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: var(--gradient-secondary);
            transition: left 0.4s ease;
            z-index: -1;
        }
        
        .hesapla-button:hover::before {
            left: 0;
        }
        
        .hesapla-button:hover {
            transform: translateY(-5px);
            box-shadow: 0 20px 50px rgba(102, 126, 234, 0.7);
        }
        
        /* Form elemanları */
        .stNumberInput > div > div > input {
            border-radius: 15px;
            border: 2px solid var(--border-color);
            transition: all 0.3s ease;
            font-size: 1.1rem;
            padding: 12px 16px;
        }
        
        .stNumberInput > div > div > input:focus {
            border-color: var(--primary-color);
            box-shadow: 0 0 0 4px rgba(31, 119, 180, 0.15);
            transform: scale(1.02);
        }
        
        /* Checkbox stilleri */
        .stCheckbox > label {
            font-weight: 600;
            color: var(--dark-bg);
            font-size: 1.1rem;
        }
        
        /* Dataframe stilleri */
        .stDataFrame {
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 12px 40px rgba(0,0,0,0.12);
            border: 2px solid var(--border-color);
            background: white;
            margin: 2rem 0;
            transition: all 0.3s ease;
        }
        
        .stDataFrame:hover {
            box-shadow: 0 16px 50px rgba(0,0,0,0.15);
            transform: translateY(-2px);
        }
        
        /* Info kutuları */
        .stAlert {
            border-radius: 15px;
            border: none;
            box-shadow: 0 6px 20px rgba(0,0,0,0.08);
            border-left: 5px solid var(--info-color);
        }
        
        /* Sidebar stilleri */
        .css-1d391kg {
            background: linear-gradient(180deg, #f8f9fa 0%, #e9ecef 100%);
        }
        
        /* Animasyonlar */
        @keyframes fadeInUp {
            from {
                opacity: 0;
                transform: translateY(30px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        
        .fade-in-up {
            animation: fadeInUp 0.6s ease-out;
        }
        /* Responsive tasarım */
        @media (max-width: 768px) {
            .main-header h1 {
                font-size: 2.5rem;
            }
            
            .main-header p {
                font-size: 1.1rem;
            }
            
            .stTabs [data-baseweb="tab"] {
                padding: 12px 20px;
                font-size: 1rem;
            }
        }
        
        /* Eski stiller korundu */
        .big-title{font-size:30px;font-weight:800;letter-spacing:.3px}
        .gpt-badge{display:inline-block;background:#eef2ff;color:#4338ca;border:1px solid #c7d2fe;border-radius:8px;padding:2px 8px;font-size:12px;margin-bottom:6px}
        .gpt-proposal{border-left:4px solid #6366f1;padding-left:10px;margin:8px 0}
        .diff-del{color:#b91c1c;text-decoration:line-through;margin-right:6px}
        .diff-add{color:#065f46;font-weight:600}
        .subtle{color:#6b7280}
        .warn{background:#fff7ed;border:1px solid #fed7aa;padding:8px 10px;border-radius:8px}
        .muted{color:#9ca3af;font-size:12px}
        .rowcard{border:1px solid #e5e7eb;border-radius:12px;padding:8px 10px;margin-bottom:6px;background:#fff}
        .rowgrid{display:grid;grid-template-columns: 1fr 1fr 120px 120px 120px;gap:8px;align-items:center}
        .badge{display:inline-block;padding:2px 8px;border:1px solid #e5e7eb;border-radius:999px;background:#f8fafc;font-size:12px;margin-right:6px}
        
        /* Gelişmiş font stilleri */
        .stMarkdown h3 {
            font-family: var(--font-secondary);
            font-size: var(--font-size-xl);
            font-weight: var(--font-bold);
            color: #1a202c;
            border-bottom: 2px solid var(--primary-color);
            padding-bottom: 0.5rem;
            margin-bottom: 1.5rem;
        }
        
        .metric-card .val {
            font-family: var(--font-mono);
            font-size: var(--font-size-2xl);
            font-weight: var(--font-bold);
            color: var(--primary-color);
        }
        
        .stDataFrame th {
            font-family: var(--font-secondary);
            font-weight: var(--font-semibold);
            font-size: var(--font-size-sm);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #2d3748;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
            padding: 16px 20px;
            border-bottom: 2px solid #dee2e6;
            text-align: left;
            position: sticky;
            top: 0;
            z-index: 10;
        }
        
        .stDataFrame td {
            font-family: var(--font-primary);
            font-weight: var(--font-normal);
            font-size: var(--font-size-sm);
            color: #4a5568;
            padding: 14px 20px;
            border-bottom: 1px solid #f1f3f4;
            line-height: 1.4;
            letter-spacing: 0.01em;
        }
        
        .stDataFrame tr:hover {
            background-color: #f8f9fa;
            transition: background-color 0.2s ease;
        }
        
        .stDataFrame tr:hover td {
            color: #2d3748;
            font-weight: var(--font-medium);
        }
        
        /* Sayısal değerler için özel font */
        .stDataFrame td:not(:first-child) {
            font-family: var(--font-mono);
            font-weight: var(--font-medium);
            text-align: right;
            color: #2c5282;
        }
        
        /* İlk sütun (eleman/rol adları) için özel font */
        .stDataFrame td:first-child {
            font-family: var(--font-secondary);
            font-weight: var(--font-semibold);
            color: #1a202c;
            text-align: left;
        }
        
        /* Tablo başlığı için özel stil */
        .stDataFrame h3 {
            font-family: var(--font-secondary);
            font-size: var(--font-size-xl);
            font-weight: var(--font-bold);
            color: #1a202c;
            margin-bottom: 1.5rem;
            padding-bottom: 0.75rem;
            border-bottom: 3px solid var(--primary-color);
            letter-spacing: -0.025em;
        }
        
        /* Yüzde değerleri için özel stil */
        .stDataFrame td:contains('%') {
            font-weight: var(--font-semibold);
            color: #805ad5;
        }
        
        /* Para birimi değerleri için özel stil */
        .stDataFrame td:contains('₽') {
            font-weight: var(--font-semibold);
            color: #38a169;
        }
        
        /* Metraj değerleri için özel stil */
        .stDataFrame td:contains('m³') {
            font-weight: var(--font-semibold);
            color: #3182ce;
        }
        
        /* Tablo satırları arası boşluk */
        .stDataFrame tbody tr {
            transition: all 0.2s ease;
        }
        
        /* Zebra striping için alternatif satır renkleri */
        .stDataFrame tbody tr:nth-child(even) {
            background-color: #fafbfc;
        }
        
        .stDataFrame tbody tr:nth-child(even):hover {
            background-color: #f1f5f9;
        }
        
        /* Tablo başlıkları için özel stil */
        .table-header h3 {
            font-family: var(--font-secondary);
            font-size: var(--font-size-xl);
            font-weight: var(--font-bold);
            color: #1a202c;
            margin: 2rem 0 1rem 0;
            padding: 1rem 1.5rem;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
            border-radius: 15px;
            border-left: 5px solid var(--primary-color);
            box-shadow: 0 4px 15px rgba(0,0,0,0.08);
            letter-spacing: -0.025em;
            text-align: center;
        }
        
        .table-header h3:hover {
            transform: translateX(5px);
            transition: transform 0.3s ease;
        }
        
        /* Tablo içi değerler için ek güzelleştirmeler */
        .stDataFrame .dataframe {
            font-family: var(--font-primary) !important;
            border-collapse: collapse !important;
            width: 100% !important;
        }
        
        /* Sayısal değerler için daha iyi hizalama */
        .stDataFrame td:not(:first-child) {
            text-align: right !important;
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            font-size: var(--font-size-sm) !important;
        }
        
        /* İlk sütun (isimler) için özel stil */
        .stDataFrame td:first-child {
            text-align: left !important;
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
            font-size: var(--font-size-sm) !important;
        }
        
        /* Tablo satırları için hover efekti */
        .stDataFrame tr:hover {
            background: linear-gradient(90deg, #f0f9ff 0%, #e0f2fe 100%) !important;
            transform: scale(1.01) !important;
            transition: all 0.2s ease !important;
        }
        
        /* Streamlit tablo override'ları - Daha güçlü */
        div[data-testid="stDataFrame"] {
            font-family: var(--font-primary) !important;
        }
        div[data-testid="stDataFrame"] table {
            font-family: var(--font-primary) !important;
        }
        
        div[data-testid="stDataFrame"] table th {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            font-size: var(--font-size-sm) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            color: #2d3748 !important;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%) !important;
            padding: 16px 20px !important;
            border-bottom: 2px solid #dee2e6 !important;
            text-align: left !important;
        }
        
        div[data-testid="stDataFrame"] table td {
            font-family: var(--font-primary) !important;
            font-weight: var(--font-normal) !important;
            font-size: var(--font-size-sm) !important;
            color: #4a5568 !important;
            padding: 14px 20px !important;
            border-bottom: 1px solid #f1f3f4 !important;
            line-height: 1.4 !important;
            letter-spacing: 0.01em !important;
        }
        
        div[data-testid="stDataFrame"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            text-align: right !important;
        }
        
        div[data-testid="stDataFrame"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
            text-align: left !important;
        }
        
        /* Ek tablo override'ları */
        .stDataFrame div[data-testid="stDataFrame"] div[data-testid="stTable"] {
            font-family: var(--font-primary) !important;
        }
        
        .stDataFrame div[data-testid="stDataFrame"] div[data-testid="stTable"] table {
            font-family: var(--font-primary) !important;
        }
        
        .stDataFrame div[data-testid="stDataFrame"] div[data-testid="stTable"] table th {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            font-size: var(--font-size-sm) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            color: #2d3748 !important;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%) !important;
            padding: 16px 20px !important;
            border-bottom: 2px solid #dee2e6 !important;
            text-align: left !important;
        }
        
        .stDataFrame div[data-testid="stDataFrame"] div[data-testid="stTable"] table td {
            font-family: var(--font-primary) !important;
            font-weight: var(--font-normal) !important;
            font-size: var(--font-size-sm) !important;
            color: #4a5568 !important;
            padding: 14px 20px !important;
            border-bottom: 1px solid #f1f3f4 !important;
            line-height: 1.4 !important;
            letter-spacing: 0.01em !important;
        }
        
        .stDataFrame div[data-testid="stDataFrame"] div[data-testid="stTable"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            text-align: right !important;
        }
        
        .stDataFrame div[data-testid="stDataFrame"] div[data-testid="stTable"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
            text-align: left !important;
        }
        
        /* Streamlit tablo hücrelerini zorla override et */
        [data-testid="stDataFrame"] table td {
            font-family: var(--font-primary) !important;
            font-weight: var(--font-normal) !important;
            font-size: var(--font-size-sm) !important;
            color: #4a5568 !important;
            padding: 14px 20px !important;
            border-bottom: 1px solid #f1f3f4 !important;
            line-height: 1.4 !important;
            letter-spacing: 0.01em !important;
        }
        
        [data-testid="stDataFrame"] table th {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            font-size: var(--font-size-sm) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            color: #2d3748 !important;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%) !important;
            padding: 16px 20px !important;
            border-bottom: 2px solid #dee2e6 !important;
            text-align: left !important;
        }
        
        /* İlk sütun (isimler) için zorla override */
        [data-testid="stDataFrame"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
            text-align: left !important;
        }
        
        /* Sayısal değerler için zorla override */
        [data-testid="stDataFrame"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            text-align: right !important;
        }
        
        /* Tüm tablo elementlerini zorla override et */
        [data-testid="stDataFrame"] * {
            font-family: var(--font-primary) !important;
        }
        
        [data-testid="stDataFrame"] table * {
            font-family: var(--font-primary) !important;
        }
        
        /* CSS specificity artırma */
        body [data-testid="stDataFrame"] table td {
            font-family: var(--font-primary) !important;
            font-weight: var(--font-normal) !important;
            font-size: var(--font-size-sm) !important;
            color: #4a5568 !important;
        }
        
        body [data-testid="stDataFrame"] table th {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            font-size: var(--font-size-sm) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            color: #2d3748 !important;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%) !important;
        }
        
        body [data-testid="stDataFrame"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
        }
        
        body [data-testid="stDataFrame"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            text-align: right !important;
        }
        
        /* En güçlü override - Streamlit'in inline CSS'ini bile geçersiz kıl */
        html body [data-testid="stDataFrame"] table td {
            font-family: var(--font-primary) !important;
            font-weight: var(--font-normal) !important;
            font-size: var(--font-size-sm) !important;
            color: #4a5568 !important;
            padding: 14px 20px !important;
            border-bottom: 1px solid #f1f3f4 !important;
            line-height: 1.4 !important;
            letter-spacing: 0.01em !important;
        }
        
        html body [data-testid="stDataFrame"] table th {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            font-size: var(--font-size-sm) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            color: #2d3748 !important;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%) !important;
            padding: 16px 20px !important;
            border-bottom: 2px solid #dee2e6 !important;
            text-align: left !important;
        }
        
        html body [data-testid="stDataFrame"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
            text-align: left !important;
        }
        
        html body [data-testid="stDataFrame"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            text-align: right !important;
        }
        
        /* Streamlit'in kendi CSS'ini tamamen override et */
        .stDataFrame [data-testid="stDataFrame"] table td,
        .stDataFrame [data-testid="stDataFrame"] table th,
        [data-testid="stDataFrame"] table td,
        [data-testid="stDataFrame"] table th {
            font-family: var(--font-primary) !important;
        }
        
        /* Hücre içi font override'ları */
        .stDataFrame [data-testid="stDataFrame"] table td:first-child,
        [data-testid="stDataFrame"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
        }
        
        .stDataFrame [data-testid="stDataFrame"] table td:not(:first-child),
        [data-testid="stDataFrame"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
        }
        
        /* JavaScript ile CSS injection için hazırlık */
        .custom-table-fonts {
            font-family: var(--font-primary) !important;
        }
        
        .custom-table-fonts td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
        }
        
        .custom-table-fonts td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            text-align: right !important;
        }
        
        .custom-table-fonts th {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
        }
        
        /* En güçlü CSS override - Streamlit'in tüm CSS'ini geçersiz kıl */
        /* CSS specificity maksimum seviyede */
        html body div[data-testid="stDataFrame"] table td {
            font-family: var(--font-primary) !important;
            font-weight: var(--font-normal) !important;
            font-size: var(--font-size-sm) !important;
            color: #4a5568 !important;
            padding: 14px 20px !important;
            border-bottom: 1px solid #f1f3f4 !important;
            line-height: 1.4 !important;
            letter-spacing: 0.01em !important;
        }
        
        html body div[data-testid="stDataFrame"] table th {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            font-size: var(--font-size-sm) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            color: #2d3748 !important;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%) !important;
            padding: 16px 20px !important;
            border-bottom: 2px solid #dee2e6 !important;
            text-align: left !important;
        }
        
        html body div[data-testid="stDataFrame"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
            text-align: left !important;
        }
        
        html body div[data-testid="stDataFrame"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            text-align: right !important;
        }
        /* Streamlit'in kendi CSS'ini tamamen override et - Tüm olası selector'lar */
        div[data-testid="stDataFrame"] table td,
        div[data-testid="stDataFrame"] table th,
        .stDataFrame table td,
        .stDataFrame table th,
        [data-testid="stDataFrame"] table td,
        [data-testid="stDataFrame"] table th {
            font-family: var(--font-primary) !important;
        }
        
        /* Hücre içi font override'ları - Tüm olası selector'lar */
        div[data-testid="stDataFrame"] table td:first-child,
        .stDataFrame table td:first-child,
        [data-testid="stDataFrame"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
        }
        
        div[data-testid="stDataFrame"] table td:not(:first-child),
        .stDataFrame table td:not(:first-child),
        [data-testid="stDataFrame"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
        }
        
        /* CSS specificity maksimum seviyede - Streamlit'in tüm CSS'ini geçersiz kıl */
        /* En güçlü selector'lar */
        html body div[data-testid="stDataFrame"] div[data-testid="stTable"] table td {
            font-family: var(--font-primary) !important;
            font-weight: var(--font-normal) !important;
            font-size: var(--font-size-sm) !important;
            color: #4a5568 !important;
            padding: 14px 20px !important;
            border-bottom: 1px solid #f1f3f4 !important;
            line-height: 1.4 !important;
            letter-spacing: 0.01em !important;
        }
        
        html body div[data-testid="stDataFrame"] div[data-testid="stTable"] table th {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            font-size: var(--font-size-sm) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            color: #2d3748 !important;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%) !important;
            padding: 16px 20px !important;
            border-bottom: 2px solid #dee2e6 !important;
            text-align: left !important;
        }
        
        html body div[data-testid="stDataFrame"] div[data-testid="stTable"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
            text-align: left !important;
        }
        
        html body div[data-testid="stDataFrame"] div[data-testid="stTable"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            text-align: right !important;
        }
        
        /* Ek güçlü selector'lar */
        html body div[data-testid="stDataFrame"] div[data-testid="stTable"] div[data-testid="stTableCell"] {
            font-family: var(--font-primary) !important;
        }
        
        html body div[data-testid="stDataFrame"] div[data-testid="stTable"] div[data-testid="stTableCell"]:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
        }
        
        html body div[data-testid="stDataFrame"] div[data-testid="stTable"] div[data-testid="stTableCell"]:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
        }
        
        /* Özel tablo wrapper CSS'i - Maksimum güç */
        .custom-table-wrapper [data-testid="stDataFrame"] table td {
            font-family: var(--font-primary) !important;
            font-weight: var(--font-normal) !important;
            font-size: var(--font-size-sm) !important;
            color: #4a5568 !important;
            padding: 14px 20px !important;
            border-bottom: 1px solid #f1f3f4 !important;
            line-height: 1.4 !important;
            letter-spacing: 0.01em !important;
        }
        
        .custom-table-wrapper [data-testid="stDataFrame"] table th {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            font-size: var(--font-size-sm) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            color: #2d3748 !important;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%) !important;
            padding: 16px 20px !important;
            border-bottom: 2px solid #dee2e6 !important;
            text-align: left !important;
        }
        
        .custom-table-wrapper [data-testid="stDataFrame"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
            text-align: left !important;
        }
        
        .custom-table-wrapper [data-testid="stDataFrame"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            text-align: right !important;
        }
        
        /* En güçlü CSS override - Streamlit'in tüm CSS'ini geçersiz kıl */
        /* CSS specificity maksimum seviyede */
        html body .custom-table-wrapper [data-testid="stDataFrame"] table td {
            font-family: var(--font-primary) !important;
            font-weight: var(--font-normal) !important;
            font-size: var(--font-size-sm) !important;
            color: #4a5568 !important;
            padding: 14px 20px !important;
            border-bottom: 1px solid #f1f3f4 !important;
            line-height: 1.4 !important;
            letter-spacing: 0.01em !important;
        }
        
        html body .custom-table-wrapper [data-testid="stDataFrame"] table th {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            font-size: var(--font-size-sm) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            color: #2d3748 !important;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%) !important;
            padding: 16px 20px !important;
            border-bottom: 2px solid #dee2e6 !important;
            text-align: left !important;
        }
        
        html body .custom-table-wrapper [data-testid="stDataFrame"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
            text-align: left !important;
        }
        
        html body .custom-table-wrapper [data-testid="stDataFrame"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            text-align: right !important;
        }
        
        /* Ek güçlü selector'lar */
        html body .custom-table-wrapper [data-testid="stDataFrame"] div[data-testid="stTable"] table td {
            font-family: var(--font-primary) !important;
            font-weight: var(--font-normal) !important;
            font-size: var(--font-size-sm) !important;
            color: #4a5568 !important;
        }
        
        html body .custom-table-wrapper [data-testid="stDataFrame"] div[data-testid="stTable"] table th {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            font-size: var(--font-size-sm) !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            color: #2d3748 !important;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%) !important;
        }
        
        html body .custom-table-wrapper [data-testid="stDataFrame"] div[data-testid="stTable"] table td:first-child {
            font-family: var(--font-secondary) !important;
            font-weight: var(--font-semibold) !important;
            color: #1a202c !important;
        }
        
        html body .custom-table-wrapper [data-testid="stDataFrame"] div[data-testid="stTable"] table td:not(:first-child) {
            font-family: var(--font-mono) !important;
            font-weight: var(--font-medium) !important;
            color: #2c5282 !important;
            text-align: right !important;
        }
    </style>
    """, unsafe_allow_html=True)

# =============== 2) ORTAK YARDIMCILAR ===============

# ---- Loading placeholder helpers (idempotent, session-safe) ----
def get_loading_placeholder():
    """Tek bir loading placeholder döndürür. Yoksa oluşturur."""
    key = "__loading_ph__"
    ph = st.session_state.get(key)
    if ph is None:
        st.session_state[key] = st.empty()
        ph = st.session_state[key]
    return ph

def clear_loading_placeholder():
    """Varsa placeholder'ı temizler; yoksa sessizce geçer."""
    ph = st.session_state.get("__loading_ph__")
    if ph is not None:
        try:
            ph.empty()
        except Exception:
            pass

# --- Price & difficulty helpers (centralized) ---
SCENARIO_BASELINE = "Gerçekçi"  # referans senaryo

# Override'lı senaryo normları okuma helper'ı
def get_effective_scenario_norms() -> dict:
    """SCENARIO_NORMS üzerine override varsa onu döndürür."""
    try:
        ovr = st.session_state.get("SCENARIO_NORMS_OVR")
        if isinstance(ovr, dict) and ovr:
            return ovr
    except Exception:
        pass
    return SCENARIO_NORMS

def get_scenario_multiplier_for_price(current_scenario: str) -> float:
    # Temel (Gerçekçi) ile mevcut senaryonun 'Temel' normunu oranla
    try:
        norms_map = get_effective_scenario_norms()
        ref = float(norms_map.get(SCENARIO_BASELINE, SCENARIO_NORMS["Gerçekçi"]) ["Temel"])
        cur = float(norms_map.get(current_scenario, SCENARIO_NORMS["Gerçekçi"]) ["Temel"])
        return (cur / ref) if ref > 0 else 1.0
    except Exception:
        return 1.0

def _update_diff_cache():
    # Zorluk UI/State: her rerun'da taze hesapla ve cache'e yaz
    keys = ["f_winter", "f_heavy", "f_repeat", "f_shared", "f_cong", "f_pump"]
    z = 1.0
    for k in keys:
        try:
            v = float(st.session_state.get(k, 0.0) or 0.0)
        except Exception:
            v = 0.0
        # v örn. 0.20 ise +%20 verimsizlik => 1.20
        z *= (1.0 + v)
    st.session_state["_diff_total_mult_cache"] = z
    return z

def get_difficulty_multiplier_cached() -> float:
    # Fiyat/hesap tarafı buradan okusun (yoksa 1.0)
    try:
        return float(st.session_state.get("_diff_total_mult_cache", 1.0))
    except Exception:
        return 1.0

@st.cache_data(ttl=3600)  # 1 saat cache
def get_default_metraj_df(selected_elements):
    """Metraj tablosu için varsayılan DataFrame'i cache'le"""
    rows = []
    for k in selected_elements:
        if k in LABELS:
            rows.append({"Eleman (Элемент)": LABELS[k], "Metraj (m³) (Объём, м³)": 1.0})
        else:
            st.warning(f"Tanımsız eleman anahtarı atlandı: {k}")
    return pd.DataFrame(rows)

@st.cache_data(ttl=3600)  # 1 saat cache
def get_default_roles_df():
    """Roller tablosu için varsayılan DataFrame'i cache'le"""
    return pd.DataFrame([
        {"Rol (Роль)":"brigadir","Ağırlık (Вес)":0.10,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":120000,"%RUS":100,"%SNG":0,"%TUR":0},
        {"Rol (Роль)":"kalfa","Ağırlık (Вес)":0.20,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":110000,"%RUS":20,"%SNG":60,"%TUR":20},
        {"Rol (Роль)":"usta_demirci","Ağırlık (Вес)":0.60,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":100000,"%RUS":10,"%SNG":70,"%TUR":20},
        {"Rol (Роль)":"usta_kalipci","Ağırlık (Вес)":0.60,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":100000,"%RUS":10,"%SNG":70,"%TUR":20},
        {"Rol (Роль)":"betoncu","Ağırlık (Вес)":1.00,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":90000,"%RUS":10,"%SNG":70,"%TUR":20},
        {"Rol (Роль)":"duz_isci","Ağırlık (Вес)":0.50,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":80000,"%RUS":10,"%SNG":70,"%TUR":20},
    ])

def gross_from_net(net: float, ndfl_rate: float) -> float:
    return float(net) if ndfl_rate<=0 else float(net)/(1.0-ndfl_rate)

def employer_cost_for_gross(gross: float, ops: float, oss: float, oms: float, nsipz: float) -> float:
    return float(gross)*(1.0+ops+oss+oms+nsipz)

# --- Progressive NDFL helpers (resident brackets 2025) ---
def _resident_ndfl_brackets_2025() -> list[tuple[float|None, float]]:
    """Returns [(upper_limit, rate), ...] with last upper_limit=None as infinity."""
    # Annual thresholds (RUB) and rates
    return [
        (2_400_000.0, 0.13),
        (5_000_000.0, 0.15),
        (20_000_000.0, 0.18),
        (50_000_000.0, 0.20),
        (None, 0.22),
    ]

def gross_from_net_progressive_resident(net_annual: float) -> float:
    """Invert progressive tax to get annual gross from annual net, using resident brackets 2025."""
    try:
        target_net = max(0.0, float(net_annual))
    except Exception:
        target_net = 0.0
    if target_net <= 0.0:
        return 0.0

    brackets = _resident_ndfl_brackets_2025()
    gross_accum = 0.0
    net_remaining = target_net
    prev_limit = 0.0

    for upper, rate in brackets:
        segment_width = (upper - prev_limit) if upper is not None else None
        segment_net_cap = (segment_width * (1.0 - rate)) if segment_width is not None else None

        if segment_width is None:
            # infinite top bracket
            gross_accum += net_remaining / (1.0 - rate)
            net_remaining = 0.0
            break

        if net_remaining >= segment_net_cap - 1e-9:
            # fill entire segment
            gross_accum += segment_width
            net_remaining -= segment_net_cap
            prev_limit = upper
            continue
        else:
            # partial in this segment
            gross_accum += net_remaining / (1.0 - rate)
            net_remaining = 0.0
            break

    return gross_accum

def try_fetch_json(url:str):
    try:
        r=requests.get(url, timeout=8, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def auto_fetch_rates():
    for u in AUTO_RATE_SOURCES:
        js=try_fetch_json(u)
        if isinstance(js,dict) and js: return js,u
    return None,None

def workdays_between(start: date, end: date, mode: str) -> int:
    if end < start: return 0
    total=0
    for i in range((end-start).days+1):
        d = start + timedelta(days=i)
        wd = d.weekday()
        if mode=="tam_calisma": total+=1
        elif mode=="her_pazar": total += (wd!=6)
        elif mode=="hafta_sonu_tatil": total += (wd not in (5,6))
        elif mode=="iki_haftada_bir_pazar":
            if wd==6:
                week_idx=((d-start).days//7)
                if (week_idx%2)==1: continue
            total+=1
        else: total+=(wd!=6)
    
    return total

def month_start(d: date)->date: return d.replace(day=1)
def next_month(d: date)->date:
    return d.replace(year=d.year+1, month=1, day=1) if d.month==12 else d.replace(month=d.month+1, day=1)
def last_day_of_month(d: date)->date: return next_month(d)-timedelta(days=1)

def iter_months(start:date,end:date):
    cur=month_start(start)
    while cur<=end:
        yield cur
        cur=next_month(cur)

def workdays_in_month_range(start: date, end: date, mode: str) -> pd.DataFrame:
    rows=[]
    for m0 in iter_months(start,end):
        m1=last_day_of_month(m0)
        a,b=max(start,m0),min(end,m1)
        if a>b: continue
        rows.append({"Ay (Месяц)":m0.strftime("%Y-%m"),"İş Günü (Раб. день)":workdays_between(a,b,mode)})
    return pd.DataFrame(rows)

def percent_input(label:str, default_pct:float, min_val:float=0.0, max_val:float=100.0, help:str="", key:str|None=None, disabled:bool=False)->float:
    # Basit widget, session_state otomatik güncellenir
    v = st.number_input(
        label, 
        min_value=min_val, 
        max_value=max_val, 
        value=default_pct, 
        step=0.5, 
        help=help, 
        key=key, 
        disabled=disabled
    )
    
    return v/100.0  # yüzde → oran
def round_preserve_sum(values):
    vals=[float(x) for x in values]
    floors=[math.floor(x) for x in vals]
    rem=int(round(sum(vals)))-sum(floors)
    fracs=[(i, vals[i]-floors[i]) for i in range(len(vals))]
    fracs.sort(key=lambda t:t[1], reverse=True)
    res=floors[:]
    for i,_ in fracs[:max(0,rem)]:
        res[i]+=1
    return res
# =============== 3) İŞVEREN MALİYETİ (RUS/SNG/TUR) ===============
def monthly_role_cost_multinational(row: pd.Series, prim_sng: bool, prim_tur: bool, extras_person_ex_vat: float) -> dict:
    """
    ÖNEMLİ:
    - İşveren primleri, yalnız RESMİ BRÜT tutara uygulanır (OPS/OSS/OMS + NSiPZ). Brüt+NDFL değil.
    - 'Gayriresmî/Elden' (nakit) kısma hiçbir vergi/prim eklenmez; sadece komisyon (CASH_COMMISSION_RATE) eklenir.
    - SNG (patent): resmi brüt, SNG_TAXED_BASE ile sınırlanır; + aylık patent tutarı eklenir.
    - VKS (TR): yalnız NSiPZ uygulanır (OPS/OSS/OMS = 0).
    """
    net=float(row["Net Maaş (₽, na ruki) (Чистая з/п, ₽)"])

    # Get constants with override support
    OVR = st.session_state.get("CONST_OVERRIDES", {})
    ndfl_rus = OVR.get("NDFL_RUS", NDFL_RUS)
    ndfl_sng = OVR.get("NDFL_SNG", NDFL_SNG)
    ndfl_tur = OVR.get("NDFL_TUR", NDFL_TUR)
    ops = OVR.get("OPS", OPS)
    oss = OVR.get("OSS", OSS)
    oms = OVR.get("OMS", OMS)
    nsipz_risk_rus_sng = OVR.get("NSIPZ_RISK_RUS_SNG", NSIPZ_RISK_RUS_SNG)
    nsipz_risk_tur_vks = OVR.get("NSIPZ_RISK_TUR_VKS", NSIPZ_RISK_TUR_VKS)
    sng_patent_month = OVR.get("SNG_PATENT_MONTH", SNG_PATENT_MONTH)
    sng_taxed_base = OVR.get("SNG_TAXED_BASE", SNG_TAXED_BASE)
    tur_taxed_base = OVR.get("TUR_TAXED_BASE", TUR_TAXED_BASE)
    cash_commission_rate = OVR.get("CASH_COMMISSION_RATE", CASH_COMMISSION_RATE)

    # Vergi rejimi: Artan (2025) mı, sabit oran mı?
    use_progressive_ndfl = bool(st.session_state.get("use_progressive_ndfl", True))

    # RUS (tam sigortalı)
    if use_progressive_ndfl:
        gross_rus = gross_from_net_progressive_resident(net*12.0) / 12.0
    else:
        gross_rus = gross_from_net(net, ndfl_rus)
    per_rus   = employer_cost_for_gross(gross_rus, ops, oss, oms, nsipz_risk_rus_sng) + extras_person_ex_vat

    # SNG (patent; tüm sigorta sistemleri + patent; resmi brüt asgariyi sağlar)
    if use_progressive_ndfl:
        # 2025 kademeli NDFL'i yıllık bazda uygula (patent avansı mahsup edilmez — sade model)
        gross_sng_full = gross_from_net_progressive_resident(net*12.0) / 12.0
    else:
        gross_sng_full = gross_from_net(net, ndfl_sng)
    # Resmi brüt asgariyi sağla
    min_off_sng = float(sng_taxed_base)
    if gross_sng_full < min_off_sng:
        gross_sng_full = min_off_sng
    if prim_sng:
        gross_sng_off = min_off_sng                               # resmi brüt en az asgari
        prim_amount   = max(gross_sng_full - gross_sng_off, 0.0)   # elden kısım (vergisiz/primsiz)
        commission    = prim_amount * cash_commission_rate
    else:
        gross_sng_off = gross_sng_full                             # prim yoksa tamamı resmi olabilir
        prim_amount   = 0.0
        commission    = 0.0
    per_sng = employer_cost_for_gross(gross_sng_off, ops, oss, oms, nsipz_risk_rus_sng) \
              + sng_patent_month + extras_person_ex_vat + prim_amount + commission

    # TUR (VKS; yalnız iş kazası primi; resmi brüt asgariyi sağlar)
    if use_progressive_ndfl:
        # VKS (TR) — progressive NDFL resident brackets on annualized basis (12 aylık varsayım)
        gross_tur_full = gross_from_net_progressive_resident(net*12.0) / 12.0
    else:
        gross_tur_full = gross_from_net(net, ndfl_tur)
    # Resmi brüt asgariyi sağla
    min_off_tur = float(tur_taxed_base)
    if gross_tur_full < min_off_tur:
        gross_tur_full = min_off_tur
    if prim_tur:
        gross_tur_off = min_off_tur
        prim_tr       = max(gross_tur_full - gross_tur_off, 0.0)
        comm_tr       = prim_tr * cash_commission_rate
    else:
        gross_tur_off = gross_tur_full
        prim_tr       = 0.0
        comm_tr       = 0.0
    per_tur = employer_cost_for_gross(gross_tur_off, 0.0,0.0,0.0,nsipz_risk_tur_vks) \
              + extras_person_ex_vat + prim_tr + comm_tr

    # Uzaktan çalışma senaryoları kaldırıldı — standart yerinde çalışma varsayımı

    # Ülke karması
    p_rus=max(float(row["%RUS"]),0.0); p_sng=max(float(row["%SNG"]),0.0); p_tur=max(float(row["%TUR"]),0.0)
    tot=p_rus+p_sng+p_tur or 100.0
    p_rus,p_sng,p_tur = p_rus/tot, p_sng/tot, p_tur/tot
    blended=p_rus*per_rus+p_sng*per_sng+p_tur*per_tur

    return {"per_person":{"RUS":per_rus,"SNG":per_sng,"TUR":per_tur,"BLENDED":blended}}

# =============== 4) NORM OLUŞTURMA ===============
def build_norms_for_scenario(scenario: str, selected_elements: list[str]) -> tuple[float, dict[str, float]]:
    """
    - selected_elements içindeki her girdiyi 'canon_key' ile normalize eder.
    - SCENARIO_NORMS'tan TR isimle okur (örn: 'Grobeton', 'Temel' vs).
    - Bulunamayan anahtar/etiketlerde hata fırlatmaz; uyarı gösterir ve o kalemi atlar.
    - Çıkış: (Temel normu, {FULL_LABEL -> relatif çarpan})
    """
    norms_map = get_effective_scenario_norms()
    norms = norms_map.get(scenario) or SCENARIO_NORMS["Gerçekçi"]
    n_temel = float(norms["Temel"])

    # 1) geçerli kanonik anahtar listesi
    canon_list: list[str] = []
    for raw in (selected_elements or []):
        k = canon_key(raw)
        if not k:
            st.warning(f"Tanımsız eleman anahtarı atlandı: {raw}")
            continue
        if k not in CANON_KEYS:
            st.warning(f"Kanonik listede yok, atlandı: {raw}")
            continue
        canon_list.append(k)

    # Hiç eleman kalmadıysa, tümünü varsay
    if not canon_list:
        canon_list = list(CANON_KEYS)

    # 2) relatif katsayıları hesaplamak için referanslar
    rel_vals = []
    for k in canon_list:
        tr_name = get_tr_name_from_label(safe_label_from_key(k))
        if tr_name not in norms:
            st.warning(f"SCENARIO_NORMS içinde bulunamadı: {tr_name}")
            continue
        rel_vals.append(norms[tr_name] / n_temel)

    avg_rel = (sum(rel_vals) / len(rel_vals)) if rel_vals else 1.0

    # 3) çıkış haritası
    k_norm: dict[str, float] = {}
    for k in canon_list:
        full_label = safe_label_from_key(k)
        tr_name = get_tr_name_from_label(full_label)
        base = norms.get(tr_name, n_temel) / n_temel
        k_norm[full_label] = base / (avg_rel or 1.0)

    return n_temel, k_norm

def apply_overhead_on_core(core_cost:float, p:float)->float:
    p=min(max(p,0.0),OVERHEAD_RATE_MAX)
    return p*core_cost

# =============== 5) GPT / ÖNERİ / ORAN KONTROL ===============
def extract_json_block(text: str) -> str:
    """```json ... ``` içinden ham JSON'u temizle."""
    if text is None: return ""
    t = text.strip()
    if "```" in t:
        t = t.split("```", 1)[1] if t.startswith("```") else t
        t = t.replace("json", "", 1) if t.lstrip().lower().startswith("json") else t
        t = t.split("```")[0]
    return t.strip()

def get_openai_client():
    api_key = (st.session_state.get("OPENAI_API_KEY","") or os.getenv("OPENAI_API_KEY",""))
    if not (_OPENAI_AVAILABLE and api_key): return None
    return OpenAI(api_key=api_key)

def gpt_propose_params(payload: dict, model: str = "gpt-4o-mini") -> dict|None:
    client = get_openai_client()
    if client is None: return None
    system = "Kıdemli şantiye maliyet şefisin. Sadece JSON yanıt ver. Yüzdeler 0-100 sayı."
    user = ("Aşağıdaki proje parametrelerine göre makul ayar öner.\n"
            'Şema: {"consumables_pct": number, "overhead_pct": number, "hours_per_day": number, '
            '"scenario": "İdeal|Gerçekçi|Kötü", '
            '"reasons": {"consumables": string, "overhead": string, "hours": string, "scenario": string}}\n\n'
            f"VERİ: {json.dumps(payload, ensure_ascii=False)}")
    try:
        r = client.chat.completions.create(
            model=model, temperature=0.2,
            messages=[{"role":"system","content":system},{"role":"user","content":user}]
        )
        return json.loads(extract_json_block(r.choices[0].message.content))
    except Exception:
        return None

def gpt_verify_rates_via_web(queries: list[str], model: str="gpt-4o-mini") -> dict|None:
    client = get_openai_client()
    if client is None: return None
    tavily_key = (st.session_state.get("TAVILY_API_KEY","") or os.getenv("TAVILY_API_KEY",""))
    snippets=[]
    if tavily_key:
        try:
            for q in queries:
                resp=requests.post("https://api.tavily.com/search",
                    json={"api_key":tavily_key,"query":q,"search_depth":"basic","max_results":3},timeout=12).json()
                for it in resp.get("results",[]):
                    snippets.append(f"{it.get('url','')} — {it.get('content','')[:800]}")
        except Exception:
            pass
    if not snippets:
        remote,src=auto_fetch_rates()
        if not remote: return None
        snippets=[json.dumps(remote,ensure_ascii=False), f"KAYNAK: {src}"]
    corpus="\n\n---\n\n".join(snippets)
    system=("Aşağıdaki metinden Rusya 2024/25 işçilik vergi/prim oranlarını çıkar. "
            'JSON anahtarları: ["NDFL_RUS","NDFL_SNG","NDFL_TUR","OPS","OSS","OMS","NSIPZ_RISK_RUS_SNG","NSIPZ_RISK_TUR_VKS",'
            '"SNG_PATENT_MONTH","SNG_TAXED_BASE","TUR_TAXED_BASE","CASH_COMMISSION_RATE"] — yüzdeler 0-1 oran.')
    try:
        r=client.chat.completions.create(model=model,temperature=0.0,
              messages=[{"role":"system","content":system},{"role":"user","content":corpus}])
        return json.loads(extract_json_block(r.choices[0].message.content))
    except Exception:
        return None

# =============== 6) RAG (Yükle-İndeksle-Ara) ===============
RAG_DIR  = "rag_data"
RAG_FILE = os.path.join(RAG_DIR, "store.jsonl")  # {id, text, embedding, meta}

def ensure_rag_dir():
    os.makedirs(RAG_DIR, exist_ok=True)
    if not os.path.exists(RAG_FILE): open(RAG_FILE, "a", encoding="utf-8").close()

def iter_rag_store():
    ensure_rag_dir()
    with open(RAG_FILE,"r",encoding="utf-8") as f:
        for line in f:
            line=line.strip()
            if not line: continue
            try: yield json.loads(line)
            except Exception: continue

def load_rag_in_memory():
    items=list(iter_rag_store())
    embeds=[np.array(it["embedding"],dtype=np.float32) for it in items]
    return items, embeds

def save_rag_records(recs:list[dict]):
    ensure_rag_dir()
    with open(RAG_FILE,"a",encoding="utf-8") as f:
        for r in recs: f.write(json.dumps(r,ensure_ascii=False)+"\n")

def chunk_text(s: str, max_words: int=220):
    words=s.split()
    return [" ".join(words[i:i+max_words]) for i in range(0,len(words),max_words)]

def file_to_chunks(uploaded) -> list[dict]:
    name=uploaded.name; ext=name.lower().split(".")[-1]; chunks=[]
    try:
        if ext=="txt":
            txt=uploaded.read().decode("utf-8","ignore")
            for i,ch in enumerate(chunk_text(txt)): chunks.append({"text":ch,"meta":{"filename":name,"kind":"txt","part":i}})
        elif ext=="csv":
            df=pd.read_csv(uploaded)
            for i,row in df.iterrows():
                s=", ".join(f"{c}={row[c]}" for c in df.columns)
                for j,ch in enumerate(chunk_text(s,120)):
                    chunks.append({"text":ch,"meta":{"filename":name,"kind":"csv","row":int(i),"part":j}})
        elif ext in ("xlsx","xls"):
            df=pd.read_excel(uploaded)
            for i,row in df.iterrows():
                s=", ".join(f"{c}={row[c]}" for c in df.columns)
                for j,ch in enumerate(chunk_text(s,120)):
                    chunks.append({"text":ch,"meta":{"filename":name,"kind":"xlsx","row":int(i),"part":j}})
        else:
            chunks.append({"text":f"[desteklenmeyen tür] {name}","meta":{"filename":name,"kind":ext}})
    except Exception as e:
        chunks.append({"text":f"[okuma hatası: {e}]","meta":{"filename":name,"kind":"err"}})
    return chunks

def embed_texts(texts:list[str]) -> list[list[float]]|None:
    client=get_openai_client()
    if client is None: return None
    try:
        res=client.embeddings.create(model="text-embedding-3-small", input=texts)
        return [d.embedding for d in res.data]
    except Exception:
        return None

def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    na=np.linalg.norm(a); nb=np.linalg.norm(b)
    if na==0 or nb==0: return 0.0
    return float(np.dot(a,b)/(na*nb))

def rag_search(query:str, topk:int=5):
    items, embeds = load_rag_in_memory()
    q_embs = embed_texts([query])
    if not q_embs: return []
    q = np.array(q_embs[0], dtype=np.float32)
    scored=[]
    for it, e in zip(items, embeds):
        scored.append((cosine_sim(q,e), it))
    scored.sort(key=lambda t:t[0], reverse=True)
    return [it for s,it in scored[:topk] if s>0.15]

def controller_chat(current_state: dict):
    """Betonarme modülüyle sınırlı AI denetleyicisi — önerir, onayınla uygular."""
    st.markdown("### 🧠 RAG + AI Denetleyici (Betonarme Modülü)")
    client = get_openai_client()
    if client is None:
        st.info("OpenAI anahtarı yok → denetleyici devre dışı.")
        return

    if "ctrl_msgs" not in st.session_state:
        st.session_state["ctrl_msgs"] = [
            {"role":"system","content":(
                "Rolün: Betonarme işçilik modülü asistanı. Sadece bu modülden konuş. "
                "Değişiklik önereceksen SADECE şu JSON şemasını döndür:\n"
                '{"proposal_id":"uuid","notes":"...","changes":[\n'
                ' {"key":"consumables_rate_pct","new_value":number,"why":"..."},\n'
                ' {"key":"overhead_rate_pct","new_value":number,"why":"..."},\n'
                ' {"key":"hours_per_day","new_value":number,"why":"..."},\n'
                ' {"key":"scenario","new_value":"İdeal|Gerçekçi|Kötü","why":"..."}\n]}'
            )}
        ]

    with st.expander("📚 Bilgi Bankasından bağlam (RAG)", expanded=False):
        q = st.text_input("Sorgu")
        if st.button("🔎 Ara") and q.strip():
            hits = rag_search(q.strip(), topk=6)
            st.session_state["rag_hits"] = hits or []
            st.success(f"{len(hits or [])} parça.")
        for it in st.session_state.get("rag_hits", []):
            st.caption(f"• {it.get('meta',{}).get('filename','?')} — {it.get('meta',{})}")
            st.code(it.get("text","")[:700])

    user_msg = st.chat_input("Mesajınız…")
    if user_msg:
        st.session_state["ctrl_msgs"].append({"role":"user","content":user_msg})
        rag_snips = "\n\n".join(
            [f"[{i+1}] {it['meta'].get('filename','?')}: {it['text'][:600]}"
             for i,it in enumerate(st.session_state.get("rag_hits", []))]
        )
        prompt = f"STATE:\n{json.dumps(current_state,ensure_ascii=False)}\n\nRAG:\n{rag_snips or '(yok)'}\n"
        try:
            r = client.chat.completions.create(
                model="gpt-4o-mini", temperature=0.2,
                messages=st.session_state["ctrl_msgs"] + [{"role":"user","content":prompt}]
            )
            reply = r.choices[0].message.content or ""
        except Exception as e:
            reply = f"Hata: {e}"

        with st.chat_message("assistant"):
            st.write(reply)

        try: st.session_state["last_controller_proposal"] = json.loads(extract_json_block(reply))
        except Exception: pass

    prop = st.session_state.get("last_controller_proposal")
    if prop and isinstance(prop, dict) and "changes" in prop:
        st.markdown("📝 AI teklifi")
        st.code(json.dumps(prop, ensure_ascii=False, indent=2), language="json")
        if st.button("✅ Önerileri uygula"):
            cons_target=None; over_target=None
            for ch in prop.get("changes", []):
                if ch.get("key")=="consumables_rate_pct":
                    st.session_state["consumables_rate"] = float(ch.get("new_value",0.0))
                if ch.get("key")=="overhead_rate_pct":
                    st.session_state["overhead_rate"] = float(ch.get("new_value",0.0))
                if ch.get("key")=="hours_per_day":
                    st.session_state["hours_per_day"]=float(ch.get("new_value"))
                if ch.get("key")=="scenario":
                    st.session_state["scenario"]=str(ch.get("new_value"))
            st.success("Uygulandı. Değişiklikler aktif.")
            # Tabloları korumak için rerun kullanmıyoruz

# ========= PART 2/3 — UI (UPDATED): tabs, inputs, pretty matrix, RAG, GPT Dev Console =========

# Helper function for setting defaults only once
def _set_once(k, v):
    if k not in st.session_state:
        st.session_state[k] = v

# Basit TR/RU birleştirici
def bi(tr_text: str, ru_text: str) -> str:
    try:
        return f"{tr_text} / {ru_text}"
    except Exception:
        return str(tr_text)

# Başlık ve metin için daha hoş TR+RU yerleşim yardımcıları
def bih(tr: str, ru: str, level: int = 3):
    lvl = max(1, min(level, 6))
    st.markdown(f"{'#'*lvl} {tr}")
    st.caption(ru)

def bitr(tr: str, ru: str):
    st.markdown(tr)
    st.caption(ru)

st.set_page_config(page_title="Betonarme İşçilik (RUB/m³) — Расчёт монолит", layout="wide")
inject_style()

# ---------- Modern Sidebar: API anahtarları (isteğe bağlı) ----------
with st.sidebar:
    st.markdown("""
    <div style="text-align: center; padding: 1rem; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border-radius: 15px; margin-bottom: 1rem;">
        <h3 style="margin: 0; color: white;">🔐 Entegrasyonlar</h3>
        <p style="margin: 0.5rem 0 0 0; opacity: 0.9; font-size: 0.9rem;">API anahtarları ile gelişmiş özellikler</p>
    </div>
    """, unsafe_allow_html=True)
    
    st.caption(bi("💡 Anahtar girmezsen GPT/RAG özellikleri çalışmaz.", "💡 Без ключей API функции GPT/RAG не работают."))

    # Versiyon kutusu (compact, styled)
    with st.container(border=True):
        cols = st.columns([0.6,0.4])
        with cols[0]:
            st.caption(bi("Sürüm","Версия"))
            st.markdown(f"<div style='display:inline-block;padding:2px 8px;border-radius:9999px;background:#eef2ff;border:1px solid #c7d2fe;color:#3730a3;font-weight:600;'>v{get_app_version(auto_bump=True)}</div>", unsafe_allow_html=True)
        with cols[1]:
            if st.button("Patch ↑", help=bi("Patch sürümünü artır","Увеличить patch-версию")):
                state = _load_version()
                state["version"] = _bump_patch(state.get("version","0.1.0"))
                state.setdefault("code_hash", _file_md5(__file__))
                _save_version(state)
                st.toast(bi("Sürüm güncellendi","Версия обновлена"))
    
    st.markdown(bi("**🤖 OpenAI API Key**", "**🤖 Ключ OpenAI API**"))
    st.session_state["OPENAI_API_KEY"] = st.text_input(
        "OpenAI API Key", 
        type="password",
        value=st.session_state.get("OPENAI_API_KEY",""),
        help="GPT önerileri için gerekli",
        placeholder="sk-..."
    )
    
    st.markdown(bi("**🌐 Tavily API Key**", "**🌐 Ключ Tavily API**"))
    st.session_state["TAVILY_API_KEY"] = st.text_input(
        "Tavily API Key (opsiyonel)", 
        type="password",
        value=st.session_state.get("TAVILY_API_KEY",""),
        help="Web arama ve doğrulama için",
        placeholder="tvly-..."
    )
    
    # Sidebar alt bilgi
    st.markdown("---")
    # Alt bilgi kaldırıldı (tekrarlı sürüm gösterimini sadeleştiriyoruz)

# Modern başlık
st.markdown("""
<div class="main-header">
    <h1>🏗️ Betonarme Hesaplama Modülü</h1>
    <p>Reinforced Concrete Labor Cost Calculator • Профессиональный калькулятор стоимости работ</p>
</div>
""", unsafe_allow_html=True)

st.markdown("""
<div style="text-align: center; margin-bottom: 2rem;">
    <p style="font-size: 1.2rem; color: #666; font-weight: 500;">
        🎯 Normalize Edilmiş Normlar  🌍 RUS/SNG/VKS Algorıtmaları  💰 Gayriresmî/Elden Vergisiz Kısım Dahil  📊 Sorumluluk Matrisi  🎓 RAG GPT Eğitim Sistemi  🧠 GPT Dev Console
    </p>
</div>
""", unsafe_allow_html=True)

# ---------- Modern Sekmeler ----------
tab_mantik, tab_sabitler, tab_genel, tab_eleman, tab_roller, tab_gider, tab_matris, tab_sonuclar, tab_asistan, tab_import = st.tabs([
    f"🧮 {bi('Mantık','Методология')}",
    f"⚙️ {bi('Sabitler','Константы')}",
    f"🚀 {bi('Genel','Общие')}", 
    f"🧩 {bi('Eleman & Metraj','Эл. и объёмы')}", 
    f"👥 {bi('Roller','Роли')}", 
    f"💰 {bi('Giderler','Затраты')}", 
    f"📋 {bi('Matris','Матрица')}", 
    f"📊 {bi('Sonuçlar','Результаты')}", 
    f"🤖 {bi('Asistan','Ассистент')}",
    f"📥 {bi('Import','Импорт')}"
])
with tab_mantik:
    bih("🧮 Hesap Mantığı ve Metodoloji","🧮 Методология расчёта", level=3)
    bitr("Bu bölüm, yazılımın neyi, nasıl ve hangi sırayla hesapladığını en sade haliyle açıklar.",
         "Этот раздел простыми словами объясняет, что и как считает программа.")

    # 1) Terminoloji
    bih("1) Terminoloji","1) Термины", level=4)
    st.markdown(
        """
        - **N**: Net maaş (çalışanın eline geçen, aylık)
        - **G**: Resmi brüt (aylık)
        - **r_NDFL**: Gelir vergisi oranı. 2025 için artan kademeli (rezident): 13/15/18/20/22
        - **OPS/OSS/OMS**: Emeklilik/Sosyal/Sağlık işveren prim oranları (yalnız resmi brüte)
        - **НСиПЗ**: İş kazası/meslek hastalığı primi (işveren)
        - **B_SNG**, **B_TUR**: Resmi brüt tavanları (SNG ve VKS için)
        - **P**: Patent aylık sabit bedeli (SNG)
        - **k_cash**: Elden kısım komisyon oranı
        - **E**: Elden (resmi tavanın üstü) kısım (varsa)
        - **extras**: Kişi başı sabit ekstralar (yemek, barınma vb., KDV uygun şekilde ayrıştırılır)
        """
    )

    # 2) Net→Brüt (artan NDFL)
    bih("2) Net → Brüt (Artan NDFL 2025)", "2) Нет → Брутто (прогрессивный НДФЛ 2025)", level=4)
    st.markdown(
        """
        - Aylık net, yıllıklaştırılır: 12 × N.
        - 2025 kademeleri (rezident): 2.4M/5M/20M/50M (₽) eşikleri, oranlar 13/15/18/20/22.
        - Yıllık netten yıllık brüte, her kademedeki net=brüt×(1−r) ilişkisiyle ters gidilerek ulaşılır; aylık brüt = yıllık brüt ÷ 12.
        - Bu mantık hem SNG (patent) hem VKS (TR) için uygulanır.
        """
    )

    # 3) SNG (patent) maliyeti
    bih("3) SNG (Patent) — İşveren Maliyeti","3) СНГ (патент) — затраты работодателя", level=4)
    st.markdown(
        """
        1) Netten brüte: G = ProgressiveInverse(12×N)/12.
        2) Resmi brüt ve elden:
           - G_official = min(G, B_SNG)
           - E = max(G − B_SNG, 0)
        3) Komisyon: C = E × k_cash
        4) İşveren primli resmi kısım: G_official × (1 + OPS + OSS + OMS + НСиПЗ)
        5) Toplam işveren maliyeti (SNG):
        """
    )
    st.latex(r"\text{Cost}_{SNG} = G_{off}\,(1+OPS+OSS+OMS+HS) + P + extras + E + C")
    st.markdown("Burada HS = НСиПЗ. Not: P (patent) NDFL'den mahsup edilmez — sabit gider olarak eklenir.")

    # 4) VKS (TR) maliyeti
    bih("4) VKS (TR) — İşveren Maliyeti","4) ВКС (Турция) — затраты работодателя", level=4)
    st.markdown(
        """
        1) Netten brüte: G = ProgressiveInverse(12×N)/12.
        2) Resmi brüt ve elden:
           - G_official = min(G, B_TUR)
           - E = max(G − B_TUR, 0)
        3) Komisyon: C = E × k_cash (VKS için kullanılmıyorsa 0)
        4) İşveren primleri: yalnız НСиПЗ
        """
    )
    st.latex(r"\text{Cost}_{VKS} = G_{off}\,(1+HS) + extras + E + C")

    # 5) Ülke karması (role bazında)
    bih("5) Ülke Karması (Rol bazında)","5) Смешение стран (по роли)", level=4)
    st.markdown(
        """
        Her rol satırı için ülke payları yüzdesel olarak verilir ve 1'e normalize edilir.
        """
    )
    st.latex(r"\text{Cost}_{per\,person} = p_{RUS}\,Cost_{RUS} + p_{SNG}\,Cost_{SNG} + p_{TUR}\,Cost_{VKS}")

    # 6) Normlar, senaryo ve zorluk
    bih("6) Normlar, Senaryo ve Zorluk","6) Нормы, сценарий и сложность", level=4)
    st.markdown(
        """
        - Temel norm (senaryo = Gerçekçi) eleman "Temel" için n_ref alınır.
        - Seçilen senaryonun "Temel" değeri ile oranlanarak senaryo çarpanı s hesaplanır.
        - Eleman göreli katsayıları k_e normalize edilerek ortalaması 1 yapılır.
        - Zorluk çarpanı z, girilen faktörlerden çarpımla oluşur: z = ∏(1+f_i).
        - Eleman normu: n_e = n_ref × s × k_e × z.
        """
    )
    st.latex(r"n_e = n_{ref} \times s \times k_e \times z")

    # 7) Çekirdek işçilik ve giderlerin eklenmesi
    bih("7) Çekirdek İşçilik ve Giderlerin Eklenmesi","7) Ядро и добавление затрат", level=4)
    st.markdown(
        """
        - Çekirdek işçilik (maliyet): seçili elemanların metrajı ve n_e kullanılarak toplanır.
        - Sarf (%), Genel Gider (%) ve Indirect (%) oranları sırasıyla uygulanır. Genel Gider için üst sınır (OVERHEAD_RATE_MAX) korunur.
        """
    )

    # 7.1) Metraj ve Adam-saat adım adım
    bih("7.1) Metraj ve Adam-saat","7.1) Объёмы и человеко-часы", level=5)
    st.markdown(
        """
        - Eleman e için metraj m_e (m³) ve norm n_e (a·s/m³) ise toplam adam-saat: A = Σ_e m_e × n_e.
        - Bir kişinin aylık çalışabileceği saat: H = gün/say × saat/gün. Uygulamada H = ortalama_iş_günü × hours_per_day.
        - Toplam kişi-ay: PM = A / H.
        - Kişi başı aylık maliyet (extras dahil) → (₽/saat) cinsinden fiyat = (M_with) / H.
        - Çekirdek m³ maliyeti: core_price = (M_with / H) × n_e.
        """
    )
    st.latex(r"A = \sum_e m_e\, n_e\quad ;\quad H = D_{avg}\,h_d\quad ;\quad PM = \dfrac{A}{H}")
    st.latex(r"\text{core\_price}_e = \left(\dfrac{M_{with}}{H}\right) \times n_e")

    # 7.2) Gider dağıtım formülleri
    bih("7.2) Gider Dağıtımı ve Toplamlar","7.2) Распределение затрат и итоги", level=5)
    st.markdown(
        """
        - Çekirdek + Genel: core_genel_e = core_price_e × (1 + genel_oran)
        - Sarf toplamı: S = (Σ_e core_genel_e × m_e) × consumables_oran
        - Indirect toplamı: I = (Σ_e core_genel_e × m_e + S) × indirect_oran
        - Eleman e'ye dağıtım ağırlığı: w_e = (core_genel_e × m_e) / Σ_e (core_genel_e × m_e)
        - Eleman e toplam (₽/m³): total_e = core_price_e + genel_e + sarf_e + indirect_e
        """
    )
    st.latex(r"\text{genel\_e} = \min(\text{overhead\_rate}, \text{max})\times \text{core\_price}_e")
    st.latex(r"S = \left(\sum_e (\text{core\_price}_e+\text{genel}_e) m_e\right) \times c_{sarf}")
    st.latex(r"I = \left(\sum_e (\text{core\_price}_e+\text{genel}_e) m_e + S\right) \times c_{indir}")
    st.latex(r"w_e = \dfrac{(\text{core\_price}_e+\text{genel}_e) m_e}{\sum_e (\text{core\_price}_e+\text{genel}_e) m_e}")
    st.latex(r"\text{total}_e = \text{core\_price}_e + \text{genel}_e + w_e\, \dfrac{S}{m_e} + w_e\, \dfrac{I}{m_e}")

    # 8) Mantık kontrolleri
    bih("8) Mantık Kontrolleri","8) Логические проверки", level=4)
    st.markdown(
        """
        - Artan NDFL tersine çevirme hem SNG hem VKS için aynı yöntemle yapılır.
        - SNG'de patent, vergiden mahsup edilmez; bilinçli basitleştirme. İleride istenirse anahtarla açılabilir.
        - VKS'de yalnız НСиПЗ uygulanır; SNG'de tüm sosyal primler resmi brüte uygulanır.
        - Ülke karması yüzdeleri her satırda normalize edilir (toplam 1 olur).
        - Genel gider üst sınırı uygulanır; UI'da da aynı sınır uyarılır.
        """
    )

    # 9) Basit anlatım (mühendis olmayanlar için)
    bih("9) Basit Anlatım (Mühendis Olmayanlar İçin)","9) Простое объяснение (для неинженеров)", level=4)
    st.markdown(
        """
        - Önce çalışanın eline geçen net maaşı (N) alıyoruz. Bunu yıllığa çeviriyoruz (12×N).
        - Devletin belirlediği vergi dilimlerine göre (2025 için 13/15/18/20/22%) bu yıllık neti geriye doğru **brüte** çeviriyoruz. Yani "brütün vergisi düşülünce net şu olsun" diye ters hesap yapıyoruz.
        - Aylık resmi brütü (G) bulunca işverenin ödeyeceği sigorta primlerini hesaplıyoruz.
            - SNG için: emeklilik (OPS), sosyal (OSS), sağlık (OMS) ve iş kazası (НСиПЗ) resmi brüt üzerinden.
            - VKS (Türk) için: sadece iş kazası (НСиПЗ).
        - SNG'de resmi brüt için bir **tavan** var. Brüt bu tavanı aşarsa, aşan kısım **elden** sayılır. Eldene vergi/prim eklemiyoruz; sadece nakit maliyeti ve varsa küçük bir komisyon (kasa/kur/çekim riskleri) ekleniyor.
        - SNG'de her çalışan için aylık **patent** sabit bedeli var. Bu bedeli ayrıca ekliyoruz (vergiden düşmüyoruz; sizin isteğinizle sade model).
        - Kişi başına bu maliyeti bulduktan sonra, seçili elemanlar için kaç **adam-saat** gerektiğini hesaplıyoruz (metraj × norm). Bir kişinin ayda kaç saat çalışabileceğini varsayarak toplam **kişi-ay** ihtiyacını buluyoruz.
        - Kişi saat maliyetini normlarla çarparak m³ başına çekirdek maliyeti buluyoruz. Sonra üzerine **genel gider**, **sarf** ve **indirect** oranlarını ekleyip toplam m³ maliyetini elde ediyoruz.
        """
    )

    # 10) Örnek hesap (basitleştirilmiş, rakamlar temsili)
    bih("10) Örnek Hesap (Basitleştirilmiş)","10) Пример расчёта (упрощённый)", level=4)
    st.markdown(
        """
        Varsayımlar:
        - Net maaş N = 100.000 ₽/ay (VKS).
        - НСиПЗ = %1,8, diğer sosyal primler = 0 (VKS olduğu için).
        - Günlük çalışma saati = 10; ayda ortalama iş günü ≈ 22 → H ≈ 220 saat/kişi·ay.
        - Eleman: Temel, norm n_e = 16 a·s/m³, metraj m = 100 m³.

        Adımlar:
        1) Net→Brüt (artan): yıllık net = 1.200.000; dillere göre ters çevirerek yaklaşık aylık brüt G ≈ 115.000 ₽.
        2) İşveren maliyeti (VKS): G × (1+НСиПЗ) ≈ 115.000 × 1.018 ≈ 117.070 ₽ (extras hariç).
        3) Kişi saat maliyeti ≈ 117.070 / 220 ≈ 532 ₽/saat.
        4) Çekirdek m³ maliyeti: core_price = 532 × 16 ≈ 8.512 ₽/m³.
        5) Genel gider %15 ise: 8.512 × 0,15 ≈ 1.277 ₽/m³.
        6) Sarf %5 ise: (8.512+1.277) × 0,05 ≈ 494 ₽/m³.
        7) Indirect %12 ise: (8.512+1.277+0.494) × 0,12 ≈ 1.275 ₽/m³.
        8) Toplam ≈ 8.512 + 1.277 + 0.494 + 1.275 ≈ 11.558 ₽/m³.

        Not: Bu örnek temsilidir; uygulamadaki değeri senaryolar, zorluk, rollerin ülke karması, patent ve ekstralar etkiler.
        """
    )

    # 11) İnfografik stil ve PDF indirme
    bih("11) Görsel Yardımlar ve Çıktı","11) Визуальные подсказки и выгрузка", level=4)
    st.markdown(
        """
        - Aşağıdaki kutucuklar, akışın hangi adımlardan geçtiğini özetler.
        - İstersen raporu PDF olarak indirebilirsin.
        """
    )

    st.markdown(
        """
        <div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px'>
          <div style='border:1px solid #e5e7eb;border-radius:12px;padding:12px;background:#fff'>
            <div class='badge'>1</div>
            <div><b>Net → Brüt</b></div>
            <div>Artan NDFL ile tersine çevirme</div>
          </div>
          <div style='border:1px solid #e5e7eb;border-radius:12px;padding:12px;background:#fff'>
            <div class='badge'>2</div>
            <div><b>İşveren Primleri</b></div>
            <div>OPS/OSS/OMS/НСиПЗ (profile göre)</div>
          </div>
          <div style='border:1px solid #e5e7eb;border-radius:12px;padding:12px;background:#fff'>
            <div class='badge'>3</div>
            <div><b>Adam-saat</b></div>
            <div>Metraj × norm → kişi-ay</div>
          </div>
          <div style='border:1px solid #e5e7eb;border-radius:12px;padding:12px;background:#fff'>
            <div class='badge'>4</div>
            <div><b>m³ Çekirdek</b></div>
            <div>Saat maliyeti × norm</div>
          </div>
          <div style='border:1px solid #e5e7eb;border-radius:12px;padding:12px;background:#fff'>
            <div class='badge'>5</div>
            <div><b>Giderler</b></div>
            <div>Genel + Sarf + Indirect</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Basit PDF (markdown → bytes) indirme — kısa açıklama export
    cpdf1, cpdf2 = st.columns([1,1])
    with cpdf1:
        if st.button("📄 Hesap Mantığını PDF indir (kısa)"):
            try:
                from io import BytesIO
                buf = BytesIO()
                content = "Hesap Mantığı — kısa özet\n\nNet→Brüt (artan NDFL), işveren primleri, adam-saat, m³ çekirdek ve gider dağıtımı adımları bu sürümde özetlenmiştir."
                buf.write(content.encode("utf-8"))
                st.download_button("İndir", data=buf.getvalue(), file_name="hesap_mantigi_ozet.txt", mime="text/plain")
            except Exception as e:
                st.warning(f"PDF yerine metin çıktı üretildi: {e}")

with tab_sabitler:
    # RUSYA GRUBU
    with st.expander("Rusya Vatandaşları (RU) / Граждане РФ", expanded=False):
        st.markdown('<div class="const-grid">', unsafe_allow_html=True)
        
        # NDFL RUS
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">💰 Gelir Vergisi (НДФЛ)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">Rusya gelir vergisi oranı</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{ratio_to_pct(eff("NDFL_RUS", NDFL_RUS)):.2f}%</div>', unsafe_allow_html=True)
        
        edit_ndfl_rus = st.toggle("Düzenle", key="edit_NDFL_RUS")
        if edit_ndfl_rus:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_NDFL_RUS", ratio_to_pct(eff("NDFL_RUS", NDFL_RUS)))
            st.number_input("%", min_value=0.0, max_value=100.0, step=0.1, value=cur_val, key="inp_NDFL_RUS")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_NDFL_RUS"):
                    OVR["NDFL_RUS"] = pct_to_ratio(st.session_state["inp_NDFL_RUS"])
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_NDFL_RUS"):
                    del st.session_state["inp_NDFL_RUS"]
                    st.session_state["edit_NDFL_RUS"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        # OPS RUS
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">🏛️ Emeklilik (ОПС)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">Emeklilik sigortası primi</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{ratio_to_pct(eff("OPS", OPS)):.2f}%</div>', unsafe_allow_html=True)
        
        edit_ops = st.toggle("Düzenle", key="edit_OPS")
        if edit_ops:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_OPS", ratio_to_pct(eff("OPS", OPS)))
            st.number_input("%", min_value=0.0, max_value=100.0, step=0.1, value=cur_val, key="inp_OPS")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_OPS"):
                    OVR["OPS"] = pct_to_ratio(st.session_state["inp_OPS"])
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_OPS"):
                    del st.session_state["inp_OPS"]
                    st.session_state["edit_OPS"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        # OSS RUS
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">🛡️ Sosyal Sigorta (ОСС)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">Sosyal sigorta primi</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{ratio_to_pct(eff("OSS", OSS)):.2f}%</div>', unsafe_allow_html=True)
        
        edit_oss = st.toggle("Düzenle", key="edit_OSS")
        if edit_oss:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_OSS", ratio_to_pct(eff("OSS", OSS)))
            st.number_input("%", min_value=0.0, max_value=100.0, step=0.1, value=cur_val, key="inp_OSS")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_OSS"):
                    OVR["OSS"] = pct_to_ratio(st.session_state["inp_OSS"])
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_OSS"):
                    del st.session_state["inp_OSS"]
                    st.session_state["edit_OSS"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        # OMS RUS
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">🏥 Sağlık (ОМС)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">Sağlık sigortası primi</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{ratio_to_pct(eff("OMS", OMS)):.2f}%</div>', unsafe_allow_html=True)
        
        edit_oms = st.toggle("Düzenle", key="edit_OMS")
        if edit_oms:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_OMS", ratio_to_pct(eff("OMS", OMS)))
            st.number_input("%", min_value=0.0, max_value=100.0, step=0.1, value=cur_val, key="inp_OMS")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_OMS"):
                    OVR["OMS"] = pct_to_ratio(st.session_state["inp_OMS"])
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_OMS"):
                    del st.session_state["inp_OMS"]
                    st.session_state["edit_OMS"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        # NSIPZ RUS
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">⚠️ İş Kazası (НСИПЗ)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">İş kazası risk primi</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{ratio_to_pct(eff("NSIPZ_RISK_RUS_SNG", NSIPZ_RISK_RUS_SNG)):.2f}%</div>', unsafe_allow_html=True)
        
        edit_nsipz_rus = st.toggle("Düzenle", key="edit_NSIPZ_RUS_SNG")
        if edit_nsipz_rus:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_NSIPZ_RUS_SNG", ratio_to_pct(eff("NSIPZ_RISK_RUS_SNG", NSIPZ_RISK_RUS_SNG)))
            st.number_input("%", min_value=0.0, max_value=100.0, step=0.1, value=cur_val, key="inp_NSIPZ_RUS_SNG")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_NSIPZ_RUS_SNG"):
                    OVR["NSIPZ_RISK_RUS_SNG"] = pct_to_ratio(st.session_state["inp_NSIPZ_RUS_SNG"])
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_NSIPZ_RUS_SNG"):
                    del st.session_state["inp_NSIPZ_RUS_SNG"]
                    st.session_state["edit_NSIPZ_RUS_SNG"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        st.markdown('</div>', unsafe_allow_html=True)
    
    # SNG GRUBU
    with st.expander("SNG Vatandaşları / Граждане СНГ", expanded=False):
        st.markdown('<div class="const-grid">', unsafe_allow_html=True)
        
        # NDFL SNG
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">💰 Gelir Vergisi (СНГ)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">SNG gelir vergisi oranı</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{ratio_to_pct(eff("NDFL_SNG", NDFL_SNG)):.2f}%</div>', unsafe_allow_html=True)
        
        edit_ndfl_sng = st.toggle("Düzenle", key="edit_NDFL_SNG")
        if edit_ndfl_sng:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_NDFL_SNG", ratio_to_pct(eff("NDFL_SNG", NDFL_SNG)))
            st.number_input("%", min_value=0.0, max_value=100.0, step=0.1, value=cur_val, key="inp_NDFL_SNG")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_NDFL_SNG"):
                    OVR["NDFL_SNG"] = pct_to_ratio(st.session_state["inp_NDFL_SNG"])
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_NDFL_SNG"):
                    del st.session_state["inp_NDFL_SNG"]
                    st.session_state["edit_NDFL_SNG"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        # Patent SNG
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">📋 Aylık Patent (Патент)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">Aylık patent ödemesi</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{eff("SNG_PATENT_MONTH", SNG_PATENT_MONTH):,.0f} ₽</div>', unsafe_allow_html=True)
        
        edit_patent_sng = st.toggle("Düzenle", key="edit_SNG_PATENT")
        if edit_patent_sng:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_SNG_PATENT", eff("SNG_PATENT_MONTH", SNG_PATENT_MONTH))
            st.number_input("₽/ay", min_value=0.0, step=100.0, value=cur_val, key="inp_SNG_PATENT")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_SNG_PATENT"):
                    OVR["SNG_PATENT_MONTH"] = st.session_state["inp_SNG_PATENT"]
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_SNG_PATENT"):
                    del st.session_state["inp_SNG_PATENT"]
                    st.session_state["edit_SNG_PATENT"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        # Taxed Base SNG
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">🏛️ Resmi Brüt Tavan (Официальная минимальная заработная плата)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">Resmi brüt maaş tavanı</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{eff("SNG_TAXED_BASE", SNG_TAXED_BASE):,.0f} ₽</div>', unsafe_allow_html=True)
        
        edit_base_sng = st.toggle("Düzenle", key="edit_SNG_BASE")
        if edit_base_sng:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_SNG_BASE", eff("SNG_TAXED_BASE", SNG_TAXED_BASE))
            st.number_input("₽/ay", min_value=0.0, step=1000.0, value=cur_val, key="inp_SNG_BASE")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_SNG_BASE"):
                    OVR["SNG_TAXED_BASE"] = st.session_state["inp_SNG_BASE"]
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_SNG_BASE"):
                    del st.session_state["inp_SNG_BASE"]
                    st.session_state["edit_SNG_BASE"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        # Cash Commission (bilgi kartı)
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">💳 Elden Ödeme Komisyonu (Комиссия)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">Elden ödeme komisyon oranı</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{ratio_to_pct(eff("CASH_COMMISSION_RATE", CASH_COMMISSION_RATE)):.2f}%</div>', unsafe_allow_html=True)
        
        edit_cash_commission = st.toggle("Düzenle", key="edit_CASH_COMMISSION")
        if edit_cash_commission:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_CASH_COMMISSION", ratio_to_pct(eff("CASH_COMMISSION_RATE", CASH_COMMISSION_RATE)))
            st.number_input("%", min_value=0.0, max_value=100.0, step=0.1, value=cur_val, key="inp_CASH_COMMISSION")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_CASH_COMMISSION"):
                    OVR["CASH_COMMISSION_RATE"] = pct_to_ratio(st.session_state["inp_CASH_COMMISSION"])
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_CASH_COMMISSION"):
                    del st.session_state["inp_CASH_COMMISSION"]
                    st.session_state["edit_CASH_COMMISSION"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        st.markdown('</div>', unsafe_allow_html=True)
    
    # TÜRK GRUBU
    with st.expander("Türk Vatandaşları (VKS) / Граждане Турции (ВКС)", expanded=False):
        st.markdown('<div class="const-grid">', unsafe_allow_html=True)
        
        # NDFL TUR
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">💰 Gelir Vergisi (Турция ВКС)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">Türkiye gelir vergisi oranı</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{ratio_to_pct(eff("NDFL_TUR", NDFL_TUR)):.2f}%</div>', unsafe_allow_html=True)
        # Progressive modda açıklama: bu alan sadece sabit oran modunda kullanılır
        if bool(st.session_state.get("use_progressive_ndfl", True)):
            st.caption(bi("(Artan NDFL aktif — bu oran yalnız 'sabit oran' modu kapalıyken kullanılır)",
                        "(Включён прогрессивный НДФЛ — эта ставка используется только при выключенном режиме фиксированной ставки)"))
        
        edit_ndfl_tur = st.toggle("Düzenle", key="edit_NDFL_TUR")
        if edit_ndfl_tur:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_NDFL_TUR", ratio_to_pct(eff("NDFL_TUR", NDFL_TUR)))
            st.number_input("%", min_value=0.0, max_value=100.0, step=0.1, value=cur_val, key="inp_NDFL_TUR")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_NDFL_TUR"):
                    OVR["NDFL_TUR"] = pct_to_ratio(st.session_state["inp_NDFL_TUR"])
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_NDFL_TUR"):
                    del st.session_state["inp_NDFL_TUR"]
                    st.session_state["edit_NDFL_TUR"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        # NSIPZ TUR
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">⚠️ İş Kazası (НСИПЗ)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">İş kazası risk primi</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{ratio_to_pct(eff("NSIPZ_RISK_TUR_VKS", NSIPZ_RISK_TUR_VKS)):.2f}%</div>', unsafe_allow_html=True)
        
        edit_nsipz_tur = st.toggle("Düzenle", key="edit_NSIPZ_TUR_VKS")
        if edit_nsipz_tur:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_NSIPZ_TUR_VKS", ratio_to_pct(eff("NSIPZ_RISK_TUR_VKS", NSIPZ_RISK_TUR_VKS)))
            st.number_input("%", min_value=0.0, max_value=100.0, step=0.1, value=cur_val, key="inp_NSIPZ_TUR_VKS")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_NSIPZ_TUR_VKS"):
                    OVR["NSIPZ_RISK_TUR_VKS"] = pct_to_ratio(st.session_state["inp_NSIPZ_TUR_VKS"])
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_NSIPZ_TUR_VKS"):
                    del st.session_state["inp_NSIPZ_TUR_VKS"]
                    st.session_state["edit_NSIPZ_TUR_VKS"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        # Taxed Base TUR
        st.markdown('<div class="const-card">', unsafe_allow_html=True)
        st.markdown('<div class="card-title">🏛️ Resmi Brüt Tavan (Официальная минимальная заработная плата)</div>', unsafe_allow_html=True)
        st.markdown('<div class="card-desc">Resmi brüt maaş tavanı</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{eff("TUR_TAXED_BASE", TUR_TAXED_BASE):,.0f} ₽</div>', unsafe_allow_html=True)
        
        edit_base_tur = st.toggle("Düzenle", key="edit_TUR_BASE")
        if edit_base_tur:
            st.markdown('<div class="edit-area">', unsafe_allow_html=True)
            cur_val = st.session_state.get("inp_TUR_BASE", eff("TUR_TAXED_BASE", TUR_TAXED_BASE))
            st.number_input("₽/ay", min_value=0.0, step=1000.0, value=cur_val, key="inp_TUR_BASE")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Kaydet", key="save_TUR_BASE"):
                    OVR["TUR_TAXED_BASE"] = st.session_state["inp_TUR_BASE"]
                    st.rerun()
            with col2:
                if st.button("Vazgeç", key="cancel_TUR_BASE"):
                    del st.session_state["inp_TUR_BASE"]
                    st.session_state["edit_TUR_BASE"] = False
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        st.markdown('</div>', unsafe_allow_html=True)
# ==================== 1B) ADAM-SAAT NORMLARI (ayrı başlık) ====================
with tab_genel:
    with st.expander("👷‍♂️ Adam-saat Normları (Senaryolar) / 👷‍♂️ Нормы трудозатрат (сценарии)", expanded=False):
        st.caption(bi("Senaryolara göre eleman bazında a·s/m³ normlarını düzenleyin. Boş bırakılanlar varsayılanı kullanır.",
                      "Редактируйте нормы a·ч/м³ по элементам для каждого сценария. Пустые — по умолчанию."))
        norms_map = get_effective_scenario_norms()
        scenarios = ["İdeal","Gerçekçi","Kötü"]
        elements_tr = ["Grobeton","Rostverk","Temel","Döşeme","Perde","Merdiven"]
        import pandas as _pd
        rows = []
        for sc in scenarios:
            base = norms_map.get(sc, SCENARIO_NORMS["Gerçekçi"]) if isinstance(norms_map.get(sc), dict) else SCENARIO_NORMS.get(sc, {})
            row = {"Senaryo": sc}
            for et in elements_tr:
                try:
                    row[et] = float(base.get(et, SCENARIO_NORMS["Gerçekçi"][et]))
                except Exception:
                    row[et] = SCENARIO_NORMS["Gerçekçi"].get(et, 16.0)
            rows.append(row)
        df0 = _pd.DataFrame(rows)
        edited = st.data_editor(df0, hide_index=True, num_rows="fixed")
        col1, col2 = st.columns(2)
        with col1:
            if st.button(bi("Kaydet (Normları Override Et)","Сохранить (Override норм)")):
                new_map = {}
                for _, r in edited.iterrows():
                    sc = str(r["Senaryo"]) if r.get("Senaryo") in scenarios else None
                    if not sc: continue
                    new_map[sc] = {et: float(r.get(et, SCENARIO_NORMS[sc][et])) for et in elements_tr}
                st.session_state["SCENARIO_NORMS_OVR"] = new_map
                st.success(bi("Adam-saat normları güncellendi.","Нормы трудозатрат обновлены."))
        with col2:
            if st.button(bi("Override'ı Temizle","Сброс Override")):
                st.session_state.pop("SCENARIO_NORMS_OVR", None)
                st.info(bi("Override temizlendi. Varsayılan normlar kullanılacak.","Сброшено. Будут использоваться нормы по умолчанию."))

    ### ✅ Çevresel/Zorluk Faktörleri — norm çarpanı
    def render_difficulty_block():
        # --- Şema meta (ikon + etiket) ---
        DIFF_META = {
            "winter":       ("❄️", "Kış koşulları"),
            "low_formwork": ("🌿", "Düşük kalıp tekrarı"),
            "congestion":   ("🏗️", "Saha sıkışıklığı"),
            "heavy_rebar":  ("🧱", "Ağır donatı yoğunluğu"),
            "pump_shared":  ("🚧", "Vinç/pompa paylaşımı"),
            "pump_height":  ("📈", "Yüksek pompa kotu/mesafesi"),
        }

        # İlk kurulum veya eski veriyi normalize et
        if "diff" not in st.session_state or not isinstance(st.session_state["diff"], dict):
            st.session_state["diff"] = {k: {"on": False, "pct": 0.0} for k in DIFF_META.keys()}

        # Eski yapıda icon/label yoksa ekle; on/pct tiplerini düzelt
        for k, (icon, label) in DIFF_META.items():
            rec = st.session_state["diff"].get(k, {})
            st.session_state["diff"][k] = {
                "on":    bool(rec.get("on", False)),
                "pct":   float(rec.get("pct", 0.0)),
                "icon":  icon,
                "label": label,
            }

        # Items listesini oluştur
        diff_def = st.session_state.get("diff", {})
        items = []
        for k, meta in diff_def.items():
            meta = meta or {}
            items.append({
                "key": k,
                "label": str(meta.get("label", k)),
                "icon": str(meta.get("icon", "🧩")),
                "step": float(meta.get("step", 0.5)),
                "min": float(meta.get("min", 0.0)),
                "max": float(meta.get("max", 30.0)),
                "default": float(meta.get("default", 0.0)),
            })
        # Label'a göre sırala
        items.sort(key=lambda it: it["label"].lower())

        # Reset işlemi
        if st.session_state.pop("diff__do_reset", False):
            for it in items:
                st.session_state.pop(f"diff_on_{it['key']}", None)
                st.session_state.pop(f"diff_pct_{it['key']}", None)
            # Önbellekleri temizle
            st.session_state.pop("difficulty_multiplier_cache", None)
            st.rerun()

        # Widget key'lerini oluştur
        for k, rec in st.session_state["diff"].items():
            st.session_state.setdefault(f"diff_on_{k}",  bool(rec["on"]))
            st.session_state.setdefault(f"diff_pct_{k}", float(rec["pct"]))

        # ---------- Başlık & yardımcı düğmeler ----------

        def _clear_diff_cache():
            # Türetilmiş/cached anahtarları temizle
            for key in list(st.session_state.keys()):
                if key.startswith(("difficulty_", "diff_total_", "diff_cache", "_diff_total_mult_cache")):
                    st.session_state.pop(key, None)



        # ---------- Faktör kartları ----------
        st.session_state.setdefault("difficulty_multiplier", 1.0)
        total_mult = float(st.session_state["difficulty_multiplier"])
        st.markdown(
            ("<div class='muted'>Aktif kalemler çarpılır ➜ "
             f"<b>Toplam ×{total_mult:,.3f}</b></div>").replace(",", " "),
            unsafe_allow_html=True
        )
        # UI state anahtarlarını garanti et
        for it in items:
            on_key  = f"diff_on_{it['key']}"
            pct_key = f"diff_pct_{it['key']}"
            if on_key not in st.session_state:
                st.session_state[on_key] = False
            if pct_key not in st.session_state:
                st.session_state[pct_key] = it["default"]

        # 3 sütunlu ızgarada kartlar - EXPANDER İÇİNDE
        with st.expander("⚙️ Çevresel/Zorluk Faktörleri — detaylar", expanded=False):
            grid_cols = 3
            for i, it in enumerate(items):
                if i % grid_cols == 0:
                    cols = st.columns(grid_cols, gap="medium")
                with cols[i % grid_cols]:
                    st.markdown(
                        f"<div class='rowcard'><div class='diff-title'>{it['icon']} {it['label']}</div>",
                        unsafe_allow_html=True
                    )
                    on_key  = f"diff_on_{it['key']}"
                    pct_key = f"diff_pct_{it['key']}"
                    st.checkbox("Aktif", key=on_key)
                    # Yüzde girişi
                    st.number_input(
                        "Etki %",
                        min_value=it["min"],
                        max_value=it["max"],
                        step=it["step"],
                        format="%.2f",
                        key=pct_key,
                        disabled=not st.session_state[on_key],
                    )
                    # Bu kalemin çarpanı
                    local_mult = 1.0 + (st.session_state[pct_key] / 100.0 if st.session_state[on_key] else 0.0)
                    st.caption(f"Bu kalem ×{local_mult:,.3f}".replace(",", " "))
                    st.markdown("</div>", unsafe_allow_html=True)

            # Sıfırlama butonu
            cA, cB = st.columns([1,1])
            with cA:
                if st.button("🧼 DEĞERLERİ SIFIRLA", key="diff_reset_bottom"):
                    st.session_state["diff__do_reset"] = True
                    st.rerun()
            with cB:
                # Toplam çarpanı hesapla
                total_mult = 1.0
                for it in items:
                    if st.session_state.get(f"diff_on_{it['key']}", False):
                        total_mult *= (1.0 + (float(st.session_state.get(f"diff_pct_{it['key']}", 0.0)) / 100.0))
                st.markdown(
                    f"<div style='text-align:right'><span class='diff-total'>Toplam ×{total_mult:,.3f}</span></div>",
                    unsafe_allow_html=True
                )

        # Toplam çarpanı hesapla
        total_mult = 1.0
        for it in items:
            if st.session_state.get(f"diff_on_{it['key']}", False):
                total_mult *= (1.0 + (float(st.session_state.get(f"diff_pct_{it['key']}", 0.0)) / 100.0))
        st.session_state["difficulty_multiplier"] = float(total_mult)

        # UI'dan diff sözlüğüne aktar
        for it in items:
            on_key = f"diff_on_{it['key']}"
            pct_key = f"diff_pct_{it['key']}"
            st.session_state["diff"][it['key']]["on"] = st.session_state.get(on_key, False)
            st.session_state["diff"][it['key']]["pct"] = st.session_state.get(pct_key, 0.0)
        
        # PART 3 için düz anahtarlar
        d = st.session_state["diff"]
        st.session_state["f_winter"] =  (d["winter"]["pct"]/100.0)       if d["winter"]["on"]       else 0.0
        st.session_state["f_repeat"] =  (d["low_formwork"]["pct"]/100.0) if d["low_formwork"]["on"] else 0.0
        st.session_state["f_cong"]   =  (d["congestion"]["pct"]/100.0)   if d["congestion"]["on"]   else 0.0
        st.session_state["f_heavy"]  =  (d["heavy_rebar"]["pct"]/100.0)  if d["heavy_rebar"]["on"]  else 0.0
        st.session_state["f_shared"] =  (d["pump_shared"]["pct"]/100.0)  if d["pump_shared"]["on"]  else 0.0
        st.session_state["f_pump"]   =  (d["pump_height"]["pct"]/100.0)  if d["pump_height"]["on"]  else 0.0
        

        _update_diff_cache()

    render_difficulty_block()
    _update_diff_cache()
    # END: Çevresel/Zorluk Faktörleri

# ==================== 2) ELEMAN & METRAJ ====================
with tab_eleman:
    bih("🧩 Betonarme Elemanları","🧩 Элементы монолитных работ", level=3)
    cols = st.columns(3)
    sel_flags={}
    for i,k in enumerate(CANON_KEYS):
        with cols[i%3]:
            sel_flags[k]=st.checkbox(LABELS[k], value=st.session_state.get(f"sel_{k}", True), key=f"sel_{k}")
    selected_elements=[k for k,v in sel_flags.items() if v]
    if not selected_elements:
        st.warning(bi("En az bir betonarme eleman seçin.", "Выберите хотя бы один элемент."))

    bih("📏 Metraj","📏 Объёмы", level=3)
    use_metraj = st.checkbox(bi("Eleman metrajlarım mevcut, girmek istiyorum",
                                 "У меня есть объёмы по элементам, хочу ввести"),
                             value=st.session_state.get("use_metraj", False), key="use_metraj")
    if use_metraj and selected_elements:
        # Metraj tablosu için state kontrolü
        current_keys = tuple(selected_elements)
        if ("metraj_df" not in st.session_state or 
            st.session_state.get("_met_for_keys") != current_keys or
            st.session_state["metraj_df"].empty):
            st.session_state["metraj_df"] = get_default_metraj_df(selected_elements)
            st.session_state["_met_for_keys"] = current_keys
        
        # Metraj tablosunu düzenle
        with st.form(key="metraj_form", clear_on_submit=False):
            edited_metraj = st.data_editor(
                st.session_state["metraj_df"], 
                num_rows="dynamic", 
                hide_index=True, 
                key="metraj_editor_form"
            )
            if st.form_submit_button(bi("💾 Metraj Kaydet","💾 Сохранить объёмы")):
                st.session_state["metraj_df"] = edited_metraj
                st.success(bi("Metraj kaydedildi!","Объёмы сохранены!"))
            else:
                # Mevcut değerleri kullan
                st.session_state["metraj_df"] = edited_metraj

# ==================== 3) ROLLER ====================
with tab_roller:
    bih("🛠️ Rol Kompozisyonu (1 m³ için)","🛠️ Состав ролей (на 1 м³)", level=3)
    # Roller tablosu için state kontrolü
    if "roles_df" not in st.session_state:
        st.session_state["roles_df"] = get_default_roles_df()
    pc = st.column_config.NumberColumn(format="%.0f", min_value=0, max_value=100, step=1)
    col_cfg = {
        "Ağırlık (Вес)": st.column_config.NumberColumn(format="%.3f", min_value=0.0, step=0.01),
        "%RUS": pc, "%SNG": pc, "%TUR": pc,
        "Net Maaş (₽, na ruki) (Чистая з/п, ₽)": st.column_config.NumberColumn(format="%.0f", min_value=0),
    }
    # Roller tablosunu düzenle
    with st.form(key="roles_form", clear_on_submit=False):
        edited_roles = st.data_editor(
            st.session_state["roles_df"],
            key="roles_editor_form",
            num_rows="dynamic",
            hide_index=True,
            column_config=col_cfg,
        )
        if st.form_submit_button(bi("💾 Roller Kaydet","💾 Сохранить роли")):
            st.session_state["roles_df"] = edited_roles
            st.success(bi("Roller kaydedildi!","Роли сохранены!"))
        else:
            # Mevcut değerleri kullan
            st.session_state["roles_df"] = edited_roles

    # Varsayılanlara döndür
    if st.button(bi("↩️ Rolleri varsayılana döndür","↩️ Сбросить роли к значениям по умолчанию")):
        st.session_state["roles_df"]=pd.DataFrame([
            {"Rol (Роль)":"brigadir","Ağırlık (Вес)":0.10,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":120000,"%RUS":100,"%SNG":0,"%TUR":0},
            {"Rol (Роль)":"kalfa","Ağırlık (Вес)":0.20,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":110000,"%RUS":20,"%SNG":60,"%TUR":20},
            {"Rol (Роль)":"usta_demirci","Ağırlık (Вес)":0.60,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":100000,"%RUS":10,"%SNG":70,"%TUR":20},
            {"Rol (Роль)":"usta_kalipci","Ağırlık (Вес)":0.60,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":100000,"%RUS":10,"%SNG":70,"%TUR":20},
            {"Rol (Роль)":"betoncu","Ağırlık (Вес)":1.00,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":90000,"%RUS":10,"%SNG":70,"%TUR":20},
            {"Rol (Роль)":"duz_isci","Ağırlık (Вес)":0.50,"Net Maaş (₽, na ruki) (Чистая з/п, ₽)":80000,"%RUS":10,"%SNG":70,"%TUR":20},
        ])
        st.success("Varsayılan roller yüklendi.")


# ==================== 4) GİDERLER (sade) ====================
with tab_gider:
    bih("👥 Global Kişi Başı (Aylık) Giderler","👥 Глобальные затраты на человека (в месяц)", level=3)
    c1,c2,c3 = st.columns(3)
    with c1:
        # Yemek
        st.session_state["food"] = st.number_input(bi("🍲 Yemek (₽/ay)","🍲 Питание (₽/мес)"), 0.0, value=10000.0, step=10.0, key="food_inp")
        st.session_state["food_vat"] = st.checkbox(bi("Yemek KDV dahil mi?","Питание с НДС?"), value=True, key="food_vat_inp")
        
        # PPE
        st.session_state["ppe"] = st.number_input(bi("🦺 PPE/СИЗ (₽/ay)","🦺 СИЗ (₽/мес)"), 0.0, value=1500.0, step=5.0, key="ppe_inp")
        st.session_state["ppe_vat"] = st.checkbox(bi("PPE KDV dahil mi?","СИЗ с НДС?"), value=True, key="ppe_vat_inp")
    with c2:
        # Barınma
        st.session_state["lodging"] = st.number_input(bi("🏠 Barınma (₽/ay)","🏠 Проживание (₽/мес)"), 0.0, value=12000.0, step=10.0, key="lodging_inp")
        st.session_state["lodging_vat"] = st.checkbox(bi("Barınma KDV dahil mi?","Проживание с НДС?"), value=True, key="lodging_vat_inp")
        
        # Eğitim
        st.session_state["training"] = st.number_input(bi("🎓 Eğitim (₽/ay)","🎓 Обучение (₽/мес)"), 0.0, value=500.0, step=5.0, key="training_inp")
        st.session_state["training_vat"] = st.checkbox(bi("Eğitim KDV dahil mi?","Обучение с НДС?"), value=True, key="training_vat_inp")
    with c3:
        # Ulaşım
        st.session_state["transport"] = st.number_input(bi("🚇 Ulaşım (₽/ay)","🚇 Транспорт (₽/мес)"), 0.0, value=3000.0, step=5.0, key="transport_inp")
        st.session_state["transport_vat"] = st.checkbox(bi("Ulaşım KDV dahil mi?","Транспорт с НДС?"), value=False, key="transport_vat_inp")
        
        # KDV oranı
        st.session_state["vat_rate"] = st.number_input(bi("KDV oranı (НДС)","Ставка НДС"), min_value=0.0, max_value=0.25, value=0.20, step=0.001, key="vat_rate_inp",
                                                       help="'KDV dahil' işaretli kalemlerde KDV ayrıştırılır. / Если отмечено 'с НДС', НДС выделяется из суммы.")

    # Sarf Grupları
    with st.expander("🧴 Sarf Grupları — % (seç-belirle) / 🧴 Группы расходников — % (выбрать-настроить)", expanded=False):
        if "cons_groups_state" not in st.session_state:
            st.session_state["cons_groups_state"] = {name: {"on": False, "pct": float(p)} for (name,p) in CONSUMABLES_PRESET}
        cons_state = st.session_state["cons_groups_state"]
        cons_sum = 0.0
        for name, _d in CONSUMABLES_PRESET:
            c1, c2 = st.columns([0.60, 0.40])
            with c1: st.write(name)
            with c2:
                on = st.checkbox(bi("Aktif","Активно"), value=cons_state[name]["on"], key=f"cg_on_{name}")
                pct = st.number_input(bi("Etki %","Доля, %"), min_value=0.0, max_value=100.0,
                                      value=float(cons_state[name]["pct"]), step=0.25, format="%.2f",
                                      key=f"cg_pct_{name}")
                cons_state[name] = {"on": bool(on), "pct": float(pct)}
                if on: cons_sum += float(pct)
        st.session_state["_cgroups_total_pct"] = float(cons_sum)
        st.markdown(f"<div class='badge'>{bi('Seçili toplam:','Выбрано всего:')} <b>{cons_sum:.2f}%</b></div>", unsafe_allow_html=True)

        # Özel kalemler
        st.markdown(bi("**➕ Özel sarf kalemleri**","**➕ Пользовательские расходники**"))
        if "cons_custom_df" not in st.session_state:
            st.session_state["cons_custom_df"] = pd.DataFrame([{"Kalem (Статья)":"", "Oran (%) (Доля, %)":0.0, "Dahil? (Включить?)":False}])
        
        # Özel sarf kalemleri tablosunu düzenle
        with st.form(key="consumables_form", clear_on_submit=False):
            edited_cons_custom = st.data_editor(
                st.session_state["cons_custom_df"],
                num_rows="dynamic", hide_index=True, key="custom_consumables_editor_form"
            )
            if st.form_submit_button(bi("💾 Sarf Kaydet","💾 Сохранить расходники")):
                st.session_state["cons_custom_df"] = edited_cons_custom
                st.success(bi("Sarf kalemleri kaydedildi!","Группа расходников сохранена!"))
            else:
                # Mevcut değerleri kullan
                st.session_state["cons_custom_df"] = edited_cons_custom

    # Genel Gider Grupları
    with st.expander("🧮 Genel Gider Grupları — % (seç-belirle) / 🧮 Группы общих расходов — % (выбрать-настроить)", expanded=False):
        if "ovh_groups_state" not in st.session_state:
            st.session_state["ovh_groups_state"] = {name: {"on": False, "pct": float(p)} for (name,p) in OVERHEAD_GROUPS_PRESET}
        ovh_state = st.session_state["ovh_groups_state"]
        ovh_sum = 0.0
        for name, _d in OVERHEAD_GROUPS_PRESET:
            c1, c2 = st.columns([0.60, 0.40])
            with c1: st.write(name)
            with c2:
                on = st.checkbox(bi("Aktif","Активно"), value=ovh_state[name]["on"], key=f"og_on_{name}")
                pct = st.number_input(bi("Etki %","Доля, %"), min_value=0.0, max_value=100.0,
                                      value=float(ovh_state[name]["pct"]), step=0.25, format="%.2f",
                                      key=f"og_pct_{name}")
                ovh_state[name] = {"on": bool(on), "pct": float(pct)}
                if on: ovh_sum += float(pct)
        if ovh_sum/100.0 > OVERHEAD_RATE_MAX:
            st.warning(bi(f"Genel gider toplamı {ovh_sum:.2f}% > izinli {OVERHEAD_RATE_MAX*100:.0f}% — hesapta {OVERHEAD_RATE_MAX*100:.0f}% ile sınırlandırılır.",
                          f"Сумма общих расходов {ovh_sum:.2f}% > лимита {OVERHEAD_RATE_MAX*100:.0f}% — в расчёте ограничим {OVERHEAD_RATE_MAX*100:.0f}%."))
        st.session_state["_ogroups_total_pct"] = float(ovh_sum)
        st.markdown(f"<div class='badge'>{bi('Seçili toplam:','Выбрано всего:')} <b>{ovh_sum:.2f}%</b></div>", unsafe_allow_html=True)

        # Özel kalemler
        st.markdown(bi("**➕ Özel genel gider kalemleri**","**➕ Пользовательские общие расходы**"))
        if "ovh_custom_df" not in st.session_state:
            st.session_state["ovh_custom_df"] = pd.DataFrame([{"Kalem (Статья)":"", "Oran (%) (Доля, %)":0.0, "Dahil? (Включить?)":False}])
        
        # Özel genel gider kalemleri tablosunu düzenle
        with st.form(key="overhead_form", clear_on_submit=False):
            edited_ovh_custom = st.data_editor(
                st.session_state["ovh_custom_df"],
                num_rows="dynamic", hide_index=True, key="custom_overhead_editor_form"
            )
            if st.form_submit_button(bi("💾 Genel Gider Kaydet","💾 Сохранить общие расходы")):
                st.session_state["ovh_custom_df"] = edited_ovh_custom
                st.success(bi("Genel gider kalemleri kaydedildi!","Группа общих расходов сохранена!"))
            else:
                # Mevcut değerleri kullan
                st.session_state["ovh_custom_df"] = edited_ovh_custom

    # Indirect (Diğer) Grupları
    with st.expander("📦 Indirect (Diğer) Grupları — % (seç-belirle) / 📦 Косвенные (прочие) — % (выбрать-настроить)", expanded=False):
        st.info(bi("ℹ️ Not: Indirect grupları varsayılan olarak pasif durumda. İhtiyaç duyduğunuz kalemleri aktif hale getirin.",
                   "ℹ️ Примечание: по умолчанию косвенные группы выключены. Активируйте нужные."))
        st.caption("📋 **Varsayılan değerler:** Şantiye Genel İdare (%7), Ekipman/Amortisman (%5), Lojistik/Sevkiyat (%3), Güvenlik & İSG (%2), Ofis/GSM/İzin-Belge (%1.5)")
        
        if "indirect_groups_state" not in st.session_state:
            st.session_state["indirect_groups_state"] = {name: {"on": False, "pct": float(p)} for (name,p) in INDIRECT_PRESET_DEFAULTS.items()}
        
        ind_state = st.session_state["indirect_groups_state"]
        ind_sum = 0.0
        
        # Indirect gruplarını göster
        for name, _d in INDIRECT_PRESET_DEFAULTS.items():
            c1, c2 = st.columns([0.65, 0.35])
            with c1: 
                st.write(name)
                # Pasif kalemler için gri renk
                if not ind_state[name]["on"]:
                    st.caption(bi("⚪ Pasif","⚪ Неактивно"))
            with c2:
                on = st.checkbox(bi("Aktif","Активно"), value=ind_state[name]["on"], key=f"ig_on_{name}")
                pct = st.number_input(bi("Etki %","Доля, %"), min_value=0.0, max_value=100.0,
                                      value=float(ind_state[name]["pct"]), step=0.25, format="%.2f",
                                      key=f"ig_pct_{name}")
                ind_state[name] = {"on": bool(on), "pct": float(pct)}
                if on: ind_sum += float(pct)
        
        st.session_state["_igroups_total_pct"] = float(ind_sum)
        
        # Toplam gösterimi
        if ind_sum > 0:
            st.success(bi(f"✅ Seçili Indirect Toplam: {ind_sum:.2f}%", f"✅ Сумма выбранных косвенных: {ind_sum:.2f}%"))
        else:
            st.warning(bi("⚠️ Indirect: Hiçbir kalem seçili değil - Varsayılan olarak tüm kalemler pasif",
                          "⚠️ Косвенные: ничего не выбрано — по умолчанию все выключены"))

        # Özel kalemler
        st.markdown(bi("**➕ Özel indirect kalemleri**","**➕ Пользовательские косвенные**"))
        st.caption(bi("Özel kalemler de varsayılan olarak pasif durumda. İhtiyaç duyduğunuz kalemleri ekleyip aktif hale getirin.",
                      "Пользовательские строки тоже по умолчанию неактивны. Добавьте и включите нужные."))
        st.caption(bi("💡 İpucu: Yeni kalem eklemek için 'Dahil?' sütunundaki kutuyu işaretleyin.",
                      "💡 Подсказка: чтобы добавить строку, отметьте чекбокс 'Включить?'."))
        if "ind_custom_df" not in st.session_state:
            st.session_state["ind_custom_df"] = pd.DataFrame([{"Kalem (Статья)":"", "Oran (%) (Доля, %)":0.0, "Dahil? (Включить?)":False}])
        
        # Özel indirect kalemleri tablosunu düzenle
        with st.form(key="indirect_form", clear_on_submit=False):
            edited_ind_custom = st.data_editor(
                st.session_state["ind_custom_df"],
                num_rows="dynamic", hide_index=True, key="custom_indirect_editor_form"
            )
            if st.form_submit_button(bi("💾 Indirect Kaydet","💾 Сохранить косвенные")):
                st.session_state["ind_custom_df"] = edited_ind_custom
                st.success(bi("Indirect kalemleri kaydedildi!","Косвенные сохранены!"))
            else:
                # Mevcut değerleri kullan
                st.session_state["ind_custom_df"] = edited_ind_custom
    
    # Grup toplamlarını hesapla
    # Sarf toplamı
    cons_total = st.session_state.get("_cgroups_total_pct", 0.0)
    cons_custom_df = st.session_state.get("cons_custom_df", pd.DataFrame())
    if isinstance(cons_custom_df, pd.DataFrame) and not cons_custom_df.empty:
        for _, rr in cons_custom_df.iterrows():
            if bool(rr.get("Dahil? (Включить?)", False)):
                cons_total += float(rr.get("Oran (%) (Доля, %)", 0.0))
    st.session_state["consumables_rate"] = cons_total / 100.0
    
    # Genel gider toplamı
    ovh_total = st.session_state.get("_ogroups_total_pct", 0.0)
    ovh_custom_df = st.session_state.get("ovh_custom_df", pd.DataFrame())
    if isinstance(ovh_custom_df, pd.DataFrame) and not ovh_custom_df.empty:
        for _, rr in ovh_custom_df.iterrows():
            if bool(rr.get("Dahil? (Включить?)", False)):
                ovh_total += float(rr.get("Oran (%) (Доля, %)", 0.0))
    st.session_state["overhead_rate"] = min(ovh_total / 100.0, OVERHEAD_RATE_MAX)
    
    # Indirect toplamı
    ind_total = st.session_state.get("_igroups_total_pct", 0.0)
    ind_custom_df = st.session_state.get("ind_custom_df", pd.DataFrame())
    if isinstance(ind_custom_df, pd.DataFrame) and not ind_custom_df.empty:
        for _, rr in ind_custom_df.iterrows():
            if bool(rr.get("Dahil? (Включить?)", False)):
                ind_total += float(rr.get("Oran (%) (Доля, %)", 0.0))
    
    # Indirect oranını session state'e kaydet
    st.session_state["indirect_rate_total"] = ind_total / 100.0
    
    # Grup toplamlarını göster: Sarf, Overhead, Indirect + Genel Toplam
    st.markdown("---")
    cols_sum = st.columns(3)
    with cols_sum[0]:
        st.info(bi(f"Sarf Toplam: {cons_total:.2f}% ({cons_total/100.0:.3f})",
                   f"Расходники всего: {cons_total:.2f}% ({cons_total/100.0:.3f})"))
    with cols_sum[1]:
        st.info(bi(f"Genel Gider Toplam: {ovh_total:.2f}% ({ovh_total/100.0:.3f})",
                   f"Overhead всего: {ovh_total:.2f}% ({ovh_total/100.0:.3f})"))
    with cols_sum[2]:
        st.info(bi(f"Indirect Toplam: {ind_total:.2f}% ({ind_total/100.0:.3f})",
                   f"Косвенные всего: {ind_total:.2f}% ({ind_total/100.0:.3f})"))

    grand_total = cons_total + ovh_total + ind_total
    st.success(bi(f"✅ Genel Toplam: {grand_total:.2f}% ({grand_total/100.0:.3f})",
                  f"✅ Итого по группам: {grand_total:.2f}% ({grand_total/100.0:.3f})"))
# ==================== 5) SORUMLULUK MATRİSİ (şık) ====================
with tab_matris:
    bih("✨ Sorumluluk Matrisi (checkbox + % katkı)", "✨ Матрица ответственности (чекбокс + вклад, %)", level=4)
    bitr("Seçtiğin satırlar bize ait maliyet sayılır. Yandaki yüzde kutusu 'toplam maliyete oran' katkısıdır.",
         "Отмеченные строки считаются затратами подрядчика. Поле с процентом — вклад в общую стоимость.")
    bitr("Üstteki manuel %'lerle çakışmayı önlemek için aşağıdaki anahtarı kullan.",
         "Чтобы избежать дублирования с ручными %, используйте переключатель ниже.")

    use_matrix_override = st.toggle(bi("🔗 Matris toplamları manuel Sarf/Overhead/Indirect yüzdelerini geçsin (override)",
                                       "🔗 Суммы матрицы перекрывают ручные проценты по расходникам/overhead/косвенным"),
                                    value=st.session_state.get("use_matrix_override", False))
    st.session_state["use_matrix_override"] = use_matrix_override

    # Katalog: (Grup, anahtar, TR, RU, kategori: consumables|overhead|indirect, varsayılan %, çakışma etiketi)
    # overlap: global_extras | core_labor | materials | None
    def _tr_resp_label(key: str, default_tr: str) -> str:
        mapping = {
            # General
            "gen_staff_work": "İşlerin yürütülmesi için personel",
            "gen_work_permit": "Personel için çalışma izni",
            "gen_visa_rf": "Yabancı çalışanlar için RF çalışma vizeleri",
            "gen_migration_resp": "RF göç mevzuatına uyum (ceza/hukuk/sınır dışı riskleri)",
            "gen_social_payments": "Personel ve alt yükleniciler için sosyal ödemeler/vergiler",
            "gen_staff_transport_domintl": "Personelin taşınması (yurtiçi/yurtdışı)",
            "gen_staff_transport_local": "Personelin yerel taşınması",
            "gen_accom_food": "Personelin barınma ve yemek giderleri",
            "gen_transport_mounting": "Montaj malzemeleri/ekipmanlarının yerel taşınması (yüklenici)",
            "gen_transport_wh_to_site": "Müşteri deposundan şantiyeye yerel taşıma",
            "gen_risk_loss_customer_ware": "Müşteri malzemelerinin depolarda kayıp riski",
            "gen_risk_loss_customer_to_finish": "Montaja verilen müşteri malzemelerinin bitime kadar kayıp riski",
            "gen_risk_own_materials_equipment": "Yüklenicinin kendi malzeme/ekipmanının kayıp riski (kablolar dâhil)",
            "gen_required_licenses": "İş türleri için gerekli lisanslar (RF düzenlemeleri)",
            "gen_insurance_equip_staff": "Ekipman ve personel sigortası",
            "gen_workplace_facilities": "Çalışma alanı donanımı: mobilya, telefon, internet, yazıcı",
            # H&S
            "hs_engineer_on_site": "İSG mühendisi – sahada daimi temsilci",
            "hs_action_plan": "İSG eylem planı",
            "hs_meetings": "İSG koordinasyon toplantılarına katılım (talebe bağlı)",
            "hs_initial_briefing": "Tüm personel için ilk İSG bilgilendirmesi",
            "hs_full_responsibility": "Yüklenici alanlarında İSG kurallarına tam sorumluluk",
            "hs_guarding_openings": "Açıklıkların korunması ve kapatılması (yüklenici alanları)",
            "hs_site_med_station": "Şantiye reviri (ilk yardım; hemşire gündüz/gece)",
            "hs_medical_costs": "Tıbbi giderler (ilaç, hastane vb.)",
            "hs_first_aid_kits": "İlk yardım ekipmanları (çalışma alanlarında setler)",
            "hs_ppe": "SİZ, iş kıyafeti ve ayakkabı",
            "hs_firefighting_eq": "Yangınla mücadele ekipmanı (tüp/örtü/su)",
            "hs_safety_labeling": "Güvenlik işaretlemeleri/uyarı levhaları",
            "hs_wind_panels": "Rüzgâr panelleri",
            "hs_protective_nets": "Koruyucu-yakalama ağları (ЗУС)",
            "hs_worker_certs": "Çalışanlar için gerekli sertifika/ehliyetler",
            "hs_consumables": "İSG için tüm sarf malzemeleri",
            "hs_lifting_consumables": "Kaldırma işleri (bkz. kule vinçler) için sarf malzemeleri",
            "hs_lifting_supervisors": "Kaldırma ekipmanı için işaretçi/rigging sorumluları",
            # Site
            "site_power_conn": "Enerji bağlantı noktaları (genel plana göre)",
            "site_power_distribution": "Yüklenici alanlarına enerji dağıtımı",
            "site_power_costs": "Elektrik giderleri",
            "site_water_conn": "Prosess suyu bağlantı noktaları (genel plana göre)",
            "site_water_distribution": "Yüklenici alanlarına su dağıtımı",
            "site_water_costs": "Su giderleri",
            "site_generator": "Gerekirse jeneratör",
            "site_main_lighting": "Alan/bina ana aydınlatması (tüm dönem)",
            "site_add_lighting": "Ek aydınlatma (yüklenici alanları)",
            "site_covered_storage": "Montaja verilmiş malzemeler için kapalı stok alanı",
            "site_closed_storage": "Montaj malzemeleri için kapalı depo/ambar",
            "site_temp_roads": "Yalnız yüklenici kullanımına geçici yollar",
            "site_add_fencing": "Yüklenici alanı için ek çit (gerekirse)",
            "site_scrap_place": "Şantiyede hurda metal depolama alanı",
            "site_lockers": "Soyunma odaları",
            "site_office": "Ofis alanları",
            "site_toilets": "Yüklenici için tuvaletler",
            "site_fire_access": "Yangın müdahale yolları ve şantiyeye sürekli erişim",
            "site_gate_guard": "Giriş kapısında güvenlik",
            "site_add_guard": "Ek güvenlik (gerekirse)",
            "site_full_fencing": "Tüm şantiye alanının çevrilmesi",
            # Works
            "w_proj_docs": "Sayısal proje dokümanları",
            "w_mos": "Yöntem bildirimi (PPR) hazırlanması",
            "w_handover_docs": "Teslim dosyaları (as-built/protokoller) hazırlanması",
            "w_docs_archive": "Arşiv/Elektronik sistemden dokümanlar",
            "w_handover_site_coord": "Şantiye ağlarının ve koordinat sisteminin devri",
            "w_rep_present": "Yüklenici temsilcisinin sahada sürekli bulunması",
            "w_rep_coord_meet": "Koordinasyon toplantılarına katılım (yüklenici temsilcisi)",
            "w_detailed_schedule": "Yüklenici işlerinin ayrıntılı iş programı",
            "w_weekly_reports": "Haftalık ilerleme raporları (kaynaklar dahil)",
            "w_weekly_safety": "Haftalık İSG raporları",
            "w_concrete_proc": "Beton tedariki",
            "w_rebar_proc": "Donatı çeliği tedariki",
            "w_scaff_form": "İskele ve kalıp sistemleri (tümü)",
            "w_tower_cranes": "Kule vinçler (operatörlü)",
            "w_temp_lifts": "Geçici şantiye asansörleri (operatörlü)",
            "w_concrete_pumps": "Beton pompaları (tüm borular ile)",
            "w_pump_operators": "Pompa operatörleri, hat montajı ve bakımı",
            "w_hyd_dist": "Hidrolik beton dağıtıcılar",
            "w_hyd_dist_ops": "Hidrolik dağıtıcı operatörleri",
            "w_aux_lifting": "Hareketli & yardımcı kaldırma araçları (kamyon, vinç, manlift)",
            "w_wheel_wash": "Tekerlek yıkama (operatörlü)",
            "w_all_equipment": "İşlerin icrası için her tür ekipman",
            "w_aux_heat_insul": "Betonda kullanılan yardımcı ısı yalıtım malzemeleri",
            "w_consumables": "İmalat sarfları (gaz, disk, tel vb.)",
            "w_measurements": "Ölçümler ve evrak (as-built dâhil)",
            "w_radios": "El telsizleri",
            "w_concrete_care": "Beton bakım işleri (kışın ısıtma dâhil)",
            "w_lab_tests": "Gerekli tüm laboratuvar testleri",
            "w_cleaning": "Yüklenici alanlarının temizliği, atıkların uzaklaştırılması",
            "w_snow_fire_access": "Ana güzergâhlar ve yangın yollarından kar/buz temizliği",
            "w_snow_local": "Yüklenici alanları/depolar/geçici yollardan kar/buz temizliği",
            "w_stormwater_site": "Şantiye sahasından yağmur suyu drenajı",
            "w_stormwater_contractor": "Yüklenici alanlarından yağmur suyu drenajı",
            "w_load_unload": "Malzemelerin sahada yükleme/boşaltması (düşey/yatay)",
            "w_transport_inside": "Saha içi malzeme taşımaları",
            "w_rebar_couplings": "Dişli/sıkma muflar + hazırlık ekipmanı (donatı)",
            "w_rebar_coupling_works": "Muflu donatı hazırlık/bağlantı çalışmaları",
            "w_material_overspend": "Malzeme israfının mali sorumluluğu",
            "w_repair_for_handover": "Teslim için gerekli onarım işleri",
        }
        return mapping.get(key, default_tr)
    resp_catalog = [
        # ---------- 1) General ----------
        ("General","gen_staff_work","Staff for work implementation","Персонал для выполнения работ","overlap_only",0.0,"core_labor"),
        ("General","gen_work_permit","Work permit for the staff","Разрешение на работу для персонала","overhead",0.0,None),
        ("General","gen_visa_rf","Russian working visas for foreign employees","Визы РФ для иностранного персонала","overhead",0.0,None),
        ("General","gen_migration_resp","Employees follow RF migration legislation (penalties/legal/deportation)","Соблюдение миграционного законодательства РФ…","overhead",0.0,None),
        ("General","gen_social_payments","Social payments/taxes for Contractor's staff & subs","Социальные отчисления, налоги…","overlap_only",0.0,"core_labor"),
        ("General","gen_staff_transport_domintl","Transportation costs of the staff (Domestic & International)","Транспортные расходы персонала (внутренние/междунар.)","indirect",0.0,None),
        ("General","gen_staff_transport_local","Local transportation of the staff","Местная перевозка своего персонала","overlap_only",0.0,"global_extras"),
        ("General","gen_accom_food","Accommodation & feeding of the staff","Проживание и питание своего персонала","overlap_only",0.0,"global_extras"),
        ("General","gen_transport_mounting","Local transportation of mounting materials/equipment (Contractor)","Местная транспортировка монтажных материалов и оборудования подрядчика","indirect",0.0,None),
        ("General","gen_transport_wh_to_site","Local transport from Customer warehouse to site","Местная транспортировка со склада Заказчика до площадки","indirect",0.0,None),
        ("General","gen_risk_loss_customer_ware","Risk of loss of Customer's materials in warehouses","Риск утраты материалов заказчика на складах…","indirect",0.0,None),
        ("General","gen_risk_loss_customer_to_finish","Risk of loss of Customer's materials delivered for mounting till finish","Риск утраты материалов заказчика, переданных подрядчику…","indirect",0.0,None),
        ("General","gen_risk_own_materials_equipment","Risk of loss of Contractor's own materials & equipment incl. cables","Риск утраты собственных материалов и оборудования подрядчика…","indirect",0.0,None),
        ("General","gen_required_licenses","Required licenses per work types (RF regulations)","Требуемые лицензии по видам работ…","overhead",0.0,None),
        ("General","gen_insurance_equip_staff","Insurance of the Contractor's equipment and staff","Страхование оборудования и персонала подрядчика","indirect",0.0,None),
        ("General","gen_workplace_facilities","Workplace Facilities: furniture, phone, internet, printer","Оснащение рабочих мест: мебель, телефон, интернет, принтер","indirect",0.0,None),

        # ---------- 2) H&S ----------
        ("H&S","hs_engineer_on_site","H&S engineer – permanent representative","Инженер ТБ – постоянный представитель подрядчика","overhead",0.0,None),
        ("H&S","hs_action_plan","H&S action plan","Программа мероприятий по ОТ и ТБ","overhead",0.0,None),
        ("H&S","hs_meetings","Participation in coordination meetings on H&S (on request)","Участие в координационных совещаниях по ОТ и ТБ…","overhead",0.0,None),
        ("H&S","hs_initial_briefing","Initial briefing for Contractor's entire staff","Первичный инструктаж по ОТ и ТБ…","overhead",0.0,None),
        ("H&S","hs_full_responsibility","Full responsibility for observance of H&S in Contractor areas","Полная ответственность за соблюдение правил ОТ и ТБ…","overhead",0.0,None),
        ("H&S","hs_guarding_openings","Guarding and closing of openings (Contractor areas)","Защитные ограждения и закрытие проемов…","indirect",0.0,None),
        ("H&S","hs_site_med_station","Site medical station (first aid; nurse day/night)","Медпункт на площадке – первая помощь…","indirect",0.0,None),
        ("H&S","hs_medical_costs","Medical costs (medicine, hospital, etc.)","Медицинские расходы (лекарство, больница и т. д.)","indirect",0.0,None),
        ("H&S","hs_first_aid_kits","Equipment for first aid (kits at working area)","Оборудование для первой помощи (аптечки)","indirect",0.0,None),
        ("H&S","hs_ppe","PPE, clothing & shoes for Contractor employees","СИЗ, одежда и обувь для сотрудников Подрядчика","overlap_only",0.0,"global_extras"),
        ("H&S","hs_firefighting_eq","Firefighting equipment (extinguisher/blanket/water)","Противопожарное оборудование…","indirect",0.0,None),
        ("H&S","hs_safety_labeling","Safety labeling / warning signs","Оснащение участка предупреждающими табличками","indirect",0.0,None),
        ("H&S","hs_wind_panels","Wind Panels","Защитный экран","indirect",0.0,None),
        ("H&S","hs_protective_nets","Protective-trapping nets (ЗУС)","Защитно-улавливающие сетки (ЗУС)","indirect",0.0,None),
        ("H&S","hs_worker_certs","All necessary certificates/attestations for workers","Все необходимые сертификаты/аттестации для рабочих","overhead",0.0,None),
        ("H&S","hs_consumables","All consumables for H&S","Все необходимые расходные материалы для ОТ и ТБ","consumables",0.0,None),
        ("H&S","hs_lifting_consumables","All consumables for lifting (incl. tower cranes)","Расходники для такелажных работ (в т.ч. башенные краны)","consumables",0.0,None),
        ("H&S","hs_lifting_supervisors","Lifting supervisors for all lifting equipment","Стропальщики/риггеры/супервайзеры по подъёмным работам","indirect",0.0,None),

        # ---------- 3) Site equipment ----------
        ("Site","site_power_conn","Power connection points (per master plan)","Точки подключения электроэнергии согласно генплану","indirect",0.0,None),
        ("Site","site_power_distribution","Distribution of power to Contractor's site","Распределение электроэнергии до зон Подрядчика","indirect",0.0,None),
        ("Site","site_power_costs","Electricity costs","Расходы на электричество","indirect",0.0,None),
        ("Site","site_water_conn","Process water connection points (per master plan)","Точки подключения тех. воды согласно генплану","indirect",0.0,None),
        ("Site","site_water_distribution","Distribution of process water to Contractor's site","Распределение воды до зон Подрядчика","indirect",0.0,None),
        ("Site","site_water_costs","Process water costs","Расходы на воду","indirect",0.0,None),
        ("Site","site_generator","Generator if needed","Генератор при необходимости","indirect",0.0,None),
        ("Site","site_main_lighting","Main lighting of areas/buildings (entire period)","Основное освещение площадок и зданий","indirect",0.0,None),
        ("Site","site_add_lighting","Additional lighting (Contractor territories)","Дополнительное освещение территорий подрядчика","indirect",0.0,None),
        ("Site","site_covered_storage","Covered storage for materials delivered for mounting","Крытые площадки складирования (выданных в монтаж)","indirect",0.0,None),
        ("Site","site_closed_storage","Closed storage/warehouses for mounting materials","Закрытые площадки / склады (выданных в монтаж)","indirect",0.0,None),
        ("Site","site_temp_roads","Temporary roads only for contractor use","Временные дороги только для подрядчика","indirect",0.0,None),
        ("Site","site_add_fencing","Additional fencing of contractor territory (if needed)","Дополнительное ограждение территории подрядчика","indirect",0.0,None),
        ("Site","site_scrap_place","Scrap metal storage place on site","Площадка хранения металлолома","indirect",0.0,None),
        ("Site","site_lockers","Locker","Раздевалки","indirect",0.0,None),
        ("Site","site_office","Office premises","Офисные помещения","indirect",0.0,None),
        ("Site","site_toilets","Toilets for contractor","Туалеты субподрядчика","indirect",0.0,None),
        ("Site","site_fire_access","Fire-fighting access, permanent access to site","Пожарные подъезды и постоянный доступ","indirect",0.0,None),
        ("Site","site_gate_guard","Safeguarding at the front gate","Охрана на проходной","indirect",0.0,None),
        ("Site","site_add_guard","Additional safeguarding (if needed)","Дополнительная охрана (по необходимости)","indirect",0.0,None),
        ("Site","site_full_fencing","Fencing of the whole construction site","Ограждение всей стройплощадки","indirect",0.0,None),

        # ---------- 4) Works implementation ----------
        ("Works","w_proj_docs","Project documentation in digital form","Проектные документы в электронном виде","overhead",0.0,None),
        ("Works","w_mos","Preparing method of statement","Подготовка ППР","overhead",0.0,None),
        ("Works","w_handover_docs","Preparing handover documents (as-built/protocols)","Подготовка акты и ИД","overhead",0.0,None),
        ("Works","w_docs_archive","Documents from archive or electronic system","Документы из архива или из ЭДО","overhead",0.0,None),
        ("Works","w_handover_site_coord","Handover site & coordinate network","Передача сетей стройплощадки и реперных точек","overhead",0.0,None),
        ("Works","w_rep_present","Responsible contractor representative always on site","Назначенный представитель подрядчика постоянно на площадке","overhead",0.0,None),
        ("Works","w_rep_coord_meet","Contractor representative in coordination meetings","Представитель подрядчика участвует в совещаниях","overhead",0.0,None),
        ("Works","w_detailed_schedule","Detailed schedule of Contractor's work","Детальный график работ подрядчика","overhead",0.0,None),
        ("Works","w_weekly_reports","Weekly reports on work completion (incl. resources)","Еженедельные отчеты по выполнению работ…","overhead",0.0,None),
        ("Works","w_weekly_safety","Weekly safety reports","Еженедельные отчеты по ОТ и ТБ","overhead",0.0,None),

        ("Works","w_concrete_proc","Concrete procurement","Закупка бетона","overlap_only",0.0,"materials"),
        ("Works","w_rebar_proc","Reinforcement bars procurement","Закупка арматуры","overlap_only",0.0,"materials"),
        ("Works","w_scaff_form","Scaffolding and formwork (all systems)","Леса и опалубки (все системы)","indirect",0.0,None),
        ("Works","w_tower_cranes","Tower cranes with operators","Башенные краны с операторами","indirect",0.0,None),
        ("Works","w_temp_lifts","Temporary construction lifts with operators","Временные грузопассажирские лифты с операторами","indirect",0.0,None),
        ("Works","w_concrete_pumps","Concrete pumps with all needed pipes","Бетононасосы со всеми трубами","indirect",0.0,None),
        ("Works","w_pump_operators","Concrete pump operators, pump line montage & maintenance","Операторы, монтаж и ТО насосных линий","indirect",0.0,None),
        ("Works","w_hyd_dist","Hydraulic concrete distributors","Гидравлические бетонораспределители","indirect",0.0,None),
        ("Works","w_hyd_dist_ops","Hydraulic concrete distributor operators","Операторы гидр. бетонораспределителей","indirect",0.0,None),
        ("Works","w_aux_lifting","Movable & auxiliary lifting devices (trucks, cranes, manlifts)","Передвижные и вспом. грузоподъёмные механизмы","indirect",0.0,None),
        ("Works","w_wheel_wash","Wheel wash with operators","Мойка колес с операторами","indirect",0.0,None),
        ("Works","w_all_equipment","All kind of equipment for works implementation","Все инструменты, используемые для выполнения работ","indirect",0.0,None),

        ("Works","w_aux_heat_insul","All auxiliary hard heat-insulation materials in concrete","Все вспомогательные твердые теплоизоляционные материалы…","overlap_only",0.0,"materials"),
        ("Works","w_consumables","Consumables for works (gas, discs, tie wires etc.)","Расходные материалы для выполнения работ","consumables",0.0,None),
        ("Works","w_measurements","Measurements including documentation","Измерения, включая исполнительную документацию","indirect",0.0,None),
        ("Works","w_radios","Suitable portable radios (walkie-talkie)","Подходящие портативные радиостанции (рации)","indirect",0.0,None),
        ("Works","w_concrete_care","Concrete care incl. heating in winter","Уход за бетоном, включая подогрев зимой","indirect",0.0,None),
        ("Works","w_lab_tests","All necessary laboratory tests","Все необходимые лабораторные испытания","indirect",0.0,None),
        ("Works","w_cleaning","Cleaning contractor's territory incl. waste removal","Уборка территорий подрядчика, вывоз мусора","indirect",0.0,None),
        ("Works","w_snow_fire_access","Snow/ice removal from main tracks & fire access roads","Уборка снега и льда с основных путей и пожарных подъездов","indirect",0.0,None),
        ("Works","w_snow_local","Snow/ice removal from Contractor areas/storage/temp roads","Уборка снега и льда с зон подрядчика/складов/временных путей","indirect",0.0,None),
        ("Works","w_stormwater_site","Discharge storm/rainwater from construction site","Слив ливневой воды с площадок","indirect",0.0,None),
        ("Works","w_stormwater_contractor","Discharge storm/rainwater from Contractor areas","Слив ливневой воды с зон подрядчика","indirect",0.0,None),
        ("Works","w_load_unload","Loading/unloading materials on site (vertical/horizontal)","Погрузка-разгрузка материалов на площадке","indirect",0.0,None),
        ("Works","w_transport_inside","Transportation of materials within construction site","Транспортировка материалов по стройплощадке","indirect",0.0,None),

        ("Works","w_rebar_couplings","Threaded/crimp couplings + tools for rebar preparation","Резьбовые/обжимные муфты + инструмент для подготовки арматуры","overlap_only",0.0,"materials"),
        ("Works","w_rebar_coupling_works","Preparation/connection works with couplings (rebar)","Подготовительные и соединительные работы арматуры с муфтами","overlap_only",0.0,"core_labor"),
        ("Works","w_material_overspend","Financial responsibility of material overspending","Материальная ответственность за перерасход материала","overlap_only",0.0,"materials"),
        ("Works","w_repair_for_handover","Repair works necessary to handover the work","Ремонтные работы, необходимые для сдачи","indirect",0.0,None),
    ]

    if "resp_matrix_state" not in st.session_state:
        st.session_state["resp_matrix_state"] = {k: {"on": False, "pct": dflt} for _,k,_,_,_,dflt,_ in resp_catalog}

    current = st.session_state["resp_matrix_state"]

    # State integrity check - ensure all required keys exist
    for k in current:
        if "cat" not in current[k]:
            current[k]["cat"] = "consumables"
        elif current[k]["cat"] == "overlap_only":
            current[k]["cat"] = "indirect"  # Map overlap_only to indirect
        if "overlap" not in current[k]:
            current[k]["overlap"] = None

    # Çakışma kontrolü ve uyarı sistemi
    def check_conflicts():
        conflicts = {
            "global_extras": [],
            "core_labor": [],
            "materials": []
        }
        
        for _,k,tr,_,_,_,overlap in resp_catalog:
            if current[k]["on"] and overlap in conflicts:
                conflicts[overlap].append(tr)
        
        return conflicts

    conflicts = check_conflicts()
    
    # Çakışma uyarıları
    if conflicts["global_extras"]:
        st.warning(f"⚠️ **Çakışma Uyarısı:** Aşağıdaki kalemler kişi-başı global giderlerde mevcut → çakışma olmaması için hesapta eklenmez:\n" + 
                  "\n".join([f"• {item}" for item in conflicts["global_extras"]]))
    
    if conflicts["core_labor"]:
        st.info(f"ℹ️ **Bilgi:** Aşağıdaki kalemler çekirdek işçilik maliyetinde dahil → ayrıca eklenmez:\n" + 
                "\n".join([f"• {item}" for item in conflicts["core_labor"]]))
    
    if conflicts["materials"]:
        st.info(f"ℹ️ **Bilgi:** Aşağıdaki kalemler malzeme maliyetinde dahil → ayrıca eklenmez:\n" + 
                "\n".join([f"• {item}" for item in conflicts["materials"]]))

    last_group = None
    mat_cols = st.columns([0.40, 0.40, 0.20])
    with mat_cols[0]: st.markdown("**Açıklama (TR)**")
    with mat_cols[1]: st.markdown("**Описание (RU)**")
    with mat_cols[2]: st.markdown("**Bizde? · %**")

    for group,k, tr, ru, cat, dflt, overlap in resp_catalog:
        tr = _tr_resp_label(k, tr)
        if group != last_group:
            last_group = group
            st.markdown(f"### 📋 {group}")
            st.markdown("---")
        
        # Çakışma kontrolü
        is_conflict = overlap in ["global_extras", "core_labor", "materials"]
        
        # Modern satır görünümü - sadece Streamlit kontrolleri
        with st.container():
            # Ana satır container
            st.markdown(f"""
            <div style="background: white; border: 1px solid #e9ecef; border-radius: 12px; padding: 1rem; margin: 0.5rem 0; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
                <div style="display: flex; align-items: center; gap: 1rem;">
                    <div style="flex: 3;">
                        <strong style="font-size: 1.1rem; color: #333;">{tr}</strong><br>
                        <small style="color: #666; font-style: italic; font-size: 0.9rem;">{ru}</small>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)
            
            # Kontroller - 3 sütun halinde
            col1, col2, col3 = st.columns([1, 1, 1])
            
            with col1:
                st.markdown("**Aktif**")
                on = st.checkbox("", value=current[k]["on"], key=f"mx_on_{k}", disabled=is_conflict, label_visibility="collapsed")
            
            with col2:
                st.markdown("**Yüzde (%)**")
                pct = st.number_input("", min_value=0.0, max_value=100.0, value=float(current[k]["pct"]), step=0.1, key=f"mx_pct_{k}", disabled=is_conflict, label_visibility="collapsed")
            
            with col3:
                st.markdown("**Kategori**")
                current_cat = current[k].get("cat", "consumables")
                # Handle overlap_only category - map it to indirect
                if current_cat == "overlap_only":
                    current_cat = "indirect"
                cat_index = ["consumables", "overhead", "indirect"].index(current_cat)
                cat_sel = st.selectbox("", options=["consumables", "overhead", "indirect"], index=cat_index, key=f"mx_cat_{k}", disabled=is_conflict, label_visibility="collapsed")
                
                # Kategori badge'i
                if not is_conflict:
                    badge_color = "category-" + cat_sel
                else:
                    badge_color = "category-overlap"
                
                st.markdown(f'<span class="category-badge {badge_color}">{cat_sel}</span>', unsafe_allow_html=True)
            
            # Çakışma uyarısı
            if is_conflict:
                if overlap == "global_extras":
                    st.warning("⚠️ **Çakışma:** Kişi-başı global giderlerde mevcut → hesapta eklenmez")
                elif overlap == "core_labor":
                    st.info("ℹ️ **Bilgi:** Çekirdek işçilik maliyetinde dahil")
                elif overlap == "materials":
                    st.info("ℹ️ **Bilgi:** Malzeme maliyetinde dahil")
            
            # State güncelleme
            current[k] = {"on": bool(on), "pct": float(pct), "cat": cat_sel, "overlap": overlap}

    mx_sums = {"consumables":0.0, "overhead":0.0, "indirect":0.0}
    for k, v in current.items():
        if v.get("on") and v.get("overlap")!="global_extras":
            cat = v["cat"]
            if cat == "overlap_only":
                cat = "indirect"  # Map overlap_only to indirect for calculations
            mx_sums[cat] += float(v["pct"])

    # Modern toplam kartları
    st.markdown("---")
    bih("📊 Matris Toplamları","📊 Итого по матрице", level=3)
    
    col_sum1, col_sum2, col_sum3 = st.columns(3)
    
    with col_sum1:
        st.markdown(f"""
        <div class="metric-card">
            <h3>🧴 Sarf</h3>
            <div class="val">{mx_sums['consumables']:.2f}%</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col_sum2:
        st.markdown(f"""
        <div class="metric-card">
            <h3>🧮 Overhead</h3>
            <div class="val">{mx_sums['overhead']:.2f}%</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col_sum3:
        st.markdown(f"""
        <div class="metric-card">
            <h3>🧾 Indirect</h3>
            <div class="val">{mx_sums['indirect']:.2f}%</div>
        </div>
        """, unsafe_allow_html=True)

    # Override kontrolü
    st.markdown("---")
    bih("⚙️ Matris Override Kontrolü","⚙️ Перекрытие матрицей", level=3)
    
    if use_matrix_override:
        st.session_state["consumables_rate_eff"]   = mx_sums["consumables"]/100.0
        st.session_state["overhead_rate_eff"]      = mx_sums["overhead"]/100.0
        st.session_state["indirect_rate_total_eff"]= mx_sums["indirect"]/100.0
        st.success(bi("✅ Override aktif: manuel Sarf/Overhead/Indirect oranları yok sayılır; hesapta matris toplamları kullanılacak.",
                      "✅ Перекрытие активно: ручные проценты игнорируются; в расчёте используются суммы матрицы."))
    else:
        st.session_state.pop("consumables_rate_eff", None)
        st.session_state.pop("overhead_rate_eff", None)
        st.session_state.pop("indirect_rate_total_eff", None)
        st.info(bi("ℹ️ Override kapalı: Manuel Sarf/Overhead/Indirect oranları kullanılacak; матрица sadece гösterim amaçlı.",
                   "ℹ️ Перекрытие выключено: используются ручные проценты; суммы матрицы — только для отображения."))
# ==================== 6) SONUÇLAR: Tüm Hesaplama Sonuçları ====================
with tab_sonuclar:
    left, right = st.columns([0.8,0.2])
    with left:
        bih("📊 Hesap Sonuçları Özeti","📊 Сводка результатов расчёта", level=2)
    with right:
        if st.button(bi("🧹 Sonuçları Temizle","🧹 Очистить результаты"), type="secondary"):
            # Varsayılan/boş hal
            st.session_state["calculation_results"] = None
            # Bazı türetilmiş cache/state alanlarını sıfırla
            st.session_state.pop("roles_calc", None)
            st.session_state.pop("elements_df", None)
            st.session_state.pop("metraj_df", None)
            st.session_state.pop("_met_for_keys", None)
            try:
                st.rerun()
            except Exception:
                st.toast(bi("Sayfa yenilendi.","Страница обновлена."))
    
    # --- Hesaplama butonu ---
    if "calculation_results" not in st.session_state:
        st.session_state["calculation_results"] = None

    # Hesaplama butonu
    if st.button(bi("🧮 HESAPLA","🧮 РАССЧИТАТЬ"), type="primary", use_container_width=True, key="hesapla_sonuclar", help="Hesaplamayı başlat"):
        # Modern loading animasyonu
        ph = get_loading_placeholder()
        with ph.container():
            st.markdown("""
            <div style="text-align: center; padding: 2rem; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border-radius: 20px; margin: 1rem 0;">
                <h3 style="margin: 0; color: white;">⚡ Hesaplama İşlemi / ⚡ Процесс расчёта</h3>
                <p style="margin: 0.5rem 0 0 0; opacity: 0.9;">Lütfen bekleyin, sonuçlar hazırlanıyor... / Пожалуйста, подождите, формируем результаты…</p>
            </div>
            """, unsafe_allow_html=True)
        with st.spinner("🚀 Hesaplamalar yapılıyor... / 🚀 Выполняем расчёты…"):
            try:
                # Güvenli değişken erişimi
                roles_df = st.session_state.get("roles_df", pd.DataFrame())
                
                # selected_elements'i doğru şekilde al - CANONICAL KEYS kullanarak
                selected_elements = []
                for k in CANON_KEYS:
                    if st.session_state.get(f"sel_{k}", True):  # Default to True if not set
                        selected_elements.append(k)
                
                if not selected_elements:
                    st.warning(bi("En az bir betonarme eleman seçin.","Выберите хотя бы один элемент."))
                    st.stop()
                
                if len(selected_elements) > 0 and len(roles_df) > 0:
                    # Temel parametreleri al
                    start_date = st.session_state.get("start_date", date.today().replace(day=1))
                    end_date = st.session_state.get("end_date", date.today().replace(day=30))
                    holiday_mode = st.session_state.get("holiday_mode", "her_pazar")
                    hours_per_day = st.session_state.get("hours_per_day", 10.0)
                    scenario = st.session_state.get("scenario", "Gerçekçi")
                    
                    # SENARYO NORMALARI VE ZORLUK - TAM ALGORİTMA
                    # Senaryoya göre temel norm (Temel için)
                    # Senaryo bazı — override destekli
                    _norms_map = get_effective_scenario_norms()
                    scenario_base = float((_norms_map.get(scenario) or SCENARIO_NORMS["Gerçekçi"]) ["Temel"])
                    
                    # Zorluk çarpanı tek merkezden hesaplanır ve cache'e yazılır
                    z_mult = get_difficulty_multiplier_cached()
                    difficulty_multiplier = z_mult  # norm hesapları bunu kullanıyor
                    
                    # Eleman normları - göreli katsayılar (Temel'e oranlanır) - CANONICAL KEYS kullanarak
                    element_relative_factors = {
                        "grobeton": 0.8,   # Grobeton
                        "rostverk": 0.9,   # Rostverk
                        "temel": 1.0,      # Temel (baz)
                        "doseme": 1.1,     # Döşeme
                        "perde": 1.2,      # Perde
                        "merdiven": 1.3    # Merdiven
                    }
                    
                    # Seçili elemanlar arasında normalize et (ortalama 1 olacak şekilde)
                    selected_factors = {k: element_relative_factors.get(k, 1.0) for k in selected_elements if k in element_relative_factors}
                    if selected_factors:
                        avg_factor = sum(selected_factors.values()) / len(selected_factors)
                        norm_mult = {k: v / avg_factor for k, v in selected_factors.items()}
                    else:
                        norm_mult = {"temel": 1.0}  # varsayılan
                    
                    # Metraj kontrolü
                    use_metraj = st.session_state.get("use_metraj", False)
                    metraj_df = st.session_state.get("metraj_df", pd.DataFrame())
                    
                    if use_metraj and not metraj_df.empty:
                        iterable = metraj_df.to_dict(orient="records")
                        col_ele = "Eleman (Элемент)"
                        col_met = "Metraj (m³) (Объём, м³)"
                        st.success("✅ Metraj verileri kullanılıyor!")
                    else:
                        # Use canonical keys and safe label helpers
                        iterable = []
                        for k in selected_elements:
                            if k in LABELS:
                                iterable.append({"Eleman (Элемент)": LABELS[k], "Metraj (m³) (Объём, м³)": 1.0})
                            else:
                                st.warning(f"Tanımsız eleman anahtarı atlandı: {k}")
                        if not iterable:
                            st.error("Hiç geçerli eleman kalmadı!")
                            st.stop()
                        col_ele = "Eleman (Элемент)"
                        col_met = "Metraj (m³) (Объём, м³)"
                        st.warning("⚠️ Metraj verileri kullanılmıyor - varsayılan 1.0 m³ değerleri kullanılıyor")
                    
                    # Norm × Metraj hesaplaması - DÜZELTİLDİ
                    norms_used = {}
                    total_metraj = 0.0
                    total_adamsaat = 0.0
                    
                    # Rusça etiketleri Türkçe anahtarlara eşleştir - CANONICAL KEYS kullanarak
                    russian_to_turkish = {
                        "Подбетонка": "grobeton",
                        "Ростверк": "rostverk", 
                        "Фундамент": "temel",
                        "Плита перекрытия": "doseme",
                        "Стена/диафрагма": "perde",
                        "Лестница": "merdiven",
                        # Tam etiket eşleştirmesi - metraj_df'den gelen tam etiketler için
                        "Grobeton (Подбетонка)": "grobeton",
                        "Rostverk (Ростверк)": "rostverk",
                        "Temel (Фундамент)": "temel",
                        "Döşeme (Плита перекрытия)": "doseme",
                        "Perde (Стена/диафрагма)": "perde",
                        "Merdiven (Лестница)": "merdiven"
                    }
                    
                    # NORM HESAPLAMASI - TAM ALGORİTMA
                    
                    for r in iterable:
                        lbl = str(r[col_ele])
                        met = float(r.get(col_met, 0.0) or 0.0)
                        
                        # Rusça etiketi Türkçe anahtara çevir
                        turkish_key = russian_to_turkish.get(lbl, lbl)
                        norm_multiplier = norm_mult.get(turkish_key, 1.0)
                        
                        # Eleman normu: n_e = senaryo_temel * göreli_katsayı * difficulty_multiplier
                        n_e = scenario_base * norm_multiplier * difficulty_multiplier
                        norms_used[lbl] = n_e
                        total_metraj += met
                        total_adamsaat += met * n_e
                        

                    

                    # TAKVİM HESAPLAMASI - TAM ALGORİTMA
                    
                    workdays = workdays_between(start_date, end_date, holiday_mode)
                    project_days = max((end_date - start_date).days + 1, 1)
                    
                    avg_workdays_per_month = workdays * 30.0 / project_days
                    hours_per_person_month = max(avg_workdays_per_month * hours_per_day, 1e-9)
                    
                    # Rol maliyeti hesaplaması - YENİ ALGORİTMA
                    sum_w = float(roles_df["Ağırlık (Вес)"].clip(lower=0.0).sum()) if not roles_df.empty else 0.0
                    month_wd_df = workdays_in_month_range(start_date, end_date, holiday_mode)
                    n_months = len(month_wd_df) if not month_wd_df.empty else 1
                    person_months_total = total_adamsaat / hours_per_person_month
                    
                    # PRIM ve gider parametreleri
                    PRIM_SNG = st.session_state.get("prim_sng", True)
                    PRIM_TUR = st.session_state.get("prim_tur", True)
                    
                    # Global kişi-başı giderler (giderler sekmesinden hesapla)
                    food = float(st.session_state.get("food", 10000.0))
                    lodging = float(st.session_state.get("lodging", 12000.0))
                    transport = float(st.session_state.get("transport", 3000.0))
                    ppe = float(st.session_state.get("ppe", 1500.0))
                    training = float(st.session_state.get("training", 500.0))
                    
                    # KDV işaretleri (giderler sekmesinden al)
                    food_vat = bool(st.session_state.get("food_vat", True))
                    lodging_vat = bool(st.session_state.get("lodging_vat", True))
                    transport_vat = bool(st.session_state.get("transport_vat", False))
                    ppe_vat = bool(st.session_state.get("ppe_vat", True))
                    training_vat = bool(st.session_state.get("training_vat", True))
                    
                    # Toplam KDV'li giderler
                    extras_base = food + lodging + transport + ppe + training
                    
                    # KDV ayrıştırma (işaretli ise)
                    def net_of_vat(x, tick):
                        vat_rate = float(st.session_state.get("vat_rate", 0.20))
                        return x / (1 + vat_rate) if tick else x
                    
                    # Toplam KDV'siz ek giderler (gerçek değerlerden hesapla)
                    extras_per_person = sum([
                        net_of_vat(food, food_vat),           # Yemek
                        net_of_vat(lodging, lodging_vat),     # Barınma
                        net_of_vat(transport, transport_vat), # Ulaşım
                        net_of_vat(ppe, ppe_vat),            # PPE
                        net_of_vat(training, training_vat)    # Eğitim
                    ])
                    
                    # YENİ ADAM-SAAT ALGORİTMASI - KURŞUN GEÇİRMEZ
                    # 1. Ülke yüzdelerini normalize et (0-0-0 ise eşit böl)
                    def _normalize_country(p_rus, p_sng, p_tur):
                        vals = [max(float(p_rus), 0.0), max(float(p_sng), 0.0), max(float(p_tur), 0.0)]
                        s = sum(vals)
                        if s <= 0:  # hepsi 0 ise eşit böl
                            return (1/3.0, 1/3.0, 1/3.0)
                        return (vals[0]/s, vals[1]/s, vals[2]/s)
                    
                    # 2. A·S fiyatını doğrudan hesapla (ara "kişi-ay" dağıtımı olmadan)
                    M_with = 0.0
                    M_bare = 0.0
                    
                    if not roles_df.empty and sum_w > 0:
                        for _, row in roles_df.iterrows():
                            share = float(row["Ağırlık (Вес)"]) / sum_w
                            
                            # Ülke yüzdelerini normalize et
                            p_rus, p_sng, p_tur = _normalize_country(row["%RUS"], row["%SNG"], row["%TUR"])
                            
                            # monthly_role_cost_multinational fonksiyonunu kullan
                            with_ex = monthly_role_cost_multinational(row, PRIM_SNG, PRIM_TUR, extras_per_person)["per_person"]["BLENDED"]
                            bare_ex = monthly_role_cost_multinational(row, PRIM_SNG, PRIM_TUR, 0.0)["per_person"]["BLENDED"]
                            
                            M_with += share * with_ex
                            M_bare += share * bare_ex
                    
                    # 3. A·S fiyatları - TEK SATIRLIK FORMÜL
                    with_extras_as_price = M_with / hours_per_person_month
                    bare_as_price = M_bare / hours_per_person_month

                    # --- NEW: price follows productivity (scenario + difficulty) ---
                    s_mult  = get_scenario_multiplier_for_price(scenario)   # senaryo etkisi
                    z_mult  = get_difficulty_multiplier_cached()            # çevresel zorluk etkisi
                    # Ayarlanabilir katsayılar (ileride istersen UI ekleyebilirsin)
                    BETA_SCENARIO_TO_PRICE  = 1.0    # 0..1 (1=tam, 0=sızdırma)
                    BETA_DIFFICULTY_TO_PRICE= 1.0    # 0..1 (1=tam, 0=sızdırma)

                    price_mult = (1 + BETA_SCENARIO_TO_PRICE  * (s_mult - 1)) \
                               * (1 + BETA_DIFFICULTY_TO_PRICE* (z_mult - 1))

                    bare_as_price        *= price_mult
                    with_extras_as_price *= price_mult

                    core_as_price        = with_extras_as_price  # m³ maliyetleri bu fiyattan üretilecek
                    
                    # Roller hesaplama tablosu için
                    roles_calc = []
                    if not roles_df.empty and sum_w > 0:
                        for _, row in roles_df.iterrows():
                            w = max(float(row["Ağırlık (Вес)"]), 0.0)
                            share = (w / sum_w)
                            persons_role = (person_months_total / n_months) * share
                            
                            # Ülke yüzdelerini normalize et
                            p_rus, p_sng, p_tur = _normalize_country(row["%RUS"], row["%SNG"], row["%TUR"])
                            
                            # monthly_role_cost_multinational kullan
                            bundle_with = monthly_role_cost_multinational(row, PRIM_SNG, PRIM_TUR, extras_per_person)
                            bundle_bare = monthly_role_cost_multinational(row, PRIM_SNG, PRIM_TUR, 0.0)
                            per_with = bundle_with["per_person"]["BLENDED"]
                            per_bare = bundle_bare["per_person"]["BLENDED"]
                            
                            roles_calc.append({
                                "Rol (Роль)": row["Rol (Роль)"],
                                "Ağırlık (Вес)": f"{w:.3f}",
                                "Pay (%) (Доля, %)": f"{share * 100:.2f}",
                                "Ortalama Kişi (Средняя численность)": f"{persons_role:.3f}",
                                "Maliyet/ay (₽)": f"{per_with:,.2f}",
                                "%RUS": f"{p_rus * 100:.1f}",
                                "%SNG": f"{p_sng * 100:.1f}",
                                "%TUR": f"{p_tur * 100:.1f}",
                                "Net Maaş (₽/ay)": f"{float(row.get('Net Maaş (₽, na ruki) (Чистая з/п, ₽)', 0)):,.0f}"
                            })
                    
                    roles_calc_df = pd.DataFrame(roles_calc)
                    
                    # m³ maliyetleri - ELEMAN ÖZGÜ NORM KULLAN
                    # Matrix override kontrolü ile oranları al
                    use_matrix_override = st.session_state.get("use_matrix_override", False)
                    if use_matrix_override:
                        # Matrix override aktif - effective rates kullan
                        overhead_rate_eff = st.session_state.get("overhead_rate_eff", OVERHEAD_RATE_DEFAULT/100.0)
                        consumables_rate_eff = st.session_state.get("consumables_rate_eff", CONSUMABLES_RATE_DEFAULT/100.0)
                        indirect_rate_total = st.session_state.get("indirect_rate_total_eff", 0.12)
                    else:
                        # Matrix override kapalı - grup hesaplanan rates kullan
                        overhead_rate_eff = st.session_state.get("overhead_rate", OVERHEAD_RATE_DEFAULT/100.0)
                        consumables_rate_eff = st.session_state.get("consumables_rate", CONSUMABLES_RATE_DEFAULT/100.0)
                        indirect_rate_total = st.session_state.get("indirect_rate_total", 0.12)
                    
                    sum_core_overhead_total = 0.0
                    tmp_store = []
                    
                    for r in iterable:
                        lbl = str(r[col_ele])
                        met = float(r.get(col_met, 0.0) or 0.0)
                        n = norms_used[lbl]  # Eleman özgü norm
                        core_m3 = with_extras_as_price * n  # Eleman özgü norm ile çarp
                        genel_m3 = min(max(overhead_rate_eff, 0.0), OVERHEAD_RATE_MAX/100.0) * core_m3
                        total_m3_core_genel = core_m3 + genel_m3
                        sum_core_overhead_total += total_m3_core_genel * met
                        tmp_store.append((lbl, met, total_m3_core_genel, core_m3, genel_m3, n))
                    
                    # Sarf + Indirect - (core_m3 + genel_m3) * metraj tabanı üzerinden
                    consumables_total = sum_core_overhead_total * max(consumables_rate_eff, 0.0)
                    indirect_total = (sum_core_overhead_total + consumables_total) * max(indirect_rate_total, 0.0)
                    
                    # Elemanlara dağıt - oransal dağıtım
                    elem_rows = []
                    project_total_cost = 0.0
                    
                    for (lbl, met, base_total, core_m3, genel_m3, n) in tmp_store:
                        # Sarf ve indirect dağıtımı: (core_m3 + genel_m3) * metraj oranı
                        weight = (base_total * met) / max(sum_core_overhead_total, 1e-9)
                        sarf_alloc = consumables_total * weight
                        indir_alloc = indirect_total * weight
                        sarf_m3 = sarf_alloc / max(met, 1e-9) if met > 0 else 0.0
                        indir_m3 = indir_alloc / max(met, 1e-9) if met > 0 else 0.0
                        total_m3 = core_m3 + genel_m3 + sarf_m3 + indir_m3
                        project_total_cost += total_m3 * max(met, 0.0)
                        
                        elem_rows.append({
                            "Eleman (Элемент)": lbl,
                            "Norm (a·s/m³) (Норма, чел·ч/м³)": f"{n:.2f}",
                            "Metraj (m³) (Объём, м³)": f"{met:,.3f}",
                            "Çekirdek (₽/m³) (Ядро, ₽/м³)": f"{core_m3:,.2f}",
                            "Genel (₽/м³) (Накладные, ₽/м³)": f"{genel_m3:,.2f}",
                            "Sarf (₽/м³) (Расходники, ₽/м³)": f"{sarf_m3:,.2f}",
                            "Indirect (₽/м³) (Косвенные, ₽/м³)": f"{indir_m3:,.2f}",
                            "Toplam (₽/м³) (Итого, ₽/м³)": f"{total_m3:,.2f}"
                        })
                    
                    elements_df = pd.DataFrame(elem_rows)
                    
                    # Özet metrikler
                    general_avg_m3 = project_total_cost / max(total_metraj, 1e-9) if total_metraj > 0 else 0.0
                    fully_loaded_as_price = project_total_cost / max(total_adamsaat, 1e-9) if total_adamsaat > 0 else 0.0
                    avg_norm_per_m3 = total_adamsaat / max(total_metraj, 1e-9) if total_metraj > 0 else 0.0
                    indirect_share = indirect_total / max(project_total_cost, 1e-9) if project_total_cost > 0 else 0.0
                    
                    # Sonuçları session state'e kaydet
                    st.session_state["calculation_results"] = {
                        "success": True,
                        "data": {
                            "bare_as_price": bare_as_price,
                            "with_extras_as_price": with_extras_as_price,
                            "fully_loaded_as_price": fully_loaded_as_price,
                            "total_adamsaat": total_adamsaat,
                            "avg_norm_per_m3": avg_norm_per_m3,
                            "general_avg_m3": general_avg_m3,
                            "total_metraj": total_metraj,
                            "project_total_cost": project_total_cost,
                            "consumables_rate_eff": consumables_rate_eff,
                            "overhead_rate_eff": overhead_rate_eff,
                            "indirect_rate_total": indirect_rate_total,
                            "indirect_total": indirect_total,
                            "indirect_share": indirect_share,
                            "elements_df": elements_df,
                            "roles_calc_df": roles_calc_df,
                            "month_wd_df": month_wd_df,
                            "person_months_total": person_months_total,
                            "hours_per_person_month": hours_per_person_month,
                            "norms_used": norms_used,
                            "difficulty_multiplier": difficulty_multiplier
                        }
                    }
                    
                    st.success("✅ Hesaplamalar tamamlandı!")
                    st.balloons()
                    
                    # Özet bilgiler
                    st.info(f"📊 **Proje Özeti:** {len(selected_elements)} eleman, {len(roles_df)} rol, {total_metraj:.1f} m³, {total_adamsaat:.0f} a·s")
                    
                    # Detaylara Bak expander'ı - Debug bilgileri burada
                    with st.expander("🔍 Detaylara Bak - Hesaplama Parametreleri", expanded=False):
                        st.markdown("### 📊 Hesaplama Parametreleri")
                        
                        col1, col2 = st.columns(2)
                        with col1:
                            st.markdown("**🎯 Temel Parametreler**")
                            st.write(f"• Senaryo: {scenario}")
                            st.write(f"• Temel norm: {scenario_base} a·s/m³")
                            st.write(f"• Zorluk çarpanı: {difficulty_multiplier:.3f}")
                            st.write(f"• Günlük çalışma: {hours_per_day} saat")
                            st.write(f"• Tatil modu: {holiday_mode}")
                            st.write(f"• İş günü: {workdays} gün")
                            st.write(f"• Proje süresi: {project_days} gün")
                        
                        with col2:
                            st.markdown("**💰 Gider Parametreleri**")
                            st.write(f"• Yemek: {food:.0f} ₽/ay (KDV: {'Dahil' if food_vat else 'Hariç'})")
                            st.write(f"• Barınma: {lodging:.0f} ₽/ay (KDV: {'Dahil' if lodging_vat else 'Hariç'})")
                            st.write(f"• Ulaşım: {transport:.0f} ₽/ay (KDV: {'Dahil' if transport_vat else 'Hariç'})")
                            st.write(f"• PPE: {ppe:.0f} ₽/ay (KDV: {'Dahil' if ppe_vat else 'Hariç'})")
                            st.write(f"• Eğitim: {training:.0f} ₽/ay (KDV: {'Dahil' if training_vat else 'Hariç'})")
                            st.write(f"• **Toplam KDV'li: {extras_base:.0f} ₽/ay**")
                            st.write(f"• **Toplam KDV'siz: {extras_per_person:.2f} ₽/ay**")
                        
                        st.markdown("### 🧮 Adam-Saat Hesaplama Detayları")
                        
                        col3, col4 = st.columns(2)
                        with col3:
                            st.markdown("**⏰ Takvim Hesaplaması**")
                            st.write(f"• Ortalama iş günü/ay: {avg_workdays_per_month:.2f} gün")
                            st.write(f"• Saat/kişi-ay: {hours_per_person_month:.2f} saat")
                            st.write(f"• Toplam kişi-ay: {person_months_total:.2f}")
                            st.write(f"• Ay sayısı: {n_months}")
                        
                        with col4:
                            st.markdown("**💵 Maliyet Hesaplaması**")
                            st.write(f"• M_with (extras dahil): {M_with:.2f} ₽")
                            st.write(f"• M_bare (extras hariç): {M_bare:.2f} ₽")
                            st.write(f"• **A·S fiyatı (with): {with_extras_as_price:.2f} ₽/a·s**")
                            st.write(f"• **A·S fiyatı (bare): {bare_as_price:.2f} ₽/a·s**")
                        
                        # Norm hesaplama detayları
                        st.markdown("### 📏 Norm Hesaplama Detayları")
                        st.write("**Eleman özgü normlar:**")
                        for lbl, norm in norms_used.items():
                            st.write(f"• {lbl}: {norm:.2f} a·s/m³")
                        
                        st.write("**Norm çarpanları:**")
                        for key, mult in norm_mult.items():
                            st.write(f"• {key}: {mult:.3f}")
                        
                        # Roller detayları
                        if not roles_df.empty:
                            st.markdown("### 👥 Rol Bazında Detaylar")
                            for _, row in roles_df.iterrows():
                                p_rus, p_sng, p_tur = _normalize_country(row["%RUS"], row["%SNG"], row["%TUR"])
                                bundle_with = monthly_role_cost_multinational(row, PRIM_SNG, PRIM_TUR, extras_per_person)
                                bundle_bare = monthly_role_cost_multinational(row, PRIM_SNG, PRIM_TUR, 0.0)
                                
                                st.markdown(f"**{row['Rol (Роль)']}** (Ağırlık: {row['Ağırlık (Вес)']})")
                                st.write(f"  • %RUS: {p_rus:.1%}, %SNG: {p_sng:.1%}, %TUR: {p_tur:.1%}")
                                st.write(f"  • Net maaş: {row.get('Net Maaş (₽, na ruki) (Чистая з/п, ₽)', 0):.0f} ₽/ay")
                                st.write(f"  • Maliyet (with extras): {bundle_with['per_person']['BLENDED']:.2f} ₽/ay")
                                st.write(f"  • Maliyet (bare): {bundle_bare['per_person']['BLENDED']:.2f} ₽/ay")
                                st.write("---")
                    
                else:
                    st.error("❌ Hesaplama için gerekli veriler eksik!")
                    st.info(f"💡 Seçili eleman sayısı: {len(selected_elements)}, Rol sayısı: {len(roles_df)}")
                    st.session_state["calculation_results"] = None
                    
            except Exception as e:
                st.error(f"❌ Hesaplama hatası: {e}")
                st.session_state["calculation_results"] = None
            finally:
                clear_loading_placeholder()
    if st.session_state.get("calculation_results"):
        results = st.session_state["calculation_results"]
        data = results["data"]
        
        # Ana metrikler - Modern kartlar
        bih("💰 Adam-Saat Fiyatları","💰 Стоимость человеко-часа", level=2)
        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(f"""
            <div class="metric-card">
                <h3>🏃 Çıplak a·s Fiyatı / Базовая цена ч·ч</h3>
                <div class="val">{data['bare_as_price']:,.2f} ₽/a·s</div>
            </div>
            """, unsafe_allow_html=True)
        with col2:
            st.markdown(f"""
            <div class="metric-card">
                <h3>🍽️ Genel Giderli a·s / С ч·ч с общими расходами</h3>
                <div class="val">{data['with_extras_as_price']:,.2f} ₽/a·s</div>
            </div>
            """, unsafe_allow_html=True)
        with col3:
            st.markdown(f"""
            <div class="metric-card">
                <h3>🎯 Her Şey Dahil a·s / Полная ч·ч (всё включено)</h3>
                <div class="val">{data['fully_loaded_as_price']:,.2f} ₽/a·s</div>
            </div>
            """, unsafe_allow_html=True)

        # Proje özeti - Modern kartlar
        bih("🏗️ Proje Özeti","🏗️ Сводка проекта", level=3)
        colA, colB, colC = st.columns(3)
        with colA:
            st.markdown(f"""
            <div class="metric-card">
                <h3>⏰ Toplam Adam-Saat / Итого ч·ч</h3>
                <div class="val">{data['total_adamsaat']:,.2f} чел·ч</div>
            </div>
            """, unsafe_allow_html=True)
        with colB:
            st.markdown(f"""
            <div class="metric-card">
                <h3>📏 m³ Başına Ort. a·s / Ср. ч·ч на м³</h3>
                <div class="val">{data['avg_norm_per_m3']:,.2f} a·s/m³</div>
            </div>
            """, unsafe_allow_html=True)
        with colC:
            st.markdown(f"""
            <div class="metric-card">
                <h3>💰 Genel Ortalama / Среднее по проекту</h3>
                <div class="val">{data['general_avg_m3']:,.2f} ₽/m³</div>
            </div>
            """, unsafe_allow_html=True)
        
        colD, colE = st.columns(2)
        with colD:
            st.markdown(f"""
            <div class="metric-card">
                <h3>📊 Toplam Metraj / Общий объём</h3>
                <div class="val">{data['total_metraj']:,.3f} m³</div>
            </div>
            """, unsafe_allow_html=True)
        with colE:
            st.markdown(f"""
            <div class="metric-card">
                <h3>💵 Toplam Maliyet / Общая стоимость</h3>
                <div class="val">{data['project_total_cost']:,.2f} ₽</div>
            </div>
            """, unsafe_allow_html=True)

        # Loading mesajını gizle
        clear_loading_placeholder()
        
        # Oranlar
        bih("📊 Etkili Oranlar","📊 Эффективные доли", level=3)
        _mode_override = bool(st.session_state.get("use_matrix_override", False))
        if _mode_override:
            st.markdown("<div style='display:inline-block;padding:2px 8px;border-radius:9999px;background:#e6fffa;border:1px solid #99f6e4;color:#065f46;font-weight:600;margin-bottom:8px;'>"+bi("Mod: Matris Override","Режим: Матрица override")+"</div>", unsafe_allow_html=True)
        else:
            st.markdown("<div style='display:inline-block;padding:2px 8px;border-radius:9999px;background:#eff6ff;border:1px solid #bfdbfe;color:#1e3a8a;font-weight:600;margin-bottom:8px;'>"+bi("Mod: Manuel %","Режим: Ручные %")+"</div>", unsafe_allow_html=True)
        st.markdown(bi(f"**🧴 Sarf:** {data['consumables_rate_eff']*100:.2f}%",
                       f"**🧴 Расходники:** {data['consumables_rate_eff']*100:.2f}%"))
        st.markdown(bi(f"**🧮 Overhead:** {data['overhead_rate_eff']*100:.2f}%",
                       f"**🧮 Overhead:** {data['overhead_rate_eff']*100:.2f}%"))
        st.markdown(f"**🧾 Indirect:** {data['indirect_rate_total']*100:.2f}%")
        st.markdown(f"**Indirect toplam:** {data['indirect_total']:,.2f} ₽ · **Pay:** {data['indirect_share']:.1%}")

        # Tablolar
        st.markdown("""
        <div class="table-header">
            <h3>📊 Eleman Bazında m³ Maliyeti</h3>
        </div>
        """, unsafe_allow_html=True)
        st.markdown('<div class="custom-table-wrapper">', unsafe_allow_html=True)
        st.dataframe(data['elements_df'], use_container_width=True, hide_index=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        st.markdown("""
        <div class="table-header">
            <h3>🧑‍🔧 Rol Dağılımı — Aylık Ortalama</h3>
        </div>
        """, unsafe_allow_html=True)
        st.markdown('<div class="custom-table-wrapper">', unsafe_allow_html=True)
        st.dataframe(data['roles_calc_df'], use_container_width=True, hide_index=True)
        st.markdown('</div>', unsafe_allow_html=True)

        # Aylık Manpower Distribution grafiği
        st.markdown("""
        <div class="table-header">
            <h3>📈 Manpower Distribution</h3>
        </div>
        """, unsafe_allow_html=True)
        
        # Dağıtım türü seçimi ve açıklamalar
        st.markdown("**📊 Aylık Adam Dağılımı (Manpower Distribution):**")
        st.markdown("*Şantiye gerçeklerine uygun, yumuşak geçişli dağılım türleri - Varsayılan: Klasik Parabolik*")
        
        col_dist1, col_dist2 = st.columns([2, 1])
        with col_dist1:
            distribution_type = st.selectbox(
                "📊 Dağıtım Türü",
                ["Klasik Parabolik", "Gelişmiş Parabolik", "Sigmoid", "Üçgen"],
                index=0,
                help="Şantiye gerçeklerine en uygun dağıtım türünü seçin (Varsayılan: Klasik Parabolik)"
            )
        
        # Dağıtım türü açıklaması
        with col_dist2:
            if distribution_type == "Klasik Parabolik":
                st.info("📈 Varsayılan parabolik")
            elif distribution_type == "Gelişmiş Parabolik":
                st.info("🏗️ Hazırlık → Yoğun → Kapanış")
            elif distribution_type == "Sigmoid":
                st.info("🔄 Çok yumuşak geçişler")
            elif distribution_type == "Üçgen":
                st.info("📐 Doğrusal artış/azalış")
        
        # Dağıtım türü detay açıklaması
        if distribution_type == "Klasik Parabolik":
            st.success("✅ **Klasik Parabolik:** Varsayılan dağılım. Şantiye gerçeklerine uygun, orta noktada maksimum, başlangıç ve sonda yumuşak azalış.")
        elif distribution_type == "Gelişmiş Parabolik":
            st.info("🏗️ **Gelişmiş Parabolik:** Gelişmiş parametrelerle. Hazırlık döneminde yumuşak artış, yoğun çalışma döneminde maksimum, kapanış döneminde yumuşak azalış.")
        elif distribution_type == "Sigmoid":
            st.info("🔄 **Sigmoid:** Çok yumuşak geçişler. Keskin değişimler olmaz, doğal şantiye profili.")
        elif distribution_type == "Üçgen":
            st.info("📐 **Üçgen:** Doğrusal artış ve azalış. Basit ama etkili, orta noktada maksimum.")
        
        # Gelişmiş parametreler (sadece Gelişmiş Parabolik için)
        if distribution_type == "Gelişmiş Parabolik":
            st.markdown("**⚙️ Gelişmiş Parametreler (Gelişmiş Parabolik):**")
            st.markdown("*Hazırlık, yoğun çalışma ve kapanış dönemlerinin karakterini ayarlayın*")
            col_param1, col_param2, col_param3 = st.columns(3)
            
            with col_param1:
                peak_position = st.slider(
                    "📍 Maksimum Nokta (%)",
                    min_value=30.0,
                    max_value=70.0,
                    value=45.0,
                    step=5.0,
                    help="Proje süresinin hangi yüzdesinde maksimum kişi sayısına ulaşılacak"
                ) / 100.0
            
            with col_param2:
                left_smoothness = st.slider(
                    "🔄 Sol Yumuşaklık",
                    min_value=1.5,
                    max_value=4.0,
                    value=2.5,
                    step=0.1,
                    help="Hazırlık dönemi geçiş yumuşaklığı (yüksek = daha yumuşak)"
                )
            
            with col_param3:
                right_smoothness = st.slider(
                    "🔄 Sağ Yumuşaklık",
                    min_value=1.0,
                    max_value=3.0,
                    value=1.8,
                    step=0.1,
                    help="Kapanış dönemi geçiş yumuşaklığı (yüksek = daha yumuşak)"
                )
        
        # Minimum ağırlık parametresi (tüm dağıtımlar için)
        st.markdown("**📉 Genel Parametreler:**")
        if distribution_type != "Üçgen":
            min_weight = st.slider(
                "📉 Minimum Ağırlık (%)",
                min_value=5.0,
                max_value=25.0,
                value=15.0,
                step=1.0,
                help="En düşük ay için minimum ağırlık yüzdesi (çok keskin geçişleri önler)"
            ) / 100.0
        else:
            min_weight = 0.1  # Üçgen için sabit
            st.info("📐 Üçgen dağılım için minimum ağırlık %10 olarak sabit")
        
        if not data['month_wd_df'].empty:
            n_months = len(data['month_wd_df'])
            
            # Farklı dağıtım türleri - şantiye gerçeklerine uygun
            def get_distribution_weights(n_months, dist_type, peak_pos=0.45, left_smooth=2.5, right_smooth=1.8, min_weight=0.15):
                if n_months <= 1:
                    return [1.0]
                
                if dist_type == "Gelişmiş Parabolik":
                    # Gelişmiş parabolik: Hazırlık, yoğun çalışma, kapanış
                    
                    weights = []
                    for i in range(n_months):
                        x = i / (n_months - 1) if n_months > 1 else 0.5
                        
                        if x <= peak_pos:
                            # Hazırlık dönemi: Yumuşak artış
                            normalized_x = x / peak_pos
                            weight = (normalized_x ** left_smooth) * (1 - min_weight) + min_weight
                        else:
                            # Kapanış dönemi: Yumuşak azalış
                            normalized_x = (x - peak_pos) / (1 - peak_pos)
                            weight = ((1 - normalized_x) ** right_smooth) * (1 - min_weight) + min_weight
                        
                        weights.append(weight)
                
                elif dist_type == "Klasik Parabolik":
                    # Klasik parabolik: y = -4(x-0.5)² + 1
                    weights = []
                    for i in range(n_months):
                        x = i / (n_months - 1) if n_months > 1 else 0.5
                        weight = -4 * (x - 0.5)**2 + 1
                        weights.append(max(weight, min_weight))
                
                elif dist_type == "Sigmoid":
                    # Sigmoid: Çok yumuşak geçişler
                    weights = []
                    for i in range(n_months):
                        x = i / (n_months - 1) if n_months > 1 else 0.5
                        # Sigmoid fonksiyonu: 1 / (1 + e^(-k(x-0.5)))
                        k = 6.0  # Yumuşaklık parametresi
                        sigmoid = 1 / (1 + math.exp(-k * (x - 0.5)))
                        weights.append(max(sigmoid, min_weight))
                
                elif dist_type == "Üçgen":
                    # Üçgen: Doğrusal artış ve azalış
                    weights = []
                    for i in range(n_months):
                        x = i / (n_months - 1) if n_months > 1 else 0.5
                        if x <= 0.5:
                            weight = 2 * x  # 0'dan 1'e doğrusal artış
                        else:
                            weight = 2 * (1 - x)  # 1'den 0'a doğrusal azalış
                        weights.append(max(weight, min_weight))
                
                else:
                    # Varsayılan: Eşit dağılım
                    weights = [1.0] * n_months
                
                # Toplamı 1'e normalize et
                total_weight = sum(weights)
                normalized_weights = [w / total_weight for w in weights]
                return normalized_weights
            
            # Parametreleri dağıtım fonksiyonuna geçir
            if distribution_type == "Gelişmiş Parabolik":
                weights = get_distribution_weights(n_months, distribution_type, peak_position, left_smoothness, right_smoothness, min_weight)
            else:
                weights = get_distribution_weights(n_months, distribution_type, min_weight=min_weight)
            headcounts_float = [data['person_months_total'] * wi for wi in weights]
            
            # Toplam adam-ay korunmalı - yuvarlama hatası düzeltmesi
            def round_preserve_sum(values):
                """Toplamı koruyarak yuvarlama"""
                rounded = [round(v) for v in values]
                total_diff = sum(values) - sum(rounded)
                
                if abs(total_diff) >= 1:
                    # En büyük farkı olan değeri düzelt
                    diffs = [(i, abs(v - r)) for i, (v, r) in enumerate(zip(values, rounded))]
                    diffs.sort(key=lambda x: x[1], reverse=True)
                    
                    for i, _ in diffs:
                        if total_diff > 0:
                            rounded[i] += 1
                            total_diff -= 1
                        elif total_diff < 0:
                            rounded[i] -= 1
                            total_diff += 1
                        if abs(total_diff) < 1:
                            break
                
                return rounded
            
            headcounts_int = round_preserve_sum(headcounts_float)
            
            month_wd_df_copy = data['month_wd_df'].copy()
            month_wd_df_copy["Manpower (Численность)"] = headcounts_int

            # Daha estetik grafik
            fig, ax = plt.subplots(figsize=(12, 6))
            
            # Bar grafiği
            bars = ax.bar(month_wd_df_copy["Ay (Месяц)"], month_wd_df_copy["Manpower (Численность)"], 
                         color='skyblue', alpha=0.7, edgecolor='navy', linewidth=1)
            
            # Çizgi grafiği (trend)
            ax.plot(range(n_months), headcounts_int, 'o-', color='red', linewidth=2, 
                   markersize=8, markerfacecolor='white', markeredgecolor='red')
            
            # Değer etiketleri
            for rect, val in zip(bars, headcounts_int):
                ax.text(rect.get_x() + rect.get_width()/2, rect.get_height() + 0.5, 
                       f"{int(val)}", ha="center", va="bottom", fontsize=11, 
                       fontweight='bold', color='darkblue')
            
            # Grafik stilleri
            ax.set_xlabel("Ay (Месяц)", fontsize=12, fontweight='bold')
            ax.set_ylabel("Kişi (Человек)", fontsize=12, fontweight='bold')
            ax.set_title(f"Manpower Distribution - Aylık Adam Dağılımı ({distribution_type})", 
                        fontsize=14, fontweight='bold', pad=20)
            
            # Grid ve eksen
            ax.grid(True, axis="y", alpha=0.3, linestyle='--')
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            
            # X ekseni etiketleri
            plt.xticks(rotation=45, ha="right", fontsize=10)
            plt.yticks(fontsize=10)
            
            # Y ekseni başlangıcı 0'dan başlasın
            ax.set_ylim(bottom=0)
            
            plt.tight_layout()
            st.pyplot(fig)
            
            # Dağıtım bilgileri
            st.markdown(f"**📊 Dağıtım Detayları ({distribution_type}):**")
            col_info1, col_info2, col_info3 = st.columns(3)
            
            with col_info1:
                st.metric("🏗️ Başlangıç", f"{headcounts_int[0]} kişi", 
                         f"{weights[0]*100:.1f}%")
            with col_info2:
                max_idx = headcounts_int.index(max(headcounts_int))
                st.metric("📈 Maksimum", f"{headcounts_int[max_idx]} kişi", 
                         f"{weights[max_idx]*100:.1f}%")
            with col_info3:
                st.metric("🏁 Bitiş", f"{headcounts_int[-1]} kişi", 
                         f"{weights[-1]*100:.1f}%")
            
            # Toplam kontrol
            st.info(f"✅ **Toplam Adam-Ay:** {data['person_months_total']:.2f} → **Dağıtılan:** {sum(headcounts_int)} kişi")
            
        else:
            st.info("Grafik için tarih aralığında en az bir ay olmalı.")

        # Rapor indirme bölümü kaldırıldı; indirmeler 'Import' sekmesine taşındı.

    else:
        st.info("💡 Hesaplama yapmak için yukarıdaki **HESAPLA** butonuna tıklayın.")
        st.markdown("**Gerekli adımlar:**")
        st.markdown("1. **Genel** sekmesinde tarih ve parametreleri ayarlayın")
        st.markdown("2. **Eleman & Metraj** sekmesinde betonarme elemanları seçin")
        st.markdown("3. **Roller** sekmesinde rol kompozisyonunu belirleyin")
        st.markdown("4. **HESAPLA** butonuna tıklayarak sonuçları görün")
# ==================== 7.1) IMPORT: Gelişmiş Veri İçe Aktarma ====================
with tab_import:
    bih("📥 İçe Aktar (CSV/Excel/JSON)","📥 Импорт (CSV/Excel/JSON)", level=3)
    bitr(
        "Projeye ait metraj, roller veya özel giderleri dış dosyalardan alabilirsiniz.",
        "Можно загрузить объёмы, роли или прочие затраты из внешних файлов."
    )

    import_option = st.radio(
        bi("Hedef tablo","Целевая таблица"),
        [bi("Eleman & Metraj","Элементы и объёмы"), bi("Roller","Роли"), bi("Özel Giderler","Прочие затраты")],
        horizontal=True
    )

    uploaded_file = st.file_uploader(bi("Dosya seçin","Выберите файл"), type=["csv","xlsx","xls","json"])

    with st.expander(bi("Kolon eşleştirme","Сопоставление колонок"), expanded=False):
        st.caption(bi("Sol: beklenen, Sağ: dosyadaki karşılık","Слева ожидается, справа — соответствие из файла"))
        if import_option.startswith("🧩") or "Eleman" in import_option:
            expected_cols = ["Eleman","Metraj (m3)"]
        elif import_option.startswith("👥") or "Rol" in import_option:
            expected_cols = ["Rol","Ağırlık (Вес)","Net Maaş (₽)"]
        else:
            expected_cols = ["Kalem","Tutar (₽)","Grup"]
        mapping = {}
        for col in expected_cols:
            mapping[col] = st.text_input(col, col)

    if uploaded_file:
        try:
            if uploaded_file.name.lower().endswith((".xlsx",".xls")):
                df_in = pd.read_excel(uploaded_file)
            elif uploaded_file.name.lower().endswith(".csv"):
                df_in = pd.read_csv(uploaded_file)
            else:
                df_in = pd.read_json(uploaded_file)

            st.dataframe(df_in.head(50), use_container_width=True)

            if st.button(bi("İçe aktar","Импортировать"), type="primary"):
                df = df_in.rename(columns=mapping)
                if "Eleman" in mapping:
                    # Eleman & Metraj
                    need = ["Eleman","Metraj (m3)"]
                    if all(c in df.columns for c in need):
                        met_df = pd.DataFrame({k: st.session_state.get(k, False) for k in CANON_KEYS}, index=[0])
                        for _, r in df.iterrows():
                            key = canon_key(str(r["Eleman"])) if "Eleman" in r else None
                            if key in CANON_KEYS:
                                st.session_state[f"sel_{key}"] = True
                                st.session_state[f"met_{key}"] = float(r["Metraj (m3)"])
                        st.success(bi("Metraj güncellendi","Объёмы обновлены"))
                    else:
                        st.error(bi("Kolonlar eksik","Не хватает колонок"))
                elif "Rol" in mapping:
                    need = ["Rol","Ağırlık (Вес)","Net Maaş (₽)"]
                    if all(c in df.columns for c in need):
                        roles_df = st.session_state.get("roles_df", get_default_roles_df())
                        for _, r in df.iterrows():
                            name = str(r["Rol"]).strip()
                            w = float(r["Ağırlık (Вес)"])
                            net = float(r["Net Maaş (₽)"])
                            if name in roles_df["Rol"].values:
                                roles_df.loc[roles_df["Rol"]==name,["Ağırlık (Вес)","Net Maaş (₽)"]] = [w, net]
                            else:
                                roles_df.loc[len(roles_df)] = [name,w,net]
                        st.session_state["roles_df"] = roles_df
                        st.success(bi("Roller güncellendi","Роли обновлены"))
                    else:
                        st.error(bi("Kolonlar eksik","Не хватает колонок"))
                else:
                    st.info(bi("Özel gider aktarımı için ayrı şablon eklenecek","Шаблон импорта прочих затрат будет добавлен"))
        except Exception as e:
            st.error(bi(f"İçe aktarma hatası: {e}", f"Ошибка импорта: {e}"))

    st.markdown("---")
    bih("📊 Yönetici Özeti Excel","📊 Excel-отчёт для руководства", level=4)
    bitr("Şık formatlı özet rapor üretir.","Генерирует красиво оформленный сводный отчёт.")

    if st.button(bi("📥 Excel üret","📥 Сформировать Excel"), type="primary"):
        try:
            buf = io.BytesIO()
            with ExcelWriter(buf, engine="xlsxwriter") as xw:
                wb = xw.book
                fmt_title = wb.add_format({"bold": True, "font_size": 18})
                fmt_kpi = wb.add_format({"bold": True, "font_size": 14, "bg_color": "#EEF3FF", "border":1})
                fmt_hdr = wb.add_format({"bold": True, "bg_color": "#DCE6F1", "border":1})
                fmt_num = wb.add_format({"num_format": "#,##0.00", "border":1})
                fmt_int = wb.add_format({"num_format": "#,##0", "border":1})

                sheet = wb.add_worksheet("Summary")
                sheet.write(0,0, bi("İşçilik Özet Raporu","Сводный отчёт по трудозатратам"), fmt_title)

                data = st.session_state.get("calculation_results") or {}
                total_pm = float(data.get("person_months_total", 0.0))
                roles_df = st.session_state.get("roles_calc_df", pd.DataFrame())
                elements_df = st.session_state.get("elements_df", pd.DataFrame())

                sheet.write(2,0, bi("Toplam Adam-Ay","Всего чел-месяцев"), fmt_kpi); sheet.write_number(2,1, total_pm, fmt_kpi)
                sheet.write(3,0, bi("Rol sayısı","Кол-во ролей"), fmt_kpi); sheet.write_number(3,1, 0 if roles_df is None or roles_df is pd.DataFrame() or roles_df.empty else len(roles_df), fmt_kpi)
                sheet.write(4,0, bi("Eleman sayısı","Кол-во элементов"), fmt_kpi); sheet.write_number(4,1, 0 if elements_df is None or elements_df is pd.DataFrame() or elements_df.empty else len(elements_df), fmt_kpi)

                if elements_df is not None and not elements_df.empty:
                    elements_df.to_excel(xw, sheet_name="Elements", index=False)
                    ws = xw.sheets["Elements"]
                    for col_i, col in enumerate(elements_df.columns):
                        ws.write(0, col_i, str(col), fmt_hdr)
                    ws.set_column(0, len(elements_df.columns)-1, 18, fmt_num)
                if roles_df is not None and not roles_df.empty:
                    roles_df.to_excel(xw, sheet_name="Roles", index=False)
                    ws2 = xw.sheets["Roles"]
                    for col_i, col in enumerate(roles_df.columns):
                        ws2.write(0, col_i, str(col), fmt_hdr)
                    ws2.set_column(0, len(roles_df.columns)-1, 18, fmt_num)

            st.download_button(
                bi("📥 Excel İndir (.xlsx)","📥 Скачать Excel (.xlsx)"),
                data=buf.getvalue(),
                file_name="yonetici_ozeti.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
        except Exception as e:
            st.error(bi(f"Excel oluşturma hatası: {e}", f"Ошибка формирования Excel: {e}"))
# ==================== 7) ASİSTAN: GPT Öneri + Oran Kontrol + RAG + DEV CONSOLE ====================
with tab_asistan:
    # ---------- GPT öneri / web doğrulama (mevcut) ----------
    st.markdown("### 🤖 GPT Öneri Pilotu")
    payload = {
        "consumables_pct": float((st.session_state.get("consumables_rate_eff",
                                st.session_state.get("consumables_rate", CONSUMABLES_RATE_DEFAULT/100.0)))*100.0),
        "overhead_pct": float((st.session_state.get("overhead_rate_eff",
                                st.session_state.get("overhead_rate", OVERHEAD_RATE_DEFAULT)))*100.0),
        "hours_per_day": float(st.session_state.get("hours_per_day",10.0)),
        "scenario": st.session_state.get("scenario","Gerçekçi"),
        "difficulty_multiplier": float(
            (1+st.session_state.get("f_winter",0.0))*
            (1+st.session_state.get("f_heavy",0.0))*
            (1+st.session_state.get("f_repeat",0.0))*
            (1+st.session_state.get("f_shared",0.0))*
            (1+st.session_state.get("f_cong",0.0))*
            (1+st.session_state.get("f_pump",0.0))
        )
    }
    gpt_can = get_openai_client() is not None
    colg1, colg2 = st.columns(2)
    with colg1:
        if st.button("🤖 GPT'den öneri iste", disabled=not gpt_can):
            resp = gpt_propose_params(payload)
            if not resp:
                st.error("GPT önerisi alınamadı.")
            else:
                st.json(resp)
                if st.button("✅ Önerileri uygula (üstteki oranları günceller)"):
                    st.session_state["consumables_rate"] = float(resp.get("consumables_pct", payload["consumables_pct"]))
                    st.session_state["overhead_rate"]    = float(resp.get("overhead_pct", payload["overhead_pct"]))
                    st.session_state["hours_per_day"]    = float(resp.get("hours_per_day", payload["hours_per_day"]))
                    st.session_state["scenario"]         = str(resp.get("scenario", payload["scenario"]))
                    st.success("Uygulandı. Yeniden hesaplayın (alt sekmede).")
    with colg2:
        if st.button("🌐 İnternetten oranları kontrol et (beta)", disabled=not gpt_can):
            queries=[
                "Россия страховые взносы 2024 ОПС ОСС ОМС проценты",
                "НСиПЗ тариф 2024 Россия производственный травматизм",
                "НДФЛ ставка Россия 2024",
                "патент мигранты стоимость в месяц 2024 Россия",
                "VKS страховые взносы Россия 2024",
            ]
            found = gpt_verify_rates_via_web(queries)
            if found:
                st.json(found)
            else:
                st.warning("Çevrimiçi doğrulama yapılamadı (ya da anahtar eksik).")

    # ---------- RAG ----------
    bih("📚 RAG: Dosya yükle → indeksle → ara","📚 RAG: загрузить → проиндексировать → искать", level=3)
    uploads = st.file_uploader(bi("Dosya yükle (.txt, .csv, .xlsx)","Загрузить файлы (.txt, .csv, .xlsx)"), type=["txt","csv","xlsx"], accept_multiple_files=True, key="rag_up")
    cR1, cR2, cR3 = st.columns(3)
    with cR1:
        if st.button(bi("📥 İndeksle (Embed + Kaydet)","📥 Проиндексировать (эмбед + сохранить)")):
            if not uploads:
                st.warning(bi("Dosya seçin.","Выберите файл(ы)."))
            else:
                chunks=[]
                for up in uploads: chunks += file_to_chunks(up)
                if not chunks:
                    st.warning(bi("Parça yok.","Нет фрагментов."))
                else:
                    texts=[c["text"] for c in chunks]
                    embs=embed_texts(texts)
                    if not embs:
                        st.error(bi("Embed alınamadı (OpenAI anahtarı gerekli).","Не удалось получить эмбеддинги (нужен ключ OpenAI)."))
                    else:
                        recs=[{"id":str(uuid.uuid4()),"text":t,"embedding":e,"meta":c.get("meta",{})} for t,e,c in zip(texts,embs,chunks)]
                        save_rag_records(recs); st.success(bi(f"İndekslendi: {len(recs)}", f"Проиндексировано: {len(recs)}"))
    with cR2:
        if st.button(bi("🧹 RAG temizle","🧹 Очистить RAG")):
            ensure_rag_dir()
            try:
                if os.path.exists(RAG_FILE): os.remove(RAG_FILE)
                open(RAG_FILE,"a").close()
                st.success(bi("RAG temizlendi.","RAG очищен."))
            except Exception as e:
                st.error(bi(f"Hata: {e}", f"Ошибка: {e}"))
    with cR3:
        q = st.text_input(bi("🔎 RAG' de ara","🔎 Поиск в RAG"), value=st.session_state.get("rag_q",""))
        if st.button(bi("Ara","Найти"), key="rag_search_btn"):
            hits = rag_search(q.strip(), topk=6) if q.strip() else []
            st.session_state["rag_hits"] = hits or []
    for it in st.session_state.get("rag_hits", []):
        st.caption(f"• {it.get('meta',{}).get('filename','?')} — {it.get('meta',{})}")
        st.code(it.get("text","")[:700])

    # ---------- 💬 GPT Dev Console (Kod Yöneticisi) ----------
    bih("💬 GPT Dev Console (Kod Yöneticisi)","💬 GPT Dev Console (управление кодом)", level=3)
    st.caption("Buradan GPT'ye doğal dille komut ver: değişiklik teklifini JSON patch olarak çıkarır; **sen onaylamadan uygulanmaz**.")

    # küçük yardımcılar (lokal — Part 1'e dokunmuyoruz)
    import difflib
    from datetime import datetime

    def _read_text(p):
        try:
            with open(p, "r", encoding="utf-8") as f: return f.read()
        except Exception as e:
            return ""

    def _write_text(p, s):
        with open(p, "w", encoding="utf-8") as f: f.write(s)

    def _ts():
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _ensure_dir(d):
        os.makedirs(d, exist_ok=True)

    def _diff(a, b, fname="app.py"):
        return "".join(difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True),
                                            fromfile=f"{fname} (old)", tofile=f"{fname} (new)"))

    def _extract_section(full_text:str, part_tag:str):
        tags = {
            "PART1": "# app.py — PART 1/3",
            "PART2": "# ========= PART 2/3",
            "PART3": "# ========= PART 3/3",
        }
        start_tag = tags["PART1"]
        mid_tag   = tags["PART2"]
        end_tag   = tags["PART3"]
        start = 0; end = len(full_text)
        if part_tag == "PART1":
            start = full_text.find(start_tag)
            end   = full_text.find(mid_tag)
        elif part_tag == "PART2":
            start = full_text.find(mid_tag)
            end   = full_text.find(end_tag)
        elif part_tag == "PART3":
            start = full_text.find(end_tag)
            end   = len(full_text)
        else:
            return full_text, 0, len(full_text), start_tag, end_tag
        if start < 0: start = 0
        if end < 0: end = len(full_text)
        return full_text[start:end], start, end, (mid_tag if part_tag!="PART1" else start_tag), (end_tag if part_tag!="PART3" else "")

    # hedef dosya seçimi
    default_target = st.session_state.get("TARGET_FILE", os.path.abspath(__file__))
    st.session_state["TARGET_FILE"] = st.text_input("🎯 Hedef dosya yolu", value=default_target, key="target_file_inp")
    target_path = st.session_state["TARGET_FILE"]
    file_text = _read_text(target_path)
    st.caption(f"Dosya uzunluğu: {len(file_text):,} karakter".replace(",", " "))

    # seçenekler
    part_choice = st.selectbox(bi("Değişiklik kapsamı","Область изменений"), ["PART2 (UI)", "PART1 (Helpers/Tax/Logic)", "PART3 (Hesap/Çıktı)", "WHOLE FILE"], index=0)
    part_key = {"PART2 (UI)":"PART2","PART1 (Helpers/Tax/Logic)":"PART1","PART3 (Hesap/Çıktı)":"PART3","WHOLE FILE":"WHOLE"}[part_choice]

    protect_crit = st.toggle(bi("🛡️ Kritik alanları koru (vergi/prim sabitleri vs.)","🛡️ Защитить критичные разделы (ставки налогов/взносов и т.п.)"), value=st.session_state.get("protect_crit", True))
    st.session_state["protect_crit"] = protect_crit
    dry_run = st.toggle(bi("🧪 Önce sandboxa yaz (dry-run)","🧪 Сначала записать в песочницу (dry-run)"), value=st.session_state.get("dry_run", True))
    st.session_state["dry_run"] = dry_run

    # bağlamı oluştur
    if part_key == "WHOLE":
        ctx = file_text
        start_marker = "# app.py — PART 1/3"
        end_marker   = ""  # dosya sonu
    else:
        ctx, start_idx, end_idx, start_marker, end_marker = _extract_section(file_text, part_key)

    guard_text = (
        "Kurallar:\n"
        "1) Değişiklik teklifini sadece JSON olarak ver: {\"notes\":\"...\",\"changes\":[{\"mode\":\"replace_between\",\"start_marker\":\"...\",\"end_marker\":\"...\",\"new_text\":\"...\",\"file\":\"optional\"}]}.\n"
        "2) Mümkünse **replace_between** módunu kullan; start_marker ve end_marker açıkça ver.\n"
        "3) new_text, tam yeni bölüm içeriği olsun (eksiksiz, çalışır durumda).\n"
        "4) Vergi/sigorta sabitlerine dokunma, sadece istenirse (protect_crit=True ise ASLA dokunma).\n"
        "5) Kod stilini ve mevcut API'leri koru; Streamlit widget key'leri reset yaratmayacak şekilde kullan.\n"
    )
    if protect_crit:
        guard_text += "6) NDFL_*, OPS/OSS/OMS, NSIPZ_*, SNG_PATENT_MONTH, *_TAXED_BASE, CASH_COMMISSION_RATE sabitlerini DEĞİŞTİRME.\n"

    bih("🗣️ GPT'ye Komutun","🗣️ Команда для GPT", level=4)
    user_cmd = st.text_area(bi("Prompt","Промпт"), height=160, key="dev_prompt",
                            placeholder="Örn: 'Asistan sekmesindeki RAG bloğunun başına kısa bir açıklama ekle ve tablo fontunu %10 büyüt.' / Пример: 'Добавь краткое описание в блок RAG и увеличь шрифт таблицы на 10%'.")

    if st.button(bi("🧩 Patch Önerisi Üret","🧩 Сгенерировать patch"), disabled=not gpt_can):
        if not user_cmd.strip():
            st.warning(bi("Bir komut yaz.","Введите команду."))
        else:
            client = get_openai_client()
            try:
                system = "Kıdemli Python/Streamlit geliştiricisisin. Sadece JSON döndür, açıklama yok."
                user = (
                    f"HEDEF DOSYA: {os.path.basename(target_path)}\n"
                    f"KAPSAM: {part_key}\n"
                    f"START_MARKER: {start_marker}\nEND_MARKER: {end_marker or '[EOF]'}\n\n"
                    f"MEVCUT BÖLÜM İÇERİĞİ (referans):\n```\n{ctx[:20000]}\n```\n\n"
                    f"{guard_text}\n"
                    f"KULLANICI İSTEK:\n{user_cmd}"
                )
                r = client.chat.completions.create(
                    model="gpt-4o-mini", temperature=0.2,
                    messages=[{"role":"system","content":system},{"role":"user","content":user}]
                )
                raw = r.choices[0].message.content or "{}"
                try:
                    st.session_state["dev_patch_json"] = json.loads(extract_json_block(raw))
                    st.success(bi("Patch alındı.","Patch получен."))
                except Exception:
                    st.error(bi("JSON parse edilemedi. Dönen içerik:","JSON не разобран. Ответ:"))
                    st.code(raw)
            except Exception as e:
                st.error(f"Hata: {e}")

    patch = st.session_state.get("dev_patch_json")
    if patch:
        bih("📦 Patch JSON","📦 Патч JSON", level=4)
        st.code(json.dumps(patch, ensure_ascii=False, indent=2), language="json")

        # tek dosya/sıralı replace_between desteği
        new_text_total = file_text
        try:
            for ch in patch.get("changes", []):
                mode = ch.get("mode","replace_between")
                fpath = ch.get("file", target_path)
                if fpath != target_path:
                    st.warning(f"Şimdilik tek dosya uygulanıyor: {os.path.basename(target_path)}. (İstekte: {fpath})")
                if mode == "replace_between":
                    sm = ch.get("start_marker") or start_marker
                    em = ch.get("end_marker") or (end_marker if end_marker else None)
                    block = ch.get("new_text","")
                    txt = new_text_total
                    sidx = txt.find(sm) if sm else 0
                    eidx = (txt.find(em, sidx) if em else len(txt))
                    if sidx < 0: sidx = 0
                    if eidx < 0: eidx = len(txt)
                    new_text_total = txt[:sidx] + (sm + "\n" if sm and not txt[sidx:sidx+len(sm)]==sm else "") + block + ("\n"+em if em else "") + txt[eidx + (len(em) if em else 0):]
                elif mode == "whole_file":
                    new_text_total = ch.get("new_text","")
                else:
                    st.error(f"Desteklenmeyen mode: {mode}")

            diff = _diff(file_text, new_text_total, fname=os.path.basename(target_path))
            bih("🧮 Diff","🧮 Разница", level=4)
            st.code(diff or "# (fark yok)")

            # Uygula / İptal
            cA, cB, cC = st.columns(3)
            with cA:
                if st.button(bi("✅ Uygula (yedek alarak)","✅ Применить (с резервной копией)")):
                    # yedekle
                    bdir = "_gpt_backups"; _ensure_dir(bdir)
                    bname = f"{os.path.basename(target_path)}.{_ts()}.bak"
                    with open(os.path.join(bdir, bname), "w", encoding="utf-8") as f:
                        f.write(file_text)
                    # dry-run mı?
                    if dry_run:
                        sdir = "_gpt_sandboxes"; _ensure_dir(sdir)
                        sname = f"{os.path.basename(target_path)}.sandbox.{_ts()}"
                        with open(os.path.join(sdir, sname), "w", encoding="utf-8") as f:
                            f.write(new_text_total)
                        st.success(f"Sandbox'a yazıldı: {os.path.join(sdir, sname)}")
                    else:
                        _write_text(target_path, new_text_total)
                        st.success("Uygulandı. (Değişiklikler aktif olması için uygulamayı yeniden çalıştırmanız gerekebilir.)")
                        # Tabloları korumak için rerun kullanmıyoruz
            with cB:
                if st.button(bi("🗑️ Patch'i sil","🗑️ Удалить patch")):
                    st.session_state.pop("dev_patch_json", None)
                    st.info(bi("Patch silindi.","Patch удалён."))
            with cC:
                if st.button(bi("↩️ Son yedeği geri yükle","↩️ Восстановить последний бэкап")):
                    bdir = "_gpt_backups"
                    if not os.path.isdir(bdir):
                        st.warning(bi("Yedek klasörü yok.","Папка с бэкапами отсутствует."))
                    else:
                        files = sorted([f for f in os.listdir(bdir) if f.startswith(os.path.basename(target_path))], reverse=True)
                        if not files:
                            st.warning(bi("Geri yüklenecek yedek bulunamadı.","Нет резервных копий для восстановления."))
                        else:
                            last_bak = os.path.join(bdir, files[0])
                            _write_text(target_path, _read_text(last_bak))
                            st.success(bi(f"Geri yüklendi: {files[0]}", f"Восстановлено: {files[0]}"))
                            # Tabloları korumak için rerun kullanmıyoruz
        except Exception as e:
            st.error(f"Patch uygula hazırlığında hata: {e}")


# ========= PART 3/3 — HESAPLAR, TABLOLAR, GRAFİK, ÇIKTILAR =========

# --- Güvenli yardımcı: KDV ayrıştırma (Part 1'de yoksa ekle) ---
if "net_of_vat" not in globals():
    def net_of_vat(x, tick):
        vr = float(st.session_state.get("vat_rate", 0.20))
        return float(x)/(1.0+vr) if bool(tick) else float(x)

# KDV ayrıştırma (işaretli ise) - Part 3 için
def net_of_vat_part3(x, tick):
    vat_rate = float(st.session_state.get("vat_rate", 0.20))
    return x / (1 + vat_rate) if tick else x

# --- Kısa alias'lar (state'ten oku) ---
PRIM_SNG = bool(st.session_state.get("prim_sng", True))
PRIM_TUR = bool(st.session_state.get("prim_tur", True))

start_date   = st.session_state.get("start_date", date.today().replace(day=1))
end_date     = st.session_state.get("end_date", date.today().replace(day=30))
holiday_mode = st.session_state.get("holiday_mode","her_pazar")
hours_per_day= float(st.session_state.get("hours_per_day",10.0))
scenario     = st.session_state.get("scenario","Gerçekçi")



# --- Difficulty multiplier: single source of truth ---
def get_difficulty_multiplier_cached() -> float:
    try:
        return float(st.session_state.get("_diff_total_mult_cache", 1.0))
    except Exception:
        return 1.0

# Zorluk çarpanı tek merkezden hesaplanır ve cache'e yazılır
z_mult = get_difficulty_multiplier_cached()
difficulty_multiplier = z_mult  # norm hesapları bunu kullanıyor

# Seçili elemanlar
selected_elements = [k for k in ELEMENT_ORDER if st.session_state.get(f"sel_{k}", True)]
if not selected_elements:
    st.warning("En az bir betonarme eleman seçin."); st.stop()

# Norm çarpanlarını oluştur
as_norm_base_from_scn, norm_mult = build_norms_for_scenario(scenario, selected_elements)

# Metraj DF (varsa)
use_metraj = bool(st.session_state.get("use_metraj", False))
metraj_df  = st.session_state.get("metraj_df", pd.DataFrame(columns=["Eleman (Элемент)","Metraj (m³) (Объём, м³)"]))

# Global kişi-başı giderleri (KDV ayrıştır)
food       = float(st.session_state.get("food",10000.0))
food_vat   = bool(st.session_state.get("food_vat",True))
ppe        = float(st.session_state.get("ppe",1500.0))
ppe_vat    = bool(st.session_state.get("ppe_vat",True))
lodging    = float(st.session_state.get("lodging",12000.0))
lodging_vat= bool(st.session_state.get("lodging_vat",True))
training   = float(st.session_state.get("training",500.0))
training_vat=bool(st.session_state.get("training_vat",True))
transport  = float(st.session_state.get("transport",3000.0))
transport_vat=bool(st.session_state.get("transport_vat",False))

extras_per_person = sum([
    net_of_vat_part3(food,food_vat),
    net_of_vat_part3(lodging,lodging_vat),
    net_of_vat_part3(transport,transport_vat),
    net_of_vat_part3(ppe,ppe_vat),
    net_of_vat_part3(training,training_vat),
])

# Manuel üst oranlar (sade): sarf, overhead, indirect
# Not: Part 2'deki "override" açıksa *_eff değerleri yazılmış olur; onları kullan.
consumables_rate = float(st.session_state.get("consumables_rate", CONSUMABLES_RATE_DEFAULT/100.0))          # 0..1
overhead_rate    = float(st.session_state.get("overhead_rate", OVERHEAD_RATE_DEFAULT))                      # 0..1
ind_rows         = st.session_state.get("ind_rows", [])
ind_custom_rows  = st.session_state.get("ind_custom_rows", [])

# Etkili oranlar (override'a bak)
# Session state'ten oranları al (0-1), yoksa default kullan
consumables_rate_eff = float(st.session_state.get("consumables_rate", CONSUMABLES_RATE_DEFAULT/100.0))
overhead_rate_eff    = float(st.session_state.get("overhead_rate", OVERHEAD_RATE_DEFAULT/100.0))

# Indirect toplamını topla (sade)
indirect_rate_total = 0.0
for r in ind_rows:
    if r.get("On"): indirect_rate_total += float(r.get("Rate",0.0))
for rr in ind_custom_rows:
    if rr.get("on"): indirect_rate_total += float(rr.get("rate",0.0))
# override varsa geç
indirect_rate_total = float(st.session_state.get("indirect_rate_total_eff", indirect_rate_total))

# ----------------- TAKVİM & SAATLER -----------------

# Tatil günleri değişikliğinde hesaplamaları güncelle
if st.session_state.get("_holiday_mode_changed", False):
    st.session_state["_holiday_mode_changed"] = False

# Tarih hesaplama mantığı: Her zaman gerçek tarihler kullan, metraj sadece m³ değerlerini etkiler
workdays = workdays_between(start_date, end_date, holiday_mode)
project_days = max((end_date-start_date).days+1, 1)

avg_workdays_per_month = workdays * 30.0 / project_days
hours_per_person_month = max(avg_workdays_per_month * hours_per_day, 1e-9)
                    
# ----------------- NORM × METRAJ -----------------
if use_metraj and not metraj_df.empty:
    iterable = metraj_df.to_dict(orient="records"); col_ele="Eleman (Элемент)"; col_met="Metraj (m³) (Объём, м³)"
else:
    iterable = [{"Eleman (Элемент)": LABELS[k], "Metraj (m³) (Объём, м³)": 1.0} for k in selected_elements]
    col_ele="Eleman (Элемент)"; col_met="Metraj (m³) (Объём, м³)"

# Rusça etiketleri Türkçe anahtarlara eşleştir - Part 3 için
russian_to_turkish_part3 = {
    "Колонна": "kolon", "Балка": "kiriş", "Плита": "döşeme", 
    "Стена": "duvar", "Лестница": "merdiven", "Фундамент": "temel", 
    "Крыша": "çatı", "Лифт": "asansör", "Подбетонка": "grobeton",
    "Плита перекрытия": "döşeme", "Стена/диафрагма": "perde",
    # Tam etiket eşleştirmesi - metraj_df'den gelen tam etiketler için
    "Grobeton (Подбетонка)": "grobeton",
    "Temel (Фундамент)": "temel",
    "Döşeme (Плита перекрытия)": "döşeme",
    "Perde (Стена/диафрагма)": "perde",
    "Merdiven (Лестница)": "merdiven"
}

norms_used={}; total_metraj=0.0; total_adamsaat=0.0
for r in iterable:
    lbl=str(r[col_ele]); met=float(r.get(col_met,0.0) or 0.0)
    
    # Rusça etiketi Türkçe anahtara çevir
    turkish_key = russian_to_turkish_part3.get(lbl, lbl)
    norm_multiplier = norm_mult.get(turkish_key, 1.0)
    
    n_e = as_norm_base_from_scn * norm_multiplier * get_difficulty_multiplier_cached()
    norms_used[lbl]=n_e
    total_metraj += met
    total_adamsaat += met*n_e

# ----------------- ROL MALİYETİ (PRIM VERGİSİ YOK!) -----------------
# ÖNEMLİ: Sigorta/vakıf kesintileri yalnız "resmî brüt" üzerinden; 'prim' (elden) kısma vergi/prim YOK.

# Global kişi-başı giderler (giderler sekmesinden hesapla)
food = float(st.session_state.get("food", 10000.0))
lodging = float(st.session_state.get("lodging", 12000.0))
transport = float(st.session_state.get("transport", 3000.0))
ppe = float(st.session_state.get("ppe", 1500.0))
training = float(st.session_state.get("training", 500.0))

# KDV işaretleri (giderler sekmesinden al)
food_vat = bool(st.session_state.get("food_vat", True))
lodging_vat = bool(st.session_state.get("lodging_vat", True))
transport_vat = bool(st.session_state.get("transport_vat", False))
ppe_vat = bool(st.session_state.get("ppe_vat", True))
training_vat = bool(st.session_state.get("training_vat", True))

# KDV ayrıştırma (işaretli ise)
def net_of_vat(x, tick):
    vat_rate = float(st.session_state.get("vat_rate", 0.20))
    return x / (1 + vat_rate) if tick else x

# Toplam KDV'siz ek giderler (gerçek değerlerden hesapla)
extras_per_person = sum([
    net_of_vat(food, food_vat),           # Yemek
    net_of_vat(lodging, lodging_vat),     # Barınma
    net_of_vat(transport, transport_vat), # Ulaşım
    net_of_vat(ppe, ppe_vat),            # PPE
    net_of_vat(training, training_vat)    # Eğitim
])

roles_df = st.session_state.get("roles_df", pd.DataFrame())
sum_w = float(roles_df["Ağırlık (Вес)"].clip(lower=0.0).sum()) if not roles_df.empty else 0.0
month_wd_df = workdays_in_month_range(start_date, end_date, holiday_mode)
n_months = len(month_wd_df) if not month_wd_df.empty else 1
person_months_total = total_adamsaat / hours_per_person_month



# YENİ ADAM-SAAT ALGORİTMASI - KURŞUN GEÇİRMEZ (Part 3 için)
# 1. Ülke yüzdelerini normalize et (0-0-0 ise eşit böl)
def _normalize_country_part3(p_rus, p_sng, p_tur):
    vals = [max(float(p_rus), 0.0), max(float(p_sng), 0.0), max(float(p_tur), 0.0)]
    s = sum(vals)
    if s <= 0:  # hepsi 0 ise eşit böl
        return (1/3.0, 1/3.0, 1/3.0)
    return (vals[0]/s, vals[1]/s, vals[2]/s)

# 2. A·S fiyatını doğrudan hesapla (ara "kişi-ay" dağıtımı olmadan)
M_with = 0.0
M_bare = 0.0

if not roles_df.empty and sum_w > 0:
    for _, row in roles_df.iterrows():
        share = float(row["Ağırlık (Вес)"]) / sum_w
        
        # Ülke yüzdelerini normalize et
        p_rus, p_sng, p_tur = _normalize_country_part3(row["%RUS"], row["%SNG"], row["%TUR"])
        
        # monthly_role_cost_multinational fonksiyonunu kullan
        with_ex = monthly_role_cost_multinational(row, PRIM_SNG, PRIM_TUR, extras_per_person)["per_person"]["BLENDED"]
        bare_ex = monthly_role_cost_multinational(row, PRIM_SNG, PRIM_TUR, 0.0)["per_person"]["BLENDED"]
        
        M_with += share * with_ex
        M_bare += share * bare_ex

# 3. A·S fiyatları - TEK SATIRLIK FORMÜL
with_extras_as_price = M_with / hours_per_person_month
bare_as_price = M_bare / hours_per_person_month



# --- NEW: price follows productivity (scenario + difficulty) ---
s_mult  = get_scenario_multiplier_for_price(scenario)   # senaryo etkisi
z_mult  = get_difficulty_multiplier_cached()            # çevresel zorluk etkisi
# Ayarlanabilir katsayılar (ileride istersen UI ekleyebilirsin)
BETA_SCENARIO_TO_PRICE  = 1.0    # 0..1 (1=tam, 0=sızdırma)
BETA_DIFFICULTY_TO_PRICE= 1.0    # 0..1 (1=tam, 0=sızdırma)

price_mult = (1 + BETA_SCENARIO_TO_PRICE  * (s_mult - 1)) \
           * (1 + BETA_DIFFICULTY_TO_PRICE* (z_mult - 1))
bare_as_price        *= price_mult
with_extras_as_price *= price_mult

core_as_price        = with_extras_as_price  # m³ maliyetleri bu fiyattan üretilecek



# Part 3 hesaplama tamamlandı

# Roller hesaplama tablosu için
roles_calc = []
if not roles_df.empty and sum_w > 0:
    for _, row in roles_df.iterrows():
        w = max(float(row["Ağırlık (Вес)"]), 0.0)
        share = (w / sum_w)
        persons_role = (person_months_total / n_months) * share
        
        # Ülke yüzdelerini normalize et
        p_rus, p_sng, p_tur = _normalize_country_part3(row["%RUS"], row["%SNG"], row["%TUR"])
        
        # monthly_role_cost_multinational kullan
        bundle_with = monthly_role_cost_multinational(row, PRIM_SNG, PRIM_TUR, extras_per_person)
        bundle_bare = monthly_role_cost_multinational(row, PRIM_SNG, PRIM_TUR, 0.0)
        per_with = bundle_with["per_person"]["BLENDED"]
        per_bare = bundle_bare["per_person"]["BLENDED"]
        
        roles_calc.append({
            "Rol (Роль)": row["Rol (Роль)"],
            "Ağırlık (Вес)": w,
            "Pay (%) (Доля, %)": f"{share * 100:.2f}",
            "Ortalama Kişi (Средняя численность)": f"{persons_role:.3f}",
            "Maliyet/ay (₽)": f"{per_with:,.2f}",
            "%RUS": f"{p_rus * 100:.1f}",
            "%SNG": f"{p_sng * 100:.1f}",
            "%TUR": f"{p_tur * 100:.1f}",
            "Net Maaş (₽/ay)": f"{float(row.get('Net Maaş (₽, na ruki) (Чистая з/п, ₽)', 0)):,.0f}"
        })
roles_calc_df = pd.DataFrame(roles_calc)

# ----------------- m³ MALİYETLERİ - ELEMAN ÖZGÜ NORM KULLAN -----------------
sum_core_overhead_total=0.0; tmp_store=[]
for r in iterable:
    lbl=str(r[col_ele]); met=float(r.get(col_met,0.0) or 0.0); n=norms_used[lbl]
    core_m3  = with_extras_as_price * n  # Eleman özgü norm ile çarp
    genel_m3 = min(max(overhead_rate_eff,0.0), OVERHEAD_RATE_MAX/100.0) * core_m3  # üst sınır
    total_m3_core_genel = core_m3 + genel_m3
    sum_core_overhead_total += total_m3_core_genel * met
    tmp_store.append((lbl,met,total_m3_core_genel,core_m3,genel_m3,n))
                    
# Sarf + Indirect - (core_m3 + genel_m3) * metraj tabanı üzerinden
consumables_total = sum_core_overhead_total * max(consumables_rate_eff,0.0)
indirect_total    = (sum_core_overhead_total + consumables_total) * max(indirect_rate_total,0.0)
                    
# Elemanlara dağıt - oransal dağıtım
elem_rows=[]; project_total_cost=0.0
for (lbl,met,base_total,core_m3,genel_m3,n) in tmp_store:
    # Sarf ve indirect dağıtımı: (core_m3 + genel_m3) * metraj oranı
    weight    = (base_total*met) / max(sum_core_overhead_total,1e-9)
    sarf_alloc  = consumables_total * weight
    indir_alloc = indirect_total    * weight
    sarf_m3  = sarf_alloc / max(met,1e-9) if met > 0 else 0.0
    indir_m3 = indir_alloc/ max(met,1e-9) if met>0 else 0.0
    total_m3 = core_m3 + genel_m3 + sarf_m3 + indir_m3
    project_total_cost += total_m3 * max(met,0.0)
    elem_rows.append({
        "Eleman (Элемент)":lbl,
        "Norm (a·s/m³) (Норма, чел·ч/м³)":f"{n:.2f}",
        "Metraj (m³) (Объём, м³)":f"{met:,.3f}",
        "Çekirdek (₽/м³) (Ядро, ₽/м³)":f"{core_m3:,.2f}",
        "Genel (₽/м³) (Накладные, ₽/м³)":f"{genel_m3:,.2f}",
        "Sarf (₽/м³) (Расходники, ₽/м³)":f"{sarf_m3:,.2f}",
        "Indirect (₽/м³) (Косвенные, ₽/м³)":f"{indir_m3:,.2f}",
        "Toplam (₽/м³) (Итого, ₽/м³)":f"{total_m3:,.2f}"
    })
elements_df = pd.DataFrame(elem_rows)

# Özet metrikler
general_avg_m3      = project_total_cost / max(total_metraj,1e-9) if total_metraj>0 else 0.0
fully_loaded_as_price = project_total_cost / max(total_adamsaat,1e-9) if total_adamsaat>0 else 0.0
avg_norm_per_m3       = total_adamsaat / max(total_metraj,1e-9) if total_metraj>0 else 0.0
indirect_share        = indirect_total / max(project_total_cost,1e-9) if project_total_cost>0 else 0.0

# ----------------- PARABOLİK MANPOWER DAĞILIMI (Part 3 için) -----------------
# Şantiye gerçeklerine uygun parabolik dağıtım
def parabolic_distribution_part3(n_months):
    if n_months <= 1:
        return [1.0]
    
    # Parabolik ağırlıklar: y = -4(x-0.5)² + 1
    # Başlangıç: düşük, orta: yüksek, son: düşük
    weights = []
    for i in range(n_months):
        x = i / (n_months - 1) if n_months > 1 else 0.5
        weight = -4 * (x - 0.5)**2 + 1
        weights.append(max(weight, 0.1))  # Minimum %10
    
    # Toplamı 1'e normalize et
    total_weight = sum(weights)
    normalized_weights = [w / total_weight for w in weights]
    return normalized_weights

# Parabolik dağıtım hesapla
if not month_wd_df.empty:
    n_months_part3 = len(month_wd_df)
    weights_part3 = parabolic_distribution_part3(n_months_part3)
    headcounts_float_part3 = [person_months_total * wi for wi in weights_part3]
    
    # Toplam adam-ay korunmalı - yuvarlama hatası düzeltmesi
    def round_preserve_sum_part3(values):
        rounded = [round(v) for v in values]
        total_diff = sum(values) - sum(rounded)
        
        if abs(total_diff) >= 1:
            diffs = [(i, abs(v - r)) for i, (v, r) in enumerate(zip(values, rounded))]
            diffs.sort(key=lambda x: x[1], reverse=True)
            
            for i, _ in diffs:
                if total_diff > 0:
                    rounded[i] += 1
                    total_diff -= 1
                elif total_diff < 0:
                    rounded[i] -= 1
                    total_diff += 1
                if abs(total_diff) < 1:
                    break
        
        return rounded
    
    headcounts_int_part3 = round_preserve_sum_part3(headcounts_float_part3)
    
    # month_wd_df'ye parabolik dağıtım ekle
    month_wd_df["Manpower (Численность)"] = headcounts_int_part3

# Sonuçlar yeni sekmede gösterilecek