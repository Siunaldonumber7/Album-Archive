# Album Archive

개인 음반 컬렉션을 관리하는 Flask 기반 웹 애플리케이션입니다.

## 주요 기능

- Apple Music KR / US / JP / GB 검색
- Apple Music 링크 등록
- 여러 앨범 선택 후 일괄 등록
- Apple Music에 없는 음반 수동 등록
- 동일 앨범의 여러 보유본 관리
- CD / LP / 카세트 / USB / 기타 포맷 관리
- 앨범 / EP(미니) / 싱글 / 믹스테잎 분류
- 사용자 장르 관리
- 관리번호와 앨범 ID 자동 생성
- 앨범 상세 및 수록곡 표시
- 수록곡 영구 DB 캐시 + 직접 편집
- 아티스트 디렉터리와 프로필 이미지
- 인덱스 기반 아티스트별 앨범 조회
- 아티스트별 보유 앨범 페이지
- 필터 / 정렬 상태 유지
- 메인 및 아티스트 페이지의 부분 갱신 UI
- 커버 이미지 직접 업로드
- 전체 발매일(YYYY-MM-DD) 저장 및 실제 발매순 정렬
- 관리자 DB 상태 점검 / 고아 데이터 정리
- SQLite 일관성 스냅샷 + 업로드 원본 백업

## 코드 구조

```text
Album-Archive/
├── app.py                 # Flask 앱 설정 + 공개 페이지 라우트
├── album_routes.py        # 앨범 등록 / 수정 / 삭제
├── admin_routes.py        # 관리자 페이지
├── track_routes.py        # 수록곡 편집 라우트
├── album_database.py      # SQLite 초기화와 DB 공통 함수
├── album_service.py       # 앨범 ID / 관리번호 관련 로직
├── library_view.py        # 필터 / 정렬 / 아티스트 화면 데이터 로직
├── catalog.py             # 장르 / 아티스트 이름 처리
├── search_engine.py       # Apple Music 검색과 아티스트 프로필 조회
├── track_service.py       # 수록곡 조회 / 캐시 / 수동 수록곡 로직
├── media_service.py       # 커버 이미지 저장 / 썸네일
├── pagination.py          # 페이지네이션
├── cache_utils.py         # 제한형 메모리 캐시
├── static/
│   ├── app.css            # 공통 스타일
│   └── app.js             # 필터 부분 갱신 / 아티스트 이미지 로딩
├── templates/
├── Dockerfile
├── docker-compose.yml
└── data/                  # NAS 영구 데이터 (Git 제외)
```

## 로컬 실행

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

기본 개발 서버 포트는 `5001`입니다.

## Synology NAS

현재 Docker Compose 설정은 아래 포트를 사용합니다.

```text
NAS 5050 → Container 5001
```

프로젝트 위치 예시:

```text
/volume1/docker/album-archive
```

GitHub의 최신 코드를 받은 뒤 재빌드:

```bash
cd /volume1/docker/album-archive
git pull
sudo /var/packages/ContainerManager/target/usr/bin/docker-compose up -d --build
```

로그 확인:

```bash
sudo docker logs --tail 100 album-archive
```

## 데이터 보존

`docker-compose.yml`에서 아래 볼륨을 사용합니다.

```yaml
volumes:
  - ./data:/data
```

따라서 컨테이너를 다시 빌드해도 다음 데이터는 유지됩니다.

- `data/albums.db`
- `data/uploads/`
- 관리자 PIN / Flask secret 등 `data/` 아래 런타임 파일

## Git에 포함하지 않는 데이터

- 실제 SQLite DB
- 업로드한 커버 이미지
- 로컬 가상환경
- 환경변수 파일
- 로그 / 임시 파일

실제 컬렉션 데이터는 `data/` 폴더에 보존됩니다.

관리자 메뉴의 **DB 점검 · 백업**에서 백업을 만들면 아래에 저장됩니다.

```text
data/backups/album-archive-YYYYMMDD-HHMMSS.tar.gz
```

백업에는 SQLite 일관성 스냅샷과 직접 업로드한 원본 커버가 포함되며,
재생성 가능한 `.thumbs` 파일은 제외됩니다.
