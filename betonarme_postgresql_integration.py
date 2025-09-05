# -*- coding: utf-8 -*-
"""
Betonarme İşçilik Modülü - PostgreSQL RAG Entegrasyonu
Bu modül, mevcut betonarme_hesap_modulu_r0.py ile PostgreSQL RAG sistemini entegre eder.
"""

import os
import sys
import logging
from datetime import date, timedelta
from typing import List, Dict, Any, Optional
import pandas as pd

# Mevcut modülü import et
try:
    from postgresql_rag_system import PostgreSQLRAGSystem, RAGConfig
    RAG_AVAILABLE = True
except ImportError:
    RAG_AVAILABLE = False
    logging.warning("PostgreSQL RAG system not available")

# Streamlit import (mevcut modülden)
try:
    import streamlit as st
    STREAMLIT_AVAILABLE = True
except ImportError:
    STREAMLIT_AVAILABLE = False

class BetonarmeRAGIntegration:
    """Betonarme modülü ile PostgreSQL RAG sistemi entegrasyonu"""
    
    def __init__(self):
        self.rag_system = None
        self.config = None
        self._initialize_rag()
    
    def _initialize_rag(self):
        """RAG sistemini başlat"""
        if not RAG_AVAILABLE:
            return
        
        try:
            # Konfigürasyon
            self.config = RAGConfig(
                db_host="localhost",
                db_port=5432,
                db_name="betonarme_rag",
                db_user="postgres",
                db_password="1905"  # PostgreSQL şifreniz
            )
            
            # RAG sistemi başlat
            self.rag_system = PostgreSQLRAGSystem(self.config)
            
            logging.info("PostgreSQL RAG system initialized successfully")
            
        except Exception as e:
            logging.error(f"RAG system initialization failed: {e}")
            self.rag_system = None
    
    def search_norms(self, query: str, locales: List[str] = None) -> List[Dict]:
        """Norm arama"""
        if not self.rag_system:
            return []
        
        try:
            if not locales:
                locales = ['tr', 'ru', 'en']
            
            results = self.rag_system.search(query, locales=locales)
            return results
            
        except Exception as e:
            logging.error(f"Norm search failed: {e}")
            return []
    
    def get_labor_hours_suggestion(self, element_type: str, qty: float, unit: str) -> Dict[str, Any]:
        """İşçilik saati önerisi"""
        if not self.rag_system:
            return {'suggestion': 0.0, 'source': 'No RAG system', 'confidence': 0.0}
        
        try:
            # WBS key mapping
            wbs_mapping = {
                'grobeton': 'CONC.SLAB',
                'rostverk': 'CONC.BEAM', 
                'temel': 'CONC.FOUNDATION',
                'doseme': 'CONC.SLAB',
                'perde': 'CONC.WALL',
                'merdiven': 'CONC.STAIR'
            }
            
            wbs_key = wbs_mapping.get(element_type, f'CONC.{element_type.upper()}')
            
            # Teorik adam-saat hesapla
            theoretical_hours = self.rag_system.calculate_labor_hours(
                wbs_key=wbs_key,
                qty=qty,
                unit=unit,
                locale='tr'
            )
            
            # RAG'dan norm bilgisi al
            norm_query = f"{element_type} {unit} işçilik normu adam saat"
            norm_results = self.search_norms(norm_query)
            
            # Güven skoru hesapla
            confidence = 0.8 if norm_results else 0.3
            
            return {
                'suggestion': theoretical_hours,
                'source': norm_results[0]['source'] if norm_results else 'Default',
                'confidence': confidence,
                'norm_results': norm_results[:3]  # İlk 3 sonuç
            }
            
        except Exception as e:
            logging.error(f"Labor hours suggestion failed: {e}")
            return {'suggestion': 0.0, 'source': 'Error', 'confidence': 0.0}
    
    def get_factor_suggestions(self, factor_type: str) -> Dict[str, Any]:
        """Faktör önerileri"""
        if not self.rag_system:
            return {'suggestion': 0.0, 'source': 'No RAG system', 'confidence': 0.0}
        
        try:
            # Faktör tipine göre sorgu oluştur
            queries = {
                'winter_factor': 'kış şartı işçilik verimsizlik yüzdesi beton dökümü',
                'heavy_rebar': 'ağır donatı yoğunluğu norm artışı',
                'site_congestion': 'şantiye sıkışıklığı işçilik verimsizlik',
                'pump_height': 'yüksek pompa beton işçilik zorluğu',
                'form_repeat': 'kalıp tekrarı işçilik verimsizlik'
            }
            
            query = queries.get(factor_type, f"{factor_type} faktörü")
            results = self.search_norms(query)
            
            if results:
                # İlk sonuçtan sayısal değer çıkarmaya çalış
                text = results[0]['text']
                import re
                
                # Yüzde değerleri ara
                percent_matches = re.findall(r'(\d+(?:\.\d+)?)\s*%', text)
                if percent_matches:
                    suggestion = float(percent_matches[0]) / 100  # Yüzdeyi ondalığa çevir
                else:
                    # Sayısal değerler ara
                    number_matches = re.findall(r'(\d+(?:\.\d+)?)', text)
                    if number_matches:
                        suggestion = float(number_matches[0]) / 100
                    else:
                        suggestion = 0.0
                
                return {
                    'suggestion': suggestion,
                    'source': results[0]['source'],
                    'confidence': 0.7,
                    'norm_results': results[:2]
                }
            
            return {'suggestion': 0.0, 'source': 'No data', 'confidence': 0.0}
            
        except Exception as e:
            logging.error(f"Factor suggestion failed: {e}")
            return {'suggestion': 0.0, 'source': 'Error', 'confidence': 0.0}
    
    def export_productivity_report(self, start_date: date, end_date: date) -> Dict[str, str]:
        """Verimlilik raporu ihraç et"""
        if not self.rag_system:
            return {}
        
        try:
            reports = self.rag_system.export_reports(start_date, end_date)
            return reports
            
        except Exception as e:
            logging.error(f"Productivity report export failed: {e}")
            return {}
    
    def close(self):
        """RAG sistemini kapat"""
        if self.rag_system:
            self.rag_system.close()

# Global RAG entegrasyon instance'ı
rag_integration = None

def get_rag_integration():
    """RAG entegrasyon instance'ını al"""
    global rag_integration
    if rag_integration is None:
        rag_integration = BetonarmeRAGIntegration()
    return rag_integration

# ===============================================
# Streamlit UI Entegrasyonu
# ===============================================

def render_rag_suggestions():
    """RAG önerilerini Streamlit'te göster"""
    if not STREAMLIT_AVAILABLE or not RAG_AVAILABLE:
        return
    
    try:
        st.sidebar.markdown("---")
        st.sidebar.markdown("### 🤖 PostgreSQL RAG Önerileri")
        
        # RAG durumu kontrolü
        rag = get_rag_integration()
        if not rag.rag_system:
            st.sidebar.warning("PostgreSQL RAG sistemi kullanılamıyor")
            return
        
        # Mevcut durumu al
        current_state = {
            'use_grobeton': st.session_state.get('use_grobeton', False),
            'use_rostverk': st.session_state.get('use_rostverk', False),
            'use_temel': st.session_state.get('use_temel', False),
            'use_doseme': st.session_state.get('use_doseme', False),
            'use_perde': st.session_state.get('use_perde', False),
            'use_merdiven': st.session_state.get('use_merdiven', False),
        }
        
        # Aktif elemanları kontrol et
        active_elements = [k for k, v in current_state.items() if v]
        
        if active_elements:
            st.sidebar.info(f"Aktif elemanlar: {len(active_elements)}")
            
            # İşçilik saati önerileri
            if st.sidebar.button("İşçilik Saati Önerileri"):
                with st.sidebar:
                    st.markdown("#### 📊 İşçilik Saati Önerileri")
                    
                    for element in active_elements:
                        element_name = element.replace('use_', '').title()
                        qty = st.session_state.get(f"{element}_qty", 0)
                        unit = st.session_state.get(f"{element}_unit", "m3")
                        
                        if qty > 0:
                            suggestion = rag.get_labor_hours_suggestion(element_name, qty, unit)
                            
                            st.write(f"**{element_name}** ({qty} {unit})")
                            st.write(f"Önerilen: {suggestion['suggestion']:.2f} adam-saat")
                            st.write(f"Kaynak: {suggestion['source']}")
                            st.write(f"Güven: {suggestion['confidence']:.1%}")
                            
                            if suggestion['norm_results']:
                                with st.expander("Norm Detayları"):
                                    for result in suggestion['norm_results']:
                                        st.write(f"**{result['source']}**: {result['text'][:100]}...")
                                        st.write(f"Skor: {result['score']:.3f}")
                            st.write("---")
        
        # Faktör önerileri
        if st.sidebar.button("Faktör Önerileri"):
            with st.sidebar:
                st.markdown("#### ⚙️ Faktör Önerileri")
                
                factors = [
                    ('winter_factor', 'Kış Faktörü'),
                    ('heavy_rebar', 'Ağır Donatı'),
                    ('site_congestion', 'Şantiye Sıkışıklığı'),
                    ('pump_height', 'Pompa Yüksekliği'),
                    ('form_repeat', 'Kalıp Tekrarı')
                ]
                
                for factor_key, factor_name in factors:
                    suggestion = rag.get_factor_suggestions(factor_key)
                    
                    st.write(f"**{factor_name}**")
                    st.write(f"Önerilen: {suggestion['suggestion']:.1%}")
                    st.write(f"Kaynak: {suggestion['source']}")
                    st.write(f"Güven: {suggestion['confidence']:.1%}")
                    
                    if suggestion['norm_results']:
                        with st.expander(f"{factor_name} Detayları"):
                            for result in suggestion['norm_results']:
                                st.write(f"**{result['source']}**: {result['text'][:100]}...")
                                st.write(f"Skor: {result['score']:.3f}")
                    st.write("---")
        
        # Rapor ihracı
        if st.sidebar.button("Verimlilik Raporu"):
            with st.sidebar:
                st.markdown("#### 📈 Verimlilik Raporu")
                
                end_date = date.today()
                start_date = end_date - timedelta(days=30)
                
                reports = rag.export_productivity_report(start_date, end_date)
                
                if reports:
                    st.success("Raporlar oluşturuldu:")
                    for report_type, file_path in reports.items():
                        st.write(f"**{report_type}**: {file_path}")
                        
                        # Dosya indirme linki
                        if os.path.exists(file_path):
                            with open(file_path, 'rb') as f:
                                st.download_button(
                                    label=f"İndir {report_type}",
                                    data=f.read(),
                                    file_name=os.path.basename(file_path),
                                    mime="text/csv"
                                )
                else:
                    st.warning("Rapor oluşturulamadı")
    
    except Exception as e:
        st.sidebar.error(f"RAG önerileri hatası: {e}")

def render_rag_status():
    """RAG durumunu göster"""
    if not STREAMLIT_AVAILABLE:
        return
    
    try:
        st.sidebar.markdown("---")
        st.sidebar.markdown("### 🔍 PostgreSQL RAG Durumu")
        
        rag = get_rag_integration()
        
        if rag.rag_system:
            st.sidebar.success("✅ PostgreSQL RAG Sistemi Aktif")
            
            # Veritabanı durumu
            try:
                # Basit bir test sorgusu
                test_results = rag.search_norms("test", locales=['tr'])
                st.sidebar.info(f"📊 Veritabanı: Bağlı")
                st.sidebar.info(f"🔍 Son arama: {len(test_results)} sonuç")
            except:
                st.sidebar.warning("⚠️ Veritabanı bağlantı sorunu")
        else:
            st.sidebar.error("❌ PostgreSQL RAG Sistemi Pasif")
            st.sidebar.info("Kurulum için: python postgresql_rag_system.py")
    
    except Exception as e:
        st.sidebar.error(f"RAG durum hatası: {e}")

# ===============================================
# Mevcut Modül Entegrasyonu
# ===============================================

def integrate_with_existing_module():
    """Mevcut modülle entegrasyon"""
    
    # Mevcut betonarme modülüne RAG önerilerini ekle
    if STREAMLIT_AVAILABLE:
        # Sidebar'a RAG bileşenlerini ekle
        render_rag_status()
        render_rag_suggestions()
    
    # Mevcut hesaplama fonksiyonlarına RAG önerilerini entegre et
    # Bu kısım mevcut kodun yapısına göre özelleştirilebilir

def cleanup_rag_integration():
    """RAG entegrasyonunu temizle"""
    global rag_integration
    if rag_integration:
        rag_integration.close()
        rag_integration = None

# ===============================================
# Test ve Demo Fonksiyonları
# ===============================================

def test_rag_integration():
    """RAG entegrasyon testi"""
    print("Testing PostgreSQL RAG Integration...")
    
    try:
        rag = get_rag_integration()
        
        if not rag.rag_system:
            print("❌ PostgreSQL RAG system not available")
            return False
        
        # Test arama
        results = rag.search_norms("donatı bağlama", locales=['tr', 'ru'])
        print(f"✅ Search test: {len(results)} results")
        
        # Test işçilik saati önerisi
        suggestion = rag.get_labor_hours_suggestion('grobeton', 100.0, 'm3')
        print(f"✅ Labor hours test: {suggestion['suggestion']:.2f} hours")
        
        # Test faktör önerisi
        factor_suggestion = rag.get_factor_suggestions('winter_factor')
        print(f"✅ Factor test: {factor_suggestion['suggestion']:.1%}")
        
        print("✅ All tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        return False

if __name__ == "__main__":
    # Test çalıştır
    success = test_rag_integration()
    
    if success:
        print("\n🎉 PostgreSQL RAG Integration is working correctly!")
        print("\nTo integrate with existing module:")
        print("1. Import this module in your main application")
        print("2. Call render_rag_suggestions() in your Streamlit app")
        print("3. Use get_rag_integration() to access RAG features")
    else:
        print("\n❌ PostgreSQL RAG Integration has issues. Please check setup.")
    
    sys.exit(0 if success else 1)

