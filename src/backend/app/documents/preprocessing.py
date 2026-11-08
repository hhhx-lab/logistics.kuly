"""确定性单据边界/源语言与有界视觉复读候选；机器结果不覆盖原文。"""
from __future__ import annotations

import re
import io
import hashlib
from dataclasses import dataclass

from app.config import ModelRole
from app.documents.parsing.dto import NormRect, DocumentRejected, QualityFlag, PREPROCESSING_VERSION
from app.infra.model_gateway import ImageInput, Succeeded
from app.prompts.schemas import VisionTranscription
from PIL import Image, ImageOps, ImageStat, ImageFilter

MAX_IMAGE_PIXELS = 40_000_000
DOCUMENT_GROUPING_VERSION = "logical-boundaries/2"


@dataclass(frozen=True)
class RecognitionInput:
    data: bytes
    mime: str
    manifest: dict
    quality_flags: tuple[QualityFlag, ...] = ()


def image_quality(image: Image.Image, *, page_no: int) -> tuple[QualityFlag, ...]:
    """有界启发式只产生疑点，白纸不判反光，绝不声称恢复缺失文字。"""
    width, height = image.size
    flags = []
    if min(width, height) < 600:
        flags.append(QualityFlag(code="image_resolution_low", page_no=page_no, detail="短边不足600像素，细小文字可能不可辨；请核对或重拍"))
    gray = ImageOps.grayscale(image)
    gray.thumbnail((768, 768))
    try:
        hist = gray.histogram(); total = max(1, sum(hist))
        dark = sum(hist[:90]) / total
        contrast = ImageStat.Stat(gray).stddev[0]
        edges = gray.filter(ImageFilter.FIND_EDGES)
        try:
            interior = edges.crop((1, 1, max(2, edges.width-1), max(2, edges.height-1)))
            try: edge_mean = ImageStat.Stat(interior).mean[0]
            finally: interior.close()
        finally: edges.close()
        if contrast > 5 and edge_mean < 2.5:
            flags.append(QualityFlag(code="image_blur_suspected", page_no=page_no, detail="局部边缘强度不足，疑似模糊；不能自动恢复，请核对或重拍"))
        # 大面积过曝且有真实暗部才提示疑似反光，正常空白背景不触发。
        if dark > .015 and sum(hist[253:]) / total > .65 and contrast > 40:
            flags.append(QualityFlag(code="image_glare_suspected", page_no=page_no, detail="大片高亮与暗部并存，疑似过曝或反光；此检查不能确认被遮挡文字"))
        strip = max(1, int(min(gray.size) * .02))
        boxes = [(0,0,gray.width,strip),(0,gray.height-strip,gray.width,gray.height),(0,0,strip,gray.height),(gray.width-strip,0,gray.width,gray.height)]
        for box in boxes:
            border = gray.crop(box)
            try:
                values = border.histogram()
                if sum(values[:90]) / max(1,sum(values)) > .08:
                    flags.append(QualityFlag(code="image_edge_content", page_no=page_no, detail="文字或暗部接近图像边缘，可能裁切不全；请确认页边完整")); break
            finally: border.close()
    finally: gray.close()
    return tuple(flags)


def prepare_image(data: bytes, *, mime: str, page_no: int = 1) -> RecognitionInput:
    """按EXIF可靠校正；原件只读，不做猜测方向、去反光或生成文字。"""
    with Image.open(io.BytesIO(data)) as original:
        if original.format not in {"JPEG", "PNG"} or getattr(original,"n_frames",1) != 1:
            raise DocumentRejected("image_format", "识别图必须是单帧JPG或PNG")
        if original.width * original.height > MAX_IMAGE_PIXELS:
            raise DocumentRejected("image_pixel_limit", "图片像素过大，拒绝展开识别图")
        orientation = int(original.getexif().get(274,1))
        if orientation not in range(1,9):
            raise DocumentRejected("image_orientation", "EXIF方向无效，无法证明识别坐标")
        corrected = ImageOps.exif_transpose(original)
        try:
            matrices = {1:[1,0,0,0,1,0],2:[-1,0,1,0,1,0],3:[-1,0,1,0,-1,1],4:[1,0,0,0,-1,1],5:[0,1,0,1,0,0],6:[0,1,0,-1,0,1],7:[0,-1,1,-1,0,1],8:[0,-1,1,1,0,0]}
            manifest = {"version":PREPROCESSING_VERSION,"source_sha256":hashlib.sha256(data).hexdigest(),"source_mime":mime,
                "pages":{str(page_no):{"exif_orientation":orientation,"source_pixels":list(original.size),"recognition_pixels":list(corrected.size),
                    "recognition_to_source_display":[1,0,0,0,1,0],"recognition_to_source_raw":matrices[orientation],"operations":["exif_transpose"] if orientation!=1 else []}}}
            output=io.BytesIO(); corrected.save(output,format="PNG")
            flags=image_quality(corrected,page_no=page_no)
            return RecognitionInput(output.getvalue(),"image/png",manifest,flags)
        finally: corrected.close()


def prepare_pdf_ocr(data: bytes, page_nos: tuple[int, ...]) -> RecognitionInput:
    """只派生待OCR页；明确Rotate按原件显示方向渲染，新PDF无Rotate歧义。"""
    import pypdfium2 as pdfium
    src=pdfium.PdfDocument(data);dst=pdfium.PdfDocument.new()
    manifest={"version":PREPROCESSING_VERSION,"source_sha256":hashlib.sha256(data).hexdigest(),"source_mime":"application/pdf","pages":{}}
    flags=[]
    try:
        for index,page_no in enumerate(page_nos):
            if not 1<=page_no<=len(src):raise ValueError("OCR页码超出原件")
            page=src[page_no-1];bitmap=image=target=image_object=None
            try:
                rotation=int(page.get_rotation())%360
                width,height=page.get_size()
                # 单页有界渲染，避免大页面与100页任务占满内存。
                scale=min(2.0,2048/max(width,height))
                bitmap=page.render(scale=scale);image=bitmap.to_pil()
                flags.extend(image_quality(image,page_no=page_no))
                if rotation:
                    target=dst.new_page(width,height)
                    image_object=pdfium.PdfImage.new(dst)
                    # JPEG内嵌避免100张派生页保留巨大原始位图；不改原件。
                    encoded=io.BytesIO();rgb=image.convert("RGB")
                    try:rgb.save(encoded,format="JPEG",quality=95,subsampling=0)
                    finally:rgb.close()
                    encoded.seek(0);image_object.load_jpeg(encoded,inline=True)
                    image_object.set_matrix(pdfium.PdfMatrix(width,0,0,height,0,0))
                    target.insert_obj(image_object);target.gen_content()
                else:
                    dst.import_pages(src,[page_no-1])
                raw_matrices={0:[1,0,0,0,1,0],90:[0,1,0,-1,0,1],180:[-1,0,1,0,-1,1],270:[0,-1,1,1,0,0]}
                manifest["pages"][str(page_no)]={"ocr_page_index":index,"source_rotation":rotation,
                    "source_display_pt":[width,height],"recognition_pixels":list(image.size),"derivative_encoding":"jpeg95" if rotation else "original_pdf_page",
                    "recognition_to_source_display":[1,0,0,0,1,0],
                    "recognition_to_source_raw":raw_matrices[rotation],"operations":["render_display_rotation"] if rotation else ["extract_ocr_page"]}
            finally:
                if image is not None:image.close()
                if bitmap is not None:bitmap.close()
                if target is not None:target.close()
                page.close()
        output=io.BytesIO();dst.save(output)
        return RecognitionInput(output.getvalue(),"application/pdf",manifest,tuple(flags))
    finally:dst.close();src.close()



@dataclass(frozen=True)
class LanguageAssessment:
    language:str|None
    uncertain:bool
    reason:str


def assess_language(text:str)->LanguageAssessment:
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    cyrillic = len(re.findall(r"[\u0400-\u04ff]", text))
    words = re.findall(r"[A-Za-zÀ-ÿ]+", text.lower())
    markers = {
        "fr": {"le", "les", "des", "une", "du", "et", "expéditeur", "destinataire", "marchandises", "facture", "livraison", "quantité", "adresse"},
        "es": {"el", "los", "las", "una", "del", "y", "remitente", "destinatario", "mercancías", "factura", "entrega", "cantidad", "dirección"},
        "en": {"the", "and", "from", "to", "of", "with", "goods", "invoice", "shipper", "seller", "buyer", "export", "import", "consignee", "quantity", "delivery", "address"},
    }
    scores = {code: len(set(words) & values) for code, values in markers.items()}
    candidates = [code for code, score in scores.items() if score >= 2]
    if chinese >= 4 and (cyrillic >= 4 or max(scores.values()) >= 2):
        return LanguageAssessment("mixed", True, "同区段含多语正文，请确认源语言")
    if cyrillic >= 4 and max(scores.values()) >= 2:
        return LanguageAssessment("mixed", True, "同区段含西里尔与拉丁正文，请确认源语言")
    if chinese >= 2:return LanguageAssessment("zh-CN", False, "检测到中文正文")
    if cyrillic >= 4:return LanguageAssessment("ru", False, "检测到俄文／西里尔正文，语义QA继续核验")
    if len(candidates) == 1:return LanguageAssessment(candidates[0], False, "检测到连续正文语言")
    if re.search(r"[àâçèêëîïôùûüœ]", text, re.I) and scores["fr"] >= 1:
        return LanguageAssessment("fr", False, "检测到法文词语")
    if re.search(r"[ñ¿¡]", text, re.I) and scores["es"] >= 1:
        return LanguageAssessment("es", False, "检测到西班牙文词语")
    return LanguageAssessment(None, True, "短标签或多语内容无法可靠确认，请选择源语言")


def document_type(text:str)->str:
    from app.documents.type_catalog import detect_document_type, base_type
    return base_type(detect_document_type(text))


def standalone_document_title(text:str)->str:
    from app.documents.type_catalog import detect_document_type
    return detect_document_type(text,standalone=True)


def _leading_metadata(segments, title)->bool:
    """仅吸纳可证明属于同页页眉的元数据，未知封面正文保留独立。"""
    page = getattr(getattr(title, "anchor", None), "page_no", None)
    for segment in segments:
        anchor = getattr(segment, "anchor", None)
        if not page or getattr(anchor, "page_no", None) != page:
            return False
        if segment.kind == "header":
            continue
        rect = getattr(anchor, "rect", None)
        if (not rect or rect.y1 > .09 or len(segment.text) > 160
                or segment.kind not in {"paragraph", "text"}
                or not re.search(r"(?:document\s+set|demonstration|\b[A-Z]{2,}-[A-Z0-9-]{4,}\b\s*[|·])", segment.text, re.I)):
            return False
    return bool(segments)


def split_document_segments(segments)->list[dict]:
    """实际标题切逻辑单据；其内真实标题切区段，保留原segment与原anchor。"""
    groups=[];current=[];kind="unknown"
    for segment in segments:
        suffix = re.search(r"\s*(?:\((?:continued|suite|continuación|продолжение|续页)\)|（续页）|[-—:]\s*(?:continued|suite|continuación|продолжение|续页))\s*$", segment.text, re.I)
        title = segment.text[:suffix.start()].strip() if suffix else segment.text
        candidate="unknown" if segment.kind == "table_cell" or getattr(segment,"table_cell",None) or getattr(getattr(segment,"anchor",None),"embedded_image",None) else standalone_document_title(title)
        continued = bool(current and candidate == kind and (suffix or segment.kind == "header"))
        if candidate!="unknown" and current and not continued and not (kind == "unknown" and _leading_metadata(current, segment)):
            groups.append({"type_candidate":kind,"segments":current});current=[]
        if candidate!="unknown":kind=candidate
        current.append(segment)
    if current:
        from app.documents.type_catalog import detect_document_type
        groups.append({"type_candidate":kind if kind!="unknown" else detect_document_type("\n".join(s.text for s in current[:6])),"segments":current})
    for group in groups:
        from app.documents.type_catalog import base_type
        group["document_type"] = group["type_candidate"]
        group["type_candidate"] = base_type(group["document_type"])
        group["title"] = next((s.text[:200] for s in group["segments"]
            if s.kind != "table_cell" and not getattr(s,"table_cell",None)
            and standalone_document_title(s.text) == group["document_type"]), group["segments"][0].text[:200])
        sections=[];current=[]
        for segment in group["segments"]:
            if segment.kind=="title" and current and not _leading_metadata(current, segment):sections.append(current);current=[]
            current.append(segment)
        if current:sections.append(current)
        group["sections"]=[{"title":part[0].text[:200],"segments":part,"language":assess_language("\n".join(s.text for s in part))} for part in sections]
    return groups


def crop_source_region(data:bytes,*,mime:str,page_no:int,rect:NormRect)->bytes:
    from app.documents.parsing.native_pdf import crop_rect_png
    if mime=="application/pdf":return crop_rect_png(data,page_no,rect)
    if mime not in {"image/jpeg","image/png"} or page_no!=1:raise ValueError("局部视觉复读仅支持PDF页或单页JPG/PNG")
    with Image.open(io.BytesIO(data)) as original:
        if original.format not in {"JPEG","PNG"} or getattr(original,"n_frames",1)!=1:raise ValueError("原图格式或帧数不符合单页坐标")
        corrected=ImageOps.exif_transpose(original)
        width,height=corrected.size
        box=(int(rect.x0*width),int(rect.y0*height),max(int(rect.x1*width),int(rect.x0*width)+1),max(int(rect.y1*height),int(rect.y0*height)+1))
        cropped=corrected.crop(box)
        try:
            cropped.thumbnail((2048,2048))
            output=io.BytesIO();cropped.save(output,format="PNG");return output.getvalue()
        finally:cropped.close();corrected.close()


async def vision_candidate(gateway,registry,binding,pdf_bytes,*,page_no:int,rect:NormRect,ocr_text:str,region_context:dict,provider_request_key:str,budget,prompt_ref=None,source_mime="application/pdf"):
    """调用者必须提供当前解析版本可证明rect与持久请求键；每次只一张局部裁图。"""
    if binding.role!=ModelRole.VISION:raise ValueError("视觉复读必须使用VISION角色")
    if len(ocr_text)>2000:raise ValueError("视觉复读只允许局部短片段")
    spec=registry.get_frozen("document.vision_region",prompt_ref["version"],prompt_ref["sha256"]) if prompt_ref else registry.get("document.vision_region")
    prompt=spec.render(region_context=region_context,ocr_text=ocr_text)
    image=ImageInput(data=crop_source_region(pdf_bytes,mime=source_mime,page_no=page_no,rect=rect),mime="image/png")
    return await gateway.image_generate(binding,[prompt],image,provider_request_key,result_type=VisionTranscription,budget=budget)
