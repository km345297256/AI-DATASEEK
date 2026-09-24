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
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (max(1, int(sys.argv[2])), max(1, int(sys.argv[2]))))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
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
                        scale = min(1.0, 2048 / max(width, height))
                        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
                        if pixmap.n == 4 and any(pixmap.samples[3::4]):
                            result = {"valid": True, "width": math.ceil(width), "height": math.ceil(height)}
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
