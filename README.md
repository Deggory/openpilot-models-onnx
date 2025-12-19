# openpilot-models

Custom driving models for openpilot (carrot fork).

## Usage (콤마 기기에서)

1. openpilot UI에서 "주행 모델" 선택
2. 원하는 모델 다운로드
3. 자동으로 컴파일 및 적용

## Models

| ID | Name | Description |
|----|------|-------------|
| (coming soon) | - | - |

## 모델 추가 방법

```bash
# 1. 새 폴더 생성
mkdir experimental_v1

# 2. ONNX 파일 복사
cp /path/to/driving_policy.onnx experimental_v1/
cp /path/to/driving_vision.onnx experimental_v1/

# 3. 스크립트 실행 (자동으로 models.json 업데이트 + 서명)
uv run python scripts/update_models.py

# 4. 커밋 및 푸시
git add . && git commit -m "feat: 새 모델 추가" && git push
```

## Structure

```
openpilot-models/
├── models.json            # Model metadata + signature
├── scripts/
│   ├── update_models.py   # 모델 자동 등록 스크립트
│   ├── sign_manifest.py   # 서명 스크립트
│   └── keys/
│       ├── private_key.pem  # 개인키 (git 제외)
│       └── public_key.pem   # 공개키
└── {model_id}/
    ├── driving_policy.onnx
    └── driving_vision.onnx
```

## Security

All models are verified using Ed25519 signatures before download.
