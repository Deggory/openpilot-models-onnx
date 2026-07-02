#!/usr/bin/env python3
"""
모델 폴더를 스캔해서 models.json을 자동 업데이트하는 스크립트

사용법:
  1. 새 폴더 생성: experimental_v1/
  2. ONNX 파일 추가:
     - experimental_v1/driving_policy.onnx
     - experimental_v1/driving_vision.onnx
  3. 스크립트 실행: python scripts/update_models.py
  4. 프롬프트에서 모델 이름/설명 입력
  5. 자동으로 manifest 업데이트 + 서명
     - models_v4.json: 전체 카탈로그 (v4+ 셀렉터용, 마스터)
     - models.json:    레거시 파일명 모델만 (v3 이하 구버전용, 파생본)

  폴더 스캔 없이 manifest만 재생성/재서명하려면:
     python scripts/update_models.py --resign-only
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# 한국 시간대 (UTC+9)
KST = timezone(timedelta(hours=9))
from pathlib import Path

# 프로젝트 루트
ROOT_DIR = Path(__file__).parent.parent
MODELS_DIR = ROOT_DIR / "models"
MODELS_JSON = ROOT_DIR / "models.json"
MODELS_JSON_V4 = ROOT_DIR / "models_v4.json"
README_FILE = ROOT_DIR / "README.md"
GITHUB_BASE_URL = "https://raw.githubusercontent.com/happymaj11r/openpilot-models/main/models"

# v3 이하 구버전 셀렉터가 아는 파일명 목록.
# 구버전 manifest 파서는 버전 게이트(minimum_selector_version) "이전에" 파일명을
# 검사하고, 미지의 파일명이 항목 하나에라도 있으면 models.json 전체를 실패시킨다.
# 따라서 이 목록을 벗어나는 파일(driving_supercombo.onnx 등)을 쓰는 모델은
# models.json(레거시 manifest, 동결)에서 제외하고 models_v4.json(전체 카탈로그,
# 마스터)에만 싣는다. minimum_selector_version만으로는 구버전을 보호할 수 없음.
LEGACY_ALLOWED_FILES = {
    "driving_vision.onnx",
    "driving_policy.onnx",
    "driving_on_policy.onnx",
    "driving_off_policy.onnx",
}

# 필수 파일 세트 (폴더 유효성 검사용, 한 세트가 전부 존재하면 유효, | 로 대체 파일 지원)
REQUIRED_FILE_SETS = [
    # 구 구조: vision + policy 분리형
    ["driving_policy.onnx|driving_on_policy.onnx", "driving_vision.onnx"],
    # 신 구조: 단일 통합 supercombo (2026-06 이후)
    ["big_driving_supercombo.onnx|driving_supercombo.onnx"],
]

# 제외 패턴 (파일명에 포함되면 등록 제외)
# 주의: big_driving_supercombo.onnx는 신형 정식 모델이므로 "big" 전체 제외 금지
EXCLUDE_PATTERNS = ["dmonitoring", "big_driving_vision", "big_driving_policy"]


def calculate_sha256(filepath: Path) -> str:
    """파일의 SHA256 해시 계산"""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def scan_model_folders() -> list[Path]:
    """models/ 폴더 내 모델 스캔 (ONNX 파일이 있는 폴더)"""
    model_folders = []

    # models 폴더가 없으면 생성
    MODELS_DIR.mkdir(exist_ok=True)

    for item in MODELS_DIR.iterdir():
        if item.is_dir():
            # 외부 호스팅 모델: meta.json이 있으면 실제 onnx 없이도 유효한 모델 폴더
            if (item / "meta.json").exists():
                model_folders.append(item)
                continue
            # 필수 파일 세트 체크 (한 세트라도 전부 존재하면 유효, | 로 대체 파일 지원)
            has_valid_set = any(
                all(
                    any((item / alt.strip()).exists() for alt in req.split("|"))
                    for req in file_set
                )
                for file_set in REQUIRED_FILE_SETS
            )
            if has_valid_set:
                model_folders.append(item)

    return model_folders


def get_model_info(folder: Path, existing_models: dict) -> dict:
    """모델 폴더에서 정보 추출"""
    model_id = folder.name

    # 외부 호스팅 모델: GitHub 100MB 제한을 넘는 파일은 Release 등에 올리고
    # meta.json(name/base_url/files/minimum_selector_version/added_at)으로 등록
    meta_file = folder / "meta.json"
    if meta_file.exists():
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            files = meta["files"]
        except (json.JSONDecodeError, KeyError, OSError) as e:
            # meta.json 손상 시 전체 갱신을 막지 않도록 기존 등록 정보를 유지
            existing = existing_models.get(model_id)
            if existing:
                print(f"  [{model_id}] meta.json 오류({e}) - 기존 models.json 항목 유지")
                return existing
            print(f"  [{model_id}] meta.json 오류({e}) - 등록 스킵")
            return None
        print(f"  [{model_id}] 외부 호스팅 모델 (meta.json 사용)")
        return {
            "id": model_id,
            "name": meta.get("name", model_id),
            "base_url": meta.get("base_url", f"{GITHUB_BASE_URL}/{model_id}"),
            "files": files,
            "minimum_selector_version": meta.get("minimum_selector_version", 1),
            "added_at": meta.get("added_at", datetime.now(KST).strftime("%Y-%m-%d")),
        }

    # 기존 모델 정보가 있으면 재사용
    existing = existing_models.get(model_id, {})

    # 파일 정보 계산 (폴더 내 모든 .onnx 파일, 제외 패턴 적용)
    files = {}
    for filepath in sorted(folder.glob("*.onnx")):
        if any(p in filepath.name.lower() for p in EXCLUDE_PATTERNS):
            print(f"    [{model_id}] 제외: {filepath.name}")
            continue
        files[filepath.name] = {
            "size": filepath.stat().st_size,
            "sha256": calculate_sha256(filepath)
        }

    # 기존 정보가 있고 파일 해시가 같으면 기존 정보 유지
    if existing and existing.get("files") == files:
        print(f"  [{model_id}] 변경 없음 (기존 정보 유지)")
        # added_at이 없으면 추가
        if "added_at" not in existing:
            existing["added_at"] = datetime.now(KST).strftime("%Y-%m-%d")
        return existing

    # 새 모델이거나 파일이 변경됨
    if existing:
        print(f"  [{model_id}] 파일 변경 감지!")
        name = existing.get("name", model_id)
        minimum_selector_version = existing.get("minimum_selector_version", 1)
        added_at = existing.get("added_at", datetime.now(KST).strftime("%Y-%m-%d"))
    else:
        print(f"  [{model_id}] 새 모델 발견!")
        name = input(f"    모델 이름 (기본: {model_id}): ").strip() or model_id
        today = datetime.now(KST).strftime("%Y-%m-%d")
        added_at = input(f"    추가 날짜 (기본: {today}): ").strip() or today

        # 호환성 검사 결과 (watcher에서 전달)
        compat_input = input(f"    modeld 호환 여부 (y/n, 기본: y): ").strip().lower()
        if compat_input == 'n':
            # SELECTOR_VERSION 환경변수에서 현재 버전 읽기 (watcher에서 전달)
            selector_ver = int(os.environ.get("SELECTOR_VERSION", "1"))
            minimum_selector_version = selector_ver + 1
            print(f"    [BLOCKED] 비호환 모델 - minimum_selector_version: {minimum_selector_version}")
        else:
            selector_ver = int(os.environ.get("SELECTOR_VERSION", "1"))
            minimum_selector_version = selector_ver

    return {
        "id": model_id,
        "name": name,
        "base_url": f"{GITHUB_BASE_URL}/{model_id}",
        "files": files,
        "minimum_selector_version": minimum_selector_version,
        "added_at": added_at
    }


def update_readme(models: list):
    """README.md의 Models 테이블 업데이트"""
    if not README_FILE.exists():
        return

    content = README_FILE.read_text(encoding="utf-8")

    # 날짜 기준 내림차순 정렬 (최신순, 같은 날짜면 나중에 추가된 모델이 위로)
    sorted_models = sorted(
        enumerate(models),
        key=lambda pair: (pair[1].get("added_at", ""), pair[0]),
        reverse=True,
    )
    sorted_models = [m for _, m in sorted_models]

    # Models 테이블 생성
    table_lines = [
        "## Models",
        "",
        "| ID | Name | Size | Added |",
        "|----|------|------|-------|",
    ]
    for m in sorted_models:
        size_mb = sum(f["size"] for f in m["files"].values()) / (1024 * 1024)
        added_at = m.get("added_at", "-")
        table_lines.append(f"| {m['id']} | {m['name']} | {size_mb:.1f}MB | {added_at} |")
    table_lines.append("")

    # ## Models 부터 다음 ## 섹션 전까지 교체
    pattern = r"## Models\n.*?(?=\n## )"
    replacement = "\n".join(table_lines)
    new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)

    README_FILE.write_text(new_content, encoding="utf-8")
    print("README.md 업데이트 완료!")


def is_legacy_compatible(model: dict) -> bool:
    """모델의 파일 세트가 구버전(v3 이하) 셀렉터 allowlist 안에 있는지"""
    return set(model.get("files") or {}) <= LEGACY_ALLOWED_FILES


def load_master_manifest() -> dict:
    """마스터 manifest 로드 (models_v4.json 우선, 없으면 models.json에서 부트스트랩)"""
    for path in (MODELS_JSON_V4, MODELS_JSON):
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return {
        "version": 1,
        "updated_at": "",
        "models": [],
        "key_id": "key_2024_01",
        "signature": ""
    }


def write_and_sign_manifests(manifest: dict) -> bool:
    """models_v4.json(전체) + models.json(레거시 호환만) 저장 후 각각 서명"""
    import subprocess

    full_models = manifest.get("models", [])
    legacy_models = [m for m in full_models if is_legacy_compatible(m)]
    excluded = [m["id"] for m in full_models if not is_legacy_compatible(m)]
    if excluded:
        print(f"models.json(레거시) 제외 - 신형 파일명 사용: {', '.join(excluded)}")

    ok = True
    for path, models in ((MODELS_JSON_V4, full_models), (MODELS_JSON, legacy_models)):
        doc = dict(manifest)
        doc["models"] = models
        doc["signature"] = "NEEDS_SIGNING"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)

        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "sign_manifest.py"), "--sign", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"서명 완료: {path.name} ({len(models)}개 모델)")
        else:
            print(f"서명 실패: {path.name}: {result.stderr}")
            ok = False
    return ok


def resign_only():
    """폴더 스캔 없이 마스터 manifest에서 두 manifest를 재생성 + 재서명

    manifest 항목을 직접 수정한 뒤(예: minimum_selector_version 차단)나,
    models.json 단일 구조를 이중 manifest 구조로 전환할 때 사용.
    """
    manifest = load_master_manifest()
    if not manifest.get("models"):
        print("모델이 없습니다 (models_v4.json / models.json 확인)")
        sys.exit(1)
    manifest["updated_at"] = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    if not write_and_sign_manifests(manifest):
        sys.exit(1)


def update_models_json():
    """manifest(models_v4.json + models.json) 업데이트"""
    print("=" * 50)
    print("모델 폴더 스캔 중...")
    print("=" * 50)

    # 기존 manifest 로드 (마스터 우선 — models.json에는 레거시 항목만 있어
    # supercombo 등 신형 모델의 이름/추가일 정보가 없다)
    manifest = load_master_manifest()

    # 기존 모델을 dict로 변환 (id -> model)
    existing_models = {m["id"]: m for m in manifest.get("models", [])}

    # 폴더 스캔
    folders = scan_model_folders()

    if not folders:
        print("\n모델 폴더를 찾을 수 없습니다.")
        print("폴더 구조 예시:")
        print("  openpilot-models/")
        print("  └── models/")
        print("      └── wmiv2/")
        print("          ├── driving_policy.onnx")
        print("          └── driving_vision.onnx")
        return

    print(f"\n{len(folders)}개 모델 폴더 발견:\n")

    # 각 폴더에서 모델 정보 추출
    new_models = []
    for folder in sorted(folders):
        model_info = get_model_info(folder, existing_models)
        if model_info is not None:
            new_models.append(model_info)

    # manifest 업데이트
    manifest["models"] = new_models
    manifest["updated_at"] = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")

    print("\n" + "=" * 50)
    print(f"manifest 업데이트! (전체 {len(new_models)}개 모델)")
    print("=" * 50)

    # 저장 + 서명 (서명 실패 시 비정상 종료 — watcher가 push를 중단하도록)
    print("\n저장 + 서명 중...")
    if not write_and_sign_manifests(manifest):
        sys.exit(1)

    # README.md 업데이트
    update_readme(new_models)

    # 결과 출력
    print("\n" + "=" * 50)
    print("등록된 모델 목록:")
    print("=" * 50)
    for m in new_models:
        size_mb = sum(f["size"] for f in m["files"].values()) / (1024 * 1024)
        print(f"  - {m['id']}: {m['name']} ({size_mb:.1f}MB, selector v{m['minimum_selector_version']}+)")


if __name__ == "__main__":
    if "--resign-only" in sys.argv:
        resign_only()
    else:
        update_models_json()
