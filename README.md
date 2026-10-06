# Album Archive

개인 음반 컬렉션을 관리하는 Flask 기반 웹 애플리케이션입니다.

## 주요 기능

- Apple Music 검색 및 링크 등록
- MusicBrainz 기반 아티스트 보조 검색
- Apple Music에 없는 음반 수동 등록
- 커버 이미지 직접 업로드
- 동일 앨범의 여러 보유본 관리
- 관리번호 / 앨범 ID 자동 생성
- 앨범 상세 및 수록곡 표시
- 수록곡 직접 추가 / 수정 / 삭제
- 앨범 관리 / 수정 / 삭제

## 로컬 실행

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

접속:

```text
http://xxx.xxx.xxx.xxx:xxxx
```

## Synology Container Manager

프로젝트 폴더 구조:

```text
Album-Archive/
├── app.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── templates/
└── data/
    ├── albums.db
    └── uploads/
```

`data/albums.db`와 `data/uploads/`는 NAS의 영구 데이터입니다.
컨테이너를 다시 빌드하거나 GitHub에서 코드를 갱신해도 이 폴더는 유지합니다.

Container Manager에서 프로젝트를 만들 때 저장소 폴더를 프로젝트 경로로 선택하고,
`docker-compose.yml`을 사용해 빌드/실행합니다.

기본 접속 포트:

```text
xxxx
```

예:

```text
http://NAS-IP:xxxx
```

## Git에 포함하지 않는 데이터

- `albums.db`
- `data/*`
- `static/uploads/*`
- `.venv/`
- `.env`

실제 DB와 직접 업로드한 커버 이미지는 별도로 백업해야 합니다.
