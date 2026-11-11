"""Relay the equipment camera receiver without decoding or re-encoding images.

The receiver listens on 127.0.0.1:8089 and serves ``/stream.mjpg``.
"""
from http.client import HTTPConnection, HTTPException
import json

from fastapi import APIRouter, HTTPException as APIError
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

router = APIRouter(prefix="/api/camera/d455", tags=["camera"])


def open_receiver(path):
    conn = HTTPConnection("127.0.0.1", 8089, timeout=10)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        if response.status != 200:
            raise OSError("Camera receiver unavailable")
        return conn, response
    except (OSError, HTTPException):
        conn.close()
        raise APIError(503, "Camera receiver unavailable") from None


@router.get("/status")
def status():
    conn, response = open_receiver("/status")
    try:
        return json.loads(response.read(65536))
    except (OSError, HTTPException, ValueError):
        raise APIError(502, "Invalid camera status") from None
    finally:
        response.close()
        conn.close()


@router.get("/stream.mjpg")
def stream():
    conn, response = open_receiver("/stream.mjpg")
    content_type = response.getheader("Content-Type", "")

    def close():
        response.close()
        conn.close()

    if not content_type.startswith("multipart/x-mixed-replace;"):
        close()
        raise APIError(502, "Invalid camera stream")

    def chunks():
        try:
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                yield chunk
        except (OSError, HTTPException):
            return
        finally:
            close()

    return StreamingResponse(
        chunks(),
        media_type=content_type,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        background=BackgroundTask(close),
    )
