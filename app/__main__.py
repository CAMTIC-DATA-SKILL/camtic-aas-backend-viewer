import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="AASX viewer backend")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("AASX_DATA_DIR", "data"),
        help="AASX 파일이 들어 있는 폴더 (기본값: ./data, 환경변수 AASX_DATA_DIR)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="코드 변경 시 자동 재시작 (개발용)")
    args = parser.parse_args()

    os.environ["AASX_DATA_DIR"] = os.path.abspath(args.data_dir)
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
