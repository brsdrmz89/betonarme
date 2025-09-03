# app.py — PART 1/3
# -*- coding: utf-8 -*-
from __future__ import annotations

import os, io, json, math, uuid, hashlib, requests  # pyright: ignore[reportMissingModuleSource]
import numpy as np  # pyright: ignore[reportMissingImports]
import streamlit as st  # pyright: ignore[reportMissingImports]
import pandas as pd  # pyright: ignore[reportMissingImports]
from datetime import date, timedelta
from pandas import ExcelWriter  # pyright: ignore[reportMissingImports]
import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports]
# FAISS opsiyonel import
try:
    import faiss
    _FAISS_AVAILABLE = True
except Exception:
    _FAISS_AVAILABLE = False
    print("⚠️ FAISS kullanılamıyor. RAG özellikleri devre dışı.")

# RAG backend opsiyonel import
if _FAISS_AVAILABLE:
    try:
        from rag_backend import init_backend, reset_backend, add_records, search, migrate_from_jsonl_if_needed, get_status
        _RAG_BACKEND_AVAILABLE = True
    except Exception as e:
        _RAG_BACKEND_AVAILABLE = False
        print(f"⚠️ RAG backend yüklenemedi: {e}")
else:
    _RAG_BACKEND_AVAILABLE = False

# =============== AUTO-RAG SİSTEMİ ===============
@st.cache_data(ttl=300, show_spinner=False)
def cached_rag_search(queries_hash: str, queries: List[str], k: int = 6, score_threshold: float = 0.25):
    """RAG arama sonuçlarını önbellekle"""
    try:
        all_results = []
        for query in queries:
            qemb = embed_texts([query])
            if qemb:
                import numpy as np
                qemb_np = np.array(qemb[0], dtype=np.float32)
                results = search(qemb_np, topk=k)
                all_results.extend(results)
        
        # Skor filtreleme ve çeşitlendirme
        filtered_results = [r for r in all_results if r['score'] >= score_threshold]
        
        # Doküman çeşitlendirmesi (her belgeden max 2 parça)
        doc_counts = {}
        diverse_results = []
        for result in filtered_results:
            doc_id = result.get('meta', {}).get('filename', 'unknown')
            if doc_counts.get(doc_id, 0) < 2:
                diverse_results.append(result)
                doc_counts[doc_id] = doc_counts.get(doc_id, 0) + 1
        
        # Basit re-ranking (tarih varsa boost)
        for result in diverse_results:
            meta = result.get('meta', {})
            if 'date' in meta or 'timestamp' in meta:
                result['score'] *= 1.05  # 5% boost
        
        return sorted(diverse_results, key=lambda x: x['score'], reverse=True)[:k*2]
    except Exception as e:
        st.error(f"RAG arama hatası: {e}")
        return []

def build_queries(state: dict) -> List[str]:
    """Mevcut duruma göre otomatik sorgular üret"""
    queries = set()
    
    # Proje bağlamı
    queries.add("Moskova betonarme işçilik birim fiyat m3")
    queries.add("Rusya şantiye maliyetleri betonarme")
    
    # Eleman bazlı sorgular
    selected_elements = []
    for element in ELEMENT_ORDER:
        if state.get(f"use_{element}", False):
            selected_elements.append(element)
            queries.add(f"{LABELS[element]} m3 işçilik normu adam*saat")
    
    # Zorluk faktörleri
    if state.get("use_winter_factor", False):
        queries.add("kış şartı işçilik verimsizlik yüzdesi beton dökümü")
        queries.add("soğuk hava beton işçilik norm artışı")
    
    if state.get("use_heavy_rebar", False):
        queries.add("ağır donatı yoğunluğu norm artışı")
        queries.add("yüksek donatı oranı işçilik zorluğu")
    
    if state.get("use_site_congestion", False):
        queries.add("şantiye sıkışıklığı işçilik verimsizlik")
        queries.add("kalabalık şantiye norm artışı")
    
    if state.get("use_pump_height", False):
        queries.add("yüksek pompa beton işçilik zorluğu")
        queries.add("pompa yüksekliği norm artışı")
    
    if state.get("use_form_repeat", False):
        queries.add("kalıp tekrarı işçilik verimsizlik")
        queries.add("tekrarlı kalıp işçilik normu")
    
    # Gider oranları
    if state.get("overhead_rate", 0) > 0:
        queries.add("şantiye genel idare gider yüzdesi betonarme")
        queries.add("overhead oranı tipik değerler")
    
    if state.get("consumables_rate", 0) > 0:
        queries.add("sarf malzemeleri oranı betonarme işçilik")
        queries.add("consumables yüzdesi referans")
    
    if state.get("indirect_rate", 0) > 0:
        queries.add("indirect giderler oranı şantiye")
        queries.add("dolaylı maliyetler yüzdesi")
    
    # Adam-saat ve çalışma koşulları
    if state.get("work_hours_per_day", 0) > 0:
        queries.add("günlük çalışma saati tipik değerler")
        queries.add("şantiye çalışma saatleri norm")
    
    if state.get("holiday_days", 0) > 0:
        queries.add("tatil gün sayısı şantiye")
        queries.add("iş günü hesaplama şantiye")
    
    # Yemek ve konaklama
    queries.add("yemek konaklama tipik tutarlar ruble/ay")
    queries.add("personel barınma yemek maliyeti")
    queries.add("şantiye yemekhane konaklama ücreti")
    
    # PPE ve eğitim
    queries.add("SİZ iş kıyafeti maliyeti")
    queries.add("personel eğitim maliyeti şantiye")
    
    # Senaryo bazlı
    scenario = state.get("scenario", "Gerçekçi")
    queries.add(f"{scenario} senaryo işçilik norm çarpanı referans")
    
    return list(queries)[:10]  # Maksimum 10 sorgu

def extract_suggestions(snippets: List[dict]) -> List[dict]:
    """LLM ile yapılandırılmış öneriler çıkar"""
    if not snippets:
        return []
    
    # Snippets'leri birleştir
    combined_text = "\n\n".join([f"Kaynak {i+1}: {s['text']}" for i, s in enumerate(snippets)])
    
    system_prompt = """Sen bir şantiye maliyet analisti olarak, verilen belgelerden sayısal değerleri çıkar ve öneriler üret.

Çıktın SADECE JSON formatında olmalı. Başka hiçbir şey yazma.

Hedef alanlar:
- winter_factor_pct: Kış faktörü yüzdesi
- heavy_rebar_pct: Ağır donatı faktörü yüzdesi  
- site_congestion_pct: Şantiye sıkışıklığı yüzdesi
- pump_height_pct: Pompa yüksekliği faktörü yüzdesi
- form_repeat_pct: Kalıp tekrarı faktörü yüzdesi
- overhead_pct: Genel giderler yüzdesi
- food_rub_month: Aylık yemek maliyeti (RUB)
- lodging_rub_month: Aylık konaklama maliyeti (RUB)
- transport_rub_month: Aylık ulaşım maliyeti (RUB)
- ppe_rub_month: Aylık SİZ maliyeti (RUB)
- training_rub_month: Aylık eğitim maliyeti (RUB)
- element_norms_m3: Eleman bazlı normlar (m3 başına adam*saat)

Her öneri için:
- field: Alan adı
- value: Sayısal değer
- unit: Birim (%, RUB, adam*saat/m3)
- source: Kaynak bilgisi
- confidence: Güven skoru (0-1)
- rationale: Gerekçe

Sadece güvenilir sayısal değerleri çıkar. Tahmin yapma."""
    
    try:
        client = openai.OpenAI(api_key=st.secrets.get("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.1,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Belgelerden öneriler çıkar:\n\n{combined_text}"}
            ]
        )
        
        result_text = response.choices[0].message.content.strip()
        
        # JSON parse et
        import json
        suggestions = json.loads(result_text)
        
        # Sadece güvenilir önerileri filtrele
        filtered_suggestions = []
        for suggestion in suggestions:
            if (isinstance(suggestion.get('value'), (int, float)) and 
                suggestion.get('confidence', 0) >= 0.55):
                filtered_suggestions.append(suggestion)
        
        return filtered_suggestions
        
    except Exception as e:
        st.warning(f"Öneri çıkarma hatası: {e}")
        return []

def run_auto_rag():
    """Auto-RAG sistemini çalıştır"""
    if not st.session_state.get("auto_rag", True):
        return
    
    try:
        # Mevcut durumu al
        current_state = {
            "use_grobeton": st.session_state.get("use_grobeton", False),
            "use_rostverk": st.session_state.get("use_rostverk", False),
            "use_temel": st.session_state.get("use_temel", False),
            "use_doseme": st.session_state.get("use_doseme", False),
            "use_perde": st.session_state.get("use_perde", False),
            "use_merdiven": st.session_state.get("use_merdiven", False),
            "use_winter_factor": st.session_state.get("use_winter_factor", False),
            "use_heavy_rebar": st.session_state.get("use_heavy_rebar", False),
            "use_site_congestion": st.session_state.get("use_site_congestion", False),
            "use_pump_height": st.session_state.get("use_pump_height", False),
            "use_form_repeat": st.session_state.get("use_form_repeat", False),
            "overhead_rate": st.session_state.get("overhead_rate", 0),
            "consumables_rate": st.session_state.get("consumables_rate", 0),
            "indirect_rate": st.session_state.get("indirect_rate", 0),
            "work_hours_per_day": st.session_state.get("work_hours_per_day", 0),
            "holiday_days": st.session_state.get("holiday_days", 0),
            "scenario": st.session_state.get("scenario", "Gerçekçi")
        }
        
        # Sorguları üret
        queries = build_queries(current_state)
        if not queries:
            return
        
        # Sorgu hash'i oluştur
        import hashlib
        queries_hash = hashlib.md5(str(sorted(queries)).encode()).hexdigest()
        
        # RAG arama yap
        snippets = cached_rag_search(queries_hash, queries)
        if not snippets:
            return
        
        # Önerileri çıkar
        suggestions = extract_suggestions(snippets)
        if suggestions:
            st.session_state["auto_rag_suggestions"] = suggestions
            st.session_state["auto_rag_snippets"] = snippets
            
    except Exception as e:
        st.error(f"Auto-RAG hatası: {e}")

def apply_suggestions(selected_suggestions: List[dict]):
    """Seçilen önerileri uygula"""
    if not selected_suggestions:
        return
    
    change_log = st.session_state.get("change_log", [])
    
    for suggestion in selected_suggestions:
        field = suggestion.get('field')
        new_value = suggestion.get('value')
        source = suggestion.get('source', 'Bilinmeyen')
        
        if not field or new_value is None:
            continue
        
        # Alan eşleştirmesi
        field_mapping = {
            'winter_factor_pct': 'winter_factor',
            'heavy_rebar_pct': 'heavy_rebar_factor',
            'site_congestion_pct': 'site_congestion_factor',
            'pump_height_pct': 'pump_height_factor',
            'form_repeat_pct': 'form_repeat_factor',
            'overhead_pct': 'overhead_rate',
            'food_rub_month': 'food_cost_month',
            'lodging_rub_month': 'lodging_cost_month',
            'transport_rub_month': 'transport_cost_month',
            'ppe_rub_month': 'ppe_cost_month',
            'training_rub_month': 'training_cost_month'
        }
        
        mapped_field = field_mapping.get(field)
        if mapped_field and mapped_field in st.session_state:
            old_value = st.session_state[mapped_field]
            st.session_state[mapped_field] = new_value
            
            # Change log'a ekle
            change_log.append({
                'field': field,
                'old_value': old_value,
                'new_value': new_value,
                'source': source,
                'timestamp': datetime.now().isoformat()
            })
    
    st.session_state["change_log"] = change_log
    st.success(f"✅ {len(selected_suggestions)} öneri uygulandı!")

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
    from openai import OpenAI  # pyright: ignore[reportMissingImports]
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

# =============== RAG BACKEND BAŞLATMA ===============
# Uygulama başlangıcında RAG backend'ini başlat (opsiyonel)
if 'rag_backend_initialized' not in st.session_state and _RAG_BACKEND_AVAILABLE:
    try:
        init_backend()
        migration_result = migrate_from_jsonl_if_needed()
        st.session_state['rag_backend_initialized'] = True
        
        # Migrasyon sonucunu göster
        if migration_result['migrated'] > 0:
            st.success(f"✅ Eski verilerden {migration_result['migrated']} kayıt taşındı, {migration_result['skipped']} kayıt atlandı.")
    except Exception as e:
        st.error(f"❌ RAG backend başlatılırken hata: {str(e)}")
        st.session_state['rag_backend_initialized'] = False
elif not _RAG_BACKEND_AVAILABLE:
    st.session_state['rag_backend_initialized'] = False

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
        # 2025 kademeli NDFL’i yıllık bazda uygula (patent avansı mahsup edilmez — sade model)
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

def gpt_propose_params(payload: dict, model: str = "gpt-4o") -> dict|None:
    client = get_openai_client()
    if client is None: return None
    
    # Moskova odaklı sistem promptu - güvenli tarafta kalma mantığı
    system = """Sen Moskova'da 15+ yıl deneyimli kıdemli şantiye maliyet analisti ve proje yöneticisisin. 
    
    MOSKOVA ŞANTİYE GERÇEKLERİ:
    - İşçilik maliyetleri yüksek (2024: 80-120 bin RUB/ay)
    - Sarf malzemeleri pahalı (tel, disk, gaz vb.)
    - Kış koşulları zorlu (donma, kar, buz)
    - İşçi verimliliği düşük (dil bariyeri, eğitim eksikliği)
    - Güvenlik standartları katı
    - Denetim ve kontrol sıkı
    
    GÜVENLİ TARAFA KALMA MANTIĞI:
    - Her zaman %10-15 ekstra maliyet buffer'ı
    - İşçi verimliliğini düşük hesapla
    - Sarf malzemelerini fazla hesapla
    - Genel giderleri yüksek tut
    - Beklenmeyen giderler için rezerv bırak
    
    ANALİZ KRİTERLERİN:
    1. Moskova şantiye koşulları ve zorlukları
    2. İşçi deneyimi ve eğitim seviyesi
    3. Mevsimsel etkiler (kış, yaz, yağmur)
    4. Ekipman ve teknoloji kullanımı
    5. Güvenlik ve kalite gereksinimleri
    6. Denetim ve kontrol maliyetleri
    7. Beklenmeyen giderler ve riskler
    
    Sadece JSON formatında yanıt ver. Tüm yüzdeler 0-100 arasında sayısal değerler olmalı."""
    
    # RAG bağlamını al
    rag_context = ""
    if "rag_hits" in st.session_state and st.session_state["rag_hits"]:
        rag_context = "\n\nRAG BAĞLAMI:\n" + "\n".join([
            f"[{i+1}] {hit.get('meta', {}).get('filename', '?')}: {hit.get('text', '')[:500]}"
            for i, hit in enumerate(st.session_state["rag_hits"][:3])
        ])
    
    # Gelişmiş kullanıcı promptu - Moskova odaklı ve detaylı açıklamalar
    user = f"""Aşağıdaki betonarme proje parametrelerini Moskova şantiye gerçeklerine göre detaylı analiz et ve güvenli tarafta kalacak öneriler sun. Her gerekçeyi uzun uzun açıkla.

PROJE VERİLERİ:
{json.dumps(payload, ensure_ascii=False, indent=2)}

MOSKOVA ŞANTİYE DETAYLI ANALİZİ İSTENEN:
1. Sarf malzemeleri oranı (unutulan malzemeler dahil) - DETAYLI GEREKÇE
2. Genel gider oranı (unutulan giderler dahil) - DETAYLI GEREKÇE
3. Indirect giderler analizi (toplam maliyete oranı: az/makul/çok) - DETAYLI GEREKÇE
4. Günlük çalışma saati (verimlilik analizi) - DETAYLI GEREKÇE
5. Senaryo seçimi (güvenli tarafta kalma) - DETAYLI GEREKÇE
6. İşçi dağılımı analizi (demirci, kalıpçı, düz işçi oranları) - DETAYLI GEREKÇE
7. Eksik sarf malzemeleri listesi
8. Eksik genel giderler listesi
9. Eksik indirect giderler listesi

{rag_context}

YANIT ŞEMASI:
{{
    "consumables_pct": number,
    "overhead_pct": number, 
    "hours_per_day": number,
    "scenario": "İdeal|Gerçekçi|Kötü",
    "confidence_score": number,
    "risk_level": "Düşük|Orta|Yüksek",
    "safety_margin": number, // Güvenlik payı yüzdesi
    "reasons": {{
        "consumables": "ÇOK DETAYLI sarf malzemeleri analizi ve eksikler - en az 200 kelime",
        "overhead": "ÇOK DETAYLI genel gider analizi ve eksik giderler - en az 200 kelime", 
        "hours": "ÇOK DETAYLI çalışma saati ve verimlilik analizi - en az 200 kelime",
        "scenario": "ÇOK DETAYLI senaryo seçimi gerekçesi - en az 200 kelime"
    }},
    "missing_items": {{
        "consumables": ["Eksik sarf malzemeleri listesi"],
        "overhead": ["Eksik genel giderler listesi"],
        "indirect": ["Eksik indirect giderler listesi"]
    }},
    "worker_distribution": {{
        "demirci_ratio": number, // Demirci oranı (%)
        "kalipci_ratio": number, // Kalıpçı oranı (%)
        "duz_isci_ratio": number, // Düz işçi oranı (%)
        "analysis": "ÇOK DETAYLI işçi dağılımı analizi ve öneriler - en az 200 kelime"
    }},
    "moscow_specific": {{
        "winter_impact": "ÇOK DETAYLI kış koşullarının etkisi - en az 150 kelime",
        "efficiency_factors": "ÇOK DETAYLI verimlilik etkileyen faktörler - en az 150 kelime",
        "safety_requirements": "ÇOK DETAYLI güvenlik gereksinimleri - en az 150 kelime",
        "additional_costs": "ÇOK DETAYLI ek maliyetler ve rezervler - en az 150 kelime"
    }},
    "indirect_analysis": {{
        "total_indirect_rate": number, // Toplam indirect oranı
        "total_cost_ratio": number, // Toplam maliyete oranı
        "assessment": "az|makul|çok", // Indirect giderlerin değerlendirmesi
        "detailed_analysis": "ÇOK DETAYLI indirect giderler analizi - en az 300 kelime"
    }}
}}"""
    
    try:
        r = client.chat.completions.create(
            model=model, temperature=0.1,  # Daha tutarlı sonuçlar için düşük temperature
            messages=[{"role":"system","content":system},{"role":"user","content":user}]
        )
        return json.loads(extract_json_block(r.choices[0].message.content))
    except Exception as e:
        st.error(f"GPT analizi hatası: {e}")
        return None

def gpt_verify_rates_via_web(queries: list[str], model: str="gpt-4o") -> dict|None:
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
                model="gpt-4o", temperature=0.2,
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
    st.markdown("Burada HS = НСиПЗ. Not: P (patent) NDFL’den mahsup edilmez — sabit gider olarak eklenir.")

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
        Her rol satırı için ülke payları yüzdesel olarak verilir ve 1’e normalize edilir.
        """
    )
    st.latex(r"\text{Cost}_{per\,person} = p_{RUS}\,Cost_{RUS} + p_{SNG}\,Cost_{SNG} + p_{TUR}\,Cost_{VKS}")

    # 6) Normlar, senaryo ve zorluk
    bih("6) Normlar, Senaryo ve Zorluk","6) Нормы, сценарий и сложность", level=4)
    st.markdown(
        """
        - Temel norm (senaryo = Gerçekçi) eleman “Temel” için n_ref alınır.
        - Seçilen senaryonun “Temel” değeri ile oranlanarak senaryo çarpanı s hesaplanır.
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
        - Eleman e’ye dağıtım ağırlığı: w_e = (core_genel_e × m_e) / Σ_e (core_genel_e × m_e)
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
        - SNG’de patent, vergiden mahsup edilmez; bilinçli basitleştirme. İleride istenirse anahtarla açılabilir.
        - VKS’de yalnız НСиПЗ uygulanır; SNG’de tüm sosyal primler resmi brüte uygulanır.
        - Ülke karması yüzdeleri her satırda normalize edilir (toplam 1 olur).
        - Genel gider üst sınırı uygulanır; UI’da da aynı sınır uyarılır.
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
        - SNG’de resmi brüt için bir **tavan** var. Brüt bu tavanı aşarsa, aşan kısım **elden** sayılır. Eldene vergi/prim eklemiyoruz; sadece nakit maliyeti ve varsa küçük bir komisyon (kasa/kur/çekim riskleri) ekleniyor.
        - SNG’de her çalışan için aylık **patent** sabit bedeli var. Bu bedeli ayrıca ekliyoruz (vergiden düşmüyoruz; sizin isteğinizle sade model).
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

# ==================== 0) SABİTLER ====================
with tab_sabitler:
    # Yardımcı fonksiyonlar
    def pct_to_ratio(x): return float(x)/100.0
    def ratio_to_pct(x): return float(x)*100.0
    
    # Override sistemi
    OVR = st.session_state.setdefault("CONST_OVERRIDES", {})
    def eff(name, default): return OVR.get(name, default)
    
    # Global sabitler (default değerler) — yukarıda tanımlananları yeniden tanımlamıyoruz
    # CASH_COMMISSION_RATE varsayılanı üstte tanımlıdır; burada yeniden tanımlamıyoruz
    
    # Kompakt kart ızgarası CSS
    st.markdown("""
    <style>
    .const-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 12px;
        margin: 1rem 0;
    }
    .const-card {
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        padding: 12px;
        background: #fff;
        transition: all 0.2s ease;
    }
    .const-card:hover {
        border-color: #007bff;
        box-shadow: 0 2px 8px rgba(0, 123, 255, 0.1);
    }
    .const-chip {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 999px;
        background: #f1f5f9;
        font-size: 12px;
        margin-left: 6px;
        color: #374151;
        font-weight: 500;
    }
    .const-header {
        background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
        padding: 0.8rem 1rem;
        border-radius: 8px;
        margin-bottom: 1rem;
        border: 1px solid #dee2e6;
    }
    .const-title {
        font-size: 1.1rem;
        font-weight: 600;
        color: #333;
        margin-bottom: 0.5rem;
    }
    .const-subtitle {
        font-size: 0.8rem;
        color: #6c757d;
        font-style: italic;
    }
    .card-title {
        font-size: 0.9rem;
        font-weight: 600;
        color: #374151;
        margin-bottom: 0.3rem;
    }
    .card-desc {
        font-size: 0.75rem;
        color: #6c757d;
        margin-bottom: 0.5rem;
    }
    .card-value {
        font-size: 0.85rem;
        font-weight: 700;
        color: #333;
        background: #f8f9fa;
        padding: 0.4rem 0.6rem;
        border-radius: 6px;
        border: 1px solid #dee2e6;
        text-align: center;
        margin: 0.5rem 0;
    }
    .edit-area {
        background: #f8f9fa;
        border: 1px solid #007bff;
        border-radius: 6px;
        padding: 0.6rem;
        margin-top: 0.5rem;
    }
    .button-row {
        display: flex;
        gap: 0.5rem;
        margin-top: 0.5rem;
    }
    .override-badge {
        display: inline-block;
        padding: 0.2rem 0.6rem;
        border-radius: 12px;
        background: #e7f3ff;
        color: #0056b3;
        font-size: 0.7rem;
        margin: 0.2rem;
        border: 1px solid #b3d9ff;
    }
    </style>
    """, unsafe_allow_html=True)
    
    # Üst özet şeridi
    st.markdown('<div class="const-header">', unsafe_allow_html=True)
    st.markdown('<div class="const-title">⚙️ Sistem Sabitleri / ⚙️ Системные константы</div>', unsafe_allow_html=True)
    st.markdown('<div class="const-subtitle">Bu gruptaki değişiklikler yalnızca bu oturum için geçerlidir. / Изменения в этой группе действуют только в текущей сессии.</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)
    
    # Override badge'leri
    if OVR:
        st.markdown("**Uygulanan Override'lar:**")
        for k, v in OVR.items():
            if isinstance(v, float):
                if v < 1.0:  # Oran
                    display_val = f"{ratio_to_pct(v):.2f}%"
                else:  # Ruble
                    display_val = f"{v:,.0f} ₽"
            else:
                display_val = str(v)
            st.markdown(f'<span class="override-badge">{k}: {display_val}</span>', unsafe_allow_html=True)
        
        col1, col2 = st.columns([1, 4])
        with col1:
            if st.button("Tümü Sıfırla", type="secondary"):
                st.session_state["CONST_OVERRIDES"] = {}
                st.rerun()
        with col2:
            st.caption("💡 Override'ları sıfırlamak için butona tıklayın.")
    else:
        st.info("ℹ️ Henüz hiçbir override uygulanmamış. Varsayılan değerler kullanılıyor.")
    
    st.markdown("<hr>", unsafe_allow_html=True)
    
    # Vergi modu seçimi (artan / sabit oran)
    st.markdown(bi("#### Vergi Modu","Режим налогообложения"))
    st.session_state["use_progressive_ndfl"] = st.toggle(
        "Artan NDFL (2025 kademeleri) kullan", value=st.session_state.get("use_progressive_ndfl", True)
    )
    
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
# ==================== 1) GENEL ====================
with tab_genel:
    col1, col2 = st.columns(2)
    with col1:
        st.session_state["prim_sng"] = st.checkbox(
            bi("SNG için gayriresmî/elden uygula (komisyonlu)", "Для СНГ применять неофициальную/наличную часть (с комиссией)"), value=st.session_state.get("prim_sng", True)
        )
    with col2:
        st.session_state["prim_tur"] = st.checkbox(
            bi("Türk (VKS) için gayriresmî/elden uygula (komisyonlu)", "Для ВКС (Турция) применять неофициальную/наличную часть (с комиссией)"), value=st.session_state.get("prim_tur", True)
        )
    st.caption(bi("ℹ️ ‘Gayriresmî/Elden’ (nakit) **hiçbir vergi/prim içermez**; yalnızca komisyon uygulanır. Resmî brüt kısma OPS/OSS/OMS + НСиПЗ (VKS'de yalnız НСиПЗ).",
                 "ℹ️ ‘Неофициальная/наличная’ часть не облагается налогами/взносами; только комиссия. На официальный брутто начисляются ОПС/ОСС/ОМС + НСиПЗ (для ВКС только НСиПЗ)."))

    # Uzaktan çalışma/НСиПЗ seçenekleri kaldırıldı (işçiler sahada çalışır varsayımı)

    cA, cB = st.columns(2)
    with cA:
        st.session_state["start_date"] = st.date_input(
            bi("Başlangıç", "Начало"), value=st.session_state.get("start_date", date.today().replace(day=1)), key="start_date_inp"
        )
    with cB:
        st.session_state["end_date"] = st.date_input(
            bi("Bitiş", "Окончание"), value=st.session_state.get("end_date", date.today().replace(day=30)), key="end_date_inp"
        )
    

    


    holiday_options=[(bi("Hiç tatil yok (7/7)","Без выходных (7/7)"),"tam_calisma"),
                     (bi("Her Pazar tatil (6/7)","Выходной только вс (6/7)"),"her_pazar"),
                     (bi("Her Cmt+Paz tatil (5/7)","Выходные сб+вс (5/7)"),"hafta_sonu_tatil"),
                     (bi("2 haftada 1 Pazar tatil","Каждые 2 недели выходной вс"),"iki_haftada_bir_pazar")]
    sel = st.selectbox(bi("Tatil günleri","Режим выходных"), [h[0] for h in holiday_options],
                       index= st.session_state.get("holiday_idx",1), key="holiday_selbox")
    st.session_state["holiday_idx"] = [h[0] for h in holiday_options].index(sel)
    st.session_state["holiday_mode"] = dict(holiday_options)[sel]
    

    
    # Tatil günleri değişikliğinde hesaplamaları güncelle
    current_holiday_mode = dict(holiday_options)[sel]
    if st.session_state.get("holiday_mode") != current_holiday_mode:
        st.session_state["holiday_mode"] = current_holiday_mode
        # Hesaplamaları güncelle
        st.session_state["_holiday_mode_changed"] = True
        # Sayfayı yenile ki hesaplamalar güncellensin
        st.rerun()

    cC, cD = st.columns(2)
    with cC:
        # Günlük çalışma saati - basit widget, session_state otomatik güncellenir
        st.session_state["hours_per_day"] = st.number_input(
            bi("Günlük çalışma saati","Часов в день"), min_value=6.0, max_value=16.0, value=10.0, step=0.5, key="hours_per_day_inp"
        )
    with cD:
        st.session_state["scenario"] = st.selectbox(
            bi("👷‍♂️ Adam-saat senaryosu","👷‍♂️ Сценарий норм трудозатрат"), ["İdeal","Gerçekçi","Kötü"],
            index=["İdeal","Gerçekçi","Kötü"].index(st.session_state.get("scenario","Gerçekçi")),
            key="scenario_sel"
        )

    # Kapsam notu (müşteri varsayımları)
    st.caption(bi("SNG kapsamı: Kırgızistan, Özbekistan, Tacikistan, Türkmenistan. VKS: Türkiye.",
                  "СНГ: Кыргызстан, Узбекистан, Таджикистан, Туркменистан. ВКС: Турция."))
    st.caption(bi("Patent bedeli her ay sabit maliyet olarak kabul edilir; NDFL mahsup edilmez (basitleştirilmiş yaklaşım).",
                  "Платёж за патент учитывается как фиксированная ежемесячная затрата; в НДФЛ не засчитывается (упрощённая модель)."))

# ==================== 1B) ADAM-SAAT NORMLARI (ayrı başlık) ====================
with tab_genel:
    with st.expander("👷‍♂️ Adam-saat Normları (Senaryolar) / 👷‍♂️ Нормы трудозатрат (сценарии)", expanded=False):
        st.caption(bi("Senaryolara göre eleman bazında a·s/m³ normlarını düzenleyin. Boş bırakılanlar varsayılanı kullanır.",
                      "Редактируйте нормы a·ч/м³ по элементам для каждого сценария. Пустые — по умолчанию."))
        norms_map = get_effective_scenario_norms()
        scenarios = ["İdeal","Gerçekçi","Kötü"]
        elements_tr = ["Grobeton","Rostverk","Temel","Döşeme","Perde","Merdiven"]
        import pandas as _pd  # pyright: ignore[reportMissingImports]
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
                    st.checkbox("Aktif", key=on_key, on_change=run_auto_rag)
                    # Yüzde girişi
                    st.number_input(
                        "Etki %",
                        min_value=it["min"],
                        max_value=it["max"],
                        step=it["step"],
                        format="%.2f",
                        key=pct_key,
                        disabled=not st.session_state[on_key],
                        on_change=run_auto_rag
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
            sel_flags[k]=st.checkbox(LABELS[k], value=st.session_state.get(f"sel_{k}", True), key=f"sel_{k}", on_change=run_auto_rag)
    selected_elements=[k for k,v in sel_flags.items() if v]
    if not selected_elements:
        st.warning(bi("En az bir betonarme eleman seçin.", "Выберите хотя бы один элемент."))
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
    
    # Auto-RAG tetikleme için gider oranları değişikliklerini izle
    if (st.session_state.get("_prev_consumables_rate", None) != st.session_state.get("consumables_rate", 0) or
        st.session_state.get("_prev_overhead_rate", None) != st.session_state.get("overhead_rate", 0) or
        st.session_state.get("_prev_indirect_rate", None) != st.session_state.get("indirect_rate_total", 0)):
        run_auto_rag()
        st.session_state["_prev_consumables_rate"] = st.session_state.get("consumables_rate", 0)
        st.session_state["_prev_overhead_rate"] = st.session_state.get("overhead_rate", 0)
        st.session_state["_prev_indirect_rate"] = st.session_state.get("indirect_rate_total", 0)
    
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
        ("General","gen_staff_work","İşlerin yürütülmesi için personel","Персонал для выполнения работ","overlap_only",0.0,"core_labor"),
        ("General","gen_work_permit","Personel için çalışma izni","Разрешение на работу для персонала","overhead",0.0,None),
        ("General","gen_visa_rf","Yabancı çalışanlar için RF çalışma vizeleri","Визы РФ для иностранного персонала","overhead",0.0,None),
        ("General","gen_migration_resp","RF göç mevzuatına uyum (ceza/hukuk/sınır dışı riskleri)","Соблюдение миграционного законодательства РФ…","overhead",0.0,None),
        ("General","gen_social_payments","Personel ve alt yükleniciler için sosyal ödemeler/vergiler","Социальные отчисления, налоги…","overlap_only",0.0,"core_labor"),
        ("General","gen_staff_transport_domintl","Personelin taşınması (yurtiçi/yurtdışı)","Транспортные расходы персонала (внутренние/междунар.)","indirect",0.0,None),
        ("General","gen_staff_transport_local","Personelin yerel taşınması","Местная перевозка своего персонала","overlap_only",0.0,"global_extras"),
        ("General","gen_accom_food","Personelin barınma ve yemek giderleri","Проживание и питание своего персонала","overlap_only",0.0,"global_extras"),
        ("General","gen_transport_mounting","Montaj malzemeleri/ekipmanlarının yerel taşınması (yüklenici)","Местная транспортировка монтажных материалов и оборудования подрядчика","indirect",0.0,None),
        ("General","gen_transport_wh_to_site","Müşteri deposundan şantiyeye yerel taşıma","Местная транспортировка со склада Заказчика до площадки","indirect",0.0,None),
        ("General","gen_risk_loss_customer_ware","Müşteri malzemelerinin depolarda kayıp riski","Риск утраты материалов заказчика на складах…","indirect",0.0,None),
        ("General","gen_risk_loss_customer_to_finish","Montaja verilen müşteri malzemelerinin bitime kadar kayıp riski","Риск утраты материалов заказчика, переданных подрядчику…","indirect",0.0,None),
        ("General","gen_risk_own_materials_equipment","Yüklenicinin kendi malzeme/ekipmanının kayıp riski (kablolar dâhil)","Риск утраты собственных материалов и оборудования подрядчика…","indirect",0.0,None),
        ("General","gen_required_licenses","İş türleri için gerekli lisanslar (RF düzenlemeleri)","Требуемые лицензии по видам работ…","overhead",0.0,None),
        ("General","gen_insurance_equip_staff","Ekipman ve personel sigortası","Страхование оборудования и персонала подрядчика","indirect",0.0,None),
        ("General","gen_workplace_facilities","Çalışma alanı donanımı: mobilya, telefon, internet, yazıcı","Оснащение рабочих мест: мебель, телефон, интернет, принтер","indirect",0.0,None),

        # ---------- 2) H&S ----------
        ("H&S","hs_engineer_on_site","İSG mühendisi – sahada daimi temsilci","Инженер ТБ – постоянный представитель подрядчика","overhead",0.0,None),
        ("H&S","hs_action_plan","İSG eylem planı","Программа мероприятий по ОТ и ТБ","overhead",0.0,None),
        ("H&S","hs_meetings","İSG koordinasyon toplantılarına katılım (talebe bağlı)","Участие в координационных совещаниях по ОТ и ТБ…","overhead",0.0,None),
        ("H&S","hs_initial_briefing","Tüm personel için ilk İSG bilgilendirmesi","Первичный инструктаж по ОТ и ТБ…","overhead",0.0,None),
        ("H&S","hs_full_responsibility","Yüklenici alanlarında İSG kurallarına tam sorumluluk","Полная ответственность за соблюдение правил ОТ и ТБ…","overhead",0.0,None),
        ("H&S","hs_guarding_openings","Açıklıkların korunması ve kapatılması (yüklenici alanları)","Защитные ограждения и закрытие проемов…","indirect",0.0,None),
        ("H&S","hs_site_med_station","Şantiye reviri (ilk yardım; hemşire gündüz/gece)","Медпункт на площадке – первая помощь…","indirect",0.0,None),
        ("H&S","hs_medical_costs","Tıbbi giderler (ilaç, hastane vb.)","Медицинские расходы (лекарство, больница и т. д.)","indirect",0.0,None),
        ("H&S","hs_first_aid_kits","İlk yardım ekipmanları (çalışma alanlarında setler)","Оборудование для первой помощи (аптечки)","indirect",0.0,None),
        ("H&S","hs_ppe","SİZ, iş kıyafeti ve ayakkabı","СИЗ, одежда и обувь для сотрудников Подрядчика","overlap_only",0.0,"global_extras"),
        ("H&S","hs_firefighting_eq","Yangınla mücadele ekipmanı (tüp/örtü/su)","Противопожарное оборудование…","indirect",0.0,None),
        ("H&S","hs_safety_labeling","Güvenlik işaretlemeleri/uyarı levhaları","Оснащение участка предупреждающими табличками","indirect",0.0,None),
        ("H&S","hs_wind_panels","Rüzgâr panelleri","Защитный экран","indirect",0.0,None),
        ("H&S","hs_protective_nets","Koruyucu-yakalama ağları (ЗУС)","Защитно-улавливающие сетки (ЗУС)","indirect",0.0,None),
        ("H&S","hs_worker_certs","Çalışanlar için gerekli sertifika/ehliyetler","Все необходимые сертификаты/аттестации для рабочих","overhead",0.0,None),
        ("H&S","hs_consumables","İSG için tüm sarf malzemeleri","Все необходимые расходные материалы для ОТ и ТБ","consumables",0.0,None),
        ("H&S","hs_lifting_consumables","Kaldırma işleri (bkz. kule vinçler) için sarf malzemeleri","Расходники для такелажных работ (в т.ч. башенные краны)","consumables",0.0,None),
        ("H&S","hs_lifting_supervisors","Kaldırma ekipmanı için işaretçi/rigging sorumluları","Стропальщики/риггеры/супервайзеры по подъёмным работам","indirect",0.0,None),

        # ---------- 3) Site equipment ----------
        ("Site","site_power_conn","Enerji bağlantı noktaları (genel plana göre)","Точки подключения электроэнергии согласно генплану","indirect",0.0,None),
        ("Site","site_power_distribution","Yüklenici alanlarına enerji dağıtımı","Распределение электроэнергии до зон Подрядчика","indirect",0.0,None),
        ("Site","site_power_costs","Elektrik giderleri","Расходы на электричество","indirect",0.0,None),
        ("Site","site_water_conn","Prosess suyu bağlantı noktaları (genel plana göre)","Точки подключения тех. воды согласно генплану","indirect",0.0,None),
        ("Site","site_water_distribution","Yüklenici alanlarına su dağıtımı","Распределение воды до зон Подрядчика","indirect",0.0,None),
        ("Site","site_water_costs","Su giderleri","Расходы на воду","indirect",0.0,None),
        ("Site","site_generator","Gerekirse jeneratör","Генератор при необходимости","indirect",0.0,None),
        ("Site","site_main_lighting","Alan/bina ana aydınlatması (tüm dönem)","Основное освещение площадок и зданий","indirect",0.0,None),
        ("Site","site_add_lighting","Ek aydınlatma (yüklenici alanları)","Дополнительное освещение территорий подрядчика","indirect",0.0,None),
        ("Site","site_covered_storage","Montaja verilmiş malzemeler için kapalı stok alanı","Крытые площадки складирования (выданных в монтаж)","indirect",0.0,None),
        ("Site","site_closed_storage","Montaj malzemeleri için kapalı depo/ambar","Закрытые площадки / склады (выданных в монтаж)","indirect",0.0,None),
        ("Site","site_temp_roads","Yalnız yüklenici kullanımına geçici yollar","Временные дороги только для подрядчика","indirect",0.0,None),
        ("Site","site_add_fencing","Yüklenici alanı için ek çit (gerekirse)","Дополнительное ограждение территории подрядчика","indirect",0.0,None),
        ("Site","site_scrap_place","Şantiyede hurda metal depolama alanı","Площадка хранения металлолома","indirect",0.0,None),
        ("Site","site_lockers","Soyunma odaları","Раздевалки","indirect",0.0,None),
        ("Site","site_office","Ofis alanları","Офисные помещения","indirect",0.0,None),
        ("Site","site_toilets","Yüklenici için tuvaletler","Туалеты субподрядчика","indirect",0.0,None),
        ("Site","site_fire_access","Yangın müdahale yolları ve şantiyeye sürekli erişim","Пожарные подъезды и постоянный доступ","indirect",0.0,None),
        ("Site","site_gate_guard","Giriş kapısında güvenlik","Охрана на проходной","indirect",0.0,None),
        ("Site","site_add_guard","Ek güvenlik (gerekirse)","Дополнительная охрана (по необходимости)","indirect",0.0,None),
        ("Site","site_full_fencing","Tüm şantiye alanının çevrilmesi","Ограждение всей стройплощадки","indirect",0.0,None),

        # ---------- 4) Works implementation ----------
        ("Works","w_proj_docs","Sayısal proje dokümanları","Проектные документы в электронном виде","overhead",0.0,None),
        ("Works","w_mos","Yöntem bildirimi (PPR) hazırlanması","Подготовка ППР","overhead",0.0,None),
        ("Works","w_handover_docs","Teslim dosyaları (as-built/protokoller) hazırlanması","Подготовка акты и ИД","overhead",0.0,None),
        ("Works","w_docs_archive","Arşiv/Elektronik sistemden dokümanlar","Документы из архива или из ЭДО","overhead",0.0,None),
        ("Works","w_handover_site_coord","Şantiye ağlarının ve koordinat sisteminin devri","Передача сетей стройплощадки и реперных точек","overhead",0.0,None),
        ("Works","w_rep_present","Yüklenici temsilcisinin sahada sürekli bulunması","Назначенный представитель подрядчика постоянно на площадке","overhead",0.0,None),
        ("Works","w_rep_coord_meet","Koordinasyon toplantılarına katılım (yüklenici temsilcisi)","Представитель подрядчика участвует в совещаниях","overhead",0.0,None),
        ("Works","w_detailed_schedule","Yüklenici işlerinin ayrıntılı iş programı","Детальный график работ подрядчика","overhead",0.0,None),
        ("Works","w_weekly_reports","Haftalık ilerleme raporları (kaynaklar dahil)","Еженедельные отчеты по выполнению работ…","overhead",0.0,None),
        ("Works","w_weekly_safety","Haftalık İSG raporları","Еженедельные отчеты по ОТ и ТБ","overhead",0.0,None),

        ("Works","w_concrete_proc","Beton tedariki","Закупка бетона","overlap_only",0.0,"materials"),
        ("Works","w_rebar_proc","Donatı çeliği tedariki","Закупка арматуры","overlap_only",0.0,"materials"),
        ("Works","w_scaff_form","İskele ve kalıp sistemleri (tümü)","Леса и опалубки (все системы)","indirect",0.0,None),
        ("Works","w_tower_cranes","Kule vinçler (operatörlü)","Башенные краны с операторами","indirect",0.0,None),
        ("Works","w_temp_lifts","Geçici şantiye asansörleri (operatörlü)","Временные грузопассажирские лифты с операторами","indirect",0.0,None),
        ("Works","w_concrete_pumps","Beton pompaları (tüm borular ile)","Бетононасосы со всеми трубами","indirect",0.0,None),
        ("Works","w_pump_operators","Pompa operatörleri, hat montajı ve bakımı","Операторы, монтаж и ТО насосных линий","indirect",0.0,None),
        ("Works","w_hyd_dist","Hidrolik beton dağıtıcılar","Гидравлические бетонораспределители","indirect",0.0,None),
        ("Works","w_hyd_dist_ops","Hidrolik dağıtıcı operatörleri","Операторы гидр. бетонораспределителей","indirect",0.0,None),
        ("Works","w_aux_lifting","Hareketli & yardımcı kaldırma araçları (kamyon, vinç, manlift)","Передвижные и вспом. грузоподъёмные механизмы","indirect",0.0,None),
        ("Works","w_wheel_wash","Tekerlek yıkama (operatörlü)","Мойка колес с операторами","indirect",0.0,None),
        ("Works","w_all_equipment","İşlerin icrası için her tür ekipman","Все инструменты, используемые для выполнения работ","indirect",0.0,None),

        ("Works","w_aux_heat_insul","Betonda kullanılan yardımcı ısı yalıtım malzemeleri","Все вспомогательные твердые теплоизоляционные материалы…","overlap_only",0.0,"materials"),
        ("Works","w_consumables","İmalat sarfları (gaz, disk, tel vb.)","Расходные материалы для выполнения работ","consumables",0.0,None),
        ("Works","w_measurements","Ölçümler ve evrak (as-built dâhil)","Измерения, включая исполнительную документацию","indirect",0.0,None),
        ("Works","w_radios","El telsizleri","Подходящие портативные радиостанции (рации)","indirect",0.0,None),
        ("Works","w_concrete_care","Beton bakım işleri (kışın ısıtma dâhil)","Уход за бетоном, включая подогрев зимой","indirect",0.0,None),
        ("Works","w_lab_tests","Gerekli tüm laboratuvar testleri","Все необходимые лабораторные испытания","indirect",0.0,None),
        ("Works","w_cleaning","Yüklenici alanlarının temizliği, atıkların uzaklaştırılması","Уборка территорий подрядчика, вывоз мусора","indirect",0.0,None),
        ("Works","w_snow_fire_access","Ana güzergâhlar ve yangın yollarından kar/buz temizliği","Уборка снега и льда с основных путей и пожарных подъездов","indirect",0.0,None),
        ("Works","w_snow_local","Yüklenici alanları/depolar/geçici yollardan kar/buz temizliği","Уборка снега и льда с зон подрядчика/складов/временных путей","indirect",0.0,None),
        ("Works","w_stormwater_site","Şantiye sahasından yağmur suyu drenajı","Слив ливневой воды с площадок","indirect",0.0,None),
        ("Works","w_stormwater_contractor","Yüklenici alanlarından yağmur suyu drenajı","Слив ливневой воды с зон подрядчика","indirect",0.0,None),
        ("Works","w_load_unload","Malzemelerin sahada yükleme/boşaltması (düşey/yatay)","Погрузка-разгрузка материалов на площадке","indirect",0.0,None),
        ("Works","w_transport_inside","Saha içi malzeme taşımaları","Транспортировка материалов по стройплощадке","indirect",0.0,None),

        ("Works","w_rebar_couplings","Dişli/sıkma muflar + hazırlık ekipmanı (donatı)","Резьбовые/обжимные муфты + инструмент для подготовки арматуры","overlap_only",0.0,"materials"),
        ("Works","w_rebar_coupling_works","Muflu donatı hazırlık/bağlantı çalışmaları","Подготовительные и соединительные работы арматуры с муфтами","overlap_only",0.0,"core_labor"),
        ("Works","w_material_overspend","Malzeme israfının mali sorumluluğu","Материальная ответственность за перерасход материала","overlap_only",0.0,"materials"),
        ("Works","w_repair_for_handover","Teslim için gerekli onarım işleri","Ремонтные работы, необходимые для сдачи","indirect",0.0,None),
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
        st.info(bi("ℹ️ Override kapalı: Manuel Sarf/Overhead/Indirect oranları kullanılacak; матрица sadece gösterim amaçlı.",
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
        # Auto-RAG tetikleme (hesaplama sonrası)
        run_auto_rag()
        
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
    # Hesaplama sonuçlarını göster
    if st.session_state.get("calculation_results"):
        results = st.session_state["calculation_results"]
        data = results["data"]
        
        # Ana metrikler - Modern kartlar
        # Profesyonel Hesap Sonuçları Dashboard
        st.markdown("""
        <div style="background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%); padding: 2rem; border-radius: 20px; margin-bottom: 2rem; text-align: center; border: 1px solid #dee2e6;">
            <h2 style="color: #495057; margin: 0; font-size: 2.5rem; font-weight: 600;">📊 HESAP SONUÇLARI DASHBOARD</h2>
            <p style="color: #6c757d; margin: 1rem 0 0 0; font-size: 1.1rem;">
                Moskova Şantiye Gerçeklerine Göre Profesyonel Maliyet Analizi ve Proje Özeti
            </p>
        </div>
        """, unsafe_allow_html=True)
        
        # 1. Adam-Saat Fiyatları Bölümü
        st.markdown("""
        <div style="background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); padding: 1.5rem; border-radius: 15px; margin-bottom: 2rem; border: 1px solid #dee2e6;">
            <h3 style="color: #495057; margin: 0; text-align: center; font-size: 1.8rem; font-weight: 600;">💰 ADAM-SAAT FİYATLARI</h3>
            <p style="color: #6c757d; margin: 0.5rem 0 0 0; text-align: center; font-size: 1rem;">
                Farklı Maliyet Seviyelerinde Profesyonel Fiyatlandırma
            </p>
        </div>
        """, unsafe_allow_html=True)
        
        # Adam-Saat Fiyatları - 3 Sütunlu Modern Layout
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.markdown(f"""
            <div style="background: #ffffff; padding: 1.5rem; border-radius: 12px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-size: 1.2rem; font-weight: 600;">🏃 Çıplak Adam-Saat</h4>
                <p style="color: #212529; font-size: 2rem; font-weight: 700; margin: 1rem 0;">{data['bare_as_price']:,.2f} ₽</p>
                <p style="color: #6c757d; margin: 0; font-size: 0.9rem;">Temel Maliyet / Базовая стоимость</p>
            </div>
            """, unsafe_allow_html=True)
        
        with col2:
            st.markdown(f"""
            <div style="background: #ffffff; padding: 1.5rem; border-radius: 12px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-size: 1.2rem; font-weight: 600;">🍽️ Genel Giderli Adam-Saat</h4>
                <p style="color: #212529; font-size: 2rem; font-weight: 700; margin: 1rem 0;">{data['with_extras_as_price']:,.2f} ₽</p>
                <p style="color: #6c757d; margin: 0; font-size: 0.9rem;">Yemek + Barınma + Ulaşım / Питание + Жильё + Транспорт</p>
            </div>
            """, unsafe_allow_html=True)
        
        with col3:
            st.markdown(f"""
            <div style="background: #ffffff; padding: 1.5rem; border-radius: 12px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-size: 1.2rem; font-weight: 600;">🎯 Her Şey Dahil Adam-Saat</h4>
                <p style="color: #212529; font-size: 2rem; font-weight: 700; margin: 1rem 0;">{data['fully_loaded_as_price']:,.2f} ₽</p>
                <p style="color: #6c757d; margin: 0; font-size: 0.9rem;">Tüm Giderler Dahil / Все расходы включены</p>
            </div>
            """, unsafe_allow_html=True)
        
        # 2. Proje Özeti Bölümü
        st.markdown("""
        <div style="background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); padding: 1.5rem; border-radius: 15px; margin-bottom: 2rem; border: 1px solid #dee2e6;">
            <h3 style="color: #495057; margin: 0; text-align: center; font-size: 1.8rem; font-weight: 600;">🏗️ PROJE ÖZETİ</h3>
            <p style="color: #6c757d; margin: 0.5rem 0 0 0; text-align: center; font-size: 1rem;">
                Kapsamlı Proje Metrikleri ve Performans Göstergeleri
            </p>
        </div>
        """, unsafe_allow_html=True)
        
        # Proje Özeti - 5 Sütunlu Modern Layout
        colA, colB, colC, colD, colE = st.columns(5)
        
        with colA:
            st.markdown(f"""
            <div style="background: #ffffff; padding: 1.2rem; border-radius: 10px; text-align: center; box-shadow: 0 2px 6px rgba(0,0,0,0.06); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-size: 1rem; font-weight: 600;">⏰ Toplam Adam-Saat</h4>
                <p style="color: #212529; font-size: 1.5rem; font-weight: 700; margin: 0.5rem 0;">{data['total_adamsaat']:,.0f}</p>
                <p style="color: #6c757d; margin: 0; font-size: 0.8rem;">чел·ч</p>
            </div>
            """, unsafe_allow_html=True)
        
        with colB:
            st.markdown(f"""
            <div style="background: #ffffff; padding: 1.2rem; border-radius: 10px; text-align: center; box-shadow: 0 2px 6px rgba(0,0,0,0.06); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-size: 1rem; font-weight: 600;">📏 m³ Başına Ort. a·s</h4>
                <p style="color: #212529; font-size: 1.5rem; font-weight: 700; margin: 0.5rem 0;">{data['avg_norm_per_m3']:,.2f}</p>
                <p style="color: #6c757d; margin: 0; font-size: 0.8rem;">a·s/m³</p>
            </div>
            """, unsafe_allow_html=True)
        
        with colC:
            st.markdown(f"""
            <div style="background: #ffffff; padding: 1.2rem; border-radius: 10px; text-align: center; box-shadow: 0 2px 6px rgba(0,0,0,0.06); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-size: 1rem; font-weight: 600;">💰 Genel Ortalama</h4>
                <p style="color: #212529; font-size: 1.5rem; font-weight: 700; margin: 0.5rem 0;">{data['general_avg_m3']:,.2f}</p>
                <p style="color: #6c757d; margin: 0; font-size: 0.8rem;">₽/m³</p>
            </div>
            """, unsafe_allow_html=True)
        
        with colD:
            st.markdown(f"""
            <div style="background: #ffffff; padding: 1.2rem; border-radius: 10px; text-align: center; box-shadow: 0 2px 6px rgba(0,0,0,0.06); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-size: 1rem; font-weight: 600;">📊 Toplam Metraj</h4>
                <p style="color: #212529; font-size: 1.5rem; font-weight: 700; margin: 0.5rem 0;">{data['total_metraj']:,.1f}</p>
                <p style="color: #6c757d; margin: 0; font-size: 0.8rem;">m³</p>
            </div>
            """, unsafe_allow_html=True)
        
        with colE:
            st.markdown(f"""
            <div style="background: #ffffff; padding: 1.2rem; border-radius: 10px; text-align: center; box-shadow: 0 2px 6px rgba(0,0,0,0.06); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-size: 1rem; font-weight: 600;">💵 Toplam Maliyet</h4>
                <p style="color: #212529; font-size: 1.5rem; font-weight: 700; margin: 0.5rem 0;">{data['project_total_cost']:,.0f}</p>
                <p style="color: #6c757d; margin: 0; font-size: 0.8rem;">₽</p>
            </div>
            """, unsafe_allow_html=True)

        # Loading mesajını gizle
        clear_loading_placeholder()
        
        # Toplam oranlar hesaplama (Indirect hariç)
        total_rate = (data['consumables_rate_eff'] + data['overhead_rate_eff']) * 100
        total_cost = data['project_total_cost']
        
        # Sorumluluk matrisi verilerini kontrol et
        use_matrix_override = st.session_state.get("use_matrix_override", False)
        matrix_data = None
        if use_matrix_override:
            # Sorumluluk matrisi verilerini al
            matrix_data = {
                "consumables": st.session_state.get("consumables_rate_eff", 0) * 100,
                "overhead": st.session_state.get("overhead_rate_eff", 0) * 100,
                "indirect": st.session_state.get("indirect_rate_total_eff", 0) * 100
            }
        
        # Profesyonel Maliyet Analizi Dashboard
        st.markdown("""
        <div style="background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%); padding: 1.5rem; border-radius: 15px; margin-bottom: 1.5rem;">
            <h3 style="color: white; margin: 0; text-align: center;">📊 PROFESYONEL MALİYET ANALİZİ DASHBOARD</h3>
            <p style="color: white; opacity: 0.9; margin: 0.5rem 0 0 0; text-align: center; font-size: 0.9rem;">
                Moskova Şantiye Gerçeklerine Göre Detaylı Maliyet Dağılımı ve Sorumluluk Matrisi Entegrasyonu
            </p>
        </div>
        """, unsafe_allow_html=True)
        
        # Ana Metrikler - 4 Sütunlu Profesyonel Layout
        col_main1, col_main2, col_main3, col_main4 = st.columns(4)
        
        with col_main1:
            st.markdown("""
            <div style="background: #ffffff; padding: 1rem; border-radius: 10px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-weight: 600;">💰 Toplam Proje Maliyeti</h4>
                <p style="color: #212529; font-size: 1.5rem; font-weight: 700; margin: 0.5rem 0;">{total_cost:,.0f} ₽</p>
            </div>
            """.format(total_cost=total_cost), unsafe_allow_html=True)
        
        with col_main2:
            st.markdown("""
            <div style="background: #ffffff; padding: 1rem; border-radius: 10px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-weight: 600;">📈 Toplam Oranlar</h4>
                <p style="color: #212529; font-size: 1.5rem; font-weight: 700; margin: 0.5rem 0;">{total_rate:.2f}%</p>
                <p style="color: #6c757d; font-size: 0.8rem; margin: 0;">Sarf + Overhead</p>
            </div>
            """.format(total_rate=total_rate), unsafe_allow_html=True)
        
        with col_main3:
            indirect_rate = data['indirect_rate_total'] * 100
            st.markdown("""
            <div style="background: #ffffff; padding: 1rem; border-radius: 10px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-weight: 600;">🧾 Indirect Oranı</h4>
                <p style="color: #212529; font-size: 1.5rem; font-weight: 700; margin: 0.5rem 0;">{indirect_rate:.2f}%</p>
                <p style="color: #6c757d; font-size: 0.8rem; margin: 0;">Toplam Maliyete Oran</p>
            </div>
            """.format(indirect_rate=indirect_rate), unsafe_allow_html=True)
        
        with col_main4:
            total_all_rates = total_rate + indirect_rate
            st.markdown("""
            <div style="background: #ffffff; padding: 1rem; border-radius: 10px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e9ecef;">
                <h4 style="color: #495057; margin: 0; font-weight: 600;">🎯 Toplam Tüm Oranlar</h4>
                <p style="color: #212529; font-size: 1.5rem; font-weight: 700; margin: 0.5rem 0;">{total_all_rates:.2f}%</p>
                <p style="color: #6c757d; font-size: 0.8rem; margin: 0;">Sarf + Overhead + Indirect</p>
            </div>
            """.format(total_all_rates=total_all_rates), unsafe_allow_html=True)
        
        # Sorumluluk Matrisi Entegrasyonu
        if matrix_data:
            st.markdown("---")
            st.markdown("""
            <div style="background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); padding: 1rem; border-radius: 10px; margin: 1rem 0; border: 1px solid #dee2e6;">
                <h4 style="color: #495057; margin: 0; text-align: center; font-weight: 600;">🔗 SORUMLULUK MATRİSİ ENTEGRASYONU AKTİF</h4>
                <p style="color: #6c757d; margin: 0.5rem 0 0 0; text-align: center; font-size: 0.9rem;">
                    Manuel oranlar yerine Sorumluluk Matrisi toplamları kullanılıyor
                </p>
            </div>
            """, unsafe_allow_html=True)
            
            col_matrix1, col_matrix2, col_matrix3 = st.columns(3)
            
            with col_matrix1:
                st.metric(
                    "🧴 Matris Sarf",
                    f"{matrix_data['consumables']:.2f}%",
                    delta=f"{matrix_data['consumables'] - (data['consumables_rate_eff'] * 100):.2f}%",
                    delta_color="normal"
                )
            
            with col_matrix2:
                st.metric(
                    "🧮 Matris Overhead",
                    f"{matrix_data['overhead']:.2f}%",
                    delta=f"{matrix_data['overhead'] - (data['overhead_rate_eff'] * 100):.2f}%",
                    delta_color="normal"
                )
            
            with col_matrix3:
                st.metric(
                    "🧾 Matris Indirect",
                    f"{matrix_data['indirect']:.2f}%",
                    delta=f"{matrix_data['indirect'] - (data['indirect_rate_total'] * 100):.2f}%",
                    delta_color="normal"
                )
        
        # Detaylı Maliyet Dağılımı Analizi
        st.markdown("---")
        st.markdown("""
        <div style="background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); padding: 1rem; border-radius: 10px; margin: 1rem 0; border: 1px solid #dee2e6;">
            <h4 style="color: #495057; margin: 0; text-align: center; font-weight: 600;">📊 DETAYLI MALİYET DAĞILIMI ANALİZİ</h4>
        </div>
        """, unsafe_allow_html=True)
        
        # Oranların dağılımı hesaplama
        sarf_share = (data['consumables_rate_eff'] * 100) / total_rate * 100
        overhead_share = (data['overhead_rate_eff'] * 100) / total_rate * 100
        
        # Toplam proje maliyetine göre gider dağılımı hesaplama
        total_project_cost = data['project_total_cost']
        
        # Her giderin toplam maliyete göre yüzdesi
        sarf_cost = data['consumables_rate_eff'] * total_project_cost
        overhead_cost = data['overhead_rate_eff'] * total_project_cost
        indirect_cost = data['indirect_rate_total'] * total_project_cost
        
        # Toplam maliyete göre yüzdeler
        sarf_percent_of_total = (sarf_cost / total_project_cost) * 100
        overhead_percent_of_total = (overhead_cost / total_project_cost) * 100
        indirect_percent_of_total = (indirect_cost / total_project_cost) * 100
        
        # Pasta grafikleri için veri hazırlama
        import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports]
        
        # 1. Ana Maliyet Dağılımı Pasta Grafiği (Sarf + Overhead'in kendi arasındaki dağılımı)
        col_pie1, col_pie2 = st.columns(2)
        
        with col_pie1:
            st.markdown("**🍰 Sarf ve Overhead Dağılımı (Kendi Aralarında)**")
            
            # Pasta grafik verileri - kendi aralarındaki dağılım
            pie_labels = ['Sarf Malzemeleri', 'Genel Giderler']
            pie_sizes = [data['consumables_rate_eff'] * 100, data['overhead_rate_eff'] * 100]
            pie_colors = ['#ff9999', '#66b3ff']
            
            # Pasta grafik oluştur
            fig, ax = plt.subplots(figsize=(8, 6))
            wedges, texts, autotexts = ax.pie(pie_sizes, labels=pie_labels, colors=pie_colors, 
                                            autopct='%1.1f%%', startangle=90)
            ax.set_title('Sarf ve Overhead Dağılımı', fontsize=14, fontweight='bold')
            
            # Grafik stilini ayarla
            plt.setp(autotexts, size=10, weight="bold")
            plt.setp(texts, size=12)
            
            st.pyplot(fig)
            plt.close()
        
        with col_pie2:
            st.markdown("**🎯 Toplam Proje Maliyetine Göre Dağılım**")
            
            # Toplam proje maliyetine göre dağılım
            total_pie_labels = ['Sarf Malzemeleri', 'Genel Giderler', 'Indirect Giderler', 'Ana Maliyet']
            total_pie_sizes = [
                sarf_percent_of_total,
                overhead_percent_of_total,
                indirect_percent_of_total,
                100 - (sarf_percent_of_total + overhead_percent_of_total + indirect_percent_of_total)
            ]
            total_pie_colors = ['#ff9999', '#66b3ff', '#99ff99', '#f0f0f0']
            
            # Pasta grafik oluştur
            fig2, ax2 = plt.subplots(figsize=(8, 6))
            wedges2, texts2, autotexts2 = ax2.pie(total_pie_sizes, labels=total_pie_labels, colors=total_pie_colors, 
                                                autopct='%1.1f%%', startangle=90)
            ax2.set_title('Toplam Proje Maliyetine Göre Dağılım', fontsize=14, fontweight='bold')
            
            # Grafik stilini ayarla
            plt.setp(autotexts2, size=10, weight="bold")
            plt.setp(texts2, size=12)
            
            st.pyplot(fig2)
            plt.close()
        
        # Sorumluluk Matrisi Etkisi Analizi
        st.markdown("---")
        st.markdown("""
        <div style="background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); padding: 1rem; border-radius: 10px; margin: 1rem 0; border: 1px solid #dee2e6;">
            <h4 style="color: #495057; margin: 0; text-align: center; font-weight: 600;">🔗 SORUMLULUK MATRİSİ MALİYET ETKİSİ ANALİZİ</h4>
        </div>
        """, unsafe_allow_html=True)
        
        # Sorumluluk matrisi etkisi hesaplama
        matrix_impact = {
            "consumables": 0.0,
            "overhead": 0.0,
            "indirect": 0.0,
            "total_impact": 0.0
        }
        
        if use_matrix_override and matrix_data:
            # Matris override aktif - etkiyi hesapla
            matrix_impact["consumables"] = matrix_data['consumables'] - (data['consumables_rate_eff'] * 100)
            matrix_impact["overhead"] = matrix_data['overhead'] - (data['overhead_rate_eff'] * 100)
            matrix_impact["indirect"] = matrix_data['indirect'] - (data['indirect_rate_total'] * 100)
            matrix_impact["total_impact"] = abs(matrix_impact["consumables"]) + abs(matrix_impact["overhead"]) + abs(matrix_impact["indirect"])
        
        # Sorumluluk matrisi etkisi gösterimi
        col_matrix_impact1, col_matrix_impact2 = st.columns(2)
        
        with col_matrix_impact1:
            st.markdown("**📊 Sorumluluk Matrisi Etkisi**")
            
            if use_matrix_override and matrix_data:
                # Etki pasta grafiği
                impact_labels = ['Sarf Etkisi', 'Overhead Etkisi', 'Indirect Etkisi']
                impact_sizes = [abs(matrix_impact["consumables"]), abs(matrix_impact["overhead"]), abs(matrix_impact["indirect"])]
                impact_colors = ['#ff6b6b', '#4ecdc4', '#45b7d1']
                
                # Sadece pozitif değerler varsa grafik göster
                if sum(impact_sizes) > 0:
                    fig3, ax3 = plt.subplots(figsize=(8, 6))
                    wedges3, texts3, autotexts3 = ax3.pie(impact_sizes, labels=impact_labels, colors=impact_colors, 
                                                        autopct='%1.1f%%', startangle=90)
                    ax3.set_title('Sorumluluk Matrisi Etkisi Dağılımı', fontsize=14, fontweight='bold')
                    
                    plt.setp(autotexts3, size=10, weight="bold")
                    plt.setp(texts3, size=12)
                    
                    st.pyplot(fig3)
                    plt.close()
                else:
                    st.info("Sorumluluk matrisi etkisi bulunmuyor.")
            else:
                st.info("Sorumluluk matrisi override aktif değil.")
        
        with col_matrix_impact2:
            st.markdown("**📈 Etki Detayları**")
            
            if use_matrix_override and matrix_data:
                st.markdown(f"""
                <div style="background: #f8f9fa; padding: 1rem; border-radius: 8px; border-left: 4px solid #28a745;">
                    <h5 style="color: #333; margin: 0 0 0.5rem 0;">🔗 Sorumluluk Matrisi Etkisi</h5>
                    <p style="margin: 0.2rem 0;"><strong>Sarf Etkisi:</strong> {matrix_impact["consumables"]:+.2f}%</p>
                    <p style="margin: 0.2rem 0;"><strong>Overhead Etkisi:</strong> {matrix_impact["overhead"]:+.2f}%</p>
                    <p style="margin: 0.2rem 0;"><strong>Indirect Etkisi:</strong> {matrix_impact["indirect"]:+.2f}%</p>
                    <p style="margin: 0.2rem 0;"><strong>Toplam Mutlak Etki:</strong> {matrix_impact["total_impact"]:.2f}%</p>
                </div>
                """, unsafe_allow_html=True)
                
                # Etki değerlendirmesi
                if matrix_impact["total_impact"] < 5:
                    impact_assessment = "Düşük Etki"
                    impact_color = "#28a745"
                elif matrix_impact["total_impact"] < 15:
                    impact_assessment = "Orta Etki"
                    impact_color = "#ffc107"
                else:
                    impact_assessment = "Yüksek Etki"
                    impact_color = "#dc3545"
                
                st.markdown(f"""
                <div style="background: #f8f9fa; padding: 1rem; border-radius: 8px; border-left: 4px solid {impact_color}; margin-top: 1rem;">
                    <h5 style="color: #333; margin: 0 0 0.5rem 0;">📊 Etki Değerlendirmesi</h5>
                    <p style="margin: 0.2rem 0;"><strong>Değerlendirme:</strong> {impact_assessment}</p>
                    <p style="margin: 0.2rem 0;"><strong>Toplam Etki:</strong> {matrix_impact["total_impact"]:.2f}%</p>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown("""
                <div style="background: #f8f9fa; padding: 1rem; border-radius: 8px; border-left: 4px solid #6c757d;">
                    <h5 style="color: #333; margin: 0 0 0.5rem 0;">🔗 Sorumluluk Matrisi Durumu</h5>
                    <p style="margin: 0.2rem 0;"><strong>Durum:</strong> Override Aktif Değil</p>
                    <p style="margin: 0.2rem 0;"><strong>Etki:</strong> 0%</p>
                    <p style="margin: 0.2rem 0;"><strong>Açıklama:</strong> Manuel oranlar kullanılıyor</p>
                </div>
                """, unsafe_allow_html=True)
        
        # Karşılaştırma ve Değerlendirme
        st.markdown("---")
        st.markdown("""
        <div style="background: linear-gradient(135deg, #ffecd2 0%, #fcb69f 100%); padding: 1rem; border-radius: 10px; margin: 1rem 0;">
            <h4 style="color: #333; margin: 0; text-align: center;">🔍 MOSKOVA ŞANTİYE KARŞILAŞTIRMA VE DEĞERLENDİRME</h4>
        </div>
        """, unsafe_allow_html=True)
        
        # Profesyonel değerlendirme kriterleri
        if total_rate < 20:
            st.success("""
            <div style="background: #d4edda; border: 1px solid #c3e6cb; border-radius: 8px; padding: 1rem; margin: 1rem 0;">
                <h5 style="color: #155724; margin: 0 0 0.5rem 0;">✅ DÜŞÜK ORANLAR - Maliyet Açısından Avantajlı</h5>
                <p style="color: #155724; margin: 0.2rem 0;"><strong>Toplam Oran:</strong> {total_rate:.2f}%</p>
                <p style="color: #155724; margin: 0.2rem 0;"><strong>Değerlendirme:</strong> Moskova şantiye standartlarına göre düşük maliyet</p>
                <p style="color: #155724; margin: 0.2rem 0;"><strong>Öneri:</strong> Bu oranları koruyarak rekabet avantajı sağlayabilirsiniz</p>
            </div>
            """.format(total_rate=total_rate), unsafe_allow_html=True)
        elif total_rate < 30:
            st.markdown("""
            <div style="background: #d1ecf1; border: 1px solid #bee5eb; border-radius: 8px; padding: 1rem; margin: 1rem 0;">
                <h5 style="color: #0c5460; margin: 0 0 0.5rem 0;">ℹ️ MAKUL ORANLAR - Normal Şantiye Koşulları</h5>
                <p style="color: #0c5460; margin: 0.2rem 0;"><strong>Toplam Oran:</strong> {total_rate:.2f}%</p>
                <p style="color: #0c5460; margin: 0.2rem 0;"><strong>Değerlendirme:</strong> Moskova şantiye gerçeklerine uygun standart oranlar</p>
                <p style="color: #0c5460; margin: 0.2rem 0;"><strong>Öneri:</strong> Mevcut durumu koruyarak proje yönetimini sürdürün</p>
            </div>
            """.format(total_rate=total_rate), unsafe_allow_html=True)
        else:
            st.markdown("""
            <div style="background: #fff3cd; border: 1px solid #ffeaa7; border-radius: 8px; padding: 1rem; margin: 1rem 0;">
                <h5 style="color: #856404; margin: 0 0 0.5rem 0;">⚠️ YÜKSEK ORANLAR - Maliyet Kontrolü Gerekli</h5>
                <p style="color: #856404; margin: 0.2rem 0;"><strong>Toplam Oran:</strong> {total_rate:.2f}%</p>
                <p style="color: #856404; margin: 0.2rem 0;"><strong>Değerlendirme:</strong> Moskova şantiye standartlarının üzerinde maliyet</p>
                <p style="color: #856404; margin: 0.2rem 0;"><strong>Öneri:</strong> Sorumluluk matrisini gözden geçirerek maliyet optimizasyonu yapın</p>
            </div>
            """.format(total_rate=total_rate), unsafe_allow_html=True)
        
        # Indirect giderler değerlendirmesi
        indirect_assessment = ""
        if data['indirect_share'] < 0.05:
            indirect_assessment = "Düşük"
        elif data['indirect_share'] < 0.10:
            indirect_assessment = "Makul"
        else:
            indirect_assessment = "Yüksek"
        
        st.markdown(f"""
        <div style="background: #e2e3e5; border: 1px solid #d6d8db; border-radius: 8px; padding: 1rem; margin: 1rem 0;">
            <h5 style="color: #383d41; margin: 0 0 0.5rem 0;">🧾 Indirect Giderler Değerlendirmesi</h5>
            <p style="color: #383d41; margin: 0.2rem 0;"><strong>Indirect Toplam:</strong> {data['indirect_total']:,.2f} ₽</p>
            <p style="color: #383d41; margin: 0.2rem 0;"><strong>Toplam Maliyete Oranı:</strong> {data['indirect_share']:.1%}</p>
            <p style="color: #383d41; margin: 0.2rem 0;"><strong>Değerlendirme:</strong> {indirect_assessment}</p>
        </div>
        """, unsafe_allow_html=True)

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
    # ---------- 🤖 MOSKOVA ODAKLI GPT ANALİZ VE ÖNERİ SİSTEMİ ----------
    st.markdown("""
    <div style="background: linear-gradient(135deg, #dc2626 0%, #991b1b 100%); padding: 1rem; border-radius: 10px; margin-bottom: 1rem;">
        <h3 style="color: white; margin: 0;">🤖 Moskova Şantiye Analiz Sistemi</h3>
        <p style="color: white; opacity: 0.9; margin: 0.5rem 0 0 0; font-size: 0.9rem;">
            Moskova gerçeklerine göre güvenli tarafta kalma analizi ve eksik gider tespiti
        </p>
    </div>
    """, unsafe_allow_html=True)
    
    # Proje analizi için gelişmiş payload
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
        ),
        "project_complexity": {
            "winter_conditions": bool(st.session_state.get("f_winter",0.0) > 0),
            "heavy_work": bool(st.session_state.get("f_heavy",0.0) > 0),
            "repetitive_work": bool(st.session_state.get("f_repeat",0.0) > 0),
            "shared_equipment": bool(st.session_state.get("f_shared",0.0) > 0),
            "congested_site": bool(st.session_state.get("f_cong",0.0) > 0),
            "pump_required": bool(st.session_state.get("f_pump",0.0) > 0)
        }
    }
    
    gpt_can = get_openai_client() is not None
    
    # Ana analiz butonu - tek buton
    if st.button("🔍 Moskova Detaylı Analiz", disabled=not gpt_can, type="primary"):
        with st.spinner("Moskova şantiye detaylı analizi yapılıyor..."):
            resp = gpt_propose_params(payload)
            if not resp:
                st.error("❌ GPT analizi başarısız oldu. API anahtarını kontrol edin.")
            else:
                st.session_state["gpt_analysis"] = resp
                st.success("✅ Moskova detaylı analizi tamamlandı!")
    
    # GPT Analiz Sonuçları Gösterimi
    if "gpt_analysis" in st.session_state:
        analysis = st.session_state["gpt_analysis"]
        
        st.markdown("### 📋 Moskova Analiz Raporu")
        
        # Özet kartları
        col_summary1, col_summary2, col_summary3, col_summary4 = st.columns(4)
        
        with col_summary1:
            st.metric(
                "Güvenlik Payı", 
                f"{analysis.get('safety_margin', 0):.0f}%",
                delta=f"+{analysis.get('safety_margin', 0):.0f}%"
            )
        
        with col_summary2:
            risk_level = analysis.get('risk_level', 'Orta')
            risk_color = {"Düşük": "normal", "Orta": "normal", "Yüksek": "inverse"}.get(risk_level, "normal")
            st.metric("Risk Seviyesi", risk_level, delta_color=risk_color)
        
        with col_summary3:
            confidence = analysis.get('confidence_score', 0)
            st.metric(
                "Güven Skoru", 
                f"{confidence:.0f}%",
                delta=f"{confidence - 50:.0f}%"
            )
        
        with col_summary4:
            scenario = analysis.get('scenario', 'Gerçekçi')
            st.metric("Önerilen Senaryo", scenario)
        
        # Detaylı analiz
        with st.expander("📊 Moskova Detaylı Analiz", expanded=True):
            tab_reasons, tab_missing, tab_workers, tab_moscow, tab_indirect, tab_raw = st.tabs([
                "🎯 Ana Öneriler", "❌ Eksik Giderler", "👥 İşçi Dağılımı", "🏗️ Moskova Özel", "💰 Indirect Analiz", "🔧 Ham Veri"
            ])
            
            with tab_reasons:
                st.markdown("#### Ana Parametre Önerileri")
                
                # Sarf malzemeleri
                col_cons1, col_cons2 = st.columns([1,2])
                with col_cons1:
                    current_cons = payload["consumables_pct"]
                    suggested_cons = analysis.get("consumables_pct", current_cons)
                    st.metric("Sarf Malzemeleri", f"{suggested_cons:.1f}%", 
                             delta=f"{suggested_cons - current_cons:.1f}%")
                with col_cons2:
                    st.info(analysis.get("reasons", {}).get("consumables", "Gerekçe belirtilmemiş"))
                
                # Genel giderler
                col_over1, col_over2 = st.columns([1,2])
                with col_over1:
                    current_over = payload["overhead_pct"]
                    suggested_over = analysis.get("overhead_pct", current_over)
                    st.metric("Genel Giderler", f"{suggested_over:.1f}%",
                             delta=f"{suggested_over - current_over:.1f}%")
                with col_over2:
                    st.info(analysis.get("reasons", {}).get("overhead", "Gerekçe belirtilmemiş"))
                
                # Çalışma saati
                col_hours1, col_hours2 = st.columns([1,2])
                with col_hours1:
                    current_hours = payload["hours_per_day"]
                    suggested_hours = analysis.get("hours_per_day", current_hours)
                    st.metric("Günlük Çalışma", f"{suggested_hours:.1f} saat",
                             delta=f"{suggested_hours - current_hours:.1f} saat")
                with col_hours2:
                    st.info(analysis.get("reasons", {}).get("hours", "Gerekçe belirtilmemiş"))
                
                # Senaryo
                col_scen1, col_scen2 = st.columns([1,2])
                with col_scen1:
                    current_scen = payload["scenario"]
                    suggested_scen = analysis.get("scenario", current_scen)
                    st.metric("Senaryo", suggested_scen,
                             delta="Değişiklik" if suggested_scen != current_scen else "Aynı")
                with col_scen2:
                    st.info(analysis.get("reasons", {}).get("scenario", "Gerekçe belirtilmemiş"))
            
            with tab_missing:
                st.markdown("#### Eksik Giderler ve Malzemeler")
                
                # Eksik sarf malzemeleri
                missing_consumables = analysis.get("missing_items", {}).get("consumables", [])
                if missing_consumables:
                    st.markdown("**❌ Eksik Sarf Malzemeleri:**")
                    for item in missing_consumables:
                        st.markdown(f"• {item}")
                else:
                    st.info("Eksik sarf malzemesi bulunmuyor.")
                
                # Eksik genel giderler
                missing_overhead = analysis.get("missing_items", {}).get("overhead", [])
                if missing_overhead:
                    st.markdown("**❌ Eksik Genel Giderler:**")
                    for item in missing_overhead:
                        st.markdown(f"• {item}")
                else:
                    st.info("Eksik genel gider bulunmuyor.")
                
                # Eksik indirect giderler
                missing_indirect = analysis.get("missing_items", {}).get("indirect", [])
                if missing_indirect:
                    st.markdown("**❌ Eksik Indirect Giderler:**")
                    for item in missing_indirect:
                        st.markdown(f"• {item}")
                else:
                    st.info("Eksik indirect gider bulunmuyor.")
            
            with tab_workers:
                st.markdown("#### İşçi Dağılımı Analizi")
                
                worker_dist = analysis.get("worker_distribution", {})
                if worker_dist:
                    col_w1, col_w2, col_w3 = st.columns(3)
                    
                    with col_w1:
                        demirci_ratio = worker_dist.get("demirci_ratio", 0)
                        st.metric("Demirci Oranı", f"{demirci_ratio:.1f}%")
                    
                    with col_w2:
                        kalipci_ratio = worker_dist.get("kalipci_ratio", 0)
                        st.metric("Kalıpçı Oranı", f"{kalipci_ratio:.1f}%")
                    
                    with col_w3:
                        duz_isci_ratio = worker_dist.get("duz_isci_ratio", 0)
                        st.metric("Düz İşçi Oranı", f"{duz_isci_ratio:.1f}%")
                    
                    # İşçi dağılımı analizi
                    analysis_text = worker_dist.get("analysis", "Analiz bulunmuyor.")
                    st.info(analysis_text)
                else:
                    st.info("İşçi dağılımı analizi bulunmuyor.")
            
            with tab_moscow:
                st.markdown("#### Moskova Özel Analiz")
                
                moscow_spec = analysis.get("moscow_specific", {})
                if moscow_spec:
                    st.markdown(f"**❄️ Kış Etkisi:** {moscow_spec.get('winter_impact', 'Belirtilmemiş')}")
                    st.markdown(f"**⚡ Verimlilik Faktörleri:** {moscow_spec.get('efficiency_factors', 'Belirtilmemiş')}")
                    st.markdown(f"**🛡️ Güvenlik Gereksinimleri:** {moscow_spec.get('safety_requirements', 'Belirtilmemiş')}")
                    st.markdown(f"**💰 Ek Maliyetler:** {moscow_spec.get('additional_costs', 'Belirtilmemiş')}")
                else:
                    st.info("Moskova özel analizi bulunmuyor.")
            
            with tab_indirect:
                st.markdown("#### Indirect Giderler Analizi")
                
                indirect_analysis = analysis.get("indirect_analysis", {})
                if indirect_analysis:
                    col_ind1, col_ind2, col_ind3 = st.columns(3)
                    
                    with col_ind1:
                        total_indirect = indirect_analysis.get("total_indirect_rate", 0)
                        st.metric("Toplam Indirect", f"{total_indirect:.1f}%")
                    
                    with col_ind2:
                        cost_ratio = indirect_analysis.get("total_cost_ratio", 0)
                        st.metric("Maliyet Oranı", f"{cost_ratio:.1f}%")
                    
                    with col_ind3:
                        assessment = indirect_analysis.get("assessment", "makul")
                        st.metric("Değerlendirme", assessment)
                    
                    # Detaylı indirect analizi
                    detailed_analysis = indirect_analysis.get("detailed_analysis", "Analiz bulunmuyor.")
                    st.info(detailed_analysis)
                else:
                    st.info("Indirect analizi bulunmuyor.")
            
            with tab_raw:
                st.json(analysis)
        
        # Uygulama butonları
        col_apply1, col_apply2, col_apply3 = st.columns([1,1,1])
        
        with col_apply1:
            if st.button("✅ Önerileri Uygula", type="primary"):
                st.session_state["consumables_rate"] = float(analysis.get("consumables_pct", payload["consumables_pct"]))
                st.session_state["overhead_rate"] = float(analysis.get("overhead_pct", payload["overhead_pct"]))
                st.session_state["hours_per_day"] = float(analysis.get("hours_per_day", payload["hours_per_day"]))
                st.session_state["scenario"] = str(analysis.get("scenario", payload["scenario"]))
                st.success("✅ Moskova önerileri başarıyla uygulandı!")
                st.rerun()
        
        with col_apply2:
            if st.button("📊 Eksik Giderleri Ekle"):
                st.info("Eksik giderleri otomatik ekleme özelliği geliştiriliyor...")
        
        with col_apply3:
            if st.button("🗑️ Analizi Temizle"):
                st.session_state.pop("gpt_analysis", None)
                st.success("Analiz temizlendi.")
                st.rerun()

    # ---------- RAG ----------
    bih("📚 RAG: Dosya yükle → indeksle → ara","📚 RAG: загрузить → проиндексировать → искать", level=3)
    
    # RAG Durum Gösterimi
    # RAG Durumu (opsiyonel)
    if _RAG_BACKEND_AVAILABLE:
        status = get_status()
        col_status1, col_status2, col_status3 = st.columns(3)
        with col_status1:
            st.metric("📊 Toplam Kayıt", f"{status['count']:,}")
        with col_status2:
            st.metric("🔢 Boyut", f"{status['dimension'] or '-'}")
        with col_status3:
            st.metric("💾 İndeks Durumu", "✅ Aktif" if status['index_exists'] else "❌ Yok")
        
        # Performans uyarısı
        if status['count'] > 20000:
            st.warning("⚠️ **Performans Uyarısı:** Çok büyük indeks (>20k kayıt). Arama yavaşlayabilir.")
    else:
        st.warning("⚠️ **RAG sistemi kullanılamıyor.** FAISS yüklenemedi.")
        st.info("💡 Alternatif: Dosya yükleme ile çalışabilirsiniz.")
    
    uploads = st.file_uploader(bi("Dosya yükle (.txt, .csv, .xlsx)","Загрузить файлы (.txt, .csv, .xlsx)"), type=["txt","csv","xlsx"], accept_multiple_files=True, key="rag_up")
    
    if _RAG_BACKEND_AVAILABLE:
        cR1, cR2, cR3 = st.columns(3)
        with cR1:
            if st.button(bi("📥 İndeksle (Embed + Kaydet)","📥 Проиндексировать (эмбед + сохранить)")):
            if not uploads:
                st.warning(bi("Dosya seçin.","Выберите файл(ы)."))
            else:
                # Progress bar başlat
                progress_bar = st.progress(0)
                status_text = st.empty()
                
                try:
                    # 1. Dosyaları parçalara ayır
                    status_text.text("📄 Dosyalar parçalara ayrılıyor...")
                    chunks = []
                    for up in uploads: 
                        chunks += file_to_chunks(up)
                    progress_bar.progress(25)
                    
                    if not chunks:
                        st.warning(bi("Parça yok.","Нет фрагментов."))
                    else:
                        # 2. Metinleri ve meta verileri hazırla
                        status_text.text("🔤 Metinler hazırlanıyor...")
                        texts = [c["text"] for c in chunks]
                        metas = [c.get("meta", {}) for c in chunks]
                        progress_bar.progress(50)
                        
                        # 3. Embedding'leri al
                        status_text.text("🧠 Embedding'ler oluşturuluyor...")
                        embs = embed_texts(texts)
                        progress_bar.progress(75)
                        
                        if not embs:
                            st.error(bi("Embed alınamadı (OpenAI anahtarı gerekli).","Не удалось получить эмбеддинги (нужен ключ OpenAI)."))
                        else:
                            # 4. FAISS backend'e ekle
                            status_text.text("💾 FAISS indeksine kaydediliyor...")
                            import numpy as np
                            embs_np = np.array(embs, dtype=np.float32)
                            
                            ids = add_records(texts, metas, embs_np)
                            progress_bar.progress(100)
                            status_text.text("✅ Tamamlandı!")
                            
                            st.success(f"✅ FAISS indeksine {len(ids)} kayıt eklendi.")
                    
                except Exception as e:
                    st.error(f"❌ İndeksleme sırasında hata: {str(e)}")
                finally:
                                            # Progress bar'ı temizle
                        progress_bar.empty()
                        status_text.empty()
        with cR2:
            if st.button(bi("🧹 RAG temizle","🧹 Очистить RAG")):
                try:
                    reset_backend()
                    st.success(bi("✅ İndeks sıfırlandı.","✅ Индекс сброшен."))
                except Exception as e:
                    st.error(bi(f"❌ Hata: {e}", f"❌ Ошибка: {e}"))
        with cR3:
        q = st.text_input(bi("🔎 RAG' de ara","🔎 Поиск в RAG"), value=st.session_state.get("rag_q",""))
        
        # Filtre inputları
        col_filter1, col_filter2 = st.columns(2)
        with col_filter1:
            filename_filter = st.text_input("📁 Dosya adı (içerir)", placeholder="örn: proje")
        with col_filter2:
            project_filter = st.text_input("🏷️ Proje etiketi", placeholder="örn: XYZ")
        
        if st.button(bi("Ara","Найти"), key="rag_search_btn"):
            if q.strip():
                try:
                    # Query embedding'i al
                    qemb = embed_texts([q.strip()])
                    if not qemb:
                        st.error("❌ Query embedding alınamadı.")
                    else:
                        import numpy as np
                        qemb_np = np.array(qemb[0], dtype=np.float32)
                        
                        # Filtreleri hazırla
                        filters = {}
                        if filename_filter:
                            filters["filename_contains"] = filename_filter
                        if project_filter:
                            filters["project"] = project_filter
                        
                        # FAISS ile ara
                        hits = search(qemb_np, topk=6, filters=filters)
                        st.session_state["rag_hits"] = hits
                        st.session_state["rag_q"] = q.strip()
                        
                        if hits:
                            st.success(f"✅ {len(hits)} sonuç bulundu.")
                        else:
                            st.info("ℹ️ Sonuç bulunamadı.")
                except Exception as e:
                    st.error(f"❌ Arama sırasında hata: {str(e)}")
            else:
                st.warning("⚠️ Arama terimi girin.")
    # Arama sonuçlarını göster
    if st.session_state.get("rag_hits"):
        st.markdown("### 🔍 Arama Sonuçları")
        for i, hit in enumerate(st.session_state["rag_hits"]):
            with st.expander(f"📄 {hit.get('meta', {}).get('filename', 'Bilinmeyen')} (Skor: {hit['score']:.3f})"):
                # Skor değerlendirmesi
                score_badge = ""
                if hit['score'] < 0.15:
                    score_badge = "🔴 Düşük güven"
                elif hit['score'] < 0.25:
                    score_badge = "🟡 Orta güven"
                else:
                    score_badge = "🟢 Yüksek güven"
                
                st.markdown(f"**{score_badge}** | **Dosya:** {hit.get('meta', {}).get('filename', 'Bilinmeyen')} | **Proje:** {hit.get('meta', {}).get('project', 'Belirtilmemiş')}")
                st.markdown("---")
                st.text(hit.get("text", "")[:1000] + ("..." if len(hit.get("text", "")) > 1000 else ""))

    # ---------- 🤖 AUTO-RAG SİSTEMİ ----------
    bih("🤖 Auto-RAG Asistanı","🤖 Auto-RAG Ассистент", level=3)
    
    # Auto-RAG Toggle
    auto_rag_enabled = st.toggle("🔄 Auto-RAG (önerileri otomatik getir)", value=st.session_state.get("auto_rag", True), key="auto_rag")
    
    if auto_rag_enabled:
        # Auto-RAG çalıştır
        run_auto_rag()
        
        # Öneri paneli göster
        if "auto_rag_suggestions" in st.session_state and st.session_state["auto_rag_suggestions"]:
            st.markdown("### 📎 Belgelerden Öneriler")
            
            selected_suggestions = []
            
            for suggestion in st.session_state["auto_rag_suggestions"]:
                field = suggestion.get('field', 'Bilinmeyen')
                value = suggestion.get('value', 0)
                unit = suggestion.get('unit', '')
                source = suggestion.get('source', 'Bilinmeyen')
                confidence = suggestion.get('confidence', 0)
                rationale = suggestion.get('rationale', '')
                
                # Öneri satırı
                col_sugg1, col_sugg2, col_sugg3, col_sugg4 = st.columns([2, 1, 1, 1])
                
                with col_sugg1:
                    st.markdown(f"**{field}** → **{value} {unit}**")
                    st.caption(f"Kaynak: {source} | Güven: {confidence:.0%}")
                
                with col_sugg2:
                    if st.checkbox("Uygula", key=f"apply_{field}_{value}"):
                        selected_suggestions.append(suggestion)
                
                with col_sugg3:
                    if st.button("📄 Kaynağı aç", key=f"source_{field}_{value}"):
                        # Kaynak snippet'i göster
                        snippets = st.session_state.get("auto_rag_snippets", [])
                        if snippets:
                            st.markdown("#### 📄 Kaynak Belge")
                            for snippet in snippets[:3]:  # İlk 3 snippet
                                st.markdown(f"**Dosya:** {snippet.get('meta', {}).get('filename', 'Bilinmeyen')}")
                                st.markdown(f"**Skor:** {snippet.get('score', 0):.3f}")
                                st.text(snippet.get('text', '')[:500] + "...")
                                st.markdown("---")
                
                with col_sugg4:
                    if confidence < 0.7:
                        st.markdown("⚠️ Düşük güven")
                    elif confidence < 0.85:
                        st.markdown("🟡 Orta güven")
                    else:
                        st.markdown("🟢 Yüksek güven")
                
                # Gerekçe
                if rationale:
                    st.info(f"💡 **Gerekçe:** {rationale}")
                
                st.markdown("---")
            
            # Seçilenleri uygula butonu
            if selected_suggestions:
                if st.button("✅ Seçilenleri Uygula", type="primary"):
                    apply_suggestions(selected_suggestions)
                    st.rerun()
        else:
            # RAG durumu kontrolü (opsiyonel)
            if _RAG_BACKEND_AVAILABLE:
                status = get_status()
                if status['count'] == 0:
                    st.warning("⚠️ **Henüz RAG verisi yok.** Dosya yükleyip indeksleyin.")
                else:
                    st.info("ℹ️ **Auto-RAG aktif.** Değişikliklerde otomatik öneriler gelecek.")
            else:
                st.warning("⚠️ **RAG sistemi kullanılamıyor.** FAISS yüklenemedi.")
        
        # Auto-RAG Günlük
        if "change_log" in st.session_state and st.session_state["change_log"]:
            with st.expander("📋 Auto-RAG Günlük (Son 20 İşlem)"):
                changes = st.session_state["change_log"][-20:]  # Son 20
                
                if changes:
                    # Tablo formatında göster
                    change_data = []
                    for change in changes:
                        change_data.append({
                            "Tarih": change.get('timestamp', '')[:19],  # İlk 19 karakter
                            "Alan": change.get('field', ''),
                            "Eski Değer": change.get('old_value', ''),
                            "Yeni Değer": change.get('new_value', ''),
                            "Kaynak": change.get('source', '')
                        })
                    
                    df_changes = pd.DataFrame(change_data)
                    st.dataframe(df_changes, use_container_width=True)
                else:
                    st.info("Henüz değişiklik yok.")
    else:
        st.info("Auto-RAG kapalı. Öneriler için açın.")

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
                    model="gpt-4o", temperature=0.2,
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