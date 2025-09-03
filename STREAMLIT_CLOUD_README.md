# 🚀 Streamlit Cloud Deployment Rehberi

## ✅ Hazır Dosyalar

### 1. `runtime.txt`
```
python-3.11.9
```
- Python sürümünü 3.11.9'a sabitledi
- Streamlit Cloud uyumlu

### 2. `requirements.txt`
```
# Modern paket sürümleri
streamlit>=1.32
numpy>=1.26,<2
pandas>=2.2
matplotlib>=3.10.0
openai>=1.99.0
requests>=2.32.0

# FAISS - Streamlit Cloud uyumlu sürüm
faiss-cpu==1.12.0 ; python_version >= "3.10" and platform_machine == "x86_64"
```
- FAISS sürümünü 1.12.0'a güncelledi
- Platform kısıtlaması eklendi (x86_64)
- Modern paket sürümleri

### 3. `rag_backend.py`
- FAISS opsiyonel import eklendi
- Hata durumlarında graceful fallback
- Streamlit Cloud uyumlu

### 4. `betonarme_hesap_modulu_r0.py`
- FAISS ve RAG backend opsiyonel import
- Auto-RAG sistemi entegre
- Streamlit Cloud uyumlu

## 🔧 Deployment Adımları

### 1. GitHub'a Yükle
```bash
git add .
git commit -m "Streamlit Cloud uyumluluğu eklendi"
git push origin main
```

### 2. Streamlit Cloud'da Deploy
1. [share.streamlit.io](https://share.streamlit.io) adresine git
2. GitHub repo'yu bağla
3. Ana dosya: `betonarme_hesap_modulu_r0.py`
4. Deploy et

### 3. Environment Variables
Streamlit Cloud'da şu environment variable'ları ekle:
- `OPENAI_API_KEY`: OpenAI API anahtarın

## ⚠️ Önemli Notlar

### FAISS Durumu
- **FAISS mevcut:** RAG sistemi tam çalışır
- **FAISS yok:** RAG devre dışı, diğer özellikler çalışır
- **Auto-RAG:** FAISS yoksa otomatik devre dışı kalır

### RAG Özellikleri
- ✅ Dosya yükleme (TXT, CSV, XLSX)
- ✅ FAISS indeksleme (opsiyonel)
- ✅ Arama ve filtreleme
- ✅ Auto-RAG sistemi
- ✅ Öneri uygulama

### Performans
- İlk yükleme: ~30 saniye
- RAG işlemleri: FAISS varlığına bağlı
- Memory kullanımı: Optimize edildi

## 🐛 Sorun Giderme

### FAISS Yüklenemezse
```
⚠️ FAISS kullanılamıyor. RAG özellikleri devre dışı.
```
- Normal, diğer özellikler çalışır
- RAG panelinde uyarı gösterilir

### OpenAI API Hatası
```
❌ Embed alınamadı (OpenAI anahtarı gerekli).
```
- Environment variable'ları kontrol et
- API anahtarının doğru olduğundan emin ol

### Memory Hatası
- Büyük dosyalar için chunk size'ı azalt
- RAG indeksini temizle
- Streamlit Cloud memory limitini kontrol et

## 📊 Özellik Durumu

| Özellik | Durum | Not |
|---------|-------|------|
| Ana Hesaplama | ✅ Çalışır | Tam uyumlu |
| GPT Önerileri | ✅ Çalışır | OpenAI API gerekli |
| RAG Sistemi | ⚠️ Opsiyonel | FAISS bağımlı |
| Auto-RAG | ⚠️ Opsiyonel | FAISS bağımlı |
| Dosya Export | ✅ Çalışır | Tam uyumlu |
| UI/UX | ✅ Çalışır | Modern tasarım |

## 🎯 Sonuç

Sistem Streamlit Cloud'da tam çalışır durumda. FAISS yüklenemezse RAG özellikleri devre dışı kalır ama diğer tüm özellikler çalışmaya devam eder.
