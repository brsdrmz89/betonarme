# -*- coding: utf-8 -*-
"""
PostgreSQL RAG System for Betonarme İşçilik Modülü
pgvector olmadan basit implementasyon
"""

import os
import json
import logging
import psycopg2
import psycopg2.extras
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, date, timedelta
import pandas as pd
from dataclasses import dataclass

# Logging ayarla
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class RAGConfig:
    """RAG sistem konfigürasyonu"""
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "betonarme_rag"
    db_user: str = "postgres"
    db_password: str = "1905"
    
    # Retriever ayarları
    default_top_k: int = 8
    score_threshold: float = 0.78
    min_sources: int = 2
    
    # Güvenlik ayarları
    enable_audit_log: bool = True
    enable_source_diversification: bool = True
    enable_score_filtering: bool = True

class PostgreSQLRAGSystem:
    """PostgreSQL tabanlı RAG sistemi"""
    
    def __init__(self, config: RAGConfig):
        self.config = config
        self.connection = None
        self._connect()
    
    def _connect(self):
        """Veritabanına bağlan"""
        try:
            self.connection = psycopg2.connect(
                host=self.config.db_host,
                port=self.config.db_port,
                database=self.config.db_name,
                user=self.config.db_user,
                password=self.config.db_password
            )
            self.connection.autocommit = True
            logger.info("✅ PostgreSQL bağlantısı başarılı")
        except Exception as e:
            logger.error(f"❌ PostgreSQL bağlantı hatası: {e}")
            raise
    
    def add_document(self, source: str, country: str, doc_type: str, 
                    title: str, lang: str, content: str, **kwargs) -> int:
        """Doküman ekle"""
        cursor = self.connection.cursor()
        
        # Dokümanı ekle
        cursor.execute("""
        INSERT INTO documents (source, country, doc_type, title, lang, content)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """, (source, country, doc_type, title, lang, content))
        
        doc_id = cursor.fetchone()[0]
        
        # İçeriği chunk'lara böl
        chunks = self._chunk_text(content, title)
        
        # Chunk'ları ekle
        for chunk in chunks:
            self._add_chunk(doc_id, chunk)
        
        logger.info(f"✅ Doküman eklendi: {title} ({len(chunks)} chunk)")
        return doc_id
    
    def _chunk_text(self, text: str, title: str) -> List[Dict]:
        """Metni chunk'lara böl"""
        max_tokens = 1000
        words = text.split()
        chunks = []
        
        current_chunk = []
        current_tokens = 0
        
        for word in words:
            current_chunk.append(word)
            current_tokens += len(word.split()) + 1
            
            if current_tokens >= max_tokens:
                chunk_text = " ".join(current_chunk)
                chunks.append({
                    'text': chunk_text,
                    'tokens': current_tokens,
                    'heading': title,
                    'section_path': title
                })
                current_chunk = []
                current_tokens = 0
        
        if current_chunk:
            chunk_text = " ".join(current_chunk)
            chunks.append({
                'text': chunk_text,
                'tokens': current_tokens,
                'heading': title,
                'section_path': title
            })
        
        return chunks
    
    def _add_chunk(self, document_id: int, chunk: Dict):
        """Chunk ekle"""
        work_types = self._extract_work_types(chunk['text'])
        norm_codes = self._extract_norm_codes(chunk['text'])
        unit = self._extract_unit(chunk['text'])
        
        cursor = self.connection.cursor()
        cursor.execute("""
        INSERT INTO chunks (document_id, section_path, heading, text, tokens, 
                           work_types, norm_codes, unit, locale)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            document_id,
            chunk['section_path'],
            chunk['heading'],
            chunk['text'],
            chunk['tokens'],
            work_types,
            norm_codes,
            unit,
            'tr'
        ))
    
    def _extract_work_types(self, text: str) -> List[str]:
        """İş tiplerini çıkar"""
        work_types = []
        text_lower = text.lower()
        
        if any(word in text_lower for word in ['donatı', 'арматура', 'rebar']):
            work_types.append('rebar')
        if any(word in text_lower for word in ['kalıp', 'опалубка', 'formwork']):
            work_types.append('formwork')
        if any(word in text_lower for word in ['beton', 'бетон', 'concrete']):
            work_types.append('concrete')
        
        return work_types
    
    def _extract_norm_codes(self, text: str) -> List[str]:
        """Norm kodlarını çıkar"""
        import re
        norm_codes = []
        
        fer_pattern = r'FER[-\s]?\d{2}[-\s]?\d{3}'
        fer_matches = re.findall(fer_pattern, text, re.IGNORECASE)
        norm_codes.extend(fer_matches)
        
        poz_pattern = r'Poz[-\s]?\d{3}'
        poz_matches = re.findall(poz_pattern, text, re.IGNORECASE)
        norm_codes.extend(poz_matches)
        
        return norm_codes
    
    def _extract_unit(self, text: str) -> Optional[str]:
        """Birim çıkar"""
        text_lower = text.lower()
        
        if 'kg' in text_lower:
            return 'kg'
        elif 'm2' in text_lower or 'm²' in text_lower:
            return 'm2'
        elif 'm3' in text_lower or 'm³' in text_lower:
            return 'm3'
        elif 'saat' in text_lower:
            return 'h'
        
        return None
    
    def search(self, query: str, locales: List[str] = None, 
              work_types: List[str] = None, top_k: int = None) -> List[Dict]:
        """Basit metin arama"""
        if not top_k:
            top_k = self.config.default_top_k
        
        if not locales:
            locales = ['tr', 'ru', 'en']
        
        cursor = self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Basit LIKE arama
        search_query = """
        SELECT c.id, c.document_id, c.section_path, c.heading, c.text,
               c.work_types, c.norm_codes, c.unit, c.locale,
               d.source, d.country, d.title as doc_title
        FROM chunks c
        JOIN documents d ON c.document_id = d.id
        WHERE c.text ILIKE %s AND c.locale = ANY(%s)
        ORDER BY 
            CASE 
                WHEN c.text ILIKE %s THEN 1
                WHEN c.heading ILIKE %s THEN 2
                ELSE 3
            END,
            c.created_at DESC
        LIMIT %s
        """
        
        query_pattern = f'%{query}%'
        params = [query_pattern, locales, query_pattern, query_pattern, top_k]
        
        cursor.execute(search_query, params)
        results = cursor.fetchall()
        
        # Sonuçları işle
        processed_results = []
        for row in results:
            processed_results.append({
                'id': row['id'],
                'document_id': row['document_id'],
                'section_path': row['section_path'],
                'heading': row['heading'],
                'text': row['text'],
                'work_types': row['work_types'] or [],
                'norm_codes': row['norm_codes'] or [],
                'unit': row['unit'],
                'locale': row['locale'],
                'score': 0.8,  # Sabit skor
                'source': row['source'],
                'country': row['country'],
                'doc_title': row['doc_title']
            })
        
        # Güvenlik katmanı uygula
        filtered_results = self._apply_security_layer(query, processed_results)
        
        # Audit log
        if self.config.enable_audit_log:
            self._log_retrieval(query, filtered_results)
        
        return filtered_results
    
    def _apply_security_layer(self, query: str, results: List[Dict]) -> List[Dict]:
        """Güvenlik katmanı uygula"""
        
        # Kaynak çeşitlendirmesi
        if self.config.enable_source_diversification:
            results = self._diversify_sources(results)
        
        # Minimum kaynak kontrolü
        if len(results) < self.config.min_sources:
            logger.warning(f"Insufficient context: only {len(results)} sources found")
        
        return results
    
    def _diversify_sources(self, results: List[Dict]) -> List[Dict]:
        """Kaynak çeşitlendirmesi uygula"""
        source_counts = {}
        diversified = []
        
        for result in results:
            source = result['source']
            if source_counts.get(source, 0) < 2:  # Her kaynaktan max 2 chunk
                diversified.append(result)
                source_counts[source] = source_counts.get(source, 0) + 1
        
        return diversified
    
    def _log_retrieval(self, query: str, results: List[Dict]):
        """Retrieval işlemini logla"""
        cursor = self.connection.cursor()
        
        cursor.execute("""
        INSERT INTO retrieval_logs (query, top_k, chunk_ids, scores, accepted)
        VALUES (%s, %s, %s, %s, %s)
        """, (
            query,
            len(results),
            [r['id'] for r in results],
            [r['score'] for r in results],
            len(results) >= self.config.min_sources
        ))
    
    def calculate_labor_hours(self, wbs_key: str, qty: float, unit: str, 
                            locale: str = 'tr') -> float:
        """İşçilik saati hesapla"""
        cursor = self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Uygun normu bul
        cursor.execute("""
        SELECT norm_lh_per_u, conditions_json
        FROM norms
        WHERE work_item_key = %s AND unit = %s AND locale = %s
        ORDER BY 
            CASE source 
                WHEN 'Internal' THEN 1
                WHEN 'Poz' THEN 2
                WHEN 'FER' THEN 3
                ELSE 4
            END,
            updated_at DESC
        LIMIT 1
        """, (wbs_key, unit, locale))
        
        result = cursor.fetchone()
        if not result:
            logger.warning(f"Norm bulunamadı: {wbs_key}, {unit}")
            return 0.0
        
        norm_lh_per_u = float(result['norm_lh_per_u'])
        conditions = result['conditions_json'] or {}
        
        # Koşul çarpanlarını uygula
        adjustment_factors = self._calculate_adjustment_factors(conditions)
        effective_norm = norm_lh_per_u * adjustment_factors
        
        return qty * effective_norm
    
    def _calculate_adjustment_factors(self, conditions: Dict) -> float:
        """Koşul çarpanlarını hesapla"""
        factors = 1.0
        
        if conditions.get('height') == '>3m':
            factors *= 1.15
        if conditions.get('weather') == 'cold':
            factors *= 1.20
        if conditions.get('complexity') == 'high':
            factors *= 1.25
        
        return factors
    
    def add_sample_data(self):
        """Örnek veri ekle"""
        logger.info("📊 Örnek veri ekleniyor...")
        
        # Revit quantities
        cursor = self.connection.cursor()
        revit_data = [
            ('model_001', 'elem_001', 'Structural Framing', 'rebar', 'REBAR.BEAM', 1500.0, 'kg'),
            ('model_001', 'elem_002', 'Structural Framing', 'formwork', 'FORM.BEAM', 25.0, 'm2'),
            ('model_001', 'elem_003', 'Structural Framing', 'concrete', 'CONC.BEAM', 2.5, 'm3'),
        ]
        
        for revit in revit_data:
            cursor.execute("""
            INSERT INTO revit_quantities (model_id, element_id, category, class_inf, wbs_key, qty, unit)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (model_id, element_id) DO NOTHING
            """, revit)
        
        # Site observations
        today = date.today()
        site_data = [
            (today, 'day', 'crew_001', 'REBAR.BEAM', 1500.0, 'kg', 180.0),
            (today, 'day', 'crew_002', 'FORM.BEAM', 25.0, 'm2', 20.0),
            (today, 'day', 'crew_003', 'CONC.BEAM', 2.5, 'm3', 1.25),
        ]
        
        for site in site_data:
            cursor.execute("""
            INSERT INTO site_observations (work_date, shift, crew_id, wbs_key, qty, unit, labor_hours)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, site)
        
        logger.info("✅ Örnek veri eklendi")
    
    def export_reports(self, start_date: date, end_date: date, 
                      output_dir: str = ".") -> Dict[str, str]:
        """Raporları ihraç et"""
        logger.info("📊 Raporlar ihraç ediliyor...")
        
        cursor = self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Variance summary
        cursor.execute("""
        SELECT 
            rq.wbs_key,
            rq.qty,
            rq.unit,
            COALESCE(SUM(so.labor_hours), 0) as actual_hours
        FROM revit_quantities rq
        LEFT JOIN site_observations so ON rq.wbs_key = so.wbs_key 
            AND so.work_date BETWEEN %s AND %s
        GROUP BY rq.wbs_key, rq.qty, rq.unit
        """, (start_date, end_date))
        
        results = cursor.fetchall()
        
        # CSV oluştur
        variance_path = os.path.join(output_dir, "variance_summary.csv")
        with open(variance_path, 'w', encoding='utf-8-sig') as f:
            f.write("period;wbs_key;qty;unit;LH_theo;LH_actual;delta;delta_%;productivity\n")
            
            for row in results:
                wbs_key = row['wbs_key']
                qty = float(row['qty'])
                unit = row['unit']
                actual_hours = float(row['actual_hours'])
                
                # Teorik saat hesapla
                theoretical_hours = self.calculate_labor_hours(wbs_key, qty, unit)
                
                # Sapma hesapla
                delta = actual_hours - theoretical_hours
                delta_percent = (delta / theoretical_hours * 100) if theoretical_hours > 0 else 0
                productivity = qty / actual_hours if actual_hours > 0 else 0
                
                period = f"{start_date} - {end_date}"
                f.write(f"{period};{wbs_key};{qty};{unit};{theoretical_hours:.2f};{actual_hours:.2f};{delta:.2f};{delta_percent:.2f};{productivity:.2f}\n")
        
        logger.info(f"✅ Rapor ihraç edildi: {variance_path}")
        return {'variance_summary': variance_path}
    
    def close(self):
        """Veritabanını kapat"""
        if self.connection:
            self.connection.close()
            logger.info("✅ PostgreSQL bağlantısı kapatıldı")

def demo_postgresql_rag():
    """PostgreSQL RAG demo"""
    print("🎯 PostgreSQL RAG System Demo")
    print("=" * 50)
    
    try:
        # Konfigürasyon
        config = RAGConfig()
        
        # Sistem başlat
        rag_system = PostgreSQLRAGSystem(config)
        
        # Örnek veri ekle
        rag_system.add_sample_data()
        
        # Doküman ekle
        print("\n📄 Doküman ekleniyor...")
        doc_id = rag_system.add_document(
            source="FER",
            country="RU",
            doc_type="norm",
            title="FER-06 Betonarme İşleri",
            lang="ru",
            content="""
            FER-06-001: Donatı bağlama işleri
            Birim: kg
            Norm: 0.15 adam-saat/kg
            
            FER-06-002: Kalıp kurulumu
            Birim: m2
            Norm: 0.8 adam-saat/m2
            
            FER-06-003: Beton dökümü
            Birim: m3
            Norm: 0.5 adam-saat/m3
            """
        )
        print(f"✅ Doküman eklendi (ID: {doc_id})")
        
        # Arama testi
        print("\n🔍 Arama testi...")
        results = rag_system.search("donatı bağlama", locales=['tr', 'ru'])
        print(f"✅ {len(results)} sonuç bulundu")
        
        for i, result in enumerate(results[:3]):
            print(f"\n📋 Sonuç {i+1}:")
            print(f"   Skor: {result['score']:.3f}")
            print(f"   Kaynak: {result['source']}")
            print(f"   Metin: {result['text'][:100]}...")
        
        # Hesaplama testi
        print("\n🧮 Hesaplama testi...")
        theoretical_hours = rag_system.calculate_labor_hours('REBAR.BEAM', 1500.0, 'kg')
        print(f"✅ Teorik adam-saat: {theoretical_hours:.2f}")
        
        # Rapor ihracı
        print("\n📊 Rapor ihracı...")
        end_date = date.today()
        start_date = end_date - timedelta(days=7)
        
        reports = rag_system.export_reports(start_date, end_date)
        print(f"✅ Raporlar: {list(reports.keys())}")
        
        # Sistemi kapat
        rag_system.close()
        
        print("\n🎉 PostgreSQL RAG Demo başarıyla tamamlandı!")
        print("\n📁 Oluşturulan dosyalar:")
        print("- variance_summary.csv (rapor)")
        
        return True
        
    except Exception as e:
        print(f"❌ Demo hatası: {e}")
        return False

if __name__ == "__main__":
    success = demo_postgresql_rag()
    if success:
        print("\n✅ PostgreSQL RAG sistemi çalışıyor!")
    else:
        print("\n❌ PostgreSQL RAG sistemi çalışmıyor.")

