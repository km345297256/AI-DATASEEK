import { numericPairs, parseArray } from './data';
type Factory = Pick<typeof import('jsroot/core'), 'createHistogram' | 'createTGraph' | 'create'>;
function binEdges(value: unknown, count: number): number[] {
  if (!Array.isArray(value) || value.length !== count + 1 || !value.every((n, i) => typeof n === 'number' && Number.isFinite(n) && (i === 0 || n > value[i - 1]))) throw new Error('ROOT 直方图缺少有效的原始分箱边界。');
  return value;
}
function setAxis(axis: Record<string, any>, edges: number[]) {
  axis.fXmin = edges[0]; axis.fXmax = edges[edges.length - 1]; axis.fXbins = Float64Array.from(edges);
}
export function buildRootNumericObject(result: Record<string, unknown>, core: Factory) {
  const metadata = result.metadata as Record<string, unknown> | undefined, rootClass = metadata?.root_class;
  if (typeof rootClass !== 'string' || !/^(?:TH[12][CDFIS]|TGraph(?:Errors|AsymmErrors)?)$/.test(rootClass)) throw new Error('ROOT 预览仅允许 TH1/TH2/TGraph 数值对象。');
  let object: Record<string, any>, option: string;
  if (rootClass.startsWith('TH2')) {
    const array = parseArray(result.array); if (array.shape.length !== 2 || array.values.some((value) => value === null)) throw new Error('TH2 需为无缺失值的二维数值。');
    const [height, width] = array.shape as [number, number];
    const xEdges = binEdges(metadata?.x_edges, width), yEdges = binEdges(metadata?.y_edges, height);
    object = core.createHistogram('TH2D', width, height); setAxis(object.fXaxis, xEdges); setAxis(object.fYaxis, yEdges);
    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) object.fArray[(width + 2) * (y + 1) + x + 1] = array.values[y * width + x];
    option = 'COLZ';
  } else {
    if (rootClass.startsWith('TH1')) {
      const y = result.array ? parseArray(result.array).values : numericPairs(result).y;
      if (result.array && parseArray(result.array).shape.length !== 1 || y.some((value) => value === null)) throw new Error('TH1 需为无缺失值的一维分箱数值。');
      const edges = binEdges(metadata?.x_edges, y.length); object = core.createHistogram('TH1D', y.length);
      setAxis(object.fXaxis, edges); y.forEach((value, index) => { object.fArray[index + 1] = value; }); option = 'HIST';
    } else { const { x, y } = numericPairs(result); object = core.createTGraph(x.length, x, y); option = 'ALP'; }
  }
  object.fName = 'preview'; object.fTitle = 'Local ROOT numeric preview'; object.fFunctions = core.create('TList');
  return { object, option };
}
