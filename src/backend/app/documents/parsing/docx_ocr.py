"""DOCX embedded OCR joins immutable parent text without inventing page locations."""
from __future__ import annotations
import io
from PIL import Image
from app.documents.parsing.dto import DocumentRejected,ParseOutcome,SourceSegmentDraft


def bounded_embedded_images(outcome,settings):
    images=outcome.embedded_images
    if len(images)>settings.docx_max_embedded_images:raise DocumentRejected('docx_invalid_container','DOCX嵌图数量超过处理上限')
    if sum(len(image.data) for image in images)>settings.docx_max_embedded_total_bytes:raise DocumentRejected('docx_invalid_container','DOCX嵌图总字节超过处理上限')
    selected=[]
    for image in images:
        if len(image.data)>settings.docx_max_embedded_image_bytes:raise DocumentRejected('docx_invalid_container','DOCX单幅嵌图体积超过处理上限')
        if image.content_type not in {'image/png','image/jpeg'}:continue
        try:
            with Image.open(io.BytesIO(image.data)) as picture:
                if picture.format not in {'PNG','JPEG'} or getattr(picture,'n_frames',1)!=1 or picture.width*picture.height>40000000:
                    raise ValueError('unsupported image shape')
        except Exception:raise DocumentRejected('docx_invalid_container','DOCX嵌图格式、像素或帧数不可处理') from None
        selected.append(image)
    return selected


def attach_embedded_ocr(outcome,results):
    """Result keys/order are stable; parent anchor remains the authoritative locator."""
    entries=[(segment.reading_order,0,index,segment) for index,segment in enumerate(outcome.segments)]
    for image,result in results:
        matching=[segment for segment in outcome.segments if segment.anchor.docx==image.anchor.docx and segment.anchor.table_cell==image.anchor.table_cell]
        order=matching[-1].reading_order if matching else image.reading_order
        for index,child in enumerate(result.segments):
            provenance={'key':image.key,'sha256':image.sha256,'ocr_parser':result.parser_version,'child_key':child.key,
                'coordinate_space':'embedded_image_normalized_top_left','rect':child.anchor.rect.model_dump() if child.anchor.rect else None,
                'provider_task_id':result.provider_task_id,'preprocessing':child.anchor.preprocessing_transform}
            anchor=image.anchor.model_copy(update={'page_no':None,'rect':None,'coordinate_space':None,'embedded_image':provenance})
            segment=SourceSegmentDraft(key=image.key+':ocr:'+child.key,kind='paragraph',text=child.text,reading_order=order,
                anchor=anchor,table_cell=image.anchor.table_cell)
            entries.append((order,1 if matching else -1,len(entries),segment))
    entries.sort(key=lambda item:item[:3])
    segments=tuple(item[3].model_copy(update={'reading_order':index}) for index,item in enumerate(entries))
    all_recognized=len(results)==len(outcome.embedded_images) and all(result.segments for _,result in results)
    flags=tuple(flag for flag in outcome.quality_flags if flag.code!='docx_embedded_image_not_ocr' or not all_recognized)+tuple(
        flag.model_copy(update={'page_no':None}) for _,result in results for flag in result.quality_flags)
    return outcome.model_copy(update={'segments':segments,'quality_flags':flags,'markdown':'\n\n'.join(segment.text for segment in segments)})
