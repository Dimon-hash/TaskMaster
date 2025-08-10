import sys
import types
from pathlib import Path

# Папка с моделями
MODELS_DIR = Path(r"C:\working\taskmaster\face_recognition_models\models")

# Файлы моделей
MODEL_68 = MODELS_DIR / "shape_predictor_68_face_landmarks.dat"
MODEL_5 = MODELS_DIR / "shape_predictor_5_face_landmarks.dat"
CNN_MODEL = MODELS_DIR / "mmod_human_face_detector.dat"
FACE_RECOG_MODEL = MODELS_DIR / "dlib_face_recognition_resnet_model_v1.dat"

# Создаём поддельный модуль face_recognition_models
fake_models = types.ModuleType("face_recognition_models")
fake_models.__file__ = __file__
fake_models.models_dir = str(MODELS_DIR)

# Функции, которые вызывает face_recognition
def pose_predictor_model_location():
    return str(MODEL_68)

def pose_predictor_five_point_model_location():
    return str(MODEL_5)

def cnn_face_detector_model_location():
    return str(CNN_MODEL)

def face_recognition_model_location():
    return str(FACE_RECOG_MODEL)

# Привязываем их в модуль
fake_models.pose_predictor_model_location = pose_predictor_model_location
fake_models.pose_predictor_five_point_model_location = pose_predictor_five_point_model_location
fake_models.cnn_face_detector_model_location = cnn_face_detector_model_location
fake_models.face_recognition_model_location = face_recognition_model_location

# Добавим для совместимости и старые переменные
fake_models.pose_predictor_68_point_model_location = str(MODEL_68)
fake_models.pose_predictor_5_point_model_location = str(MODEL_5)

# Регистрируем фейковый модуль в sys.modules
sys.modules["face_recognition_models"] = fake_models

# Теперь можно импортировать face_recognition
import face_recognition
import logging
import numpy as np
import asyncio
from concurrent.futures import ThreadPoolExecutor
import dlib

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2)

def ensure_model():
    for model in [MODEL_68, MODEL_5, CNN_MODEL, FACE_RECOG_MODEL]:
        if not model.exists():
            raise FileNotFoundError(f"Модель не найдена: {model}")
    logger.info(f"[OK] Все модели найдены в {MODELS_DIR}")

def _sync_extract_face_features(image_path: str) -> list | None:
    ensure_model()
    img = face_recognition.load_image_file(image_path)
    encodings = face_recognition.face_encodings(img)
    if not encodings:
        return None
    return encodings[0].tolist()

async def extract_face_from_photo(image_path: Path) -> list | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _sync_extract_face_features, str(image_path))

def compare_faces(features1, features2, threshold=0.9):
    if features1 is None or features2 is None:
        return False, 0.0
    a = np.array(features1, dtype=np.float32)
    b = np.array(features2, dtype=np.float32)
    sim = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
    return sim >= threshold, sim

if __name__ == "__main__":
    f1 = _sync_extract_face_features("face1.jpg")
    f2 = _sync_extract_face_features("face2.jpg")
    match, score = compare_faces(f1, f2)
    print(f"Совпадение: {match}, Схожесть: {score:.3f}")
