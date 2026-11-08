"""制作独立的汇演合成单证；版式从本机中文参考模板继承。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION_START
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/使用交付/汇演示例包_20261014"
TEMPLATE = Path("/Users/hwaigc/.local/share/codex-doc-tools/reference-zh.docx")
A = "第一组_法国设备海运"
B = "第二组_西班牙食品冷链"
SELLER_A = "Atelier Horizon Démo SAS"
BUYER_A = "Shanghai Meridian Demo Equipment Co., Ltd."
SELLER_B = "Huerta Clara Demo S.L."
BUYER_B = "Shanghai Freshway Demo Foods Co., Ltd."


def fields(title, values):
    return {"kind": "fields", "title": title, "values": values}


def table(title, columns, rows, widths=None):
    return {"kind": "table", "title": title, "columns": columns, "rows": rows, "widths": widths}


def text(title, paragraphs):
    return {"kind": "text", "title": title, "paragraphs": paragraphs}


def page(title, blocks, landscape=False):
    return {"title": title, "blocks": blocks, "landscape": landscape}


def make_documents():
    docs = []

    def add(group, name, language, number, title, pages):
        if language == "zh-CN":
            for current in pages:
                current["landscape"] = False
        docs.append({"group": group, "name": name, "language": language, "number": number, "title": title, "pages": pages})

    add(A, "A01_商业发票_法语_3页", "fr", "INV-DEMO-FR-261014", "FACTURE COMMERCIALE", [
        page("Parties et conditions de vente", [
            fields("Références commerciales", [("N° de facture", "INV-DEMO-FR-261014"), ("Date", "14 octobre 2026"), ("Commande client", "PO-DEMO-CN-261014-A"), ("Référence de l'expédition", "SHIP-DEMO-A-2026"), ("Devise", "EUR"), ("Conditions de livraison", "CIF Shanghai — Incoterms® 2020")]),
            fields("Vendeur / exportateur", [("Raison sociale", SELLER_A), ("Adresse", "18 rue des Ateliers Démo, 69007 Lyon, France"), ("Contact", "Claire Martin — contact de démonstration"), ("Téléphone", "+33 1 00 00 10 14"), ("Courriel", "export@horizon-demo.example"), ("Référence fiscale de démonstration", "FR-DEMO-TAX-A01")]),
            fields("Acheteur / destinataire", [("Raison sociale", BUYER_A), ("Adresse", "88 Demo Logistics Road, Pudong, Shanghai 201306, China"), ("Contact", "Lin Wei — contact de démonstration"), ("Téléphone", "+86 21 0000 1014"), ("Courriel", "import@meridian-demo.example"), ("Référence fiscale de démonstration", "CN-DEMO-TAX-A01")]),
            text("Objet de la vente", ["Fourniture de composants industriels pour une ligne de conditionnement. Les trois références et les lots sont détaillés sur la page suivante.", "La quantité commerciale est exprimée en pièces. Le nombre de caisses représente l'emballage extérieur et ne doit pas être additionné aux pièces."])
        ]),
        page("Détail des marchandises", [
            table("Lignes de facture — une seule fois par référence", ["N°", "Référence", "Désignation", "Modèle", "Lot", "Quantité", "Unité", "Prix EUR", "Montant EUR"], [
                ["1", "SRV-400", "Modules servomoteurs industriels", "400 W / 220 V", "FR-SRV-2610", "60", "pièces", "750,00", "45 000,00"],
                ["2", "VLV-25", "Vannes de régulation en acier inoxydable", "DN25", "FR-VLV-2610", "40", "pièces", "120,00", "4 800,00"],
                ["3", "SNS-T8", "Capteurs de température pour convoyeur", "-20 °C à +120 °C", "FR-SNS-2610", "20", "pièces", "85,00", "1 700,00"]
            ], [1.2, 2.3, 6.6, 3.5, 3.3, 2.2, 2.1, 2.7, 2.7]),
            table("Récapitulatif de la cargaison", ["Quantité totale", "Emballage extérieur", "Origine", "Poids net", "Poids brut", "Volume"], [["120 pièces", "12 caisses en bois", "France / FR", "620 kg", "720 kg", "1,884 m³"]]),
            text("Instructions relatives aux références", ["Les références SRV-400, VLV-25 et SNS-T8 restent inchangées dans toute traduction. Les lots identifient les mêmes marchandises sur la facture, la liste de colisage et le connaissement.", "Ce tableau contient la quantité de marchandises. Les dimensions et la répartition par caisse sont des informations d'emballage, pas des lignes de vente supplémentaires."])
        ], True),
        page("Totaux, transport et instructions", [
            table("Calcul du montant payable", ["Libellé", "Montant", "Devise"], [["Sous-total marchandises", "51 500,00", "EUR"], ["Fret maritime", "900,00", "EUR"], ["Assurance transport", "100,00", "EUR"], ["TOTAL FACTURE CIF", "52 500,00", "EUR"]]),
            fields("Transport prévu", [("Lieu de prise en charge", "Lyon, France"), ("Port de chargement", "Marseille, France"), ("Port de déchargement", "Shanghai, China"), ("Lieu de livraison", "88 Demo Logistics Road, Pudong, Shanghai"), ("Réservation", "BK-DEMO-A-261014"), ("Référence du connaissement", "BL-DEMO-A-261015")]),
            text("Paiement et contrôle documentaire", ["Paiement à 30 jours à compter de la date de facture. Aucun compte bancaire réel n'est fourni dans cet exemple.", "Vérifier séparément les montants, les unités, les références de lot et les rôles des parties. La présente facture ne constitue ni une déclaration en douane ni une décision de mainlevée.", "Document de démonstration : sociétés, coordonnées et références fictives. Aucune signature officielle ou certification n'est reproduite."]),
            fields("Validation commerciale", [("Nom du préparateur", "Claire Martin — démonstration"), ("Signature", "________________________________"), ("Date de contrôle", "________________________________")])
        ])
    ])

    pack_a_rows = [["1", "SRV-400", "FR-SRV-2610", "60", "pcs", "6", "wooden crates", "360", "420"], ["2", "VLV-25", "FR-VLV-2610", "40", "pcs", "4", "wooden crates", "240", "260"], ["3", "SNS-T8", "FR-SNS-2610", "20", "pcs", "2", "wooden crates", "20", "40"]]
    add(A, "A02_装箱单_英语_3页", "en", "PL-DEMO-A-261014", "PACKING LIST", [
        page("Shipment and handling particulars", [
            fields("Document references", [("Packing list number", "PL-DEMO-A-261014"), ("Packing date", "14 October 2026"), ("Shipment reference", "SHIP-DEMO-A-2026"), ("Invoice reference", "INV-DEMO-FR-261014"), ("Purchase order", "PO-DEMO-CN-261014-A")]),
            fields("Shipper", [("Legal name", SELLER_A), ("Address", "18 rue des Ateliers Démo, 69007 Lyon, France"), ("Email", "export@horizon-demo.example"), ("Telephone", "+33 1 00 00 10 14")]),
            fields("Consignee", [("Legal name", BUYER_A), ("Delivery address", "88 Demo Logistics Road, Pudong, Shanghai 201306, China"), ("Email", "import@meridian-demo.example"), ("Telephone", "+86 21 0000 1014")]),
            fields("Shipping marks and routing", [("Marks", "MERIDIAN / SHIP-DEMO-A-2026 / CRATE 01–12"), ("Receipt location", "Lyon, France"), ("Port of loading", "Marseille, France"), ("Port of discharge", "Shanghai, China"), ("Goods origin", "France / FR")]),
            text("Handling instructions", ["Keep dry. Lift from the marked base. Protect the sensor cases from impact. No dangerous-goods classification is asserted in this demonstration.", "The goods quantity is 120 pieces; the external package count is 12 wooden crates. These are different measurement bases."])
        ]),
        page("Goods and package summary", [
            table("Goods lines — net and gross weights in kg", ["Line", "SKU", "Lot", "Quantity", "Unit", "Packages", "Package type", "Net kg", "Gross kg"], pack_a_rows, [1, 2.4, 3.9, 2.1, 1.5, 2.3, 5.9, 2.7, 2.7]),
            table("Declared totals", ["Goods", "Packages", "Net weight", "Gross weight", "Measurement"], [["120 pcs", "12 wooden crates", "620 kg", "720 kg", "1.884 m³"]]),
            text("Packing-level interpretation", ["Gross weight includes packaging. Net weight covers goods only. A difference between gross and net weight is expected and is not a cross-document discrepancy.", "The next page allocates these same packages. It is an appendix to this packing list, not a second shipment or a second set of goods."])
        ], True),
        page("Packing allocation and handover", [
            table("Allocation appendix — do not add again to the goods summary", ["Crate range", "Contents", "Size per crate", "Net / crate", "Gross / crate"], [["01–06", "10 servo modules per crate", "80 × 60 × 50 cm", "60 kg", "70 kg"], ["07–10", "10 regulation valves per crate", "60 × 40 × 40 cm", "60 kg", "65 kg"], ["11–12", "10 temperature sensors per crate", "40 × 30 × 25 cm", "10 kg", "20 kg"]]),
            fields("Handover record", [("Packed by", "Warehouse Team A — demonstration"), ("Handed to", "Blue Meridian Demo Forwarding Ltd."), ("Handover date", "14 October 2026"), ("Outer packages", "12 wooden crates"), ("Visible damage", "None recorded in this synthetic example"), ("Receiver signature", "________________________________")]),
            text("Reconciliation notes", ["The commercial invoice prices the components by piece. The bill of lading declares the external crates. Match the shipment reference, SKU and lot before comparing quantities.", "All names, routes, references and contacts are synthetic demonstration data; this document is not valid for shipment."])
        ])
    ])

    def bl(group, name, number, ship, seller, buyer, route, cargo, totals, reefer=False):
        blocks = [fields("Transport references", [("Bill of lading number", number), ("Shipment reference", ship), ("Booking reference", "BK-DEMO-" + ("B" if reefer else "A") + "-261014"), ("Issue date", "15 October 2026"), ("Service", "Ocean transport — non-negotiable demonstration copy")]),
            fields("Shipper", [("Legal name", seller), ("Address", "14 Camino de la Huerta Demo, 46024 Valencia, Spain" if reefer else "18 rue des Ateliers Démo, 69007 Lyon, France")]),
            fields("Consignee", [("Legal name", buyer), ("Address", "56 Demo Cold-chain Road, Pudong, Shanghai 201306, China" if reefer else "88 Demo Logistics Road, Pudong, Shanghai 201306, China"), ("Email", "receiving@freshway-demo.example" if reefer else "import@meridian-demo.example")]),
            fields("Notify party / carrier", [("Notify party", "Harbour Link Demo Logistics Co., Ltd."), ("Notify email", "arrival@harbour-demo.example"), ("Carrier", "Blue Meridian Demo Ocean Line"), ("Vessel / voyage", "MV DEMO HORIZON / 026E")]), fields("Route", route)]
        second = [table("Description of goods — goods quantity and package count are distinct", ["SKU", "Description", "Lot", "Goods quantity", "Goods unit", "Packages"], cargo),
            fields("Transport declaration", totals)]
        if reefer:
            second.append(fields("Refrigerated transport instructions", [("Set point", "+2 °C"), ("Product instruction range", "0 °C to +4 °C"), ("Ventilation", "As agreed in the booking instruction"), ("Logger reference", "LOGGER-DEMO-B-01")]))
        second.extend([text("Document interpretation", ["This is a synthetic, non-negotiable demonstration copy. It is not issued by a real carrier and is not a document of title.", "Descriptions and package counts are shipping particulars for the same cargo. They do not establish customs clearance, legal compliance or product certification."]), fields("Carrier acknowledgement", [("Prepared by", "Demo Documentation Desk"), ("Signature", "________________________________"), ("Acknowledgement date", "________________________________")])])
        add(group, name, "en", number, "BILL OF LADING", [page("Transport and parties", blocks), page("Cargo declaration and instructions", second)])

    bl(A, "A03_海运提单_英语_2页", "BL-DEMO-A-261015", "SHIP-DEMO-A-2026", SELLER_A, BUYER_A,
        [("Place of receipt", "Lyon, France"), ("Port of loading", "Marseille, France"), ("Port of discharge", "Shanghai, China"), ("Place of delivery", "88 Demo Logistics Road, Pudong, Shanghai")],
        [["SRV-400", "Industrial servo modules", "FR-SRV-2610", "60", "pcs", "6 wooden crates"], ["VLV-25", "Stainless steel regulation valves", "FR-VLV-2610", "40", "pcs", "4 wooden crates"], ["SNS-T8", "Conveyor temperature sensors", "FR-SNS-2610", "20", "pcs", "2 wooden crates"]],
        [("Total packages", "12 wooden crates"), ("Total goods quantity", "120 pcs"), ("Net weight", "620 kg"), ("Gross weight", "720 kg"), ("Volume", "1.884 m³"), ("Container reference", "CONTAINER-DEMO-A-01"), ("Seal reference", "SEAL-DEMO-A-1014"), ("Freight", "Prepaid")])

    add(A, "A04_采购订单_中文_2页", "zh-CN", "PO-DEMO-CN-261014-A", "采购订单", [
        page("采购主体与交付要求", [fields("订单信息", [("订单编号", "PO-DEMO-CN-261014-A"), ("订单日期", "2026年10月14日"), ("同票货批", "SHIP-DEMO-A-2026"), ("币种", "EUR"), ("交货条款", "CIF上海，Incoterms® 2020")]),
            fields("买方／收货人", [("英文法定名称", BUYER_A), ("演示中文名称", "上海子午线演示设备有限公司"), ("收货地址", "中国上海市浦东新区演示物流路88号，201306"), ("联系人", "林伟（演示）"), ("电子邮箱", "import@meridian-demo.example")]),
            fields("卖方／发货人", [("法定名称", SELLER_A), ("所在地", "法国里昂，18 rue des Ateliers Démo，69007"), ("电子邮箱", "export@horizon-demo.example")]),
            text("交付要求", ["本订单对应同一批工业组件。商品按件计价，外包装为12个木箱；不得将木箱数当作商品件数。", "法文商业发票、英文装箱单与英文海运提单为关联材料。翻译时保留订单号、SKU、批次号、金额和计量单位。", "文件内的企业、地址及联系信息为演示合成，不能用于实际交易或运输。"])]),
        page("采购明细与确认", [table("采购商品", ["SKU", "中文品名", "型号", "批次", "数量", "单价EUR", "金额EUR"], [["SRV-400", "工业伺服模块", "400 W / 220 V", "FR-SRV-2610", "60件", "750.00", "45000.00"], ["VLV-25", "不锈钢调节阀", "DN25", "FR-VLV-2610", "40件", "120.00", "4800.00"], ["SNS-T8", "输送线温度传感器", "-20 °C至+120 °C", "FR-SNS-2610", "20件", "85.00", "1700.00"]]),
            fields("金额与包装", [("商品合计", "51500.00 EUR"), ("海运费", "900.00 EUR"), ("保险费", "100.00 EUR"), ("应付总额", "52500.00 EUR"), ("商品总数量", "120件"), ("外包装数量", "12个木箱"), ("净重／毛重", "620 kg／720 kg")]),
            fields("确认栏", [("采购经办", "林伟（演示）"), ("审核签字", "________________________________"), ("确认日期", "________________________________")])], True)
    ])

    add(B, "B01_商业发票_西班牙语_3页", "es", "INV-DEMO-ES-261014", "FACTURA COMERCIAL", [
        page("Partes y condiciones de la operación", [fields("Referencias", [("Número de factura", "INV-DEMO-ES-261014"), ("Fecha", "14 de octubre de 2026"), ("Pedido del comprador", "PO-DEMO-CN-261014-B"), ("Referencia del envío", "SHIP-DEMO-B-2026"), ("Moneda", "EUR"), ("Condición de entrega", "CIF Shanghai — Incoterms® 2020")]),
            fields("Vendedor / expedidor", [("Razón social", SELLER_B), ("Dirección", "14 Camino de la Huerta Demo, 46024 Valencia, España"), ("Persona de contacto", "Lucía Torres — demostración"), ("Correo electrónico", "export@huerta-demo.example"), ("Teléfono", "+34 96 000 10 14")]),
            fields("Comprador / destinatario", [("Razón social", BUYER_B), ("Dirección", "56 Demo Cold-chain Road, Pudong, Shanghai 201306, China"), ("Contacto", "Zhao Min — demostración"), ("Correo electrónico", "receiving@freshway-demo.example"), ("Teléfono", "+86 21 0000 2014")]),
            text("Descripción del envío", ["Fruta fresca destinada a una demostración de control documental. Las cantidades comerciales se expresan en cajas; los palés son unidades de agrupación, no cantidades adicionales de mercancía.", "Este ejemplo no declara autorización fitosanitaria ni admisibilidad de estos productos en el país de destino."])]),
        page("Detalle de la mercancía", [table("Productos, lotes y precios", ["N°", "SKU", "Producto", "Lote", "Cantidad", "Unidad", "Precio EUR", "Importe EUR"], [["1", "FRA-8", "Fresas frescas, variedad de demostración", "ES-FRA-2610", "400", "cajas", "18,00", "7.200,00"], ["2", "ARA-5", "Arándanos frescos, variedad de demostración", "ES-ARA-2610", "200", "cajas", "22,00", "4.400,00"]], [1, 2.3, 7.8, 4, 2.4, 2, 3.5, 3.5]),
            table("Resumen de cantidades y pesos", ["Producto", "Cajas", "Neto por caja", "Peso neto", "Peso bruto"], [["FRA-8", "400", "8 kg", "3.200 kg", "3.440 kg"], ["ARA-5", "200", "5 kg", "1.000 kg", "1.080 kg"], ["TOTAL", "600", "Bases diferentes", "4.200 kg", "4.520 kg"]]),
            text("Origen y embalaje", ["Origen declarado para ambas referencias: España / ES. Embalaje exterior: 12 palés, con un total de 600 cajas.", "Los puntos separan millares y las comas separan decimales en esta factura: 7.200,00 EUR significa siete mil doscientos euros."])], True),
        page("Total, ruta y condiciones de conservación", [table("Importe total", ["Concepto", "Importe", "Moneda"], [["Mercancía", "11.600,00", "EUR"], ["Transporte refrigerado", "1.250,00", "EUR"], ["Seguro", "150,00", "EUR"], ["TOTAL CIF", "13.000,00", "EUR"]]),
            fields("Ruta marítima", [("Lugar de recepción", "Valencia, España"), ("Puerto de carga", "Valencia, España"), ("Puerto de descarga", "Shanghai, China"), ("Entrega", "56 Demo Cold-chain Road, Pudong, Shanghai"), ("Conocimiento de embarque", "BL-DEMO-B-261015")]),
            fields("Instrucciones del producto", [("Temperatura de consigna", "+2 °C"), ("Intervalo indicado", "0 °C a +4 °C"), ("Fecha de envasado", "14 de octubre de 2026"), ("Fecha de consumo preferente de demostración", "4 de noviembre de 2026")]),
            text("Observaciones", ["Comparar los lotes y la cantidad de cada producto antes de traducir. El control de temperatura es una instrucción logística; no equivale a un certificado sanitario.", "Todos los datos y entidades son ficticios. Documento de demostración, no válido para transporte ni despacho aduanero."]), fields("Preparación", [("Preparado por", "Lucía Torres — demostración"), ("Firma", "________________________________")])])
    ])

    def packing_b(corrected=False):
        q = 400 if corrected else 390
        count, nw, gw = q + 200, q * 8 + 1000, q * 8.6 + 1080
        number = "PL-DEMO-B-261014-R1" if corrected else "PL-DEMO-B-261014"
        group = B + "/补件_不要与初版同时当新货物上传" if corrected else B
        add(group, "B02_装箱单_英语_修正版_3页" if corrected else "B02_装箱单_英语_初版_3页", "en", number, "PACKING LIST", [
            page("Shipment particulars", [fields("References", [("Packing list number", number), ("Shipment reference", "SHIP-DEMO-B-2026"), ("Invoice reference", "INV-DEMO-ES-261014"), ("Purchase order", "PO-DEMO-CN-261014-B"), ("Packed on", "14 October 2026"), ("Revision", "R1 — replaces PL-DEMO-B-261014; same cargo, not an additional shipment" if corrected else "Initial issue")]),
                fields("Shipper", [("Legal name", SELLER_B), ("Address", "14 Camino de la Huerta Demo, 46024 Valencia, Spain"), ("Email", "export@huerta-demo.example")]),
                fields("Consignee", [("Legal name", BUYER_B), ("Address", "56 Demo Cold-chain Road, Pudong, Shanghai 201306, China"), ("Email", "receiving@freshway-demo.example" if corrected else "receving@freshway-demo.example"), ("Telephone", "+86 21 0000 2014")]),
                fields("Routing", [("Port of loading", "Valencia, Spain"), ("Port of discharge", "Shanghai, China"), ("Goods origin", "Spain / ES"), ("Shipping marks", "FRESHWAY / SHIP-DEMO-B-2026 / PALLET 01–12")]),
                text("Handling", ["Keep refrigerated. The set point is +2 °C, with product handling instructions of 0 °C to +4 °C.", "One carton is one sales package. Pallets are outer grouping units. Do not compare pallet count directly with carton quantity."])]),
            page("Goods quantities and weights", [table("Goods lines", ["SKU", "Product", "Lot", "Quantity", "Unit", "Net kg", "Gross kg"], [["FRA-8", "Fresh strawberries", "ES-FRA-2610", str(q), "cartons", str(q * 8), f"{q * 8.6:.0f}"], ["ARA-5", "Fresh blueberries", "ES-ARA-2610", "200", "cartons", "1000", "1080"]], [2.6, 8.2, 4.3, 3, 3, 2.9, 2.5]),
                table("Shipment totals", ["Cartons", "Outer grouping", "Net weight", "Gross weight"], [[str(count), "12 pallets", f"{nw} kg", f"{gw:.0f} kg"]]),
                text("Quantity basis", ["This is a full packing list for both lots. The listed quantities cover the complete cargo under SHIP-DEMO-B-2026; this document does not describe a partial delivery.", "The weight lines use product-specific packing bases. Strawberries: net 8 kg and gross 8.6 kg per carton. Blueberries: net 5 kg and gross 5.4 kg per carton."])], True),
            page("Pallet allocation and cold-chain handover", [table("Allocation appendix — grouping only, not extra goods lines", ["Pallet range", "Product", "Cartons per pallet", "Grouping count"], [["01–07", "Strawberries", "50", "7 pallets"], ["08", "Strawberries", "50" if corrected else "40", "1 pallet"], ["09–12", "Blueberries", "50", "4 pallets"]]),
                fields("Handover", [("Handover date", "15 October 2026"), ("Pallets handed over", "12 pallets"), ("Measured temperature", "+2.1 °C"), ("Temperature logger", "LOGGER-DEMO-B-01"), ("Prepared by", "Demo Cold-chain Warehouse Team"), ("Receiver signature", "________________________________")]),
                text("Record notes", ["Pallet allocation corresponds to the goods summary on page 2. It must not be counted as a second set of goods.", "This synthetic record does not provide a real health certificate, import permit or customs release.", "Revision R1 corrects the strawberry quantity, the dependent weights and the consignee email; it does not create a new cargo lot." if corrected else "All companies, contacts, shipment numbers and logistics records in this document are fictitious demonstration data."])])
        ])

    packing_b()
    packing_b(True)
    bl(B, "B03_冷链海运提单_英语_2页", "BL-DEMO-B-261015", "SHIP-DEMO-B-2026", SELLER_B, BUYER_B,
        [("Place of receipt", "Valencia, Spain"), ("Port of loading", "Valencia, Spain"), ("Port of discharge", "Shanghai, China"), ("Place of delivery", "56 Demo Cold-chain Road, Pudong, Shanghai")],
        [["FRA-8", "Fresh strawberries", "ES-FRA-2610", "400", "cartons", "8 pallets"], ["ARA-5", "Fresh blueberries", "ES-ARA-2610", "200", "cartons", "4 pallets"]],
        [("Total outer packages", "12 pallets"), ("Total cartons", "600 cartons"), ("Net weight", "4200 kg"), ("Gross weight", "4520 kg"), ("Reefer reference", "REEFER-DEMO-B-01"), ("Seal reference", "SEAL-DEMO-B-1015"), ("Freight", "Prepaid")], True)

    add(B, "B04_采购订单_中文_2页", "zh-CN", "PO-DEMO-CN-261014-B", "采购订单", [
        page("冷链食品采购与交付要求", [fields("订单信息", [("订单编号", "PO-DEMO-CN-261014-B"), ("订单日期", "2026年10月14日"), ("货批编号", "SHIP-DEMO-B-2026"), ("交货条款", "CIF上海，Incoterms® 2020"), ("币种", "EUR")]),
            fields("买方／收货人", [("英文法定名称", BUYER_B), ("演示中文名称", "上海鲜行演示食品有限公司"), ("地址", "中国上海市浦东新区演示冷链路56号，201306"), ("联系人", "赵敏（演示）"), ("电子邮箱", "receiving@freshway-demo.example")]),
            fields("卖方／发货人", [("法定名称", SELLER_B), ("地址", "西班牙瓦伦西亚，14 Camino de la Huerta Demo，46024"), ("电子邮箱", "export@huerta-demo.example")]),
            text("交付条件", ["本订单要求完整交付同一货批的两种演示水果。数量按箱计，托盘仅用于集合搬运；同一SKU、批次和运输阶段才可作数量比较。", "温度设定+2 °C，产品运输要求0 °C至+4 °C。记录温度不等同植物检疫或食品安全证明。", "这是文件处理演示情景，不表示所列水果贸易线路已获得实际准入许可。"])]),
        page("采购明细与接收确认", [table("采购明细", ["SKU", "品名", "批次", "数量", "净重", "单价EUR", "金额EUR"], [["FRA-8", "新鲜草莓", "ES-FRA-2610", "400箱", "3200 kg", "18.00", "7200.00"], ["ARA-5", "新鲜蓝莓", "ES-ARA-2610", "200箱", "1000 kg", "22.00", "4400.00"]]),
            fields("总计", [("箱数", "600箱"), ("托盘数", "12托盘"), ("净重", "4200 kg"), ("毛重", "4520 kg"), ("商品金额", "11600.00 EUR"), ("冷链运输费／保险费", "1250.00 EUR／150.00 EUR"), ("CIF应付总额", "13000.00 EUR")]),
            fields("接收确认", [("收货经办", "赵敏（演示）"), ("箱数核对", "________________________________"), ("温度记录核对", "________________________________"), ("签字日期", "________________________________")]),
            text("使用边界", ["本采购订单未签收，仅表示演示采购要求；不得推断货物已经到港、海关已放行或收货人已签收。企业、地址和联系方式均为合成资料。"])] , True)
    ])
    return docs


def set_font(style, size, name="Arial", east_asia="Songti SC"):
    style.font.name = name
    style.font.size = Pt(size)
    style.font.color.rgb = RGBColor(0, 0, 0)
    fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    fonts.set(qn("w:eastAsia"), east_asia)


def shade(cell, color):
    node = OxmlElement("w:shd")
    node.set(qn("w:fill"), color)
    cell._tc.get_or_add_tcPr().append(node)


def row_rules(row, header=False):
    trpr = row._tr.get_or_add_trPr()
    trpr.append(OxmlElement("w:cantSplit"))
    if header:
        trpr.append(OxmlElement("w:tblHeader"))


def add_table(doc, columns, rows, total_width, widths=None, merged_title=None):
    result = doc.add_table(rows=2 if merged_title else 1, cols=len(columns))
    result.style = "Table Grid"
    result.autofit = False
    proportions = widths or [1] * len(columns)
    for col, fraction in zip(result.columns, proportions):
        col.width = Cm(total_width * fraction / sum(proportions))
    if merged_title:
        cell = result.rows[0].cells[0].merge(result.rows[0].cells[-1])
        cell.paragraphs[0].paragraph_format.first_line_indent = Cm(0)
        cell.paragraphs[0].paragraph_format.left_indent = Cm(0)
        cell.paragraphs[0].paragraph_format.right_indent = Cm(0)
        r = cell.paragraphs[0].add_run(merged_title)
        r.bold = True
        r.font.size = Pt(9)
        shade(cell, "D9E0E5")
        row_rules(result.rows[0], True)
    for row_index, values in enumerate([columns] + rows):
        row = result.rows[1 if merged_title else 0] if row_index == 0 else result.add_row()
        row_rules(row, row_index == 0)
        for cell, value, fraction in zip(row.cells, values, proportions):
            cell.width = Cm(total_width * fraction / sum(proportions))
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            p = cell.paragraphs[0]
            p.paragraph_format.first_line_indent = Cm(0)
            p.paragraph_format.left_indent = Cm(0)
            p.paragraph_format.right_indent = Cm(0)
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after = Pt(3)
            p.paragraph_format.line_spacing = 1.08
            r = p.add_run(str(value))
            r.font.size = Pt(9)
            if row_index == 0:
                r.bold = True
                shade(cell, "E9EEF2")
    doc.add_paragraph().paragraph_format.space_after = Pt(0)
    return result


def configure_section(section, landscape, number, index, total):
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = (Cm(29.7), Cm(21)) if landscape else (Cm(21), Cm(29.7))
    section.top_margin = section.bottom_margin = Cm(1.45)
    section.left_margin = section.right_margin = Cm(1.55)
    section.header_distance = section.footer_distance = Cm(.6)
    section.header.is_linked_to_previous = False
    section.footer.is_linked_to_previous = False
    p = section.header.paragraphs[0]
    r = p.add_run(f"{number}    |    DOCUMENT SET / DEMONSTRATION")
    r.font.size = Pt(8)
    r.font.color.rgb = RGBColor(70, 70, 70)
    p = section.footer.paragraphs[0]
    r = p.add_run(f"DEMONSTRATION ONLY — NOT VALID FOR SHIPMENT    |    {index} / {total}")
    r.font.size = Pt(7)
    r.font.color.rgb = RGBColor(70, 70, 70)


def write_docx(spec):
    doc = Document(TEMPLATE)
    for node in list(doc._element.body):
        if node.tag != qn("w:sectPr"):
            doc._element.body.remove(node)
    set_font(doc.styles["Normal"], 10)
    set_font(doc.styles["Heading 1"], 20, east_asia="Heiti SC")
    set_font(doc.styles["Heading 2"], 12, east_asia="Heiti SC")
    doc.styles["Normal"].paragraph_format.space_after = Pt(5)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.1
    for idx, current in enumerate(spec["pages"], 1):
        section = doc.sections[0] if idx == 1 else doc.add_section(WD_SECTION_START.NEW_PAGE)
        configure_section(section, current["landscape"], spec["number"], idx, len(spec["pages"]))
        title = spec["title"] if idx == 1 else spec["title"] + " — " + spec["number"]
        doc.add_heading(title, 1)
        p = doc.add_paragraph(current["title"])
        p.runs[0].italic = True
        p.paragraph_format.space_after = Pt(9)
        total_width = 26.6 if current["landscape"] else 17.9
        for block in current["blocks"]:
            doc.add_heading(block["title"], 2)
            if block["kind"] == "table":
                add_table(doc, block["columns"], block["rows"], total_width, block.get("widths"), merged_title=block["title"] if len(block["columns"]) >= 5 else None)
            elif block["kind"] == "fields":
                result = add_table(doc, ["", ""], block["values"], total_width, [1, 2.8])
                result._tbl.remove(result.rows[0]._tr)
                for row in result.rows:
                    shade(row.cells[0], "F1F3F5")
                    row.cells[0].paragraphs[0].runs[0].bold = True
            else:
                for value in block["paragraphs"]:
                    doc.add_paragraph(value)
                rule = doc.add_paragraph()
                borders = OxmlElement("w:pBdr")
                bottom = OxmlElement("w:bottom")
                for key, value in {"val": "single", "sz": "4", "color": "8C969E", "space": "1"}.items():
                    bottom.set(qn("w:" + key), value)
                borders.append(bottom)
                rule._p.get_or_add_pPr().append(borders)
                rule.paragraph_format.space_after = Pt(3)
    doc.core_properties.title = spec["title"] + " / " + spec["number"]
    doc.core_properties.author = "DataClawHub Logistics Demo"
    doc.core_properties.subject = "Synthetic multilingual shipping document; demonstration only"
    doc.core_properties.language = spec["language"]
    path = OUT / spec["group"] / (spec["name"] + ".docx")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def write_raster_assets():
    """从已导出的PDF生成高清页图和无文本层的扫描备选。"""
    import pypdfium2 as pdfium
    from PIL import ImageFilter, ImageOps
    from reportlab.pdfgen import canvas

    sources = [(A, "A01_商业发票_法语_3页"), (B, "B02_装箱单_英语_初版_3页")]
    records = []
    for group, stem in sources:
        source = OUT / group / (stem + ".pdf")
        target = OUT / group / "扫描备选_同件勿重复上传"
        target.mkdir(parents=True, exist_ok=True)
        pdf = pdfium.PdfDocument(source)
        scan_path = target / (stem + "_图片型扫描.pdf")
        scan = canvas.Canvas(str(scan_path), pageCompression=1)
        scan.setTitle(stem + " / synthetic raster alternative")
        scan.setAuthor("DataClawHub Logistics Demo")
        for i in range(len(pdf)):
            current = pdf[i]
            width, height = current.get_size()
            bitmap = current.render(scale=300 / 72)
            picture = ImageOps.grayscale(bitmap.to_pil()).filter(ImageFilter.GaussianBlur(.18))
            path = target / (stem + f"_第{i + 1}页_300dpi.jpg")
            picture.save(path, "JPEG", quality=92, dpi=(300, 300))
            scan.setPageSize((width, height))
            scan.drawImage(str(path), 0, 0, width=width, height=height)
            scan.showPage()
            records.append({"file": str(path.relative_to(OUT)), "source": str(source.relative_to(OUT)), "source_page": i + 1, "dpi": 300, "kind": "synthetic_raster_page"})
            bitmap.close()
            current.close()
        scan.save()
        records.append({"file": str(scan_path.relative_to(OUT)), "source": str(source.relative_to(OUT)), "pages": len(pdf), "kind": "synthetic_image_only_pdf"})
        pdf.close()

    public = OUT / "公开样例"
    target = public / "高清图片"
    target.mkdir(parents=True, exist_ok=True)
    for source in sorted(public.glob("*.pdf")):
        pdf = pdfium.PdfDocument(source)
        for i in range(min(2, len(pdf))):
            current = pdf[i]
            bitmap = current.render(scale=300 / 72)
            path = target / (source.stem + f"_第{i + 1}页_300dpi.png")
            bitmap.to_pil().save(path, dpi=(300, 300))
            records.append({"file": str(path.relative_to(OUT)), "source": str(source.relative_to(OUT)), "source_page": i + 1, "dpi": 300, "kind": "public_template_page"})
            bitmap.close()
            current.close()
        pdf.close()
    for record in records:
        record["sha256"] = hashlib.sha256((OUT / record["file"]).read_bytes()).hexdigest()
    (OUT / "源稿/图片与扫描生成清单.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--raster-assets", action="store_true", help="从已导出的PDF生成扫描备选和公开页图")
    args = parser.parse_args()
    OUT = args.output.resolve()
    if args.raster_assets:
        write_raster_assets()
        return
    specs = make_documents()
    (OUT / "源稿").mkdir(parents=True, exist_ok=True)
    (OUT / "源稿/单证内容与版式.json").write_text(json.dumps(specs, ensure_ascii=False, indent=2))
    manifest = []
    for spec in specs:
        path = write_docx(spec)
        manifest.append({"file": str(path.relative_to(OUT)), "language": spec["language"], "document_number": spec["number"],
            "expected_pages": len(spec["pages"]), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        print(path)
    (OUT / "源稿/DOCX生成清单.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
