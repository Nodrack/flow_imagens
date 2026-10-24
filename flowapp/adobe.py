"""Formatos do Adobe Stock: categorias, metadados embutidos no JPEG e CSV do portal de colaborador.

O portal lê título e palavras-chave do XMP (dc:title, dc:subject) ao receber o arquivo; o IPTC
vai junto para programas que só leem esse padrão. O CSV aplica título, palavras-chave e categoria
a um lote já enviado (Uploaded Files → New → Upload CSV).
"""
from __future__ import annotations

import csv
import io
import struct
import unicodedata
from xml.sax.saxutils import escape

ADOBE_CATEGORIES = {
    1: "Animals",
    2: "Buildings and Architecture",
    3: "Business",
    4: "Drinks",
    5: "The Environment",
    6: "States of Mind",
    7: "Food",
    8: "Graphic Resources",
    9: "Hobbies and Leisure",
    10: "Industry",
    11: "Landscapes",
    12: "Lifestyle",
    13: "People",
    14: "Plants and Flowers",
    15: "Culture and Religion",
    16: "Science",
    17: "Social Issues",
    18: "Sports",
    19: "Technology",
    20: "Transport",
    21: "Travel",
}
CSV_TITLE_LIMIT = 70
CSV_FILENAME_LIMIT = 30


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(text.lower().replace("&", "and").split())


def category_number(text: str | int | None) -> int | None:
    """Número da categoria Adobe a partir do número, do nome exato ou de um nome contido no texto."""
    if text is None:
        return None
    if isinstance(text, int):
        return text if text in ADOBE_CATEGORIES else None
    folded = _fold(str(text))
    if not folded:
        return None
    leading = folded.split(" ")[0].rstrip(".:-")
    if leading.isdigit() and int(leading) in ADOBE_CATEGORIES:
        return int(leading)
    names = {_fold(name): number for number, name in ADOBE_CATEGORIES.items()}
    if folded in names:
        return names[folded]
    for name, number in sorted(names.items(), key=lambda item: -len(item[0])):
        if name in folded or name.removeprefix("the ") in folded:
            return number
    return None


def build_xmp(title: str, keywords: list[str]) -> bytes:
    title_xml = escape(title)
    subjects = "".join(f"<rdf:li>{escape(keyword)}</rdf:li>" for keyword in keywords)
    packet = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">'
        f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{title_xml}</rdf:li></rdf:Alt></dc:title>'
        f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">{title_xml}</rdf:li></rdf:Alt></dc:description>'
        f"<dc:subject><rdf:Bag>{subjects}</rdf:Bag></dc:subject>"
        f"<photoshop:Headline>{title_xml}</photoshop:Headline>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    )
    return packet.encode("utf-8")


def _truncate_utf8(text: str, limit: int) -> bytes:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return data
    return data[:limit].decode("utf-8", "ignore").encode("utf-8")


def _iptc_record(record: int, dataset: int, value: bytes) -> bytes:
    return struct.pack(">BBBH", 0x1C, record, dataset, len(value)) + value


def build_iptc_app13(title: str, keywords: list[str]) -> bytes:
    """Segmento APP13 (Photoshop 3.0 / 8BIM 0x0404) com IPTC IIM em UTF-8."""
    iptc = _iptc_record(1, 90, b"\x1b%G")  # Conjunto de caracteres UTF-8.
    iptc += _iptc_record(2, 0, b"\x00\x04")
    iptc += _iptc_record(2, 5, _truncate_utf8(title, 64))  # Object Name
    iptc += _iptc_record(2, 105, _truncate_utf8(title, 256))  # Headline
    for keyword in keywords:
        iptc += _iptc_record(2, 25, _truncate_utf8(keyword, 64))
    iptc += _iptc_record(2, 120, _truncate_utf8(title, 2000))  # Caption/Abstract
    if len(iptc) % 2:
        iptc += b"\x00"
    resource = b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00" + struct.pack(">I", len(iptc)) + iptc
    payload = b"Photoshop 3.0\x00" + resource
    return b"\xff\xed" + struct.pack(">H", len(payload) + 2) + payload


def insert_app13(jpeg: bytes, segment: bytes) -> bytes:
    """Insere o APP13 depois dos segmentos APPn iniciais (JFIF, ICC, XMP)."""
    if jpeg[:2] != b"\xff\xd8":
        raise ValueError("Conteúdo não é um JPEG.")
    position = 2
    while position + 4 <= len(jpeg) and jpeg[position] == 0xFF and 0xE0 <= jpeg[position + 1] <= 0xEF:
        length = struct.unpack(">H", jpeg[position + 2:position + 4])[0]
        position += 2 + length
    return jpeg[:position] + segment + jpeg[position:]


def adobe_csv(rows: list[dict]) -> tuple[str, list[str]]:
    """CSV do portal (Filename, Title, Keywords, Category, Releases) e avisos de formato."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["Filename", "Title", "Keywords", "Category", "Releases"])
    warnings: list[str] = []
    for row in rows:
        filename, title = row["filename"], row["title"]
        if len(filename) > CSV_FILENAME_LIMIT:
            warnings.append(f"{filename}: nome acima de {CSV_FILENAME_LIMIT} caracteres.")
        if len(title) > CSV_TITLE_LIMIT:
            warnings.append(f"{filename}: título com {len(title)} caracteres (limite do CSV: {CSV_TITLE_LIMIT}).")
        category = category_number(row.get("category"))
        writer.writerow([filename, title, ", ".join(row["keywords"]), category or "", ""])
    return output.getvalue(), warnings
