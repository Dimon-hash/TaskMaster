import bz2
import urllib.request
from pathlib import Path

def ensure_fr_model():
    import face_recognition_models
    model_dir = Path(face_recognition_models.__file__).parent / "models"
    model_path = model_dir / "shape_predictor_68_face_landmarks.dat"
    model_bz2 = model_path.with_suffix(".dat.bz2")

    if model_path.exists():
        print(f"[OK] Модель уже есть: {model_path}")
        return

    print(f"[INFO] Скачиваю модель в {model_dir}...")
    model_dir.mkdir(parents=True, exist_ok=True)
    url = "https://github.com/davisking/dlib-models/raw/master/shape_predictor_68_face_landmarks.dat.bz2"
    urllib.request.urlretrieve(url, model_bz2)

    print("[INFO] Распаковываю...")
    with bz2.BZ2File(model_bz2, 'rb') as f_in:
        with open(model_path, 'wb') as f_out:
            f_out.write(f_in.read())

    model_bz2.unlink()
    print(f"[DONE] Модель установлена: {model_path}")

if __name__ == "__main__":
    ensure_fr_model()
