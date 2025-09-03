import os
import json
import numpy as np
import faiss
from typing import List, Dict, Optional, Any
import logging

# Logging ayarları
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RAGBackend:
    def __init__(self):
        self.rag_data_dir = "rag_data"
        self.index_path = os.path.join(self.rag_data_dir, "index.faiss")
        self.meta_path = os.path.join(self.rag_data_dir, "meta.jsonl")
        self.index_meta_path = os.path.join(self.rag_data_dir, "index_meta.json")
        
        self.index = None
        self.dimension = None
        self.count = 0
        
    def _ensure_rag_data_dir(self):
        """rag_data klasörünü oluştur"""
        if not os.path.exists(self.rag_data_dir):
            os.makedirs(self.rag_data_dir)
            logger.info(f"rag_data klasörü oluşturuldu: {self.rag_data_dir}")
    
    def _load_index_meta(self) -> Dict[str, Any]:
        """index_meta.json dosyasını yükle"""
        if os.path.exists(self.index_meta_path):
            try:
                with open(self.index_meta_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"index_meta.json yüklenirken hata: {e}")
        return {"dim": None, "count": 0}
    
    def _save_index_meta(self):
        """index_meta.json dosyasını kaydet"""
        meta_data = {
            "dim": self.dimension,
            "count": self.count
        }
        try:
            with open(self.index_meta_path, 'w', encoding='utf-8') as f:
                json.dump(meta_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"index_meta.json kaydedilirken hata: {e}")
    
    def _load_faiss_index(self):
        """FAISS indeksini yükle"""
        if os.path.exists(self.index_path):
            try:
                self.index = faiss.read_index(self.index_path)
                self.dimension = self.index.d
                logger.info(f"FAISS indeksi yüklendi: {self.index_path}")
            except Exception as e:
                logger.error(f"FAISS indeksi yüklenirken hata: {e}")
                self.index = None
        else:
            logger.info("FAISS indeksi bulunamadı, yeni oluşturulacak")
    
    def _create_new_index(self, dimension: int):
        """Yeni FAISS indeksi oluştur"""
        self.dimension = dimension
        self.index = faiss.IndexFlatIP(dimension)  # Inner Product (cosine için)
        self.count = 0
        logger.info(f"Yeni FAISS indeksi oluşturuldu: {dimension} boyut")
    
    def _normalize_vectors(self, vectors: np.ndarray) -> np.ndarray:
        """Vektörleri L2-norm ile normalize et (cosine similarity için)"""
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1  # Sıfır vektörleri koru
        return vectors / norms
    
    def _get_next_id(self) -> int:
        """Bir sonraki ID'yi al"""
        if os.path.exists(self.meta_path):
            try:
                with open(self.meta_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    if lines:
                        last_line = lines[-1].strip()
                        if last_line:
                            last_record = json.loads(last_line)
                            return last_record.get("id", 0) + 1
            except Exception as e:
                logger.error(f"Son ID okunurken hata: {e}")
        return 0

def init_backend() -> None:
    """RAG backend'ini başlat"""
    global rag_backend
    rag_backend = RAGBackend()
    rag_backend._ensure_rag_data_dir()
    
    # Mevcut indeksi yükle veya yeni oluştur
    meta_data = rag_backend._load_index_meta()
    if meta_data["dim"] is not None:
        rag_backend._load_faiss_index()
        if rag_backend.index is None:
            # Yükleme başarısız, yeni oluştur
            rag_backend._create_new_index(meta_data["dim"])
    else:
        # İlk kez çalıştırılıyor
        rag_backend._create_new_index(1536)  # OpenAI embedding boyutu
    
    rag_backend.count = meta_data["count"]
    logger.info(f"RAG backend başlatıldı: {rag_backend.count} kayıt, {rag_backend.dimension} boyut")

def reset_backend() -> None:
    """Backend'i sıfırla"""
    global rag_backend
    try:
        # Dosyaları sil
        if os.path.exists(rag_backend.index_path):
            os.remove(rag_backend.index_path)
        if os.path.exists(rag_backend.meta_path):
            os.remove(rag_backend.meta_path)
        if os.path.exists(rag_backend.index_meta_path):
            os.remove(rag_backend.index_meta_path)
        
        # Yeni indeks oluştur
        rag_backend._create_new_index(rag_backend.dimension or 1536)
        rag_backend._save_index_meta()
        
        logger.info("RAG backend sıfırlandı")
    except Exception as e:
        logger.error(f"Backend sıfırlanırken hata: {e}")

def add_records(texts: List[str], metas: List[Dict], embeddings: np.ndarray) -> List[int]:
    """Kayıtları ekle"""
    global rag_backend
    
    if len(texts) != len(metas) or len(texts) != len(embeddings):
        raise ValueError("texts, metas ve embeddings listeleri aynı uzunlukta olmalı")
    
    if len(embeddings) == 0:
        return []
    
    # Boyut kontrolü
    if rag_backend.dimension is None:
        rag_backend._create_new_index(embeddings.shape[1])
    elif embeddings.shape[1] != rag_backend.dimension:
        raise ValueError(f"Embedding boyutu uyumsuz: beklenen {rag_backend.dimension}, gelen {embeddings.shape[1]}")
    
    # Vektörleri normalize et
    embeddings_norm = rag_backend._normalize_vectors(embeddings.astype(np.float32))
    
    # ID'leri al
    start_id = rag_backend._get_next_id()
    ids = list(range(start_id, start_id + len(texts)))
    
    # meta.jsonl'e ekle
    try:
        with open(rag_backend.meta_path, 'a', encoding='utf-8') as f:
            for i, (text, meta, record_id) in enumerate(zip(texts, metas, ids)):
                record = {
                    "id": record_id,
                    "text": text,
                    "meta": meta
                }
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as e:
        logger.error(f"meta.jsonl'e yazılırken hata: {e}")
        raise
    
    # FAISS indeksine ekle
    try:
        rag_backend.index.add(embeddings_norm)
        rag_backend.count += len(texts)
        rag_backend._save_index_meta()
        
        # İndeksi diske yaz
        faiss.write_index(rag_backend.index, rag_backend.index_path)
        
        logger.info(f"{len(texts)} kayıt eklendi, toplam: {rag_backend.count}")
        return ids
    except Exception as e:
        logger.error(f"FAISS indeksine eklenirken hata: {e}")
        raise

def search(query_emb: np.ndarray, topk: int = 6, filters: Optional[Dict] = None) -> List[Dict]:
    """Arama yap"""
    global rag_backend
    
    if rag_backend.index is None or rag_backend.count == 0:
        return []
    
    # Boyut kontrolü
    if query_emb.shape[0] != rag_backend.dimension:
        raise ValueError(f"Query embedding boyutu uyumsuz: beklenen {rag_backend.dimension}, gelen {query_emb.shape[0]}")
    
    # Query'yi normalize et
    query_emb_norm = rag_backend._normalize_vectors(query_emb.reshape(1, -1).astype(np.float32))
    
    # FAISS'ten daha fazla sonuç al (filtreleme için)
    search_k = topk * 5 if filters else topk
    search_k = min(search_k, rag_backend.count)
    
    try:
        scores, indices = rag_backend.index.search(query_emb_norm, search_k)
        scores = scores[0]
        indices = indices[0]
    except Exception as e:
        logger.error(f"FAISS araması sırasında hata: {e}")
        return []
    
    # meta.jsonl'den kayıtları oku
    results = []
    try:
        with open(rag_backend.meta_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
            for score, idx in zip(scores, indices):
                if idx < 0 or idx >= len(lines):  # Geçersiz index
                    continue
                    
                try:
                    record = json.loads(lines[idx].strip())
                    
                    # Filtreleme
                    if filters:
                        if not _apply_filters(record, filters):
                            continue
                    
                    result = {
                        "id": record["id"],
                        "text": record["text"],
                        "meta": record["meta"],
                        "score": float(score)
                    }
                    results.append(result)
                    
                    if len(results) >= topk:
                        break
                        
                except Exception as e:
                    logger.warning(f"Kayıt okunurken hata: {e}")
                    continue
                    
    except Exception as e:
        logger.error(f"meta.jsonl okunurken hata: {e}")
        return []
    
    return results

def _apply_filters(record: Dict, filters: Dict) -> bool:
    """Filtreleri uygula"""
    meta = record.get("meta", {})
    
    for filter_key, filter_value in filters.items():
        if filter_key == "filename_contains":
            filename = meta.get("filename", "")
            if filter_value.lower() not in filename.lower():
                return False
        elif filter_key == "project":
            project = meta.get("project", "")
            if filter_value != project:
                return False
    
    return True

def migrate_from_jsonl_if_needed(old_path: str = "rag_data/store.jsonl") -> Dict:
    """Eski JSONL formatından migrasyon"""
    global rag_backend
    
    if os.path.exists(rag_backend.index_path):
        logger.info("FAISS indeksi zaten mevcut, migrasyon gerekmiyor")
        return {"migrated": 0, "skipped": 0}
    
    if not os.path.exists(old_path):
        logger.info("Eski store.jsonl dosyası bulunamadı")
        return {"migrated": 0, "skipped": 0}
    
    try:
        migrated = 0
        skipped = 0
        
        with open(old_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                try:
                    record = json.loads(line.strip())
                    
                    # Gerekli alanları kontrol et
                    if "text" not in record or "meta" not in record:
                        skipped += 1
                        continue
                    
                    # Embedding varsa kullan, yoksa atla
                    if "embedding" in record:
                        embedding = np.array(record["embedding"], dtype=np.float32)
                        if len(embedding.shape) == 1:
                            embedding = embedding.reshape(1, -1)
                        
                        # İlk kayıt için boyut ayarla
                        if rag_backend.dimension is None:
                            rag_backend._create_new_index(embedding.shape[1])
                        
                        add_records([record["text"]], [record["meta"]], embedding)
                        migrated += 1
                    else:
                        skipped += 1
                        
                except Exception as e:
                    logger.warning(f"Satır {line_num} okunurken hata: {e}")
                    skipped += 1
                    continue
        
        logger.info(f"Migrasyon tamamlandı: {migrated} kayıt taşındı, {skipped} kayıt atlandı")
        return {"migrated": migrated, "skipped": skipped}
        
    except Exception as e:
        logger.error(f"Migrasyon sırasında hata: {e}")
        return {"migrated": 0, "skipped": 0}

def get_status() -> Dict[str, Any]:
    """Backend durumunu al"""
    global rag_backend
    return {
        "count": rag_backend.count if rag_backend else 0,
        "dimension": rag_backend.dimension if rag_backend else None,
        "index_exists": os.path.exists(rag_backend.index_path) if rag_backend else False
    }

# Global backend instance
rag_backend = None
