"""이미지 빌드 때 PaddleOCR 모델을 받아 둔다. 실행 중인 Pod는 외부로 나가지 못하므로 여기서 받아야 한다."""
import numpy as np

from app.ocr import engine

img = np.full((200, 600, 3), 255, dtype=np.uint8)
print(engine().predict(img)[0]["rec_texts"])
