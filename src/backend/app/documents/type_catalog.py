"""文件用途与货物领域目录；仅指导有来源的识别、翻译和复核。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

CATALOG_VERSION = "logistics-types@2"
BASE_TYPES = {"commercial_invoice", "packing_list", "bill_of_lading", "supporting", "unknown"}
ROLE_LABELS = {
    "seller": "卖方", "buyer": "买方", "shipper": "发货人", "consignee": "收货人",
    "notify_party": "通知方", "carrier": "承运人", "forwarder": "货运代理", "exporter": "出口商",
    "importer": "进口商", "manufacturer": "生产商", "issuer": "签发机构", "insurer": "保险人",
    "insured": "被保险人", "warehouse": "仓库", "deliverer": "交付方", "receiver": "签收方",
    "signatory": "签署人", "applicant": "申请人", "end_user": "最终用户", "declarant": "申报人",
    "owner": "所有人", "supplier": "供应商", "laboratory": "检验机构",
}
PARTY_ROLES = frozenset(ROLE_LABELS)


@dataclass(frozen=True)
class DocumentProfile:
    code: str
    label: str
    group: str
    base_type: str
    titles: tuple[str, ...]
    roles: tuple[str, ...]
    fields: tuple[tuple[str, str, str], ...]
    review_focus: tuple[str, ...]
    layout: str
    version: str = "1"
    capability_state: str = "directory_supported"
    verified_sample_ids: tuple[str, ...] = ()


def fields(role: str, *values: tuple[str, str]) -> tuple[tuple[str, str, str], ...]:
    return tuple((key, role, label) for key, label in values)


_TRANSPORT = fields("transport", ("booking_no", "订舱号"), ("departure_date", "出发日期"),
    ("arrival_date", "到达日期"), ("transit_place", "经由地"), ("vehicle_no", "车辆号"))
_CERTIFICATE = fields("document", ("certificate_no", "证书编号"), ("issue_date", "签发日期"),
    ("expiry_date", "原件注明有效期截止日"), ("issuing_country", "签发国家"))
_GOODS = fields("goods", ("batch_no", "货物批次号"), ("production_date", "生产日期"),
    ("expiry_date", "货物有效期截止日"), ("product_grade", "原件等级规格"), ("lot_no", "原件货批号"))
_DANGEROUS = fields("goods", ("un_number", "原件UN/ID编号"), ("proper_shipping_name", "正确运输名称"),
    ("hazard_class", "危险类别"), ("subsidiary_hazard", "附属危险"), ("packing_group", "包装组"),
    ("packing_instruction", "原件包装指令"), ("emergency_contact", "原件应急联系方式"))
_ANALYSIS = fields("goods", ("sample_id", "样品编号"), ("analysis_date", "分析日期"),
    ("test_parameter", "检测项目"), ("test_result", "检测结果"), ("test_unit", "检测单位"),
    ("test_method", "检测方法"), ("specification_limit", "原件列明的检测限值或规格"))
_ORIGIN = fields("goods", ("origin_criterion", "原件原产资格描述"), ("botanical_name", "植物学名"))
_DOCUMENT = fields("document", ("order_no", "订单号"), ("contract_no", "合同号"), ("signature_text", "可辨认签字文字"))


def _p(code, label, group, base, titles, roles, extra=(), focus=(), layout="抬头、角色信息、明细或声明、签署区"):
    return DocumentProfile(code, label, group, base, tuple(titles), tuple(roles), tuple(extra) + _DOCUMENT,
                           tuple(focus), layout)


DOCUMENT_PROFILES = (
    _p("commercial_invoice", "商业发票", "贸易与结算", "commercial_invoice", ("commercial invoice", "invoice", "商业发票"), ("seller", "buyer", "signatory"), fields("document", ("invoice_no", "发票号"), ("invoice_date", "发票日期")), ("发票金额/币种/贸易术语按原文；买方与收货人不等同。",)),
    _p("proforma_invoice", "形式发票", "贸易与结算", "commercial_invoice", ("proforma invoice", "pro forma invoice", "形式发票"), ("seller", "buyer", "signatory"), fields("document", ("invoice_no", "形式发票号"), ("invoice_date", "形式发票日期")), ("保留形式发票性质，不翻译成已结算的商业发票。",)),
    _p("packing_list", "装箱单", "贸易与结算", "packing_list", ("packing list", "装箱单", "箱单"), ("shipper", "consignee", "signatory"), fields("document", ("packing_list_no", "箱单号"), ("packing_list_date", "箱单日期")) + fields("goods", ("shipping_marks", "唛头"), ("volume", "体积"), ("volume_unit", "体积单位")), ("保持包装层级、毛净重、唛头和跨页表格对应。",)),
    _p("sales_contract", "贸易合同／订单", "贸易与结算", "supporting", ("sales contract", "purchase order", "贸易合同", "采购订单"), ("seller", "buyer", "signatory"), fields("amounts", ("payment_terms", "付款条件")), ("条款语义不扩张，订单编号与运输单号分别保留。",)),
    _p("bill_of_lading", "海运提单", "运输与交付", "bill_of_lading", ("bill of lading", "ocean bill of lading", "海运提单", "提单"), ("shipper", "consignee", "notify_party", "carrier", "signatory"), fields("document", ("bill_of_lading_no", "提单号"), ("on_board_date", "装船日期")) + _TRANSPORT, ("To order属于原文收货描述；接货地、装卸港、交货地不能合并。",)),
    _p("sea_waybill", "海运单／Sea Waybill", "运输与交付", "supporting", ("sea waybill", "海运单"), ("shipper", "consignee", "carrier", "signatory"), fields("document", ("waybill_no", "海运单号")) + _TRANSPORT, ("保留非转让海运单性质，不改称提单。",)),
    _p("multimodal_bill", "多式联运提单／FBL", "运输与交付", "bill_of_lading", ("fiata multimodal transport bill of lading", "multimodal transport bill of lading", "多式联运提单"), ("shipper", "consignee", "notify_party", "carrier", "forwarder", "signatory"), fields("document", ("bill_of_lading_no", "多式联运提单号")) + _TRANSPORT, ("区分不同运输阶段和签发主体；译本不是重新签发的FBL。",)),
    _p("air_waybill", "空运运单／AWB", "运输与交付", "supporting", ("air waybill", "airway bill", "航空运单", "空运运单"), ("shipper", "consignee", "carrier", "forwarder", "signatory"), fields("document", ("air_waybill_no", "空运单号")) + fields("transport", ("departure_airport", "始发机场"), ("destination_airport", "目的机场"), ("flight_no", "航班号")) + fields("goods", ("chargeable_weight", "计费重量")), ("区分主单和分单；机场代码、计费重量不意译或猜值。",)),
    _p("rail_consignment", "铁路运单／CIM·SMGS", "运输与交付", "supporting", ("rail consignment note", "cim consignment note", "smgs consignment note", "铁路运单", "国际铁路货物联运运单"), ("shipper", "consignee", "carrier", "signatory"), fields("document", ("waybill_no", "铁路运单号")) + fields("transport", ("departure_station", "始发站"), ("destination_station", "到达站"), ("wagon_no", "车皮号")) + _TRANSPORT, ("CIM、SMGS版本以原件为准；经由站、附页与车辆信息完整翻译。",)),
    _p("road_consignment", "公路运单／CMR", "运输与交付", "supporting", ("cmr consignment note", "international consignment note", "公路运单", "国际公路货物运单"), ("shipper", "consignee", "carrier", "signatory"), fields("document", ("waybill_no", "公路运单号")) + _TRANSPORT, ("交接地点、承运保留意见和签收日期分别定位。",)),
    _p("courier_waybill", "快递运单", "运输与交付", "supporting", ("express waybill", "courier waybill", "快递运单"), ("shipper", "consignee", "carrier"), fields("document", ("waybill_no", "快递运单号")), ("条码须有实际解码证据；寄收地址不可颠倒。",)),
    _p("postal_declaration", "邮政报关单／CN22·CN23", "运输与交付", "supporting", ("customs declaration cn22", "customs declaration cn23", "邮政报关单", "cn22", "cn23"), ("shipper", "consignee", "declarant"), fields("document", ("declaration_no", "邮政申报号")), ("内容申报与运单不同；申报价值、数量单位及签署完整保留。",)),
    _p("delivery_receipt", "交付单／签收单", "运输与交付", "supporting", ("delivery note", "proof of delivery", "delivery receipt", "交付单", "签收单", "收货单"), ("deliverer", "receiver", "consignee", "signatory"), fields("document", ("receipt_no", "签收编号")) + fields("transport", ("delivery_date", "实际交付日期"), ("receipt_comment", "原件签收意见")), ("签收数量不默认等于发货数量；不补签名。",)),
    _p("warehouse_receipt", "仓单／入库出库单", "运输与交付", "supporting", ("warehouse receipt", "仓单", "入库单", "出库单"), ("warehouse", "owner", "deliverer", "receiver", "signatory"), fields("document", ("warehouse_receipt_no", "仓单号")), ("货物所有人与入库交付人分别提取；仓位与收货地址不混。",)),
    _p("certificate_of_origin", "原产地证书", "证书与检验", "supporting", ("certificate of origin", "原产地证书", "原产地证"), ("exporter", "importer", "issuer", "signatory"), _CERTIFICATE + _ORIGIN, ("原产国依据原文，不根据发运地推断；不自动判定原产资格。",)),
    _p("phytosanitary_certificate", "植物检疫证书", "证书与检验", "supporting", ("phytosanitary certificate", "植物检疫证书"), ("exporter", "consignee", "issuer", "signatory"), _CERTIFICATE + _ORIGIN + fields("goods", ("treatment_type", "检疫处理方式"), ("treatment_date", "检疫处理日期"), ("treatment_dose", "原件处理剂量及单位"), ("treatment_duration", "处理持续时间"), ("treatment_temperature", "处理温度"), ("additional_declaration", "补充声明")), ("学名、处理浓度/温度、附页编号逐字核对；只翻译，不替代NPPO签发。",)),
    _p("phytosanitary_reexport", "转口植物检疫证书", "证书与检验", "supporting", ("phytosanitary certificate for re-export", "phytosanitary certificate for reexport", "转口植物检疫证书"), ("exporter", "consignee", "issuer", "signatory"), _CERTIFICATE + _ORIGIN + fields("document", ("original_certificate_no", "原出口植物检疫证书号")), ("转口证与原证关系需原文支持；保留附页和官方声明。",)),
    _p("health_certificate", "卫生／食品健康证书", "证书与检验", "supporting", ("health certificate", "sanitary certificate", "卫生证书", "食品健康证书"), ("exporter", "importer", "manufacturer", "issuer", "signatory"), _CERTIFICATE + _GOODS + fields("goods", ("health_declaration", "原件健康声明")), ("批次/生产日期和声明范围保留；不生成不存在的检验结论。",)),
    _p("veterinary_certificate", "兽医卫生证书", "证书与检验", "supporting", ("veterinary certificate", "兽医卫生证书"), ("exporter", "importer", "manufacturer", "issuer", "signatory"), _CERTIFICATE + _GOODS + fields("goods", ("animal_species", "动物品种"), ("health_declaration", "兽医声明")), ("动物品种、生产场所、官方声明/附页完整识别。",)),
    _p("fumigation_certificate", "熏蒸／处理证书", "证书与检验", "supporting", ("fumigation certificate", "treatment certificate", "熏蒸证书", "处理证书"), ("shipper", "consignee", "issuer", "signatory"), _CERTIFICATE + fields("goods", ("treatment_type", "处理方式"), ("treatment_date", "处理日期"), ("treatment_dose", "原件剂量及单位"), ("treatment_duration", "处理持续时间"), ("treatment_temperature", "处理温度")), ("剂量、持续时间、温度的单位/负号不能改变。",)),
    _p("inspection_certificate", "检验／检疫证书", "证书与检验", "supporting", ("inspection certificate", "检验证书", "检疫证书"), ("shipper", "consignee", "laboratory", "issuer", "signatory"), _CERTIFICATE + _ANALYSIS, ("检验项目、取样和货批对应；不把可读签章当合法性证明。",)),
    _p("certificate_of_analysis", "质量／成分分析证书·COA", "证书与检验", "supporting", ("certificate of analysis", "analysis certificate", "质量分析证书", "成分分析证书"), ("manufacturer", "laboratory", "issuer", "signatory"), _CERTIFICATE + _ANALYSIS + _GOODS, ("结果、方法、单位、原件限值不串列；没有原文限值不判超标。",)),
    _p("cold_chain_record", "冷链温度记录", "证书与检验", "supporting", ("temperature record", "cold chain record", "温度记录", "冷链记录"), ("shipper", "consignee", "carrier"), fields("transport", ("temperature_min", "原件最低温度"), ("temperature_max", "原件最高温度"), ("temperature_unit", "温度单位"), ("record_period", "记录期间/时区"), ("logger_no", "记录设备号")), ("摄氏/华氏、负号和记录时区完整保留；不推定承运温控阈值。",)),
    _p("dangerous_goods_declaration", "危险货物运输申报", "危险品与受控材料", "supporting", ("shipper’s declaration for dangerous goods", "shipper's declaration for dangerous goods", "dangerous goods declaration", "危险货物申报", "危险品申报"), ("shipper", "consignee", "carrier", "signatory"), _DANGEROUS + fields("document", ("air_waybill_no", "原件空运单号")) + fields("transport", ("aircraft_limitation", "原件航空器限制")), ("UN/ID、正确运输名、类别/附属危险、包装组、数量包装按原件；不自动判断可运输。",)),
    _p("dangerous_goods_manifest", "危险货物舱单／清单", "危险品与受控材料", "supporting", ("dangerous goods manifest", "危险货物舱单", "危险货物清单"), ("shipper", "consignee", "carrier", "signatory"), _DANGEROUS + _TRANSPORT, ("危险品明细与舱位/装备信息保持原件行对应。",)),
    _p("safety_data_sheet", "安全数据表／SDS", "危险品与受控材料", "supporting", ("safety data sheet", "material safety data sheet", "安全数据表", "安全技术说明书"), ("supplier", "manufacturer", "signatory"), _DANGEROUS + fields("goods", ("cas_no", "CAS编号"), ("chemical_composition", "原件成分"), ("hazard_statement", "危害声明")) + fields("document", ("revision_date", "SDS修订日期")), ("保留章节和第2/14节来源；危害语义不弱化；不重新签发SDS。",)),
    _p("import_export_license", "进出口许可文件", "危险品与受控材料", "supporting", ("import license", "export license", "import licence", "export licence", "进口许可证", "出口许可证"), ("applicant", "exporter", "importer", "issuer", "signatory"), _CERTIFICATE + fields("document", ("license_no", "许可证号"), ("authorized_scope", "原件许可范围")), ("许可范围/有效期逐字保留；仅识别翻译，不判许可证真实有效。",)),
    _p("dual_use_license", "两用物项／军品许可材料", "危险品与受控材料", "supporting", ("dual-use export license", "dual use export licence", "两用物项出口许可证", "军品出口许可证"), ("applicant", "exporter", "importer", "end_user", "issuer", "signatory"), _CERTIFICATE + fields("document", ("license_no", "许可编号"), ("authorized_scope", "原件许可范围")) + fields("goods", ("end_use", "原件最终用途"), ("control_code", "原件物项控制编码")), ("不凭产品名判军品或两用物项；不改写最终用户用途和许可数量。",)),
    _p("end_user_certificate", "最终用户／最终用途证明", "危险品与受控材料", "supporting", ("end user certificate", "end-user certificate", "end use statement", "最终用户和最终用途证明", "最终用户证明"), ("exporter", "importer", "end_user", "issuer", "signatory"), _CERTIFICATE + fields("goods", ("end_use", "最终用途"), ("control_code", "原件控制编码")), ("主体和声明原样，不补授权、承诺或官方签章。",)),
    _p("currency_declaration", "货币／有价凭证运输申报", "危险品与受控材料", "supporting", ("report of international transportation of currency", "currency transportation report", "货币运输申报", "有价凭证运输申报"), ("declarant", "owner", "receiver", "carrier", "signatory"), fields("goods", ("monetary_instrument", "原件货币/有价凭证种类"), ("denomination", "原件面额")) + fields("amounts", ("declared_value", "原件申报金额")), ("实体钞票运输与贸易结算币种分别处理；不推定各国金额阈值。",)),
    _p("customs_declaration", "报关单／货物申报", "申报与保险", "supporting", ("customs declaration", "goods declaration", "进出口货物报关单", "报关单"), ("exporter", "importer", "declarant", "carrier", "signatory"), fields("document", ("declaration_no", "申报编号")) + fields("transport", ("declaration_port", "申报口岸"), ("transport_mode", "运输方式")) + fields("goods", ("trade_country", "贸易国家"), ("supervision_mode", "原件监管方式")), ("代码和数量单位保留；未审定地区/版本不验证填制合法性。",)),
    _p("cargo_manifest", "货物舱单／载货清单", "申报与保险", "supporting", ("cargo manifest", "货物舱单", "载货清单"), ("shipper", "consignee", "carrier", "signatory"), fields("document", ("manifest_no", "舱单号")) + _TRANSPORT, ("不同运单/货批不能按表格序号合并。",)),
    _p("insurance_certificate", "货运保险单／凭证", "申报与保险", "supporting", ("insurance certificate", "insurance policy", "货运保险单", "保险凭证"), ("insurer", "insured", "signatory"), fields("document", ("policy_no", "保险单号")) + fields("amounts", ("insured_amount", "保险金额")) + fields("document", ("coverage_terms", "原件保险条款")), ("不把保险金额当发票货值；条款翻译不扩张保障。",)),
    _p("generic_document", "通用物流单据", "通用", "supporting", ("通用物流单据",), tuple(ROLE_LABELS), (), ("类型未确认时按原件角色和完整源段提取/翻译，不套专属法定必填。",)),
    _p("supporting", "其他随附材料", "通用", "supporting", (), tuple(ROLE_LABELS), (), ("不明确类型保留其他材料，关键原文仍须识别翻译。",)),
    _p("unknown", "未确认类型", "通用", "unknown", (), tuple(ROLE_LABELS), (), ("等待用户确认类型，不能从货物名称猜文件用途。",)),
)
DOCUMENT_TYPES = {item.code: item for item in DOCUMENT_PROFILES}

# 标题词典只确定文件用途，不声明监管适用性或表格解析完成。
MULTILINGUAL_TITLES = {
    "commercial_invoice": {"fr": ("facture commerciale",), "es": ("factura comercial",), "ru": ("коммерческий инвойс", "коммерческий счет", "коммерческий счёт")},
    "proforma_invoice": {"fr": ("facture pro forma", "facture proforma"), "es": ("factura proforma", "factura pro forma"), "ru": ("счет-проформа", "счёт-проформа", "инвойс-проформа")},
    "packing_list": {"fr": ("liste de colisage", "liste de conditionnement"), "es": ("lista de empaque", "lista de embalaje"), "ru": ("упаковочный лист",)},
    "bill_of_lading": {"fr": ("connaissement", "connaissement maritime"), "es": ("conocimiento de embarque",), "ru": ("коносамент",)},
    "sea_waybill": {"fr": ("lettre de transport maritime",), "es": ("carta de porte marítimo",), "ru": ("морская накладная",)},
    "air_waybill": {"fr": ("lettre de transport aérien",), "es": ("guía aérea", "carta de porte aéreo"), "ru": ("авиагрузовая накладная", "авианакладная")},
    "road_consignment": {"fr": ("lettre de voiture internationale", "lettre de voiture cmr"), "es": ("carta de porte internacional", "carta de porte cmr"), "ru": ("международная товарно-транспортная накладная", "накладная cmr")},
    "rail_consignment": {"fr": ("lettre de voiture ferroviaire",), "es": ("carta de porte ferroviario",), "ru": ("железнодорожная накладная",)},
    "courier_waybill": {"fr": ("bordereau d'expédition express",), "es": ("guía de mensajería",), "ru": ("курьерская накладная",)},
    "customs_declaration": {"fr": ("déclaration en douane", "déclaration de marchandises"), "es": ("declaración aduanera", "declaración de mercancías"), "ru": ("таможенная декларация", "декларация на товары")},
    "certificate_of_origin": {"fr": ("certificat d'origine",), "es": ("certificado de origen",), "ru": ("сертификат происхождения",)},
    "phytosanitary_certificate": {"fr": ("certificat phytosanitaire",), "es": ("certificado fitosanitario",), "ru": ("фитосанитарный сертификат",)},
    "phytosanitary_reexport": {"fr": ("certificat phytosanitaire pour la réexportation",), "es": ("certificado fitosanitario para la reexportación",), "ru": ("фитосанитарный сертификат на реэкспорт",)},
    "health_certificate": {"fr": ("certificat sanitaire",), "es": ("certificado sanitario",), "ru": ("санитарный сертификат",)},
    "veterinary_certificate": {"fr": ("certificat vétérinaire",), "es": ("certificado veterinario",), "ru": ("ветеринарный сертификат",)},
    "certificate_of_analysis": {"fr": ("certificat d'analyse",), "es": ("certificado de análisis",), "ru": ("сертификат анализа",)},
    "dangerous_goods_declaration": {"fr": ("déclaration de marchandises dangereuses",), "es": ("declaración de mercancías peligrosas",), "ru": ("декларация опасных грузов",)},
    "safety_data_sheet": {"fr": ("fiche de données de sécurité",), "es": ("ficha de datos de seguridad",), "ru": ("паспорт безопасности",)},
    "delivery_receipt": {"fr": ("bon de livraison",), "es": ("albarán de entrega",), "ru": ("подтверждение доставки",)},
    "generic_document": {"fr": ("document logistique",), "es": ("documento logístico",), "ru": ("логистический документ",)},
}


def profile_titles(profile: DocumentProfile) -> tuple[str, ...]:
    return profile.titles + tuple(title for titles in MULTILINGUAL_TITLES.get(profile.code, {}).values() for title in titles)


@dataclass(frozen=True)
class CargoProfile:
    code: str
    label: str
    keywords: tuple[str, ...]
    fields: tuple[tuple[str, str, str], ...]
    review_focus: str


def _c(code, label, terms, extra, focus):
    return CargoProfile(code, label, tuple(terms), _GOODS + tuple(extra), focus)


CARGO_PROFILES = (
    _c("grain", "米麦玉米／粮食", ("rice", "wheat", "maize", "corn", "大米", "小麦", "玉米", "粮食"), _ORIGIN + _ANALYSIS, "品种、批次、产地、净重和检测单位；不凭粮食名称判植物检疫义务。"),
    _c("flour", "面粉／谷物制品", ("flour", "面粉", "谷物制品"), _ANALYSIS, "加工程度、用途、批次和水分等原件结果；不得套用鲜植物义务。"),
    _c("edible_oil", "油料／食用油", ("edible oil", "vegetable oil", "soybean oil", "食用油", "油料", "大豆油"), _ANALYSIS, "油种/精炼状态、批次和检测单位，浓度/纯度不猜值。"),
    _c("fruit", "水果", ("fresh fruit", "apple", "apples", "orange", "水果", "苹果", "柑橘"), _ORIGIN, "植物学名/品种、产地、采收批次与原件处理声明。"),
    _c("vegetable", "蔬菜", ("vegetables", "fresh vegetable", "cabbage", "蔬菜", "白菜"), _ORIGIN, "学名、鲜/加工状态、产地和批次按原件。"),
    _c("animal_food", "肉乳／水产", ("meat", "milk powder", "seafood", "beef", "肉类", "奶粉", "水产"), fields("goods", ("animal_species", "动物品种"), ("health_declaration", "原件卫生声明")), "品种、生产场所、批次、加工状态和原件健康声明。"),
    _c("processed_food", "加工食品", ("processed food", "canned food", "加工食品", "罐头"), _ANALYSIS, "配料/加工状态、生产有效日期、批次，标签与证书分开。"),
    _c("cold_chain", "冷链／冷藏冷冻", ("cold chain", "refrigerated", "frozen", "冷链", "冷藏", "冷冻"), fields("transport", ("temperature_min", "原件最低温度"), ("temperature_max", "原件最高温度"), ("temperature_unit", "温度单位")), "温度单位/负号、记录期间、温控要求按原件，不能编冷链阈值。"),
    _c("metal", "金属／合金", ("metal", "steel", "aluminium", "copper", "金属", "钢材", "铝", "铜"), _ANALYSIS + fields("goods", ("material_grade", "材质牌号"), ("chemical_composition", "成分")), "牌号、成分、规格、重量和单位；元素符号原样。"),
    _c("heavy_metal", "重金属物质", ("heavy metal", "mercury", "cadmium", "lead metal", "重金属", "汞", "镉", "铅锭"), _ANALYSIS + fields("goods", ("chemical_composition", "成分"), ("cas_no", "CAS编号")), "具体物质/成分/浓度与单位，不能把lead time当铅或把材料浓度当食品限值。"),
    _c("mineral", "矿石／矿物", ("iron ore", "mineral", "ore", "矿石", "矿物"), _ANALYSIS + fields("goods", ("material_grade", "矿石品位")), "矿种、湿/干基准、取样/分析、品位、水分和吨单位。"),
    _c("petroleum", "原油／石油制品", ("crude oil", "petroleum", "diesel", "原油", "石油", "柴油"), _DANGEROUS + _ANALYSIS + fields("goods", ("volume", "体积"), ("volume_unit", "体积单位"), ("measurement_basis", "原件计量温度/基准")), "品名/批次、质量/体积与计量基准，危险性按实际声明。"),
    _c("natural_gas", "天然气／LNG·LPG", ("natural gas", "liquefied natural gas", "lng", "lpg", "天然气", "液化气"), _DANGEROUS + fields("goods", ("measurement_basis", "原件计量基准"), ("chemical_composition", "成分")), "压缩/液化形态、计量基准、温压/成分按原件，gas一词不足以认定天然气。"),
    _c("chemical", "化学品／危险品", ("chemical", "dangerous goods", "hazardous", "化学品", "危险品"), _DANGEROUS + fields("goods", ("cas_no", "CAS编号"), ("hazard_statement", "危害声明")), "UN/ID、正确运输名、CAS、包装和危险词不误译，不判承运资格。"),
    _c("controlled_goods", "武器／军品／两用物项", ("weapon", "ammunition", "dual-use", "武器", "弹药", "军品", "两用物项"), fields("goods", ("end_use", "最终用途"), ("control_code", "原件控制编码")), "只识别翻译核对物项、最终用户用途、许可引用，不判断合法出口或生成授权。"),
    _c("currency_transport", "钞票／货币／有价凭证", ("banknotes", "currency transportation", "monetary instruments", "钞票", "现钞运输", "有价凭证"), fields("goods", ("monetary_instrument", "货币/有价凭证种类"), ("denomination", "面额")), "实体货币数量、币种金额与持有人；USD或付款币种不是钞票运输证据。"),
    _c("generic", "通用／尚不明确", (), (), "原件有明确值才提取；不补货类事实或监管结论。"),
)
CARGO_TYPES = {item.code: item for item in CARGO_PROFILES}


def normalize_type_code(value: str | None) -> str:
    return {"other": "supporting", "unclassified": "unknown"}.get(value, value or "unknown")


def is_document_type_code(value: str | None) -> bool:
    return normalize_type_code(value) in DOCUMENT_TYPES


def normalize_cargo_profiles(values) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > 16 or any(not isinstance(v, str) or v not in CARGO_TYPES for v in values):
        raise ValueError("请选择目录中的货物领域，最多16项")
    return list(dict.fromkeys(values))


def document_type_label(code: str) -> str:
    return DOCUMENT_TYPES.get(normalize_type_code(code), DOCUMENT_TYPES["unknown"]).label


def base_type(code: str) -> str:
    return DOCUMENT_TYPES.get(normalize_type_code(code), DOCUMENT_TYPES["unknown"]).base_type


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).replace("’", "'")).strip().casefold().strip(" .:：-—")


def _contains(text: str, term: str) -> bool:
    if re.search(r"[\u4e00-\u9fff]", term):
        return term in text
    return bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text))


def detect_document_type(text: str, *, standalone=False) -> str:
    value = _normalized(text)
    matches = [p for p in DOCUMENT_PROFILES if p.code not in {"generic_document", "supporting", "unknown"}
               and any(value == _normalized(title) if standalone else _contains(value, _normalized(title)) for title in profile_titles(p))]
    if not matches:
        return "unknown"
    # 更具体的标题包含通用标题时选择具体类型，其余冲突不猜。
    specific = [p for p in matches if not any(p is not other and any(_normalized(t) != _normalized(a) and _contains(_normalized(t), _normalized(a)) for t in profile_titles(other) for a in profile_titles(p))
                                                for other in matches)]
    return specific[0].code if len(specific) == 1 else "unknown"


def cargo_candidates(segments: list[dict]) -> list[dict]:
    results = []
    for p in CARGO_PROFILES:
        hits = [str(s["id"]) for s in segments if any(_contains(str(s.get("text", "")).casefold(), word) for word in p.keywords)]
        if hits:
            results.append({"code": p.code, "source_segment_ids": hits[:20], "state": "candidate"})
    return results


def candidate_profile(logical, parse) -> dict:
    return (getattr(parse, "layout_manifest", None) or {}).get("logical_document_profiles", {}).get(str(logical.id), {})


def candidate_document_type(logical, parse) -> str:
    code = candidate_profile(logical, parse).get("document_type")
    return code if code in DOCUMENT_TYPES else logical.type_candidate.value


def document_type_choices(section_ids: list[str]) -> list[dict]:
    return [{"option_id": p.code, "label": p.label, "detail": p.group + " · " + p.layout,
             "logical_document_type": p.code, "section_ids": section_ids}
            for p in DOCUMENT_PROFILES if p.code != "unknown"]


def catalog_view() -> dict:
    return {"version": CATALOG_VERSION, "items": [{"code": p.code, "label": p.label, "group": p.group,
        "base_type": p.base_type, "layout": p.layout, "review_focus": list(p.review_focus),
        "profile_version": p.version, "capability_state": p.capability_state,
        "verified_sample_ids": list(p.verified_sample_ids),
        "titles_by_language": {"zh-CN": [t for t in p.titles if re.search(r"[\u4e00-\u9fff]", t)],
            "en": [t for t in p.titles if t.isascii()], **MULTILINGUAL_TITLES.get(p.code, {})}}
        for p in DOCUMENT_PROFILES],
        "cargo_profiles": [{"code": p.code, "label": p.label, "review_focus": p.review_focus} for p in CARGO_PROFILES]}
