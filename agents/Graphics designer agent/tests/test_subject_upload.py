"""Stage-2 upload-as-subject: the deterministic composite path (variant UPLOAD).

Also pins the byte-identical law: the composite branch is ONLY reachable via
variant "UPLOAD" — every other variant keeps the AI-generation path, and the
mere presence of ``subject_asset_ref`` changes nothing until asked for.
"""

from io import BytesIO

import pytest
from PIL import Image

from graphics_designer_agent import pipeline
from graphics_designer_agent.runs import create_run, read_artifact, save_artifact
from graphics_designer_agent.stage2_element.composite import paste_subject


def _png(w: int, h: int, rgba: tuple[int, int, int, int]) -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (w, h), rgba).save(buf, format="PNG")
    return buf.getvalue()


def _open(png: bytes) -> Image.Image:
    return Image.open(BytesIO(png)).convert("RGBA")


def test_paste_subject_bottom_center_default():
    base = _png(400, 400, (10, 20, 60, 255))
    subj = _png(100, 50, (250, 10, 10, 255))
    out = _open(paste_subject(base, subj, None))
    # 55% contain-fit of a 100x50 subject on 400px canvas -> 220x110, pasted
    # bottom-center with a 4% (16px) margin: x 90..310, y 274..384.
    assert out.getpixel((200, 329)) == (250, 10, 10, 255)
    assert out.getpixel((5, 5)) == (10, 20, 60, 255)
    assert out.size == (400, 400)


def test_paste_subject_honors_placement_cell():
    base = _png(300, 300, (10, 20, 60, 255))
    subj = _png(80, 80, (10, 250, 10, 255))
    out = _open(paste_subject(base, subj, "top-left"))
    assert out.getpixel((20, 20)) == (10, 250, 10, 255)
    assert out.getpixel((295, 295)) == (10, 20, 60, 255)


def test_paste_subject_unknown_placement_falls_back():
    base = _png(200, 200, (10, 20, 60, 255))
    subj = _png(50, 50, (250, 10, 10, 255))
    # Unknown key must not raise — falls back to bottom-center.
    out = _open(paste_subject(base, subj, "??nonsense??"))
    assert out.size == (200, 200)


def test_generate_stage2_upload_is_deterministic_composite():
    run = create_run("upload-user")
    pipeline.generate(run, 1, "A")
    pipeline.approve(run, 1)
    ref = save_artifact(run["id"], 2, "subject", "cafe1234", _png(60, 60, (255, 0, 0, 255)))
    run["config"]["subject_asset_ref"] = ref

    attempt = pipeline.generate(run, 2, "UPLOAD")

    assert attempt["variant"] == "UPLOAD"
    assert attempt["provider"] == "upload-composite"
    assert attempt["method"] == "deterministic"
    assert run["state"] == "STAGE2_REVIEW"
    # The artifact exists, is a readable PNG, and keeps the Stage-1 canvas size.
    base = _open(read_artifact(run["id"], run["stages"]["1"]["approved"]["artifact"]))
    out = _open(read_artifact(run["id"], attempt["artifact"]))
    assert out.size == base.size
    # Approving it advances the pipeline exactly like an AI attempt would.
    pipeline.approve(run, 2)
    assert run["state"].startswith("STAGE3")


def test_upload_variant_without_ref_is_a_clear_error():
    run = create_run("upload-user-2")
    pipeline.generate(run, 1, "A")
    pipeline.approve(run, 1)
    with pytest.raises(pipeline.PipelineError):
        pipeline.generate(run, 2, "UPLOAD")


def test_ref_presence_alone_does_not_hijack_ai_variants():
    run = create_run("upload-user-3")
    pipeline.generate(run, 1, "A")
    pipeline.approve(run, 1)
    ref = save_artifact(run["id"], 2, "subject", "beef5678", _png(40, 40, (255, 0, 0, 255)))
    run["config"]["subject_asset_ref"] = ref
    # A normal variant still goes through the provider path, not the compositor.
    attempt = pipeline.generate(run, 2, "A")
    assert attempt["variant"] == "A"
    assert attempt["provider"] != "upload-composite"


# =========================================================================== #
# Direct-to-GCS run uploads (2026-10-09): subject / background / prompt /
# element through ``POST /api/gd/runs/{id}/uploads`` + ``…/finalize``.
#
# The decode path's adversarial cases live here because a run's images are
# where the 50 MB photos land. Every file is generated in the test (or in a
# tmp dir), never committed. Backend imports happen inside the fixture so the
# pure pipeline tests above still run on the standalone agent interpreter.
# =========================================================================== #
import io
import struct
import sys
import zlib
from pathlib import Path

MB = 1024 * 1024
DIRECT_USER = {"id": "gd-direct-user", "email": "d@legalsoft.com"}


@pytest.fixture()
def run_upload(monkeypatch):
    """Direct uploads ON over the in-memory bucket, a signed-in caller, and a
    fresh run. Yields ``(gcs, base_path, run_id, client)``."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    pytest.importorskip("google.cloud.firestore")  # backend-only dependency
    from app.main import app as fastapi_app
    from app.routers.tests.test_gd_elements_api import client, install_direct_gcs
    from app.security import get_current_user

    gcs = install_direct_gcs(monkeypatch)
    monkeypatch.setitem(fastapi_app.dependency_overrides, get_current_user,
                        lambda: dict(DIRECT_USER))
    r = client.post("/api/gd/runs", json={})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    return gcs, f"/api/gd/runs/{rid}", rid, client


def _up(run_upload, surface, data, file_name="photo.bin", content_type="application/octet-stream"):
    from app.routers.tests.test_gd_elements_api import direct_upload

    gcs, base, _rid, client = run_upload
    return direct_upload(base, surface, data, gcs, content_type=content_type,
                         file_name=file_name, http=client)


def _md5(data: bytes) -> str:
    import hashlib

    return hashlib.md5(data).hexdigest()


def _save(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def _working(run_upload, ref: str) -> Image.Image:
    _gcs, _base, rid, _client = run_upload
    return Image.open(BytesIO(read_artifact(rid, ref)))


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _png_header_only(w: int, h: int, color_type: int = 6) -> bytes:
    """A PNG whose IHDR claims ``w`` x ``h`` (RGBA) with almost no pixel data:
    a few hundred bytes on the wire, ``w*h*4`` bytes to decode."""
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + _png_chunk(b"IEND", b""))


def _srgb_to_lab(r: float, g: float, b: float) -> tuple[float, float, float]:
    def lin(c):
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    R, G, B = lin(r), lin(g), lin(b)
    X = (0.4360747 * R + 0.3850649 * G + 0.1430804 * B) / 0.9642
    Y = 0.2225045 * R + 0.7168786 * G + 0.0606169 * B
    Z = (0.0139322 * R + 0.0971045 * G + 0.7141733 * B) / 0.8249

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116
    return 116 * f(Y) - 16, 500 * (f(X) - f(Y)), 200 * (f(Y) - f(Z))


#: Process-ink colours the test profile maps each CMYK channel to — deliberately
#: NOT the naive ``255 - C`` result, so the colour-managed path is visible.
_INKS = {"c": (0, 174, 239), "m": (236, 0, 140), "y": (255, 242, 0), "k": (35, 31, 32)}


def _cmyk_icc_profile() -> bytes:
    """A minimal, valid ICC v2 CMYK->Lab output profile (lut8, 2-point grid),
    built in the test so no binary profile is committed or needed from the OS."""
    clut = bytearray()
    for c in (0, 1):
        for m in (0, 1):
            for y in (0, 1):
                for k in (0, 1):
                    rgb = [255.0, 255.0, 255.0]
                    for on, ink in ((c, "c"), (m, "m"), (y, "y"), (k, "k")):
                        if on:
                            rgb = [v * t / 255 for v, t in zip(rgb, _INKS[ink])]
                    L, a, bb = _srgb_to_lab(*rgb)
                    clut += bytes([round(L * 255 / 100), round(a + 128), round(bb + 128)])
    ident = bytes(range(256))
    lut = (b"mft1" + b"\0" * 4 + bytes([4, 3, 2, 0])
           + b"".join(struct.pack(">i", v) for v in (65536, 0, 0, 0, 65536, 0, 0, 0, 65536))
           + ident * 4 + bytes(clut) + ident * 3)
    desc_txt = b"test cmyk\0"
    desc = b"desc" + b"\0" * 4 + struct.pack(">I", len(desc_txt)) + desc_txt + b"\0" * 81
    wtpt = b"XYZ " + b"\0" * 4 + b"".join(struct.pack(">i", round(v * 65536)) for v in (0.9642, 1.0, 0.8249))
    tags = [(b"desc", desc), (b"wtpt", wtpt), (b"A2B0", lut), (b"cprt", b"text" + b"\0" * 4 + b"none\0")]
    offset = 128 + 4 + 12 * len(tags)
    table, body = b"", b""
    for sig, data in tags:
        table += sig + struct.pack(">II", offset + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    header = (struct.pack(">I", offset + len(body)) + b"lcms" + bytes([2, 0x10, 0, 0]) + b"prtr"
              + b"CMYK" + b"Lab " + b"\0" * 12 + b"acsp" + b"\0" * 24 + struct.pack(">I", 0)
              + b"".join(struct.pack(">i", round(v * 65536)) for v in (0.9642, 1.0, 0.8249))
              + b"\0" * 48)
    assert len(header) == 128
    return header + struct.pack(">I", len(tags)) + table + body


@pytest.fixture(scope="module")
def fifty_mb_jpeg(tmp_path_factory) -> bytes:
    """A real 4600x4600 noise JPEG (~48.8 MiB), padded after EOI to exactly the
    50 MiB cap. Written to a tmp dir, never to the repo."""
    import numpy as np

    pixels = np.random.default_rng(7).integers(0, 256, size=(4600, 4600, 3), dtype=np.uint8)
    data = _save(Image.fromarray(pixels, "RGB"), "JPEG", quality=95, subsampling=0)
    assert len(data) < 50 * MB
    data += b"\0" * (50 * MB - len(data))       # Pillow ignores bytes after EOI
    path = tmp_path_factory.mktemp("gd-direct") / "fifty.jpg"
    path.write_bytes(data)
    return path.read_bytes()


def test_a_50_mb_jpeg_background_becomes_a_bounded_jpeg_the_pipeline_uses(run_upload, fifty_mb_jpeg):
    gcs, base, rid, client = run_upload
    signed, r = _up(run_upload, "background", fifty_mb_jpeg, "huge.jpg", "image/jpeg")
    assert signed["max_bytes"] == 50 * MB
    assert r.status_code == 200, r.text
    body = r.json()
    up = body["upload"]
    assert body["role"] == "background" and body["ref"] == f"{_md5(fifty_mb_jpeg)}-w4096.jpg"
    assert up["original"]["bytes"] == 50 * MB
    assert (up["original"]["width"], up["original"]["height"]) == (4600, 4600)
    assert up["working"] == {"width": 4096, "height": 4096, "format": "jpeg"}
    original = f"generated/gd/{rid}/originals/{_md5(fifty_mb_jpeg)}.jpg"
    assert gcs.store[original][0] == fifty_mb_jpeg and gcs.meta[original]["disposition"] == "attachment"
    assert gcs.pending() == []
    # the proxy serves it as what it is, and Stage 1 UPLOAD cover-fits it
    art = client.get(f"{base}/artifact/{body['ref']}")
    assert art.status_code == 200 and art.headers["content-type"] == "image/jpeg"
    r = client.post(f"{base}/config", json={"background_asset_ref": body["ref"]})
    assert r.status_code == 200, r.text
    r = client.post(f"{base}/generate", json={"stage": 1, "variant": "UPLOAD"})
    assert r.status_code == 200, r.text
    # one byte over the cap never reaches GCS
    r = client.post(f"{base}/uploads", json={"surface": "background", "content_type": "image/jpeg"})
    assert gcs.browser_put(r.json(), fifty_mb_jpeg + b"\0") == 400


def test_a_png_over_100_megapixels_is_refused_with_its_dimensions(run_upload):
    gcs, base, rid, _client = run_upload
    _, r = _up(run_upload, "subject", _png_header_only(10001, 10001), "poster.png", "image/png")
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert d["code"] == "image_too_large" and (d["width"], d["height"]) == (10001, 10001)
    assert d["decoded_bytes"] == 10001 * 10001 * 4 and d["limit_bytes"] == 400_000_000
    assert "10001x10001" in d["message"] and d["file"] == "poster.png"
    assert gcs.pending() == [] and not any("/originals/" in p for p in gcs.store)
    # past Pillow's own ceiling (2 x 125 MP) the open itself refuses — still a 422
    _, r = _up(run_upload, "subject", _png_header_only(16000, 16000, color_type=0), "big.png")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "image_too_large"


def test_cmyk_converts_through_its_icc_profile_and_flags_when_it_has_none(run_upload):
    cmyk = Image.new("CMYK", (64, 64), (255, 0, 0, 0))   # pure cyan ink
    with_icc = _save(cmyk, "JPEG", quality=95, icc_profile=_cmyk_icc_profile())
    _, r = _up(run_upload, "background", with_icc, "print.jpg", "image/jpeg")
    assert r.status_code == 200, r.text
    assert r.json()["upload"]["flags"] == []
    px = _working(run_upload, r.json()["ref"]).convert("RGB").getpixel((32, 32))
    assert abs(px[0] - 0) < 20 and abs(px[1] - 174) < 20 and abs(px[2] - 239) < 20, px

    without = _save(cmyk, "JPEG", quality=95)
    _, r = _up(run_upload, "background", without, "print2.jpg", "image/jpeg")
    assert r.status_code == 200, r.text
    assert r.json()["upload"]["flags"] == ["color_converted_without_profile"]
    px = _working(run_upload, r.json()["ref"]).convert("RGB").getpixel((32, 32))
    assert px[0] < 20 and px[1] > 235 and px[2] > 235, px


def test_sixteen_bit_greyscale_is_scaled_to_eight_bit_not_clipped(run_upload):
    import numpy as np

    deep = Image.fromarray(np.full((40, 30), 0x8000, dtype=np.uint16))
    assert deep.mode == "I;16"
    _, r = _up(run_upload, "subject", _save(deep, "PNG"), "scan.png", "image/png")
    assert r.status_code == 200, r.text
    im = _working(run_upload, r.json()["ref"])
    assert im.format == "PNG" and im.size == (30, 40)
    assert im.convert("L").getpixel((5, 5)) == 128     # 0x8000 >> 8, not clipped to 255


def test_exif_rotation_is_applied_to_the_working_copy(run_upload):
    img = Image.new("RGB", (400, 200), (10, 120, 220))
    exif = img.getexif()
    exif[0x0112] = 6                                    # rotate 90 CW on display
    _, r = _up(run_upload, "subject", _save(img, "JPEG", exif=exif.tobytes()), "phone.jpg")
    assert r.status_code == 200, r.text
    up = r.json()["upload"]
    assert (up["original"]["width"], up["original"]["height"]) == (400, 200)   # stored grid
    assert (up["working"]["width"], up["working"]["height"]) == (200, 400)     # as displayed
    assert _working(run_upload, r.json()["ref"]).size == (200, 400)


def test_a_multi_page_tiff_uses_page_one_and_says_so(run_upload):
    pages = [Image.new("RGB", (50, 40), c) for c in ((250, 0, 0), (0, 250, 0), (0, 0, 250))]
    tiff = _save(pages[0], "TIFF", save_all=True, append_images=pages[1:])
    _, r = _up(run_upload, "background", tiff, "layers.tif", "image/tiff")
    assert r.status_code == 200, r.text
    assert r.json()["upload"]["pages_used"] == "1 of 3"
    assert r.json()["upload"]["kind"] == "tiff"
    px = _working(run_upload, r.json()["ref"]).convert("RGB").getpixel((25, 20))
    assert px[0] > 200 and px[1] < 40, px


_PSD = b"8BPS\x00\x01" + b"\0" * 200
_EPS = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
_HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\0" * 200
_AVIF = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + b"\0" * 200


@pytest.mark.parametrize("data,got,advice", [
    (_PSD, "psd", "export a flattened PNG/JPEG/TIFF"),
    (_EPS, "eps", "export a flattened PNG/JPEG/TIFF"),
    (_HEIC, "heic", "export as JPEG/PNG"),
    (_AVIF, "avif", "export as JPEG/PNG"),
    (b"GIF89a" + b"\0" * 100, "gif", "use PNG, JPEG, WEBP, TIFF"),
    (b"just some text, not an image", "unknown", "use PNG, JPEG, WEBP, TIFF"),
])
def test_files_renamed_to_png_are_refused_by_what_their_bytes_are(run_upload, data, got, advice):
    gcs, *_ = run_upload
    _, r = _up(run_upload, "subject", data, "innocent.png", "image/png")
    assert r.status_code == 415, r.text
    d = r.json()["detail"]
    assert d["code"] == "unsupported_file_type" and d["got"] == got and advice in d["message"]
    assert d["accepted"] == ["png", "jpeg", "webp", "tiff"] and d["file"] == "innocent.png"
    assert gcs.pending() == [] and not any("/originals/" in p for p in gcs.store)


def test_a_corrupt_image_is_an_honest_422_not_a_crash(run_upload):
    png = _png(40, 40, (1, 2, 3, 255))
    _, r = _up(run_upload, "subject", png[: len(png) // 2], "cut.png", "image/png")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "image_unreadable"


def test_prompt_images_attach_to_the_run_once_each_up_to_three(run_upload):
    gcs, base, rid, client = run_upload
    images = [_png(20 + i, 20, (i * 80, 0, 0, 255)) for i in range(3)]
    refs = []
    for i, img in enumerate(images):
        _, r = _up(run_upload, "prompt", img, f"p{i}.png")
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "prompt" and r.json()["ref"].endswith("-w4096.png")
        refs.append(r.json()["ref"])
        if i == 0:   # the same image attached twice is one attachment, not two
            _, again = _up(run_upload, "prompt", img, "p0-again.png")
            assert again.status_code == 200 and again.json()["ref"] == refs[0]
            assert client.get(base).json()["config"]["prompt_image_refs"] == refs
    assert client.get(base).json()["config"]["prompt_image_refs"] == refs
    r = client.post(f"{base}/uploads", json={"surface": "prompt", "content_type": "image/png"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "prompt_image_limit_reached"


def test_an_element_upload_keeps_alpha_and_is_usable_as_an_image_element(run_upload):
    gcs, base, rid, client = run_upload
    sticker = _png(30, 30, (255, 0, 0, 0))
    _, r = _up(run_upload, "element", sticker, "sticker.png", "image/png")
    assert r.status_code == 200, r.text
    ref = r.json()["ref"]
    assert _working(run_upload, ref).mode == "RGBA"
    r = client.post(f"{base}/config",
                    json={"elements": [{"kind": "image", "ref": ref, "x": 0.5, "y": 0.5}]})
    assert r.status_code == 200, r.text


def test_a_subject_upload_feeds_the_stage_two_composite(run_upload):
    gcs, base, rid, client = run_upload
    _, r = _up(run_upload, "subject", _png(60, 60, (255, 0, 0, 255)), "me.png", "image/png")
    assert r.status_code == 200, r.text
    assert client.post(f"{base}/generate", json={"stage": 1, "variant": "A"}).status_code == 200
    assert client.post(f"{base}/approve", json={"stage": 1}).status_code == 200
    assert client.post(f"{base}/config", json={"subject_asset_ref": r.json()["ref"]}).status_code == 200
    r = client.post(f"{base}/generate", json={"stage": 2, "variant": "UPLOAD"})
    assert r.status_code == 200, r.text
    assert r.json()["attempt"]["provider"] == "upload-composite"


def test_a_run_ticket_is_bound_to_its_owner_and_its_run(run_upload, monkeypatch):
    from app.main import app as fastapi_app
    from app.security import get_current_user

    from app.routers.tests.test_gd_elements_api import direct_upload

    gcs, base, rid, client = run_upload
    signed, _ = direct_upload(base, "subject", _png(11, 11, (0, 0, 0, 255)), gcs,
                              finalize=False, http=client)
    other_run = client.post("/api/gd/runs", json={}).json()["id"]
    r = client.post(f"/api/gd/runs/{other_run}/uploads/finalize", json={"ticket": signed["ticket"]})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "upload_ticket_wrong_target"
    monkeypatch.setitem(fastapi_app.dependency_overrides, get_current_user,
                        lambda: {"id": "someone-else", "email": "x@legalsoft.com"})
    r = client.post(f"{base}/uploads", json={"surface": "subject"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "run_not_found"
    r = client.post(f"{base}/uploads/finalize", json={"ticket": signed["ticket"]})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "upload_ticket_wrong_user"
    assert len(gcs.pending()) == 1


def test_upload_artifacts_are_stored_by_name_in_cloud_mode(monkeypatch):
    from graphics_designer_agent import runs as gd_runs

    pytest.importorskip("google.cloud.firestore")
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from app.services import storage

    puts = []
    monkeypatch.setattr(gd_runs, "GD_STORAGE_BACKEND", "cloud")
    monkeypatch.setattr(storage, "put_generated",
                        lambda partition, file_name, data, content_type: puts.append(
                            (partition, file_name, content_type)) or "gs://b/x")
    name = "0123456789abcdef0123456789abcdef-w4096.jpg"
    assert gd_runs.save_upload_artifact("run123", name, b"jpg", "image/jpeg") == name
    assert puts == [("gd/run123", name, "image/jpeg")]
    assert gd_runs.is_own_artifact_ref("run123", name)
    with pytest.raises(ValueError):
        gd_runs.save_upload_artifact("run123", "../escape.png", b"x", "image/png")


def test_sniffing_names_what_the_bytes_are():
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from app.services.upload_intake import sniff

    assert sniff(_png(2, 2, (0, 0, 0, 255))) == "png"
    assert sniff(_HEIC) == "heic" and sniff(_AVIF) == "avif" and sniff(_PSD) == "psd"
    assert sniff(_EPS) == "eps"
    assert sniff(b"%PDF-1.6\n%\xe2\xe3\n<</Creator(Adobe Illustrator 28.0)>>") == "ai"
    assert sniff(b"%PDF-1.7\n1 0 obj") == "pdf"
    assert sniff(b"BM" + b"\0" * 12 + (40).to_bytes(4, "little") + b"\0" * 40) == "bmp"
    assert sniff(b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>') == "svg"
    assert sniff(b"OTTO" + b"\0" * 8) == "otf" and sniff(b"\x00\x01\x00\x00" + b"\0" * 8) == "ttf"
    assert sniff(b"wOF2" + b"\0" * 8) == "woff2" and sniff(b"") == "unknown"
