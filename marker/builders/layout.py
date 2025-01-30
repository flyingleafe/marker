from typing import Annotated, List, Optional, Tuple

import numpy as np
from surya.layout import LayoutPredictor
from surya.layout.schema import LayoutResult, LayoutBox
from surya.ocr_error import OCRErrorPredictor
from surya.ocr_error.schema import OCRErrorDetectionResult

from marker.builders import BaseBuilder
from marker.providers import ProviderOutput, ProviderPageLines
from marker.providers.pdf import PdfProvider
from marker.schema import BlockTypes
from marker.schema.document import Document
from marker.schema.groups.page import PageGroup
from marker.schema.polygon import PolygonBox
from marker.schema.registry import get_block_class
from marker.settings import settings
from marker.util import matrix_intersection_area


class LayoutBuilder(BaseBuilder):
    """
    A builder for performing layout detection on PDF pages and merging the results into the document.
    """
    batch_size: Annotated[
        Optional[int],
        "The batch size to use for the layout model.",
        "Default is None, which will use the default batch size for the model."
    ] = None
    force_layout_block: Annotated[
        str,
        "Skip layout and force every page to be treated as a specific block type.",
    ] = None

    def __init__(self, layout_model: LayoutPredictor, ocr_error_model: OCRErrorPredictor, config=None):
        self.layout_model = layout_model
        self.ocr_error_model = ocr_error_model

        super().__init__(config)

    def __call__(self, document: Document, provider: PdfProvider):
        if self.force_layout_block is not None:
            # Assign the full content of every page to a single layout type
            layout_results = self.forced_layout(document.pages)
        else:
            layout_results = self.surya_layout(document.pages)
        self.add_blocks_to_pages(document.pages, layout_results)
        # self.merge_blocks(document.pages, provider.page_lines)

    def get_batch_size(self):
        if self.batch_size is not None:
            return self.batch_size
        elif settings.TORCH_DEVICE_MODEL == "cuda":
            return 6
        return 6

    def forced_layout(self, pages: List[PageGroup]) -> List[LayoutResult]:
        layout_results = []
        for page in pages:
            layout_results.append(
                LayoutResult(
                    image_bbox=page.polygon.bbox,
                    bboxes=[
                        LayoutBox(
                            label=self.force_layout_block,
                            position=0,
                            top_k={self.force_layout_block: 1},
                            polygon=page.polygon.polygon,
                        ),
                    ],
                    sliced=False
                )
            )
        return layout_results


    def surya_layout(self, pages: List[PageGroup]) -> List[LayoutResult]:
        layout_results = self.layout_model(
            [p.get_image(highres=False) for p in pages],
            batch_size=int(self.get_batch_size())
        )
        return layout_results

    def surya_ocr_error_detection(self, pages:List[PageGroup], provider_page_lines: ProviderPageLines) -> OCRErrorDetectionResult:
        page_texts = []
        for document_page in pages:
            page_text = ''
            provider_lines = provider_page_lines.get(document_page.page_id, [])
            page_text = '\n'.join(' '.join(s.text for s in line.spans) for line in provider_lines)
            page_texts.append(page_text)

        ocr_error_detection_results = self.ocr_error_model(
            page_texts,
            batch_size=int(self.get_batch_size())       #TODO Better Multiplier
        )
        return ocr_error_detection_results

    def add_blocks_to_pages(self, pages: List[PageGroup], layout_results: List[LayoutResult]):
        for page, layout_result in zip(pages, layout_results):
            layout_page_size = PolygonBox.from_bbox(layout_result.image_bbox).size
            provider_page_size = page.polygon.size
            page.layout_sliced = layout_result.sliced  # This indicates if the page was sliced by the layout model
            for bbox in sorted(layout_result.bboxes, key=lambda x: x.position):
                block_cls = get_block_class(BlockTypes[bbox.label])
                layout_block = page.add_block(block_cls, PolygonBox(polygon=bbox.polygon))
                layout_block.polygon = layout_block.polygon.rescale(layout_page_size, provider_page_size)
                layout_block.top_k = {BlockTypes[label]: prob for (label, prob) in bbox.top_k.items()}
                page.add_structure(layout_block)

            # Ensure page has non-empty structure
            if page.structure is None:
                page.structure = []

            # Ensure page has non-empty children
            if page.children is None:
                page.children = []