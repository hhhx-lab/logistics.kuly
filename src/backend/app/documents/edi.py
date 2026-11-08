"""有界EDIFACT原始段阅读。未完成官方目录核验，不注册为上传或业务字段适配器。"""
from dataclasses import dataclass


class EdiSyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class EdiSegment:
    ordinal: int
    tag: str
    elements: tuple[tuple[str, ...], ...]
    raw: str
    # 字符偏移，不冒充字节偏移、页码或PDF矩形。
    character_start: int
    character_end: int


@dataclass(frozen=True)
class EdiReading:
    segments: tuple[EdiSegment, ...]
    message_reference: str
    declared_directory: str
    capability_state: str = "pending_official_verification"


def _split(raw: str, separator: str, release: str, *, unescape=False) -> list[str]:
    result, part = [], []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == release:
            if index + 1 == len(raw):
                raise EdiSyntaxError("dangling_release_character")
            if not unescape:
                part.append(char)
            part.append(raw[index + 1])
            index += 2
            continue
        if char == separator:
            result.append("".join(part))
            part = []
        else:
            part.append(char)
        index += 1
    result.append("".join(part))
    return result


def read_iftmin_segments(text: str) -> EdiReading:
    """只阅读单interchange、单IFTMIN D.01B消息，检查语法边界及包络计数。"""
    if not text or len(text) > 2_000_000:
        raise EdiSyntaxError("edi_size_limit")
    component, element, release, terminator = ":", "+", "?", "'"
    offset = 0
    if text.startswith("UNA"):
        if len(text) < 9 or text[7] != " ":
            raise EdiSyntaxError("unsupported_service_advice")
        component, element, release, terminator = text[3], text[4], text[6], text[8]
        if len(set(text[3:9])) != 6:
            raise EdiSyntaxError("conflicting_service_characters")
        offset = 9
    body = text[offset:]
    raw_parts = _split(body, terminator, release)
    if raw_parts[-1].strip():
        raise EdiSyntaxError("unterminated_segment")
    segments = []
    cursor = offset
    for raw in raw_parts[:-1]:
        clean = raw.strip("\r\n \t")
        start = cursor + len(raw) - len(raw.lstrip("\r\n \t"))
        cursor += len(raw) + 1
        if not clean or len(clean) > 20_000 or len(segments) >= 10_000:
            raise EdiSyntaxError("edi_segment_limit_or_empty_segment")
        parts = _split(clean, element, release)
        tag = parts[0]
        if len(tag) != 3 or not tag.isascii() or not tag.isupper() or not tag.isalpha():
            raise EdiSyntaxError("invalid_segment_tag")
        values = tuple(tuple(_split(value, component, release, unescape=True)) for value in parts[1:])
        segments.append(EdiSegment(len(segments) + 1, tag, values, text[start:cursor], start, cursor))
    if len(segments) < 5 or [s.tag for s in segments[:2]] != ["UNB", "UNH"] or [s.tag for s in segments[-2:]] != ["UNT", "UNZ"]:
        raise EdiSyntaxError("unsupported_interchange_envelope")
    if any(s.tag in {"UNB", "UNH", "UNT", "UNZ", "UNG", "UNE"} for s in segments[2:-2]):
        raise EdiSyntaxError("only_single_ungrouped_message_supported")
    unb, unh, unt, unz = segments[0], segments[1], segments[-2], segments[-1]
    try:
        if unb.elements[0] not in {("UNOA", "3"), ("UNOB", "3"), ("UNOC", "3")}:
            raise EdiSyntaxError("unsupported_syntax_identifier")
        if unh.elements[1] != ("IFTMIN", "D", "01B", "UN"):
            raise EdiSyntaxError("unsupported_message_directory")
        reference = unh.elements[0][0]
        if not reference or unt.elements != ((str(len(segments) - 2),), (reference,)):
            raise EdiSyntaxError("message_count_or_reference_mismatch")
        interchange_reference = unb.elements[4][0]
        if not interchange_reference or unz.elements != (("1",), (interchange_reference,)):
            raise EdiSyntaxError("interchange_count_or_reference_mismatch")
    except IndexError as exc:
        raise EdiSyntaxError("incomplete_envelope") from exc
    return EdiReading(tuple(segments), reference, "IFTMIN:D:01B:UN")
