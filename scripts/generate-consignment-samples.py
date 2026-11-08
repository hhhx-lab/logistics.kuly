"""生成固定合成验收材料及真值；不包含真实个人、商业资料或官方签发文件。"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

from docx import Document
from docx.shared import Pt
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from PIL import ImageFilter

TITLE = {
    "commercial_invoice": ("商业发票", "COMMERCIAL INVOICE", "FACTURE COMMERCIALE", "FACTURA COMERCIAL", "КОММЕРЧЕСКИЙ СЧЁТ"),
    "packing_list": ("装箱单", "PACKING LIST", "LISTE DE COLISAGE", "LISTA DE EMBALAJE", "УПАКОВОЧНЫЙ ЛИСТ"),
    "bill_of_lading": ("海运提单", "BILL OF LADING", "CONNAISSEMENT MARITIME", "CONOCIMIENTO DE EMBARQUE", "КОНОСАМЕНТ"),
    "customs_declaration": ("报关单", "CUSTOMS DECLARATION", "DÉCLARATION EN DOUANE", "DECLARACIÓN ADUANERA", "ТАМОЖЕННАЯ ДЕКЛАРАЦИЯ"),
    "road_consignment": ("公路运单", "CMR CONSIGNMENT NOTE", "LETTRE DE VOITURE CMR", "CARTA DE PORTE CMR", "НАКЛАДНАЯ CMR"),
}
LANGUAGES = ("zh-CN", "en", "fr", "es", "ru")
LABEL = {
    "document_no": ("单据编号", "Document No", "N° document", "Nº documento", "Номер документа"),
    "seller": ("卖方", "Seller", "Vendeur", "Vendedor", "Продавец"),
    "buyer": ("买方", "Buyer", "Acheteur", "Comprador", "Покупатель"),
    "shipper": ("发货人", "Shipper", "Expéditeur", "Remitente", "Отправитель"),
    "consignee": ("收货人", "Consignee", "Destinataire", "Destinatario", "Получатель"),
    "carrier": ("承运人", "Carrier", "Transporteur", "Transportista", "Перевозчик"),
    "notify_party": ("通知方", "Notify party", "Partie à notifier", "Parte a notificar", "Уведомляемая сторона"),
    "exporter": ("出口商", "Exporter", "Exportateur", "Exportador", "Экспортёр"),
    "importer": ("进口商", "Importer", "Importateur", "Importador", "Импортёр"),
    "port_of_loading": ("装货港", "Port of loading", "Port de chargement", "Puerto de carga", "Порт погрузки"),
    "port_of_discharge": ("卸货港", "Port of discharge", "Port de déchargement", "Puerto de descarga", "Порт разгрузки"),
    "place_of_receipt": ("接货地", "Place of receipt", "Lieu de prise en charge", "Lugar de recepción", "Место приёма"),
    "place_of_delivery": ("交货地", "Place of delivery", "Lieu de livraison", "Lugar de entrega", "Место доставки"),
    "transport_reference": ("运输参考号", "Transport reference", "Référence transport", "Referencia transporte", "Ссылка перевозки"),
    "sku": ("货号", "SKU", "Référence article", "Código de producto", "Артикул"),
    "specification": ("规格", "Specification", "Modèle", "Especificación", "Модель"),
    "lot_no": ("批号", "Lot No", "Numéro de lot", "Número de lote", "Номер партии"),
    "package_range": ("箱号范围", "Package range", "N° colis", "Números de bulto", "Номера мест"),
    "quantity": ("数量", "Quantity", "Quantité", "Cantidad", "Количество"),
    "quantity_unit": ("单位", "Unit", "Unité", "Unidad", "Единица"),
    "net_weight": ("净重", "Net weight", "Poids net", "Peso neto", "Вес нетто"),
    "net_weight_unit": ("净重单位", "Net weight unit", "Unité poids net", "Unidad peso neto", "Единица веса нетто"),
    "gross_weight": ("毛重", "Gross weight", "Poids brut", "Peso bruto", "Вес брутто"),
    "gross_weight_unit": ("毛重单位", "Gross weight unit", "Unité poids brut", "Unidad peso bruto", "Единица веса брутто"),
    "chargeable_weight": ("计费重量", "Chargeable weight", "Poids taxable", "Peso facturable", "Оплачиваемый вес"),
    "chargeable_weight_unit": ("计费重量单位", "Chargeable weight unit", "Unité poids taxable", "Unidad peso facturable", "Единица оплачиваемого веса"),
    "statutory_quantity_1": ("法定第一数量", "Statutory quantity 1", "Quantité légale 1", "Cantidad legal 1", "Количество 1"),
    "statutory_quantity_1_unit": ("法定第一单位", "Statutory unit 1", "Unité légale 1", "Unidad legal 1", "Единица количества 1"),
    "country_of_origin": ("原产国", "Country of origin", "Pays d'origine", "País de origen", "Страна происхождения"),
}


def spec(case, ordinal, kind, language="en", fmt="pdf", layout=0, quantity="100", lot="LOT-A"):
    fields = {"document_no": f"DEMO-{case:02}-{ordinal}", "transport_reference": f"TR-DEMO-{case:02}"}
    if kind == "commercial_invoice": fields.update(seller="ALPHA Demo Trading", buyer="BRAVO Demo Import")
    elif kind == "customs_declaration": fields.update(exporter="ALPHA Demo Trading", importer="BRAVO Demo Import")
    else: fields.update(shipper="ALPHA Demo Trading", consignee="BRAVO Demo Import", carrier="DELTA Demo Logistics")
    if kind in {"bill_of_lading", "road_consignment"}:
        fields.update(port_of_loading="Marseille", port_of_discharge="Shanghai", place_of_delivery="Demo Warehouse Shanghai")
    return {"sample_id": f"C{case:02}-D{ordinal}", "case_id": f"C{case:02}", "document_type": kind,
        "language": language, "format": fmt, "layout": layout, "fields": fields,
        "lines": [{"sku": "00003", "specification": "CT-40", "lot_no": lot, "quantity": quantity,
            "quantity_unit": "cartons", "country_of_origin": "FR"}]}


def sample_set():
    definitions = [
        [("commercial_invoice", "en", "pdf", 0), ("packing_list", "en", "pdf", 1), ("bill_of_lading", "en", "pdf", 2)],
        [("commercial_invoice", "fr", "docx", 1), ("packing_list", "es", "pdf", 2), ("bill_of_lading", "en", "pdf", 0)],
        [("commercial_invoice", "zh-CN", "pdf", 2), ("packing_list", "en", "pdf", 0), ("customs_declaration", "ru", "docx", 0)],
        [("commercial_invoice", "en", "pdf", 0), ("packing_list", "en", "pdf", 1), ("bill_of_lading", "en", "pdf", 2)],
        [("commercial_invoice", "en", "pdf", 1), ("bill_of_lading", "fr", "pdf", 1), ("bill_of_lading", "es", "docx", 2)],
        [("commercial_invoice", "es", "pdf", 2), ("bill_of_lading", "ru", "pdf", 0), ("packing_list", "en", "docx", 1)],
        [("commercial_invoice", "fr", "pdf", 0), ("packing_list", "en", "pdf", 2), ("bill_of_lading", "es", "png", 1)],
        [("commercial_invoice", "en", "docx", 1), ("bill_of_lading", "en", "pdf", 2), ("road_consignment", "zh-CN", "pdf", 0)],
        [("commercial_invoice", "ru", "pdf", 2), ("packing_list", "en", "pdf", 1), ("customs_declaration", "zh-CN", "pdf", 1)],
        [("commercial_invoice", "en", "pdf", 0), ("commercial_invoice", "en", "docx", 1), ("commercial_invoice", "fr", "pdf", 2)],
        [("commercial_invoice", "en", "mixed_pdf", 1), ("bill_of_lading", "zh-CN", "pdf", 0), ("packing_list", "ru", "docx", 2)],
        [("commercial_invoice", "en", "png", 0), ("packing_list", "en", "pdf", 1), ("customs_declaration", "zh-CN", "docx", 2)],
    ]
    cases = []
    for number, docs in enumerate(definitions, 1):
        samples = [spec(number, index, *values) for index, values in enumerate(docs, 1)]
        case = {"case_id": f"C{number:02}", "split": "development" if number <= 8 else "acceptance",
            "scope": {"cargo_id": f"cargo-C{number:02}", "leg_id": f"leg-C{number:02}-1", "completeness": "complete"}, "documents": samples}
        cases.append(case)
    for doc in cases[1]["documents"][1:]: doc["fields"]["consignee"] = "CHARLIE Demo Warehouse"
    cases[2]["documents"][0]["lines"][0].update(net_weight="1000", net_weight_unit="kg")
    cases[2]["documents"][1]["lines"][0].update(net_weight="1", net_weight_unit="t")
    cases[3]["documents"][1]["lines"][0]["quantity"] = "98"
    for doc, value in zip(cases[4]["documents"][1:], ("60", "40")): doc["lines"][0]["quantity"] = value
    cases[5]["scope"]["completeness"] = "partial"
    for doc in cases[5]["documents"][1:]: doc["lines"][0]["quantity"] = "60"
    cases[6]["documents"][1]["lines"][0]["lot_no"] = "LOT-B"
    cases[6]["documents"][2]["lines"][0]["specification"] = "CT-80"
    cases[7]["documents"][2]["fields"].update(place_of_receipt="Shanghai", place_of_delivery="Demo Warehouse Shenzhen", carrier="ECHO Demo Road Carrier")
    cases[7]["documents"][2]["leg_id"] = "leg-C08-2"
    cases[8]["documents"][2]["lines"][0].update(net_weight="1000", net_weight_unit="kg", gross_weight="1100", gross_weight_unit="kg", chargeable_weight="1200", chargeable_weight_unit="kg", statutory_quantity_1="1000", statutory_quantity_1_unit="kg")
    cases[9]["documents"][0]["relation_hint"] = "prior_original"
    cases[9]["documents"][1]["relation_hint"] = "replacement_original"
    cases[9]["documents"][2]["relation_hint"] = "translation_candidate_requires_snapshot_proof"
    cases[9]["documents"][1]["fields"]["document_no"] = cases[9]["documents"][0]["fields"]["document_no"]
    cases[9]["documents"][2]["fields"]["document_no"] = cases[9]["documents"][0]["fields"]["document_no"]
    cases[11]["documents"][0]["degraded_scan"] = True
    return cases


def render_pdf(sample, font):
    output, anchors = io.BytesIO(), []
    pdf = canvas.Canvas(output, pagesize=A4, invariant=1)
    width, height = A4
    page = 0
    def draw(kind, continued=False, lines=None, summary=None):
        nonlocal page
        page += 1
        lang = LANGUAGES.index(sample["language"])
        pdf.setFont(font, 15)
        pdf.drawString(36, height - 42, TITLE[kind][lang] + (" (continued)" if continued else ""))
        pdf.setFont(font, 8)
        pdf.drawString(36, height - 58, "SYNTHETIC SAMPLE / TEST ONLY")
        y = height - 92
        active_fields = dict(sample["fields"])
        if kind == "packing_list" and sample["document_type"] != kind:
            active_fields.pop("seller", None); active_fields.pop("buyer", None)
            active_fields.update(shipper="ALPHA Demo Trading", consignee="BRAVO Demo Import")
        for key, value in active_fields.items():
            label = LABEL.get(key, (key,) * 5)[lang]
            if sample["layout"] == 1: pdf.rect(34, y - 5, width - 68, 24, stroke=1)
            pdf.setFont(font, 10)
            pdf.drawString(40, y, f"{label}: {value}")
            anchors.append({"field_key": "name" if key in {"seller", "buyer", "shipper", "consignee", "carrier", "exporter", "importer"} else key,
                "role": key if key in {"seller", "buyer", "shipper", "consignee", "carrier", "exporter", "importer"} else "document" if key == "document_no" else "transport",
                "logical_document_type": kind, "raw_value": value, "page_no": page, "precision": "rect",
                "bbox": [40 / width, (height - y - 12) / height, min(.96, (40 + pdfmetrics.stringWidth(f'{label}: {value}', font, 10)) / width), (height - y + 3) / height]})
            y -= 27 if sample["layout"] == 1 else 23
        active_lines = lines if lines is not None else sample["lines"]
        columns = ("sku", "specification", "lot_no", "quantity", "quantity_unit", "country_of_origin")
        col_widths = (70, 76, 77, 65, 65, width - 72 - 353)
        top = y - 20
        x = 36
        pdf.setFont(font, 8)
        for key, cell_width in zip(columns, col_widths):
            if sample["layout"] != 2: pdf.rect(x, top - 30, cell_width, 30)
            pdf.drawString(x + 4, top - 18, LABEL[key][lang])
            x += cell_width
        pdf.line(36, top, width - 36, top); pdf.line(36, top - 30, width - 36, top - 30)
        bottom = top - 30
        for row_no, line in enumerate(active_lines):
            x = 36
            for key, cell_width in zip(columns, col_widths):
                value = line.get(key, "")
                if sample["layout"] != 2: pdf.rect(x, bottom - 27, cell_width, 27)
                pdf.drawString(x + 4, bottom - 17, value)
                anchors.append({"logical_document_type": kind, "column_key": key, "line_index": row_no,
                    "raw_value": value, "page_no": page, "precision": "rect",
                    "bbox": [(x+3)/width, (height-bottom+2)/height, (x+cell_width-3)/width, (height-bottom+25)/height]})
                x += cell_width
            bottom -= 27
            pdf.line(36, bottom, width - 36, bottom)
            for key, value in line.items():
                if key in columns: continue
                pdf.drawString(40, bottom - 17, f"{LABEL.get(key, (key,) * 5)[lang]}: {value}")
                anchors.append({"logical_document_type": kind, "column_key": key, "line_index": row_no,
                    "raw_value": value, "page_no": page, "precision": "rect",
                    "bbox": [40/width, (height-bottom+2)/height, .9, (height-bottom+22)/height]})
                bottom -= 23
        if summary:
            pdf.drawString(40, bottom - 17, summary)
        pdf.setFont(font, 8); pdf.drawString(36, 28, f"{sample['sample_id']} / {page}")
        pdf.showPage()
    draw(sample["document_type"])
    if sample["format"] == "mixed_pdf":
        first = {**sample["lines"][0], "quantity": "60", "package_range": "1-60"}
        second = {**sample["lines"][0], "quantity": "40", "package_range": "61-100"}
        sample["logical_documents"] = [{"type": "commercial_invoice", "lines": sample["lines"]},
            {"type": "packing_list", "lines": [first, second], "pages": [2, 3]}]
        draw("packing_list", lines=[first], summary="Subtotal: 60 cartons")
        draw("packing_list", continued=True, lines=[second], summary="Total: 100 cartons")
    pdf.save()
    return output.getvalue(), anchors


def render_docx(sample, font):
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name, style.font.size = "Arial Unicode MS", Pt(10)
    lang = LANGUAGES.index(sample["language"])
    doc.add_heading(TITLE[sample["document_type"]][lang], level=1)
    doc.add_paragraph("SYNTHETIC SAMPLE / TEST ONLY")
    anchors = []
    for index, (key, value) in enumerate(sample["fields"].items(), 2):
        doc.add_paragraph(f"{LABEL.get(key, (key,) * 5)[lang]}: {value}")
        anchors.append({"field_key": "name" if key in {"seller", "buyer", "shipper", "consignee", "carrier", "exporter", "importer"} else key,
            "role": key if key in {"seller", "buyer", "shipper", "consignee", "carrier", "exporter", "importer"} else "document" if key == "document_no" else "transport",
            "raw_value": value, "precision": "paragraph_only", "paragraph_index": index})
    columns = tuple(sample["lines"][0])
    table = doc.add_table(rows=1, cols=len(columns))
    table.style = "Table Grid"
    for index, key in enumerate(columns): table.rows[0].cells[index].text = LABEL.get(key, (key,) * 5)[lang]
    for row_index, line in enumerate(sample["lines"]):
        cells = table.add_row().cells
        for cell_index, key in enumerate(columns):
            value = line.get(key, "")
            cells[cell_index].text = value
            anchors.append({"column_key": key, "raw_value": value, "line_index": row_index,
                "precision": "cell", "table_index": 0, "table_row": row_index + 1, "table_col": cell_index})
    output = io.BytesIO(); doc.save(output)
    return output.getvalue(), anchors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path, required=True, help="含中文与西里尔字形的现有TTF；不下载安装字体")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "docs/测试评测/assets/同票多单证合成样本")
    args = parser.parse_args()
    pdfmetrics.registerFont(TTFont("SampleFont", str(args.font)))
    args.output.mkdir(parents=True, exist_ok=True)
    cases = sample_set()
    for case in cases:
        for sample in case["documents"]:
            fmt = sample["format"]
            if fmt == "docx": data, anchors = render_docx(sample, "SampleFont")
            else:
                data, anchors = render_pdf(sample, "SampleFont")
                if fmt == "png":
                    source = pdfium.PdfDocument(data); page = source[0]; bitmap = page.render(scale=2)
                    image = bitmap.to_pil()
                    try:
                        if sample.get("degraded_scan"):
                            blurred = image.filter(ImageFilter.GaussianBlur(1.8)); image.close(); image = blurred
                        output = io.BytesIO(); image.save(output, format="PNG"); data = output.getvalue()
                    finally: image.close(); bitmap.close(); page.close(); source.close()
            suffix = "pdf" if fmt == "mixed_pdf" else fmt
            filename = f"{sample['sample_id']}_{sample['document_type']}.{suffix}"
            (args.output / filename).write_bytes(data)
            sample.update(filename=filename, sha256=hashlib.sha256(data).hexdigest(), expected_anchors=anchors,
                expected_logical_types=["commercial_invoice", "packing_list"] if fmt == "mixed_pdf" else [sample["document_type"]])
    manifest = {"schema_version": 1, "provenance": "synthetic_project_generated", "date": "2026-10-14",
        "official_forms": False, "legal_review_completed": False, "cases": cases}
    (args.output / "sample_set.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"Generated {len(cases)} cases / {sum(len(c['documents']) for c in cases)} documents at {args.output}")


if __name__ == "__main__": main()
