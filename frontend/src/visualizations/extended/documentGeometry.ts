/** Budgets apply to each of the two temporary RGBA canvases, not the PDF file. */
export const DOCUMENT_MAX_CANVAS_PIXELS = 8 * 1024 * 1024;
export const DOCUMENT_MAX_CANVAS_SIDE = 8192;
export const PDF_CSS_UNITS = 96 / 72;
export type DocumentZoom = number | 'fit-width' | 'fit-page';

export interface DocumentGeometryInput {
  pageWidth: number;
  pageHeight: number;
  zoom: DocumentZoom;
  availableWidth: number;
  availableHeight: number;
  devicePixelRatio: number;
}

/** Keep layout size independent of raster density: budget clipping never changes zoom. */
export function documentGeometry(input: DocumentGeometryInput) {
  const { pageWidth, pageHeight, availableWidth, availableHeight } = input;
  if (![pageWidth, pageHeight, availableWidth, availableHeight].every(value => Number.isFinite(value) && value > 0)) {
    throw new Error('PDF 页面或容器尺寸无效。');
  }
  const naturalWidth = pageWidth * PDF_CSS_UNITS, naturalHeight = pageHeight * PDF_CSS_UNITS;
  let zoom: number;
  if (input.zoom === 'fit-width') zoom = Math.min(4, availableWidth / naturalWidth);
  else if (input.zoom === 'fit-page') zoom = Math.min(4, availableWidth / naturalWidth, availableHeight / naturalHeight);
  else {
    zoom = input.zoom;
    if (!Number.isFinite(zoom) || zoom < 0.5 || zoom > 4) throw new Error('PDF 缩放范围为 50%–400%。');
  }
  const cssWidth = naturalWidth * zoom, cssHeight = naturalHeight * zoom;
  if (![cssWidth, cssHeight].every(value => Number.isFinite(value) && value >= 1 && value <= 32768)) {
    throw new Error('PDF 页面尺寸超出交互式预览范围。');
  }
  const requestedDensity = Number.isFinite(input.devicePixelRatio) && input.devicePixelRatio > 0 ? input.devicePixelRatio : 1;
  const density = Math.min(requestedDensity, Math.sqrt(DOCUMENT_MAX_CANVAS_PIXELS / (cssWidth * cssHeight)), DOCUMENT_MAX_CANVAS_SIDE / cssWidth, DOCUMENT_MAX_CANVAS_SIDE / cssHeight);
  const pixelWidth = Math.max(1, Math.floor(cssWidth * density));
  const pixelHeight = Math.max(1, Math.floor(cssHeight * density));
  return {
    cssWidth, cssHeight, pixelWidth, pixelHeight, viewportScale: PDF_CSS_UNITS * zoom,
    scaleX: pixelWidth / cssWidth, scaleY: pixelHeight / cssHeight, zoom,
    budgetLimited: density < requestedDensity - 0.001,
  };
}
