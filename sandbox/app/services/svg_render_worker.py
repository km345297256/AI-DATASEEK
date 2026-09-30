"""Fixed offline SVG renderer entrypoint. stdin is XML data, never Python code."""
import json
import math
import os
import sys


def main():
    # Native MuPDF diagnostics can include an entire embedded image or path.
    # Redirect at descriptor level BEFORE importing the native library. Only
    # our fixed metadata JSON can reach the retained result descriptor.
    result_fd = os.dup(1)
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    result = {"valid": False, "reason": "invalid_content"}
    try:
        pixels_fd = int(sys.argv[2]) if len(sys.argv) == 3 else None
        max_png_bytes = 5 * 1024 * 1024
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        file_size = max_png_bytes if pixels_fd is not None else 0
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))
        import pymupdf
        content = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
        if len(content) > 64 * 1024 * 1024:
            result["reason"] = "image_size_limit"
        else:
            with pymupdf.open(stream=content, filetype="svg") as document:
                if len(document) == 1:
                    page = document[0]
                    width, height = page.rect.width, page.rect.height
                    if (not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0
                            or width * height > int(sys.argv[1])):
                        result["reason"] = "image_size_limit"
                    else:
                        # Artifact checking needs only visible pixels. Preview
                        # uses up to 3x sampling so ordinary vector plots remain
                        # readable when the UI zooms, within the same pixel cap.
                        scale = min(3.0 if pixels_fd is not None else 1.0,
                                    2048 / max(width, height))
                        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
                        if pixmap.n == 4 and any(pixmap.samples[3::4]):
                            if pixels_fd is None:
                                result = {"valid": True, "width": math.ceil(width), "height": math.ceil(height)}
                            else:
                                png = pixmap.tobytes("png")
                                if len(png) > max_png_bytes:
                                    result["reason"] = "image_size_limit"
                                else:
                                    with os.fdopen(pixels_fd, "wb") as destination:
                                        destination.write(png)
                                    result = {"valid": True, "width": pixmap.width,
                                              "height": pixmap.height, "png_size": len(png)}
    except ImportError:
        result["reason"] = "validator_unavailable"
    except MemoryError:
        result["reason"] = "image_size_limit"
    except Exception:
        pass
    os.write(result_fd, json.dumps(result, separators=(",", ":")).encode())
    os.close(result_fd)


if __name__ == "__main__":
    main()
