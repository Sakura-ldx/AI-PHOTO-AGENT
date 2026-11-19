# JPG Upload Failure Analysis Report

> Historical incident report: paths and test counts below describe the implementation at the time of the JPG/MPO fix. See the [current code index](CURRENT_CODEBASE_INDEX.zh-CN.md) for active upload/storage behavior.

Date: 2026-07-08

## Summary

The reported `POST /api/photos HTTP/1.1" 400 Bad Request` was caused by camera/phone `.jpg` files that Pillow identifies as `MPO`, not strict `JPEG`.

These files still have a JPEG/Exif header and commonly use a `.jpg` extension, but the first implementation only allowed Pillow formats `JPEG` and `PNG`. The upload was therefore rejected after the raw file had already been written to `uploads/<photo_id>/original_upload`.

A second source of confusion was a stale FastAPI process on port `8000`. The source code had already been updated, but the browser was still talking to an old process. After stopping PID `8296` and restarting with the project `.venv`, the real failed sample uploaded successfully.

## Evidence

- Failed upload artifacts existed under `uploads/*/original_upload`.
- All inspected failed samples were valid image files with:
  - Pillow format: `MPO`
  - Mode: `RGB`
  - Size: `6000 x 4000`
  - Header prefix: `FFD8 FFE1 ... Exif`
- Direct source-level validation with `FileStorage.extract_metadata(...)` now returns:
  - `format='JPEG'`
  - `content_type='image/jpeg'`
- New runtime validation against `http://127.0.0.1:8000/api/photos` using a real failed sample returned `200`.

## Upload Code Path

The upload endpoint is implemented in `app/main.py`:

- `POST /api/photos` receives an `UploadFile`.
- It delegates to `FileStorage.save_upload(...)`.
- It returns `photo_id` and image metadata.

The storage and validation logic is implemented in `app/services/storage.py`:

- `save_upload(...)` reads the uploaded file bytes.
- It writes a temporary `original_upload`.
- `extract_metadata(...)` opens the file with Pillow and reads true image metadata.
- Valid images are renamed to `original.jpg` or `original.png`.
- Session JSON is saved under `generated/sessions/`.

## Root Cause

The original validation assumed Pillow's image format would be exactly:

- `JPEG`
- `PNG`

The user's failing `.jpg` files were identified by Pillow as:

- `MPO`

That format is a JPEG-based multi-picture format. It is common for camera and phone files to carry a `.jpg` extension while being identified internally as `MPO`.

Because `MPO` was not in `SUPPORTED_IMAGE_FORMATS`, the backend raised:

```text
Only JPEG and PNG images are supported.
```

FastAPI then returned:

```text
400 Bad Request
```

## Fix

The backend now normalizes JPEG-compatible `MPO` files to `JPEG` before checking the allowed format list.

Implementation behavior:

- `MPO` -> `JPEG`
- `JPEG` remains `JPEG`
- `PNG` remains `PNG`
- Returned metadata uses canonical `content_type`:
  - `JPEG` -> `image/jpeg`
  - `PNG` -> `image/png`

This preserves the MVP contract of supporting JPEG/PNG while accepting real-world `.jpg` variants from cameras and phones.

## Tests Added

`tests/test_api.py` now includes:

- Generic MIME JPG upload:
  - Filename: `sample.jpg`
  - MIME: `application/octet-stream`
  - Expected result: `200`, `format=JPEG`, `content_type=image/jpeg`
- MPO-as-JPG upload:
  - Filename: `sample.jpg`
  - Pillow format: `MPO`
  - MIME: `application/octet-stream`
  - Expected result: `200`, `format=JPEG`, `content_type=image/jpeg`

## Verification Results

Commands run:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Result at the time of this upload fix:

```text
10 passed
```

Full project test result after adding launcher tests:

```text
13 passed
```

Current full project test result after adding the Phase 2 learning module:

```text
18 passed
```

Runtime verification:

- Old stale server process on port `8000`: PID `8296`
- Stopped PID `8296`
- Restarted server with project `.venv`: PID `24992`
- Re-uploaded a real previously failed `original_upload` sample.
- Result:

```text
200
{"metadata":{"width":6000,"height":4000,"format":"JPEG","color_mode":"RGB","content_type":"image/jpeg"}}
```

## Remaining Notes

- The old failed `uploads/*/original_upload` files remain as diagnostic artifacts.
- The current server at `http://127.0.0.1:8000` has been restarted with the fixed code.
- If a browser still shows an old error message, refresh the page before retrying.
- Future hardening should add structured upload logging so the exact rejected Pillow format is visible in server logs.
