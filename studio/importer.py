"""Inspect uploaded data locally; never send a reference video to the GPU."""
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from studio.media import probe, reference_frames
from studio.models import Source

EXTENSIONS = {".txt", ".md", ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".mkv", ".webm", ".wav", ".mp3", ".m4a"}


def import_file(service, project_id: str, path: Path, name: str, role: str) -> list[dict]:
    suffix = path.suffix.lower()
    text, status, kind = "", "ready", "text"
    frames = []
    if suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8-sig")
        if not text.strip() or len(text) > 200_000:
            raise ValueError("Văn bản phải có nội dung và tối đa 200.000 ký tự.")
    elif suffix == ".pdf":
        kind = "pdf"
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("PDF có mật khẩu: mở khóa trước khi nhập.")
        if len(reader.pages) > 300:
            raise ValueError("PDF vượt 300 trang; chia tài liệu thành các phần nhỏ.")
        pages = []
        for index, page in enumerate(reader.pages):
            pages.append(f"[Trang {index+1}]\n" + (page.extract_text() or ""))
        text = "\n\n".join(pages)
        readable = [p.split("\n", 1)[1].strip() for p in pages]
        if not any(readable):
            status, text = "needs_ocr", ""
        elif not all(readable):
            status = "partial_text"
        if len(text) > 200_000:
            raise ValueError("PDF vượt 200.000 ký tự; chia tài liệu thành các phần nhỏ.")
    elif suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        kind = "image"
        with Image.open(path) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError("Ảnh vượt 40 megapixel.")
            image.verify()
        role = "visual"
    elif suffix in {".wav", ".mp3", ".m4a"}:
        kind, role = "audio", "visual"
        if not probe(path)["audio"]:
            raise ValueError("Tệp không có âm thanh.")
    else:
        kind, role = "video", "visual"
        frames = reference_frames(path, path.parent / "frames")
    artifact = service.artifact(path, project_id, name)
    values = [{"title": name, "kind": kind, "role": role, "text": text, "status": status,
               "selected": kind in {"text", "pdf", "image"} and status == "ready", "description": "",
               "citation": "", "artifact_id": artifact["id"]}]
    for index, frame in enumerate(frames):
        frame_artifact = service.artifact(frame, project_id, f"Khung hình {index+1}.jpg")
        values.append({"title": f"{name} · khung {index+1}", "kind": "frame", "role": "visual", "text": "",
                       "status": "ready", "selected": False, "description": "", "citation": "",
                       "artifact_id": frame_artifact["id"], "parent_artifact_id": artifact["id"]})
    with service.sessions() as session:
        rows = [Source(project_id=project_id, data=value) for value in values]
        session.add_all(rows)
        session.commit()
    return [service.read(row) for row in rows]
