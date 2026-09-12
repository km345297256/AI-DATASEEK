"""Read-only, range-backed EDF/BDF fixed-record signal windows.

Implemented from the public EDF/EDF+ and BioSemi specifications, not from
PyEDFlib code. PyEDFlib's filename-based API is deliberately not used: a
preview must never download an entire recording just to select a time window.
Only ordinary continuous EDF/BDF and header-declared EDF+C/BDF+C are accepted.
No patient, recording identity, date, free-text annotation or transducer field
is returned. This is an exploratory display, not a diagnostic signal pipeline.

References: https://www.edfplus.info/specs/edf.html
https://www.edfplus.info/specs/edfplus.html
https://www.biosemi.com/faq/file_format.htm
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable

MAX_SOURCE_BYTES = 8 * 1024**3
MAX_HEADER_BYTES = 1024**2
MAX_READ_BYTES = 1024**2
MAX_TOTAL_BYTES = 8 * 1024**2
MAX_READS = 128
MAX_SAMPLES = 16384
MAX_CHANNELS = 8
MAX_SIGNALS = 256
MAX_DURATION_SECONDS = 60
MAX_OUTPUT_BYTES = 2 * 1024**2
CALIBRATION = "physical_min+(digital-digital_min)*(physical_max-physical_min)/(digital_max-digital_min)"
WARNINGS = [
    "仅显示所选通道和相对时间窗；不重采样、不滤波、不用于诊断。",
    "标注与状态通道不作为波形读取，患者及记录身份字段不返回。",
]
_NUMERIC = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?\Z")


class SignalPreviewError(ValueError):
    """Only fixed safe messages; never interpolate raw parser fields."""


def validate_options(options):
    if not isinstance(options, dict) or set(options) - {"channels", "start_seconds", "duration_seconds"}:
        raise SignalPreviewError("不支持的信号时间窗参数。")
    channels = options.get("channels")
    if "channels" in options and (not isinstance(channels, list) or not 1 <= len(channels) <= MAX_CHANNELS
        or any(type(v) is not int or not 0 <= v < MAX_SIGNALS for v in channels)
        or len(set(channels)) != len(channels)):
        raise SignalPreviewError("信号通道选择无效。")
    for key in ("start_seconds", "duration_seconds"):
        if key in options:
            value = options[key]
            if type(value) not in (int, float) or not 0 <= value <= 10**13 or not math.isfinite(value):
                raise SignalPreviewError("信号时间窗必须为有限非负数。")
    if "duration_seconds" in options and not 0 < options["duration_seconds"] <= MAX_DURATION_SECONDS:
        raise SignalPreviewError("信号时间窗必须大于零且不超过 60 秒。")
    return dict(options)


class BoundedRangeSource:
    """A narrow byte capability with independent accounting before each read."""

    def __init__(self, size: int, read_range: Callable[[int, int], bytes], limits=None):
        if type(size) is not int or not 256 <= size <= MAX_SOURCE_BYTES or not callable(read_range):
            raise SignalPreviewError("信号文件大小超出只读预览预算。")
        maxima = {"max_read_bytes": MAX_READ_BYTES, "max_total_bytes": MAX_TOTAL_BYTES, "max_reads": MAX_READS}
        if limits is not None:
            if not isinstance(limits, dict) or set(limits) != set(maxima):
                raise SignalPreviewError("信号读取预算无效。")
            for key, value in limits.items():
                if type(value) is not int or not 1 <= value <= maxima[key]:
                    raise SignalPreviewError("信号读取预算无效。")
            maxima = dict(limits)
        self.size, self._reader, self.limits = size, read_range, maxima
        self.read_bytes = self.read_requests = 0

    def read(self, offset: int, length: int) -> bytes:
        if (type(offset) is not int or type(length) is not int or offset < 0 or length <= 0
            or offset + length > self.size or length > self.limits["max_read_bytes"]
            or self.read_bytes + length > self.limits["max_total_bytes"]
            or self.read_requests >= self.limits["max_reads"]):
            raise SignalPreviewError("信号窗口读取超出预算，请缩短窗口或减少通道。")
        self.read_requests += 1
        self.read_bytes += length
        data = self._reader(offset, length)
        if not isinstance(data, bytes) or len(data) != length:
            raise SignalPreviewError("信号窗口数据长度与声明不一致。")
        return data


def _text(data: bytes) -> str:
    if any(v < 32 or v > 126 for v in data):
        raise SignalPreviewError("信号文件头不是标准 ASCII 字段。")
    return data.decode("ascii").strip()


def _display(data: bytes) -> str:
    value = _text(data)
    if re.search(r"(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", value, re.I):
        return "[redacted]"[:len(data)]
    return value


def _int(data: bytes, *, minimum=0, maximum=10**9) -> int:
    text = _text(data)
    if not re.fullmatch(r"-?\d{1,9}", text):
        raise SignalPreviewError("信号文件头的整数字段无效。")
    value = int(text)
    if not minimum <= value <= maximum:
        raise SignalPreviewError("信号文件头的整数字段超出预算。")
    return value


def _fraction(data: bytes) -> Fraction:
    text = _text(data)
    if not _NUMERIC.fullmatch(text):
        raise SignalPreviewError("信号文件头的数值字段无效。")
    # Header fields are at most eight bytes; explicitly bound exponent before
    # constructing a Fraction, which otherwise permits enormous integers.
    if "e" in text.lower() and abs(int(text.lower().split("e")[1])) > 24:
        raise SignalPreviewError("信号文件头的数值指数超出预算。")
    value = Fraction(text)
    if abs(value) > 10**24:
        raise SignalPreviewError("信号文件头的数值超出预算。")
    return value


@dataclass(frozen=True)
class Signal:
    id: int
    label: str
    unit: str
    samples: int
    offset: int
    physical_min: Fraction
    physical_max: Fraction
    digital_min: int
    digital_max: int
    channel_type: str

    def choice(self, duration: Fraction):
        return {"id": self.id, "label": self.label, "unit": self.unit,
                "sample_rate": float(self.samples / duration), "samples_per_record": self.samples,
                "selectable": self.channel_type == "signal", "channel_type": self.channel_type,
                "physical_min": float(self.physical_min), "physical_max": float(self.physical_max),
                "digital_min": self.digital_min, "digital_max": self.digital_max}


def _headers(source: BoundedRangeSource, fmt: str):
    if fmt not in {"edf", "bdf"}:
        raise SignalPreviewError("仅支持生理信号 EDF/BDF，不支持 ESRF EDF 图像。")
    main = source.read(0, 256)
    signature = b"0       " if fmt == "edf" else b"\xffBIOSEMI"
    if main[:8] != signature:
        raise SignalPreviewError("文件签名不匹配生理信号 EDF/BDF。")
    # Validate encoding but never retain or publish identity/date fields.
    _text(main[8:256])
    count = _int(main[252:256], minimum=1, maximum=MAX_SIGNALS)
    header_bytes = _int(main[184:192], minimum=512, maximum=MAX_HEADER_BYTES)
    if header_bytes != (count + 1) * 256 or header_bytes >= source.size:
        raise SignalPreviewError("信号文件头长度与通道数量不一致。")
    reserved = _text(main[192:236])
    if reserved.startswith(("EDF+D", "BDF+D")):
        raise SignalPreviewError("暂不支持不连续 EDF+D/BDF+D；不能将间断记录显示为连续信号。")
    plus = reserved.startswith("EDF+C" if fmt == "edf" else "BDF+C")
    if reserved.startswith(("EDF+", "BDF+")) and not plus:
        raise SignalPreviewError("不支持或不匹配的 EDF/BDF 扩展。")
    records = _int(main[236:244], minimum=1, maximum=99999999)
    duration = _fraction(main[244:252])
    if not Fraction(1, 1000000) <= duration <= 86400:
        raise SignalPreviewError("数据记录时长超出信号预览预算。")
    header = source.read(256, header_bytes - 256)
    _text(header)
    fields, position = [], 0
    for width in (16, 80, 8, 8, 8, 8, 8, 80, 8, 32):
        fields.append([header[position + i * width:position + (i + 1) * width] for i in range(count)])
        position += count * width
    sample_bytes = 2 if fmt == "edf" else 3
    low, high = -(1 << (8 * sample_bytes - 1)), (1 << (8 * sample_bytes - 1)) - 1
    signals, offset = [], 0
    for i in range(count):
        label = _display(fields[0][i])
        unit = _display(fields[2][i])
        pmin, pmax = _fraction(fields[3][i]), _fraction(fields[4][i])
        dmin = _int(fields[5][i], minimum=low, maximum=high)
        dmax = _int(fields[6][i], minimum=low, maximum=high)
        samples = _int(fields[8][i], minimum=1, maximum=99999999)
        raw_label = _text(fields[0][i]).lower()
        channel_type = "signal"
        if raw_label in {"edf annotations", "bdf annotations"}:
            channel_type = "annotation"
        elif fmt == "bdf" and (raw_label in {"status", "trigger", "trigger status"} or (i == count - 1 and not plus)):
            channel_type = "status"
        if dmin >= dmax or (channel_type == "signal" and pmin == pmax):
            raise SignalPreviewError("信号数字或物理校准范围无效。")
        # EDF permits inverted physical gain; do not silently flip polarity.
        if channel_type == "signal" and samples / duration > 10**7:
            raise SignalPreviewError("信号采样率超出只读预览预算。")
        signals.append(Signal(i, label, unit, samples, offset, pmin, pmax, dmin, dmax, channel_type))
        offset += samples * sample_bytes
    if header_bytes + offset * records != source.size:
        raise SignalPreviewError("数据记录长度与文件大小不一致，或录制尚未完成。")
    if plus and not any(s.channel_type == "annotation" for s in signals):
        raise SignalPreviewError("EDF+/BDF+ 缺少规定的标注通道。")
    return signals, duration, records, header_bytes, offset, sample_bytes, fmt.upper() + ("+C" if plus else "")


def _ceil(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def edf_window_preview(read_range: Callable[[int, int], bytes], size: int, fmt: str, options=None, limits=None):
    """Read headers, then exact selected samples in [start, start + duration).

    All read intervals are planned and checked before reading any sample. No
    resampling, unselected records, annotation contents or local files are used.
    """
    options = validate_options({} if options is None else options)
    source = BoundedRangeSource(size, read_range, limits)
    signals, duration, records, header_bytes, record_bytes, sample_bytes, variant = _headers(source, fmt)
    available = [s.id for s in signals if s.channel_type == "signal"]
    if not available:
        raise SignalPreviewError("文件不包含可预览的普通信号通道。")
    channels = options.get("channels", available[:1])
    if any(i not in available for i in channels):
        raise SignalPreviewError("不能将标注、状态或不存在的通道作为信号绘制。")
    selected = [signals[i] for i in channels]
    total_duration = duration * records
    start = Fraction(str(options.get("start_seconds", 0)))
    if start >= total_duration:
        raise SignalPreviewError("信号时间窗起点超出录制范围。")
    if "duration_seconds" in options:
        length = Fraction(str(options["duration_seconds"]))
        if start + length > total_duration:
            raise SignalPreviewError("信号时间窗终点超出录制范围。")
    else:
        # Adapt only the *default requested window*, not the sample rate.
        sample_seconds = Fraction(MAX_SAMPLES - len(selected), sum(s.samples for s in selected)) * duration
        read_seconds = max(1, (source.limits["max_reads"] - source.read_requests) // len(selected) - 1) * duration
        length = min(Fraction(10), total_duration - start, sample_seconds, read_seconds)
    plan, spans, total_samples = [], [], 0
    for signal in selected:
        first = _ceil(start * signal.samples / duration)
        stop = _ceil((start + length) * signal.samples / duration)
        count = stop - first
        if count <= 0:
            raise SignalPreviewError("此时间窗内没有所选通道采样点，请扩大或移动窗口。")
        total_samples += count
        if total_samples > MAX_SAMPLES:
            raise SignalPreviewError("信号窗口超过 16384 点预算，请缩短窗口或减少通道。")
        fragments = []
        cursor = first
        while cursor < stop:
            record, in_record = divmod(cursor, signal.samples)
            count_part = min(stop - cursor, signal.samples - in_record)
            offset = header_bytes + record * record_bytes + signal.offset + in_record * sample_bytes
            byte_count = count_part * sample_bytes
            fragments.append((len(plan), count_part))
            plan.append((offset, byte_count))
            if len(plan) > source.limits["max_reads"]:
                raise SignalPreviewError("信号窗口读取次数超出预算，请缩短窗口。")
            cursor += count_part
        spans.append((signal, first, stop, fragments))
    if (len(plan) + source.read_requests > source.limits["max_reads"]
        or source.read_bytes + sum(n for _, n in plan) > source.limits["max_total_bytes"]
        or any(n > source.limits["max_read_bytes"] for _, n in plan)):
        raise SignalPreviewError("信号窗口读取超出预算，请缩短窗口或减少通道。")
    chunks = [source.read(offset, length) for offset, length in plan]
    series = []
    for signal, first, stop, fragments in spans:
        gain = (signal.physical_max - signal.physical_min) / (signal.digital_max - signal.digital_min)
        x, y = [], []
        for sample in range(first, stop):
            x.append(float(sample * duration / signal.samples))
        for chunk_index, _ in fragments:
            chunk = chunks[chunk_index]
            for p in range(0, len(chunk), sample_bytes):
                digital = int.from_bytes(chunk[p:p + sample_bytes], "little", signed=True)
                if not signal.digital_min <= digital <= signal.digital_max:
                    raise SignalPreviewError("数字采样值超出声明的校准范围。")
                y.append(float(signal.physical_min + (digital - signal.digital_min) * gain))
        if any(not math.isfinite(v) for v in x + y) or any(a >= b for a, b in zip(x, x[1:])):
            raise SignalPreviewError("信号时间或幅值无法安全表示为有限数值。")
        series.append({"channel": signal.id, "label": signal.label, "unit": signal.unit,
                       "sample_rate": float(signal.samples / duration), "x": x, "y": y})
    metadata = {"format": fmt, "variant": variant, "total_duration_seconds": float(total_duration),
        "record_duration_seconds": float(duration), "records": records, "signal_count": len(signals),
        "header_bytes": header_bytes, "source_bytes": size, "read_bytes": source.read_bytes,
        "read_requests": source.read_requests, "identity_fields_hidden": True, "annotations_hidden": True,
        "no_resampling": True, "calibration": CALIBRATION,
        "time_origin": "first data record; relative seconds",
        "continuity": "header-declared; annotation timeline not read",
        "limits": {"max_samples": MAX_SAMPLES, "max_channels": MAX_CHANNELS,
                   "max_duration_seconds": MAX_DURATION_SECONDS, "max_header_bytes": MAX_HEADER_BYTES,
                   "max_source_bytes": MAX_SOURCE_BYTES}}
    result = {"contract_version": 2, "type": "edf", "reader": "edf", "kind": "series",
              "media_type": "application/json", "series": series,
              "choices": {"channels": [s.choice(duration) for s in signals]},
              "selected": {"channels": channels, "start_seconds": float(start), "duration_seconds": float(length)},
              "metadata": metadata, "warnings": WARNINGS[:],
              "sampled": start != 0 or length != total_duration or set(channels) != set(available)}
    if len(json.dumps(metadata, allow_nan=False).encode()) > MAX_HEADER_BYTES or len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT_BYTES:
        raise SignalPreviewError("信号窗口输出超过预览预算。")
    return result
